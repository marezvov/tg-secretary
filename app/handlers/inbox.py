"""Multimodal Inbox: capture-first, 1.8s debounce, text/voice/photo/files/URLs.

Every incoming message is written to ``inbox_log`` before any network call.
If the LLM/VLM or a download fails the record stays retryable via /inbox.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import runtime
from ..browser_agent import BrowserError, extract_page
from ..config import settings
from ..database import Database
from ..documents import DocumentError, extract_document_text
from ..llm_client import LLMError, chat_json, chat_text, vision_json
from ..progress import StatusReporter
from ..prompts import document_summary_prompt, inbox_prompt, photo_prompt
from ..runtime import HEAVY_SEMAPHORE
from ..schemas import InboxItem, InboxResult
from ..stt_whisper import STTError, whisper
from ..ui_formatter import (
    format_event,
    format_inbox_item,
    format_note,
    format_reminder,
    format_schedule_slot,
    format_task_compact,
    safe_edit,
    send_long,
)
from ..utils import (
    clip,
    esc,
    extract_urls,
    fmt_dt,
    parse_datetime,
    parse_hhmm,
    title_from_text,
    to_db,
    to_db_date,
)

log = logging.getLogger(__name__)
router = Router(name="inbox")

DEBOUNCE_SECONDS = 1.8
PAGE_SIZE = 8
MAX_BATCH_IMAGES = 4
MAX_FETCH_URLS = 2
MAX_PROMPT_CHARS = 24000
MAX_NOTE_CHARS = 60000

STATUS_NAMES = {
    "all": "Все",
    "processed": "Обработанные",
    "pending": "В очереди",
    "pending_retry": "Ошибки",
    "failed": "Ошибки",
}
TYPE_ICONS = {
    "text": "💬",
    "voice": "🎧",
    "photo": "📷",
    "document": "📄",
}


def db() -> Database:
    if runtime.database is None:
        raise RuntimeError("Database is not ready")
    return runtime.database


class InboxStates(StatesGroup):
    reply = State()


# --------------------------------------------------------------------------- buffer
@dataclass
class InboxPart:
    kind: str
    inbox_id: int = 0
    message_id: int = 0
    text: str = ""
    caption: str = ""
    file_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int = 0
    duration: int = 0
    media_group_id: str | None = None
    reply_to_text: str = ""
    target_task_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        for key in (
            "message_id", "text", "caption", "file_id", "file_name",
            "mime_type", "file_size", "duration", "media_group_id",
            "reply_to_text", "target_task_id",
        ):
            value = getattr(self, key)
            if value not in (None, "", 0):
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InboxPart":
        return cls(
            kind=str(data.get("kind") or "text"),
            inbox_id=int(data.get("inbox_id") or 0),
            message_id=int(data.get("message_id") or 0),
            text=str(data.get("text") or ""),
            caption=str(data.get("caption") or ""),
            file_id=str(data.get("file_id") or ""),
            file_name=str(data.get("file_name") or ""),
            mime_type=str(data.get("mime_type") or ""),
            file_size=int(data.get("file_size") or 0),
            duration=int(data.get("duration") or 0),
            media_group_id=data.get("media_group_id"),
            reply_to_text=str(data.get("reply_to_text") or ""),
            target_task_id=int(data["target_task_id"]) if data.get("target_task_id") else None,
        )


@dataclass
class Batch:
    chat_id: int
    parts: list[InboxPart] = field(default_factory=list)
    timer: asyncio.Task[Any] | None = None


PENDING: dict[int, Batch] = {}
BACKGROUND: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


def _raw_payload(chat_id: int, part: InboxPart) -> str:
    return json.dumps(
        {"chat_id": chat_id, "parts": [part.to_dict()]},
        ensure_ascii=False,
    )


async def _capture(message: Message, part: InboxPart) -> None:
    """Write to inbox_log first, only then add to the debounce buffer."""
    raw = _raw_payload(message.chat.id, part)
    inbox_id = await db().create_inbox(part.kind, raw, status="pending")
    part.inbox_id = inbox_id
    _enqueue(message.chat.id, part)


def _enqueue(chat_id: int, part: InboxPart) -> None:
    batch = PENDING.get(chat_id)
    if batch is None:
        batch = Batch(chat_id=chat_id)
        PENDING[chat_id] = batch
    batch.parts.append(part)
    if batch.timer is not None and not batch.timer.done():
        batch.timer.cancel()
    batch.timer = asyncio.create_task(_debounced_flush(chat_id, batch))
    _track(batch.timer)


async def _debounced_flush(chat_id: int, batch: Batch) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    if PENDING.get(chat_id) is batch:
        PENDING.pop(chat_id, None)
    bot = runtime.bot
    if bot is None:
        log.error("Bot is not ready, cannot process inbox batch")
        return
    try:
        await _process_parts(bot, chat_id, list(batch.parts))
    except Exception:  # noqa: BLE001 - background safety net
        log.exception("Unhandled inbox batch failure")
        await _set_status(batch.parts, "pending_retry")


async def shutdown_pending() -> None:
    """Cancel timers and leave captured messages retryable after restart."""
    for batch in list(PENDING.values()):
        if batch.timer is not None:
            batch.timer.cancel()
        await _set_status(batch.parts, "pending_retry")
    PENDING.clear()


async def recover_unfinished() -> int:
    """On startup, turn stale pending/processing rows into retryable ones."""
    try:
        return await db().execute_rowcount(
            "UPDATE inbox_log SET status='pending_retry' "
            "WHERE status IN ('pending', 'processing')"
        )
    except Exception as exc:  # noqa: BLE001 - startup should not die
        log.warning("Could not recover unfinished inbox rows: %s", exc)
        return 0


# --------------------------------------------------------------------------- capture handlers
@router.message(Command("inbox"))
async def cmd_inbox(message: Message, state: FSMContext) -> None:
    await state.clear()
    arg = _command_arg(message).lower()
    status = _normalize_status(arg)
    text, keyboard = await _inbox_payload(status, 0)
    await send_long(message.bot, message.chat.id, text, keyboard)


async def _extract_reply_info(message: Message, state: FSMContext) -> tuple[str, int | None]:
    reply_to_text = ""
    target_task_id = None
    try:
        data = await state.get_data()
        reply_to_text = str(data.get("reply_context") or "")
        target_task_id = data.get("target_task_id")
        current_state = await state.get_state()
        if current_state == InboxStates.reply:
            await state.clear()
    except Exception:
        pass

    if message.reply_to_message:
        quoted = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if quoted:
            prefix = f"Ответ на сообщение: «{quoted[:300]}»"
            reply_to_text = f"{prefix}\n{reply_to_text}".strip()
    return reply_to_text, target_task_id


@router.message(StateFilter(None, InboxStates.reply), F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    reply_to_text, target_task_id = await _extract_reply_info(message, state)
    part = InboxPart(
        kind="text",
        text=text,
        message_id=message.message_id,
        reply_to_text=reply_to_text,
        target_task_id=target_task_id,
    )
    await _capture_safe(message, part)


@router.message(StateFilter(None, InboxStates.reply), F.voice)
async def on_voice(message: Message, state: FSMContext) -> None:
    voice = message.voice
    reply_to_text, target_task_id = await _extract_reply_info(message, state)
    part = InboxPart(
        kind="voice",
        message_id=message.message_id,
        caption=(message.caption or "").strip(),
        file_id=voice.file_id,
        mime_type=voice.mime_type or "audio/ogg",
        file_size=voice.file_size or 0,
        duration=voice.duration or 0,
        media_group_id=message.media_group_id,
        reply_to_text=reply_to_text,
        target_task_id=target_task_id,
    )
    await _capture_safe(message, part)


@router.message(StateFilter(None, InboxStates.reply), F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    reply_to_text, target_task_id = await _extract_reply_info(message, state)
    part = InboxPart(
        kind="photo",
        message_id=message.message_id,
        caption=(message.caption or "").strip(),
        file_id=photo.file_id,
        file_size=photo.file_size or 0,
        media_group_id=message.media_group_id,
        reply_to_text=reply_to_text,
        target_task_id=target_task_id,
    )
    await _capture_safe(message, part)


@router.message(StateFilter(None, InboxStates.reply), F.document)
async def on_document(message: Message, state: FSMContext) -> None:
    document = message.document
    reply_to_text, target_task_id = await _extract_reply_info(message, state)
    part = InboxPart(
        kind="document",
        message_id=message.message_id,
        caption=(message.caption or "").strip(),
        file_id=document.file_id,
        file_name=document.file_name or "document",
        mime_type=document.mime_type or "",
        file_size=document.file_size or 0,
        media_group_id=message.media_group_id,
        reply_to_text=reply_to_text,
        target_task_id=target_task_id,
    )
    await _capture_safe(message, part)


async def _capture_safe(message: Message, part: InboxPart) -> None:
    try:
        await _capture(message, part)
    except Exception as exc:  # noqa: BLE001 - DB failures must be visible
        log.exception("Could not capture inbox message")
        await message.answer(
            "⚠️ Не смог сохранить сообщение в Inbox. Проверь базу и попробуй ещё раз.\n"
            f"<i>{esc(clip(exc, 300))}</i>"
        )


@router.message()
async def on_unsupported(message: Message) -> None:
    if message.text and message.text.strip().startswith("/"):
        await message.answer(
            "🤷 Неизвестная команда. Открой /help или /menu.",
        )
        return
    await message.answer(
        "Пока я понимаю текст, голосовые, фото и документы "
        "(PDF, DOCX, TXT, MD). Отправь что-нибудь из этого."
    )


# --------------------------------------------------------------------------- processing
async def _process_parts(bot: Bot, chat_id: int, parts: list[InboxPart]) -> bool:
    now = await settings.now()
    tz = await settings.timezone()
    tz_name = str(tz)
    profile = await settings.user_profile()

    texts: list[str] = []
    captions: list[str] = []
    images: list[tuple[bytes, str]] = []
    document_entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    source_urls: list[str] = []
    created: list[dict[str, Any]] = []

    reporter = await StatusReporter.create(bot, chat_id, settings)
    typing = asyncio.create_task(_typing_loop(bot, chat_id))
    try:
        for part in parts:
            try:
                if part.kind == "text":
                    if part.text.strip():
                        texts.append(part.text.strip())
                elif part.kind == "voice":
                    await reporter.step("voice", "🎧 Расшифровываю аудио через Whisper...")
                    transcript = await _transcribe(bot, chat_id, part)
                    if transcript:
                        texts.append(transcript)
                        await reporter.step_done(f"🎧 Аудио расшифровано ({len(transcript)} симв.)")
                elif part.kind == "photo":
                    if part.caption.strip():
                        captions.append(part.caption.strip())
                    if not bool(await settings.get("vlm_enabled")):
                        warnings.append(
                            "🧠 VLM выключен в настройках. Фото сохранено в Inbox "
                            "и будет доступно для повтора."
                        )
                        errors.append("VLM отключён — изображение не разобрано.")
                        continue
                    await reporter.step("llm", "📷 Загружаю и подготавливаю изображение...")
                    data = await _download(bot, part)
                    images.append((data, part.caption.strip()))
                elif part.kind == "document":
                    doc_label = part.file_name or "документ"
                    await reporter.step("doc", f"📄 Извлекаю текст из {clip(doc_label, 30)}...")
                    entry = await _process_document(bot, part)
                    if entry:
                        document_entries.append(entry)
                        await reporter.step_done(f"📄 Документ разобран: {clip(doc_label, 25)}")
                else:
                    warnings.append(f"Неизвестный тип вложения: {esc(part.kind)}")
            except (STTError, DocumentError, LLMError) as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - one part must not kill the batch
                log.exception("Inbox part failed (%s)", part.kind)
                errors.append(f"Сбой обработки ({part.kind}): {exc}")

        if captions:
            texts.extend(captions)

        # URL enrichment: only the first couple of links, sandboxed by the
        # browser module and explicitly marked as untrusted in the prompt.
        page_blocks: list[str] = []
        all_urls = _unique_urls(texts)
        for url in all_urls[:MAX_FETCH_URLS]:
            try:
                await reporter.step("browser", f"🌐 Открываю ссылку: {clip(url, 45)}...")
                page = await extract_page(
                    url, goal="Извлечь факты, даты, дедлайны и условия участия"
                )
                page_blocks.append(page.prompt_text(limit=12000))
                source_urls.append(url)
                await reporter.step_done(f"🌐 Страница открыта: {clip(page.title or url, 30)}")
            except BrowserError as exc:
                warnings.append(f"🌐 {url}: {esc(clip(exc, 220))}")
            except Exception as exc:  # noqa: BLE001 - browser is optional
                log.warning("Link extraction failed for %s: %s", url, exc)
                warnings.append(f"🌐 Не удалось открыть {esc(clip(url, 120))}")
        if all_urls:
            source_urls.extend(url for url in all_urls if url not in source_urls)

        context_parts: list[str] = []
        if texts:
            context_parts.append("\n\n".join(texts))
        if page_blocks:
            context_parts.append("\n\n".join(page_blocks))
        if source_urls:
            context_parts.append(
                "Ссылки из сообщения:\n" + "\n".join(f"- {url}" for url in source_urls)
            )
        content = "\n\n".join(context_parts).strip()
        if len(content) > MAX_PROMPT_CHARS:
            content = content[:MAX_PROMPT_CHARS] + "\n\n[... текст обрезан ...]"

        active_tasks = await db().list_tasks(filter_name="active", limit=7)

        dialog_snippets: list[str] = []
        target_task_id: int | None = None
        for part in parts:
            if part.reply_to_text:
                dialog_snippets.append(part.reply_to_text)
            if part.target_task_id:
                target_task_id = part.target_task_id

        if target_task_id:
            target_task = await db().get_task(target_task_id)
            if target_task:
                dl = f", дедлайн: {target_task['deadline_at']}" if target_task.get("deadline_at") else ""
                dialog_snippets.append(
                    f"Дополняемая задача [ID {target_task_id}]: «{target_task['title']}» ({target_task['category']}{dl})"
                )

        try:
            recent_logs = await db().fetchall(
                "SELECT raw_content FROM inbox_log WHERE status='processed' ORDER BY id DESC LIMIT 2"
            )
            for row in reversed(recent_logs):
                try:
                    payload = json.loads(row["raw_content"])
                    for p in payload.get("parts", []):
                        t = p.get("text") or p.get("caption")
                        if t:
                            dialog_snippets.append(f"Предыдущее сообщение пользователя: «{clip(t, 200)}»")
                except Exception:
                    pass
        except Exception:
            pass

        recent_dialog = "\n".join(dialog_snippets) if dialog_snippets else None

        result_items: list[InboxItem] = []
        summaries: list[str] = []

        vision_success = 0
        if images:
            await reporter.step("llm", "🧠 Анализирую изображение и текст через VLM...")
            for image, caption in images[:MAX_BATCH_IMAGES]:
                try:
                    raw = await vision_json(
                        image,
                        photo_prompt(
                            now, tz_name, profile,
                            (caption or content)[:8000] or "(без подписи)",
                            active_tasks=active_tasks,
                            recent_dialog=recent_dialog,
                        ),
                        InboxResult,
                        max_tokens=1200,
                    )
                    parsed = InboxResult.model_validate(raw)
                    result_items.extend(parsed.items)
                    if parsed.reply_summary:
                        summaries.append(parsed.reply_summary)
                    vision_success += 1
                except Exception as exc:  # noqa: BLE001 - fallback below
                    log.warning("VLM inbox failed: %s", exc)
                    errors.append(f"VLM: {exc}")
            if len(images) > MAX_BATCH_IMAGES:
                warnings.append(
                    f"📷 Разобраны первые {MAX_BATCH_IMAGES} изображения из {len(images)}."
                )
            if vision_success > 0:
                await reporter.step_done("🧠 Изображения проанализированы")

        if content and (not images or vision_success == 0):
            try:
                await reporter.step("llm", "🧠 Анализирую запрос через нейросеть, готовлю ответ...")
                raw = await chat_json(
                    [
                        {
                            "role": "user",
                            "content": inbox_prompt(
                                now, tz_name, profile, content,
                                active_tasks=active_tasks,
                                recent_dialog=recent_dialog,
                            ),
                        }
                    ],
                    InboxResult,
                    max_tokens=1200,
                )
                parsed = InboxResult.model_validate(raw)
                result_items.extend(parsed.items)
                if parsed.reply_summary:
                    summaries.append(parsed.reply_summary)
                await reporter.step_done("🧠 Ответ нейросети готов")
            except LLMError as exc:
                errors.append(f"LLM: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Inbox LLM routing failed")
                errors.append(f"LLM: {exc}")

        result_items = _dedupe_items(result_items)
        source_url = source_urls[0] if source_urls else None

        if result_items:
            await reporter.step("calendar", f"📅 Сохраняю задачи и события ({len(result_items)})...")

        for item in result_items:
            try:
                created_entry = await _apply_item(
                    item, now, tz, source_url, warnings, target_task_id=target_task_id
                )
                if created_entry:
                    created.append(created_entry)
            except Exception as exc:  # noqa: BLE001 - keep other items
                log.exception("Could not persist inbox item %s", item.type)
                errors.append(f"«{item.title}»: {exc}")

        fatal = bool(errors) and not created and not document_entries
        status = "pending_retry" if fatal else "processed"
        await _set_status(parts, status)

        # Clean up temporary progress message before rendering final card
        await reporter.finish(delete=True)

        summary = _join_summaries(summaries)
        if fatal:
            await _send_failure(bot, chat_id, parts, errors, warnings)
        else:
            await _send_result(
                bot, chat_id, created, document_entries, summary,
                warnings, errors, now, parts,
            )
        return not fatal
    except Exception as exc:  # noqa: BLE001 - whole batch safety net
        log.exception("Inbox processing crashed")
        await reporter.fail(str(exc))
        await _set_status(parts, "pending_retry")
        await _send_failure(bot, chat_id, parts, [str(exc)], warnings)
        return False
    finally:
        typing.cancel()


async def _typing_loop(bot: Bot, chat_id: int) -> None:
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:  # noqa: BLE001 - cosmetic only
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return


async def _download(bot: Bot, part: InboxPart) -> bytes:
    if not part.file_id:
        raise ValueError("В сообщении нет file_id")
    stream = await bot.download(part.file_id)
    if stream is None:
        raise ValueError("Telegram не вернул файл")
    try:
        if isinstance(stream, bytes):
            data = stream
        elif hasattr(stream, "read"):
            data = stream.read()
        else:
            data = bytes(stream)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if not data:
        raise ValueError("Файл пустой")
    return data


async def _transcribe(bot: Bot, chat_id: int, part: InboxPart) -> str:
    data = await _download(bot, part)
    transcript = await whisper.transcribe(data, suffix=".ogg")
    if transcript is None:
        raise STTError(
            "Whisper отключён в настройках (/settings → модели). "
            "Включи модель или отправь текст."
        )
    await bot.send_message(
        chat_id,
        "🎧 <b>Расшифровка</b>\n"
        f"<blockquote>{esc(transcript)}</blockquote>",
    )
    return transcript


async def _process_document(bot: Bot, part: InboxPart) -> dict[str, Any] | None:
    data = await _download(bot, part)
    async with HEAVY_SEMAPHORE:
        title, text = await asyncio.to_thread(
            extract_document_text, data, part.file_name or "document"
        )
    text = text[:MAX_NOTE_CHARS]
    if part.caption.strip():
        title = part.caption.strip()[:120]
    note_id, created = await _save_note_once(title, text, tags="документ")
    summary = ""
    try:
        summary = await chat_text(
            [
                {
                    "role": "user",
                    "content": document_summary_prompt(title, text),
                }
            ],
            max_tokens=700,
        )
    except LLMError as exc:
        log.warning("Document summary failed: %s", exc)
    return {
        "id": note_id,
        "title": title,
        "summary": summary.strip(),
        "new": created,
    }


async def _save_note_once(
    title: str, content: str, tags: str | None = None
) -> tuple[int, bool]:
    existing = await db().fetchone(
        "SELECT id FROM notes WHERE title=? AND content=? LIMIT 1",
        (title, content),
    )
    if existing is not None:
        return int(existing["id"]), False
    note_id = await db().create_note(title, content, tags=tags)
    return note_id, True


# --------------------------------------------------------------------------- apply items
async def _apply_item(
    item: InboxItem,
    now: datetime,
    tz: Any,
    source_url: str | None,
    warnings: list[str],
    target_task_id: int | None = None,
) -> dict[str, Any] | None:
    kind = item.type
    start = parse_datetime(item.start_dt, tz)
    end = parse_datetime(item.end_dt, tz)
    deadline = parse_datetime(item.deadline_at, tz)
    remind_at = parse_datetime(item.reminder_at, tz)

    # Check if this item updates an existing task
    update_task_id = item.task_id or target_task_id
    if update_task_id and kind == "task":
        existing = await db().get_task(update_task_id)
        if existing is not None:
            updates: dict[str, Any] = {}
            if item.title and item.title != "Без названия" and item.title != existing["title"]:
                updates["title"] = item.title
            if item.category and item.category != existing["category"]:
                updates["category"] = item.category
            if item.energy and item.energy != existing["energy"]:
                updates["energy"] = item.energy
            if item.est_minutes and item.est_minutes != existing["est_minutes"]:
                updates["est_minutes"] = item.est_minutes
            if deadline:
                updates["deadline_at"] = to_db(deadline)
            if item.first_step and item.first_step != existing["first_step"]:
                updates["first_step"] = item.first_step
            if source_url or item.source_url or item.location_or_url:
                updates["source_url"] = item.source_url or source_url or item.location_or_url
            if updates:
                await db().update_task(update_task_id, **updates)
            if remind_at is not None:
                await db().create_reminder(
                    f"⏰ {existing['title']}", to_db(remind_at), task_id=update_task_id
                )
            return {"kind": "task", "id": update_task_id, "updated": True}

    if kind == "recurring_schedule":
        weekday = item.weekday
        start_time = parse_hhmm(item.start_time)
        end_time = parse_hhmm(item.end_time)
        if weekday is None or start_time is None or end_time is None:
            warnings.append(
                f"📅 У регулярного дела «{esc(item.title)}» нет точного слота времени в расписании — сохранил как задачу под свободные окна."
            )
            first_step = item.first_step or "Выделить 30-60 минут в свободное окно и позаниматься."
            return await _create_task(
                item, now, deadline, remind_at, source_url, first_step=first_step
            )
        if end_time <= start_time:
            end_time = (datetime.combine(now.date(), start_time) + timedelta(hours=1)).time()
        existing = await db().list_recurring(int(weekday))
        for slot in existing:
            if (
                slot["start_time"] == start_time.strftime("%H:%M")
                and slot["end_time"] == end_time.strftime("%H:%M")
                and str(slot["title"]).strip().lower() == item.title.strip().lower()
            ):
                return {"kind": "recurring", "id": int(slot["id"])}
        slot_id = await db().create_recurring(
            int(weekday),
            start_time.strftime("%H:%M"),
            end_time.strftime("%H:%M"),
            item.title,
            category=item.category,
            location=item.location_or_url,
        )
        return {"kind": "recurring", "id": slot_id}

    if kind == "event":
        if start is None:
            warnings.append(
                f"📅 У события «{esc(item.title)}» не было времени — сохранил как задачу."
            )
            return await _create_task(item, now, deadline, remind_at, source_url)
        event_id = await db().create_event(
            title=item.title,
            category=item.category,
            start_dt=to_db(start),
            end_dt=to_db(end) if end and end > start else None,
            location_or_url=item.location_or_url,
            reg_deadline=to_db(deadline) if deadline else None,
            reg_status=item.reg_status,
            prep_notes=item.prep_notes,
            source_url=item.source_url or source_url,
        )
        return {"kind": "event", "id": event_id}

    if kind == "reminder":
        target = remind_at or start
        if target is None:
            warnings.append(
                f"⏰ У напоминания «{esc(item.title)}» нет времени — сохранил как задачу, "
                "чтобы не потерять."
            )
            return await _create_task(
                item, now, deadline, remind_at, source_url,
                first_step="Уточнить, когда напомнить",
            )
        reminder_id = await db().create_reminder(item.title, to_db(target))
        return {"kind": "reminder", "id": reminder_id}

    if kind == "note":
        content = (item.content or item.prep_notes or item.title or "").strip()
        title = item.title.strip()
        if title in {"", "Без названия"}:
            title = title_from_text(content or "Заметка")
        if not content:
            content = source_url or item.location_or_url or title
        note_id, _created = await _save_note_once(
            title, content[:MAX_NOTE_CHARS], tags="inbox"
        )
        return {"kind": "note", "id": note_id}

    # Default: task, including unknown types coerced by Pydantic.
    return await _create_task(item, now, deadline, remind_at, source_url)


async def _create_task(
    item: InboxItem,
    now: datetime,
    deadline: datetime | None,
    remind_at: datetime | None,
    source_url: str | None,
    first_step: str | None = None,
) -> dict[str, Any]:
    start = parse_datetime(item.start_dt, now.tzinfo)
    first = first_step or item.first_step
    if not first:
        first = f"Открыть «{item.title}» и сделать первый шаг на 10 минут."
    task_id = await db().create_task(
        title=item.title,
        category=item.category,
        energy=item.energy,
        est_minutes=item.est_minutes,
        deadline_at=to_db(deadline) if deadline else None,
        scheduled_date=to_db_date(start.date()) if start else None,
        scheduled_time=start.strftime("%H:%M") if start else None,
        first_step=first,
        source_url=item.source_url or source_url,
    )
    if remind_at is not None:
        await db().create_reminder(
            f"⏰ {item.title}", to_db(remind_at), task_id=task_id
        )
    return {"kind": "task", "id": task_id}


def _dedupe_items(items: list[InboxItem]) -> list[InboxItem]:
    seen: set[tuple[str, str, str]] = set()
    result: list[InboxItem] = []
    for item in items:
        key = (
            item.type,
            item.title.strip().lower(),
            str(item.start_dt or item.deadline_at or item.reminder_at or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _join_summaries(summaries: list[str]) -> str:
    result: list[str] = []
    for summary in summaries:
        text = " ".join(str(summary).split())
        if text and text not in result:
            result.append(text)
    return " ".join(result)


def _unique_urls(texts: list[str]) -> list[str]:
    result: list[str] = []
    for text in texts:
        for url in extract_urls(text):
            cleaned = url.rstrip(".,;:!?)]}»\"'")
            if cleaned not in result:
                result.append(cleaned)
    return result


# --------------------------------------------------------------------------- result cards
async def _send_result(
    bot: Bot,
    chat_id: int,
    created: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    summary: str,
    warnings: list[str],
    errors: list[str],
    now: datetime,
    parts: list[InboxPart],
) -> None:
    if not created and not documents:
        if summary:
            lines = [esc(summary)]
        else:
            lines = [
                "💬 <b>Принято!</b>",
                "",
                "Если хочешь поставить задачу, напоминание или спросить о расписании — просто напиши.",
            ]
    else:
        has_updated = any(entry.get("updated") for entry in created)
        if has_updated:
            lines = ["✅ <b>Обновил задачу:</b>"]
        elif len(created) > 1:
            lines = ["✅ <b>Зафиксировал:</b>"]
        else:
            lines = ["✅ <b>Готово:</b>"]
        if summary:
            lines.extend(["", esc(summary)])

    task_entries: list[tuple[int, int]] = []
    event_entries: list[int] = []
    reminder_entries: list[int] = []
    task_number = 0

    for entry in created:
        kind = entry["kind"]
        row_id = int(entry["id"])
        if kind == "task":
            task = await db().get_task(row_id)
            if task is None:
                continue
            task_number += 1
            task_entries.append((task_number, row_id))
            status_tag = " <i>[обновлена]</i>" if entry.get("updated") else ""
            block = f"<b>{task_number}.</b>{status_tag} " + format_task_compact(task, now)
            if task["first_step"]:
                block += f"\n   🪜 {esc(task['first_step'])}"
            lines.extend(["", block])
        elif kind == "event":
            event = await db().get_event(row_id)
            if event is None:
                continue
            event_entries.append(row_id)
            lines.extend(["", "📅 " + format_event(event, now, detailed=True)])
        elif kind == "reminder":
            reminder = await db().get_reminder(row_id)
            if reminder is None:
                continue
            reminder_entries.append(row_id)
            lines.extend(["", "⏰ " + format_reminder(reminder, now, detailed=True)])
        elif kind == "note":
            note = await db().get_note(row_id)
            if note is None:
                continue
            lines.extend(["", "🗒 " + format_note(note, full=False)])
        elif kind == "recurring":
            slot = await db().get_recurring(row_id)
            if slot is None:
                continue
            lines.extend(["", "🔁 Регулярное занятие:", format_schedule_slot(slot)])

    for document in documents:
        lines.extend(
            [
                "",
                f"📄 <b>{esc(document['title'])}</b> — "
                + ("сохранён в заметки." if document["new"] else "уже был в заметках."),
            ]
        )
        if document["summary"]:
            lines.extend(["", esc(document["summary"])])

    if warnings:
        lines.extend(["", "⚠️ <b>Замечания</b>"])
        for warning in _dedupe_strings(warnings):
            lines.append(f"• {warning}")
    if errors:
        lines.extend(["", "⚠️ <b>Не всё удалось</b>"])
        for error in _dedupe_strings(errors):
            lines.append(f"• {esc(clip(error, 300))}")

    keyboard = _result_keyboard(task_entries, event_entries, reminder_entries, parts)
    await send_long(bot, chat_id, "\n".join(lines), keyboard)


def _result_keyboard(
    tasks: list[tuple[int, int]],
    events: list[int],
    reminders: list[int],
    parts: list[InboxPart],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    single_task = len(tasks) == 1
    for number, task_id in tasks[:4]:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Готово" if single_task else f"✅ #{number}",
                    callback_data=f"t:done:{task_id}:active:0",
                ),
                InlineKeyboardButton(
                    text="⏭ На завтра" if single_task else f"⏭ #{number}",
                    callback_data=f"t:tom:{task_id}:active:0",
                ),
                InlineKeyboardButton(
                    text="📅 В окно" if single_task else f"📅 #{number}",
                    callback_data=f"t:auto:{task_id}:active:0",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить" if single_task else f"🗑 #{number}",
                    callback_data=f"t:delask:{task_id}:active:0",
                ),
            ]
        )
    for event_id in events[:3]:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚪️ Регистрация",
                    callback_data=f"event:reg:{event_id}",
                ),
                InlineKeyboardButton(
                    text="📅 Событие", callback_data=f"event:view:{event_id}"
                ),
            ]
        )
    for reminder_id in reminders[:2]:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Напоминание", callback_data=f"rem:done:{reminder_id}:n"
                ),
                InlineKeyboardButton(
                    text="⏰ +2ч", callback_data=f"rem:snooze:120:{reminder_id}:n"
                ),
            ]
        )

    # Interactive Reply / Supplement button
    reply_task_id = tasks[0][1] if tasks else 0
    rows.append(
        [
            InlineKeyboardButton(
                text="💬 Ответить / Дополнить",
                callback_data=f"inbox:reply:{reply_task_id}",
            )
        ]
    )

    bottom_row: list[InlineKeyboardButton] = []
    if tasks:
        bottom_row.append(InlineKeyboardButton(text="📋 Задачи", callback_data="t:list:active:0"))
    bottom_row.append(InlineKeyboardButton(text="📥 Inbox", callback_data="inbox:list:0"))
    bottom_row.append(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    rows.append(bottom_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("inbox:reply:"))
async def cb_inbox_reply(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    raw_id = parts[2] if len(parts) > 2 else "0"
    task_id = int(raw_id) if raw_id.isdigit() and int(raw_id) > 0 else None

    task_context = ""
    if task_id:
        task = await db().get_task(task_id)
        if task:
            dl = f", дедлайн: {task['deadline_at']}" if task.get("deadline_at") else ""
            task_context = f"Задача [ID {task_id}]: «{task['title']}»{dl}"

    await state.set_state(InboxStates.reply)
    await state.update_data(target_task_id=task_id, reply_context=task_context)
    await callback.answer()

    lines = ["💬 <b>Режим диалога и дополнения</b>", ""]
    if task_context:
        lines.append(f"📌 {esc(task_context)}\n")
    lines.extend([
        "Напиши текстом или отправь голосовое:",
        "• Что дополнить или изменить (дедлайн, ссылку, примечание, оценку времени);",
        "• Или задай вопрос секретарю по этой задаче / планам.",
    ])
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Выйти из диалога", callback_data="inbox:reply:cancel")]]
    )
    await callback.message.answer("\n".join(lines), reply_markup=cancel_kb)


@router.callback_query(F.data == "inbox:reply:cancel")
async def cb_inbox_reply_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Диалог завершён")
    try:
        await callback.message.edit_text("✖️ Диалог завершён. Ты можешь отправлять любые сообщения как обычно.")
    except Exception:
        pass


async def _send_failure(
    bot: Bot,
    chat_id: int,
    parts: list[InboxPart],
    errors: list[str],
    warnings: list[str],
) -> None:
    lines = [
        "⚠️ <b>Не удалось обработать сообщение</b>",
        "",
        "Оно сохранено в Inbox — можно повторить позже.",
    ]
    for error in _dedupe_strings(errors):
        lines.append(f"• {esc(clip(error, 320))}")
    if warnings:
        lines.append("")
        for warning in _dedupe_strings(warnings):
            lines.append(f"• {warning}")
    rows: list[list[InlineKeyboardButton]] = []
    inbox_id = next((part.inbox_id for part in parts if part.inbox_id), 0)
    if inbox_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить", callback_data=f"inbox:retry:{inbox_id}"
                ),
                InlineKeyboardButton(
                    text="📥 Открыть Inbox", callback_data=f"inbox:show:{inbox_id}"
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    await send_long(bot, chat_id, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text not in result:
            result.append(text)
    return result


async def _set_status(parts: list[InboxPart], status: str) -> None:
    for inbox_id in {part.inbox_id for part in parts if part.inbox_id}:
        try:
            await db().update_inbox(int(inbox_id), status=status)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not update inbox %s: %s", inbox_id, exc)


# --------------------------------------------------------------------------- inbox views
@router.callback_query(F.data == "inbox:list:0")
async def cb_inbox_list_short(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _inbox_payload("all", 0)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("inbox:list:"))
async def cb_inbox_list(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    status = _normalize_status(parts[3] if len(parts) > 3 else "all")
    await callback.answer()
    text, keyboard = await _inbox_payload(status, offset)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("inbox:show:"))
async def cb_inbox_show(callback: CallbackQuery) -> None:
    inbox_id = _callback_int(callback.data, 2)
    item = await db().get_inbox(inbox_id) if inbox_id else None
    if item is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await callback.answer()
    text, keyboard = _inbox_item_payload(item)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("inbox:retry:"))
async def cb_inbox_retry(callback: CallbackQuery) -> None:
    inbox_id = _callback_int(callback.data, 2)
    item = await db().get_inbox(inbox_id) if inbox_id else None
    if item is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    parts = _parts_from_item(item)
    if not parts:
        await callback.answer("В записи нет данных для повтора", show_alert=True)
        return
    await db().update_inbox(inbox_id, status="pending")
    await callback.answer("🔄 Повторяю…")
    await safe_edit(
        callback,
        f"🔄 <b>Повторная обработка</b>\n\n{esc(item['msg_type'])} · {esc(item['created_at'])}",
    )
    await _process_parts(callback.bot, callback.message.chat.id, parts)


@router.callback_query(F.data.startswith("inbox:delask:"))
async def cb_inbox_delask(callback: CallbackQuery) -> None:
    inbox_id = _callback_int(callback.data, 2)
    item = await db().get_inbox(inbox_id) if inbox_id else None
    if item is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback,
        f"🗑 Удалить запись <b>#{inbox_id}</b> из Inbox?\n\n"
        "Созданные из неё задачи и заметки останутся на месте.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить", callback_data=f"inbox:del:{inbox_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✖️ Отмена", callback_data=f"inbox:show:{inbox_id}"
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("inbox:del:"))
async def cb_inbox_del(callback: CallbackQuery) -> None:
    inbox_id = _callback_int(callback.data, 2)
    if inbox_id:
        await db().delete_inbox(inbox_id)
    await callback.answer("Удалено")
    text, keyboard = await _inbox_payload("all", 0)
    await safe_edit(callback, text, keyboard)


async def _inbox_payload(status: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    status = _normalize_status(status)
    total = await db().count_inbox()
    processed = await db().count_inbox("processed")
    pending = await db().count_inbox("pending")
    retry = await db().count_inbox("pending_retry") + await db().count_inbox("failed")
    items = await db().list_inbox(
        limit=PAGE_SIZE,
        offset=max(0, offset),
        status=None if status == "all" else status,
    )
    lines = [
        "📥 <b>Inbox</b>",
        "",
        f"Всего: <b>{total}</b> · ✅ {processed} · 🕓 {pending} · ⚠️ {retry}",
    ]
    if status != "all":
        lines.append(f"Фильтр: <b>{STATUS_NAMES.get(status, status)}</b>")
    lines.append("")
    if not items:
        lines.append("Записей нет.")
    for item in items:
        lines.append(format_inbox_item(item, datetime.now()))
        lines.append("")
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=f"Все ({total})", callback_data="inbox:list:0:all"),
            InlineKeyboardButton(
                text=f"✅ ({processed})", callback_data="inbox:list:0:processed"
            ),
        ],
        [
            InlineKeyboardButton(text=f"🕓 ({pending})", callback_data="inbox:list:0:pending"),
            InlineKeyboardButton(text=f"⚠️ ({retry})", callback_data="inbox:list:0:pending_retry"),
        ],
    ]
    item_buttons = [
        InlineKeyboardButton(
            text=f"{TYPE_ICONS.get(str(item['msg_type']), '•')} #{item['id']} · "
                 f"{str(item['status']).replace('pending_retry', 'ошибка')}",
            callback_data=f"inbox:show:{item['id']}",
        )
        for item in items
    ]
    for index in range(0, len(item_buttons), 2):
        rows.append(item_buttons[index:index + 2])
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"inbox:list:{max(0, offset - PAGE_SIZE)}:{status}"
            )
        )
    if offset + PAGE_SIZE < (len(items) + offset) or len(items) == PAGE_SIZE:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"inbox:list:{offset + PAGE_SIZE}:{status}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _inbox_item_payload(item: Any) -> tuple[str, InlineKeyboardMarkup]:
    status = str(item["status"])
    lines = [
        f"📥 <b>Inbox #{item['id']}</b>",
        f"Тип: <b>{esc(item['msg_type'])}</b> · статус: <b>{esc(status)}</b>",
        f"Создано: {esc(item['created_at'])}",
        "",
        "<b>Содержимое</b>",
        _raw_preview(str(item["raw_content"])),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if status in {"pending_retry", "failed", "pending"}:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить", callback_data=f"inbox:retry:{item['id']}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить", callback_data=f"inbox:delask:{item['id']}"
            ),
            InlineKeyboardButton(text="⬅️ К Inbox", callback_data="inbox:list:0:all"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _raw_preview(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return f"<blockquote>{esc(clip(raw, 2600))}</blockquote>"
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, list) or not parts:
        return f"<blockquote>{esc(clip(raw, 2600))}</blockquote>"
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or "text")
        icon = TYPE_ICONS.get(kind, "•")
        text = str(part.get("text") or part.get("caption") or "").strip()
        details = []
        if part.get("file_name"):
            details.append(str(part["file_name"]))
        if part.get("duration"):
            details.append(f"{part['duration']} сек")
        if part.get("file_size"):
            details.append(f"{int(part['file_size']) // 1024} КБ")
        header = f"{icon} <b>{esc(kind)}</b>"
        if details:
            header += " · " + esc(" · ".join(details))
        lines.append(header)
        if text:
            lines.append(f"<blockquote>{esc(clip(text, 2200))}</blockquote>")
    return "\n".join(lines) or "<i>Пусто</i>"


def _parts_from_item(item: Any) -> list[InboxPart]:
    raw = str(item["raw_content"] or "")
    inbox_id = int(item["id"])
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return [InboxPart(kind="text", text=raw, inbox_id=inbox_id)]
    raw_parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(raw_parts, list) or not raw_parts:
        return [InboxPart(kind="text", text=raw, inbox_id=inbox_id)]
    result: list[InboxPart] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            continue
        part = InboxPart.from_dict(raw_part)
        part.inbox_id = inbox_id
        result.append(part)
    return result


def _normalize_status(value: str | None) -> str:
    text = (value or "all").strip().lower()
    aliases = {
        "": "all",
        "all": "all",
        "все": "all",
        "processed": "processed",
        "done": "processed",
        "обработанные": "processed",
        "pending": "pending",
        "queue": "pending",
        "в очереди": "pending",
        "pending_retry": "pending_retry",
        "retry": "pending_retry",
        "failed": "pending_retry",
        "ошибки": "pending_retry",
    }
    return aliases.get(text, "all")


def _callback_int(data: str | None, index: int) -> int:
    parts = (data or "").split(":")
    if len(parts) <= index:
        return 0
    try:
        return int(parts[index])
    except (TypeError, ValueError):
        return 0


def _command_arg(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
