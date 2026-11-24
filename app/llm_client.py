"""OpenAI-compatible LLM/VLM client with healthcheck, JSON validation and retries."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from openai import AsyncOpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel, ValidationError

from .config import env
from .runtime import HEAVY_SEMAPHORE

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: AsyncOpenAI | None = None


class LLMError(RuntimeError):
    pass


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=env.llm_base_url,
            api_key=env.llm_api_key,
            timeout=180.0,
            max_retries=0,
        )
    return _client


async def close_llm() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def wait_for_llm(timeout: int = 120, interval: int = 3) -> bool:
    """Poll /models until the local LLM has loaded its weights.

    A cold Qwen 35B can spend a couple of minutes loading from disk after
    rtcwake, so failing fast would only produce restart loops.
    """
    url = env.llm_base_url.rstrip("/") + "/models"
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_log = 0.0
    async with httpx.AsyncClient(timeout=6.0) as http:
        while True:
            try:
                response = await http.get(
                    url, headers={"Authorization": f"Bearer {env.llm_api_key}"}
                )
                if response.status_code < 500:
                    log.info("LLM is ready at %s (HTTP %s)", url, response.status_code)
                    return True
                log.debug("LLM healthcheck HTTP %s", response.status_code)
            except Exception as exc:  # noqa: BLE001 - startup diagnostics
                log.debug("LLM healthcheck failed: %s", exc)
            elapsed = loop.time() - started
            if elapsed + interval >= timeout:
                break
            if elapsed - last_log >= 12:
                log.warning(
                    "Waiting for LLM at %s (%.0fs/%.0fs)…", url, elapsed, timeout
                )
                last_log = elapsed
            await asyncio.sleep(interval)
    log.warning(
        "LLM did not become ready in %ss. The bot will still start; "
        "incoming items will be retryable.",
        timeout,
    )
    return False


def _extract_json(text: str) -> Any:
    if text is None:
        raise LLMError("Empty LLM response")
    raw = str(text).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(raw[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Response is not valid JSON: {raw[:400]}")


async def _chat(
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    json_mode: bool,
    acquire_lock: bool = True,
) -> str:
    kwargs: dict[str, Any] = {
        "model": env.llm_model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    async def call() -> Any:
        try:
            return await get_client().chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalise transport errors
            raise LLMError(f"LLM request failed: {exc}") from exc

    if acquire_lock:
        async with HEAVY_SEMAPHORE:
            response = await call()
    else:
        # The caller already owns the global heavy-operation slot
        # (for example the SoM browser loop).
        response = await call()
    if not response.choices:
        raise LLMError("LLM returned no choices")
    content = response.choices[0].message.content
    if content is None:
        raise LLMError("LLM returned empty content")
    return str(content)


async def chat_json(
    messages: list[dict[str, Any]],
    model_cls: type[T] | None = None,
    max_tokens: int = 1200,
    temperature: float = 0.15,
    retries: int = 1,
    acquire_lock: bool = True,
) -> Any:
    """Request JSON, validate with Pydantic, retry once on schema/syntax errors."""
    conversation = list(messages)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        content = ""
        try:
            content = await _chat(
                conversation, max_tokens, temperature, json_mode=True,
                acquire_lock=acquire_lock,
            )
            data = _extract_json(content)
            if model_cls is not None:
                validated = model_cls.model_validate(data)
                return validated.model_dump(mode="python")
            if not isinstance(data, dict):
                raise LLMError("Expected a JSON object")
            return data
        except (LLMError, ValidationError, ValueError, TypeError) as exc:
            last_error = exc
            log.warning("LLM JSON attempt %s failed: %s", attempt + 1, exc)
            if attempt >= retries:
                break
            conversation = list(messages)
            if content:
                conversation.append({"role": "assistant", "content": content[:8000]})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        "Предыдущий ответ не прошёл проверку: "
                        f"{str(exc)[:500]}. Верни ИСПРАВЛЕННЫЙ ответ строго в формате "
                        "JSON по заданной схеме. Без markdown, без пояснений."
                    ),
                }
            )
    raise LLMError(f"Could not obtain valid JSON: {last_error}")


async def chat_text(
    messages: list[dict[str, Any]],
    max_tokens: int = 900,
    temperature: float = 0.3,
    acquire_lock: bool = True,
) -> str:
    return await _chat(
        messages, max_tokens, temperature, json_mode=False, acquire_lock=acquire_lock
    )


async def vision_json(
    image: bytes,
    prompt: str,
    model_cls: type[T] | None = None,
    max_tokens: int = 1200,
    retries: int = 1,
    acquire_lock: bool = True,
) -> Any:
    data_url = image_to_data_url(image)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    return await chat_json(
        messages, model_cls=model_cls, max_tokens=max_tokens, retries=retries,
        acquire_lock=acquire_lock,
    )


def compress_image(data: bytes, max_side: int = 1280, quality: int = 85) -> bytes:
    """Resize to max 1280px on the long side as JPEG q85 (Stateless Vision)."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                background.paste(image.convert("RGB"), mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            return output.getvalue()
    except Exception as exc:  # noqa: BLE001 - image data may be malformed
        raise LLMError(f"Could not process image: {exc}") from exc


def image_to_data_url(data: bytes) -> str:
    compressed = compress_image(data)
    encoded = base64.b64encode(compressed).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
