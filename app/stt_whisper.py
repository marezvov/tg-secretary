"""Lazy CPU-only faster-whisper with automatic RAM release after idle time."""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import tempfile
import time
from typing import Any

from .config import settings
from .runtime import HEAVY_SEMAPHORE

log = logging.getLogger(__name__)

IDLE_UNLOAD_SECONDS = 15 * 60


class STTError(RuntimeError):
    pass


class WhisperService:
    def __init__(self) -> None:
        self._model: Any = None
        self._model_name: str | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self._unload_task: asyncio.Task[Any] | None = None

    async def transcribe(self, audio: bytes, suffix: str = ".ogg") -> str | None:
        model_name = str(await settings.get("whisper_model") or "small")
        if model_name.lower() == "off":
            return None
        if not audio:
            raise STTError("Пустой аудиофайл")
        async with HEAVY_SEMAPHORE:
            async with self._lock:
                model = await self._ensure_model(model_name)
                path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".ogg") as handle:
                        handle.write(audio)
                        path = handle.name
                    text = await asyncio.to_thread(self._run_model, model, path)
                finally:
                    if path:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                self._last_used = time.monotonic()
                self._schedule_unload()
        text = (text or "").strip()
        log.info("Transcribed %s bytes -> %s chars", len(audio), len(text))
        return text

    async def _ensure_model(self, model_name: str) -> Any:
        if self._model is not None and self._model_name == model_name:
            return self._model
        if self._model is not None:
            log.info("Switching Whisper model %s -> %s", self._model_name, model_name)
            self._drop_model()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError(
                "faster-whisper не установлен. Выполни: pip install faster-whisper"
            ) from exc
        log.info("Loading Whisper model '%s' on CPU (int8)…", model_name)
        try:
            self._model = await asyncio.to_thread(
                WhisperModel, model_name, device="cpu", compute_type="int8"
            )
            self._model_name = model_name
        except Exception as exc:  # noqa: BLE001 - model download/load failures
            self._model = None
            self._model_name = None
            raise STTError(f"Не удалось загрузить Whisper '{model_name}': {exc}") from exc
        log.info("Whisper '%s' loaded", model_name)
        return self._model

    def _run_model(self, model: Any, path: str) -> str:
        segments, _info = model.transcribe(
            path,
            language="ru",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        parts = []
        for segment in segments:
            text = (segment.text or "").strip()
            if text:
                parts.append(text)
        return " ".join(parts)

    def _schedule_unload(self) -> None:
        if self._unload_task is not None and not self._unload_task.done():
            self._unload_task.cancel()
        try:
            self._unload_task = asyncio.create_task(self._unload_after_idle())
        except RuntimeError:
            self._unload_task = None

    async def _unload_after_idle(self) -> None:
        try:
            await asyncio.sleep(IDLE_UNLOAD_SECONDS)
            async with self._lock:
                if time.monotonic() - self._last_used < IDLE_UNLOAD_SECONDS - 5:
                    return
                if self._model is not None:
                    log.info("Unloading Whisper model after %s min idle", IDLE_UNLOAD_SECONDS // 60)
                    self._drop_model()
        except asyncio.CancelledError:
            return

    def _drop_model(self) -> None:
        self._model = None
        self._model_name = None
        gc.collect()

    async def shutdown(self) -> None:
        if self._unload_task is not None and not self._unload_task.done():
            self._unload_task.cancel()
        async with self._lock:
            self._drop_model()


whisper = WhisperService()
