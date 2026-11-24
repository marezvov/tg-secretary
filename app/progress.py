"""Live execution status reporter for sequential operations.

Provides real-time feedback in Telegram as multi-step tasks run
(link fetching, audio transcription, document parsing, LLM routing, scheduling).
Supports verbose, compact and off modes, with granular per-category toggles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

log = logging.getLogger(__name__)

# Minimum interval between Telegram message edits to avoid rate limits
MIN_EDIT_INTERVAL = 0.35


class StatusReporter:
    """Tracks and displays live progress for multi-step tasks in Telegram."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        mode: str = "verbose",
        categories: dict[str, bool] | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.mode = mode if mode in {"verbose", "compact", "off"} else "verbose"
        self.categories: dict[str, bool] = categories or {
            "browser": True,
            "voice": True,
            "doc": True,
            "llm": True,
            "calendar": True,
            "general": True,
        }
        self.message_id: int | None = None
        self.completed_steps: list[str] = []
        self.current_step: str = ""
        self._last_edit: float = 0.0
        self._lock = asyncio.Lock()
        self._finished = False

    @classmethod
    async def create(cls, bot: Bot, chat_id: int, settings: Any) -> "StatusReporter":
        """Instantiate a reporter configured according to user settings."""
        try:
            mode = str(await settings.get("progress_mode") or "verbose")
            categories = {
                "browser": bool(await settings.get("progress_notify_browser")),
                "voice": bool(await settings.get("progress_notify_voice")),
                "doc": bool(await settings.get("progress_notify_doc")),
                "llm": bool(await settings.get("progress_notify_llm")),
                "calendar": bool(await settings.get("progress_notify_calendar")),
                "general": True,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read progress settings: %s", exc)
            mode = "verbose"
            categories = {
                "browser": True, "voice": True, "doc": True,
                "llm": True, "calendar": True, "general": True,
            }
        return cls(bot=bot, chat_id=chat_id, mode=mode, categories=categories)

    def is_enabled(self, category: str = "general") -> bool:
        """Check if this category should be displayed in current mode."""
        if self.mode == "off" or self._finished:
            return False
        return self.categories.get(category, True)

    async def step(self, category: str, text: str) -> None:
        """Advance to a new step, updating or sending the status message."""
        if not self.is_enabled(category):
            return

        async with self._lock:
            if self._finished:
                return

            if self.current_step:
                # Move current step to completed if not already added
                self.completed_steps.append(self.current_step)
                # Keep last 5 completed steps max to avoid overly long messages
                if len(self.completed_steps) > 5:
                    self.completed_steps = self.completed_steps[-5:]

            self.current_step = text
            rendered = self._render_text()
            await self._update_message(rendered)

    async def step_done(self, done_text: str | None = None) -> None:
        """Mark the active step as completed with an optional custom label."""
        if self.mode == "off" or self._finished:
            return

        async with self._lock:
            if self._finished:
                return

            if done_text:
                self.completed_steps.append(done_text)
            elif self.current_step:
                self.completed_steps.append(self.current_step)

            if len(self.completed_steps) > 5:
                self.completed_steps = self.completed_steps[-5:]
            self.current_step = ""

            rendered = self._render_text()
            await self._update_message(rendered)

    async def finish(self, delete: bool = True) -> None:
        """Clean up the progress message (delete or leave as done)."""
        async with self._lock:
            if self._finished:
                return
            self._finished = True

            if self.message_id is not None and delete:
                try:
                    await self.bot.delete_message(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                    )
                except Exception:
                    pass
                self.message_id = None

    async def fail(self, error_text: str) -> None:
        """Report a failure in the status message."""
        async with self._lock:
            if self._finished:
                return
            self._finished = True

            if self.message_id is not None:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=f"❌ <b>Ошибка обработки:</b>\n{error_text}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    def _render_text(self) -> str:
        """Build HTML string based on mode and step history."""
        if self.mode == "compact":
            text = self.current_step or (
                self.completed_steps[-1] if self.completed_steps else "Обработка..."
            )
            return f"⏳ <i>{text}</i>"

        # Verbose mode
        lines = ["⏳ <b>Выполнение:</b>"]
        for step_text in self.completed_steps:
            # Strip initial leading icon if it matches, or use checkmark
            cleaned = step_text.lstrip("⏳ •-").strip()
            lines.append(f"✓ <code>{cleaned}</code>")
        if self.current_step:
            lines.append(f"⏳ <b>{self.current_step}</b>")

        return "\n".join(lines)

    async def _update_message(self, text: str) -> None:
        """Send a new message or edit the existing one with rate limiting."""
        now = time.monotonic()
        elapsed = now - self._last_edit
        if elapsed < MIN_EDIT_INTERVAL:
            await asyncio.sleep(MIN_EDIT_INTERVAL - elapsed)

        try:
            if self.message_id is None:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode="HTML",
                )
                self.message_id = msg.message_id
            else:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode="HTML",
                )
            self._last_edit = time.monotonic()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                if self.message_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=text,
                        parse_mode="HTML",
                    )
            except Exception:
                pass
        except TelegramBadRequest as exc:
            # Ignored: "message is not modified", "message to edit not found"
            if "message is not modified" not in str(exc).lower():
                log.debug("Telegram edit error in StatusReporter: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not update progress message: %s", exc)
