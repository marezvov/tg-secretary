"""Tests for conversational inbox, task updates, reply flows, and schedule fallback."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.database import Database
from app.handlers.inbox import InboxPart, _apply_item, _result_keyboard, _send_result
from app.prompts import inbox_prompt
from app.schemas import InboxItem, InboxResult


class TestConversationalInbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db = Database(self.tmp.name)
        await self.db.connect()
        await self.db.init_schema()
        settings.bind(self.db)
        from app import runtime
        runtime.database = self.db

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.tmp.name):
            try:
                os.unlink(self.tmp.name)
            except OSError:
                pass

    def test_inbox_part_serialization(self):
        part = InboxPart(
            kind="text",
            message_id=42,
            text="Готовиться к олимпиаде",
            reply_to_text="Ответ на: «Где задача?»",
            target_task_id=15,
        )
        data = part.to_dict()
        self.assertEqual(data["reply_to_text"], "Ответ на: «Где задача?»")
        self.assertEqual(data["target_task_id"], 15)

        restored = InboxPart.from_dict(data)
        self.assertEqual(restored.reply_to_text, "Ответ на: «Где задача?»")
        self.assertEqual(restored.target_task_id, 15)

    def test_inbox_prompt_includes_active_tasks_and_dialog(self):
        now = datetime(2026, 11, 24, 14, 15)
        active_tasks = [
            {"id": 1, "title": "Подготовка к олимпиаде", "category": "olympiad", "deadline_at": "2026-11-29 23:59"}
        ]
        recent_dialog = "Пользователь спросил: «Где задача?»"
        prompt = inbox_prompt(
            now=now,
            tz_name="Europe/Moscow",
            profile="Школьник 11 класс",
            content="Добавь ссылку на архив задач https://v-olymp.ru",
            active_tasks=active_tasks,
            recent_dialog=recent_dialog,
        )
        self.assertIn("Подготовка к олимпиаде", prompt)
        self.assertIn("2026-11-29 23:59", prompt)
        self.assertIn("Пользователь спросил: «Где задача?»", prompt)
        self.assertIn("reply_summary", prompt)

    async def test_recurring_schedule_missing_time_fallback_to_task(self):
        """When user says 'каждый день до 29 ноября в свободное время', it must not be lost!"""
        now = datetime(2026, 11, 24, 14, 15)
        tz = await settings.timezone()
        warnings: list[str] = []

        item = InboxItem(
            type="recurring_schedule",
            title="Подготовка к олимпиаде по криптографии",
            category="olympiad",
            deadline_at="2026-11-29 23:59",
            location_or_url="https://v-olymp.ru/tasks-archive",
            weekday=None,
            start_time=None,
            end_time=None,
        )

        entry = await _apply_item(item, now, tz, "https://v-olymp.ru/tasks-archive", warnings)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["kind"], "task")

        # Verify task is created in database with deadline and source url
        task = await self.db.get_task(entry["id"])
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Подготовка к олимпиаде по криптографии")
        self.assertIn("2026-11-29", str(task["deadline_at"]))
        self.assertEqual(task["source_url"], "https://v-olymp.ru/tasks-archive")
        self.assertTrue(any("нет точного" in w or "сохранил как задачу" in w for w in warnings))

    async def test_update_existing_task_via_target_task_id(self):
        """When user supplements or clarifies an existing task, it must update it."""
        task_id = await self.db.create_task(
            title="Подготовка к олимпиаде",
            category="olympiad",
            energy="high",
            est_minutes=30,
        )
        now = datetime(2026, 11, 24, 14, 15)
        tz = await settings.timezone()
        warnings: list[str] = []

        item = InboxItem(
            type="task",
            title="Подготовка к олимпиаде по криптографии (финал)",
            est_minutes=60,
            deadline_at="2026-11-29 23:59",
            location_or_url="https://v-olymp.ru",
        )

        entry = await _apply_item(
            item, now, tz, "https://v-olymp.ru", warnings, target_task_id=task_id
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], task_id)
        self.assertTrue(entry.get("updated"))

        updated_task = await self.db.get_task(task_id)
        self.assertEqual(updated_task["title"], "Подготовка к олимпиаде по криптографии (финал)")
        self.assertEqual(updated_task["est_minutes"], 60)
        self.assertIn("2026-11-29", str(updated_task["deadline_at"]))
        self.assertEqual(updated_task["source_url"], "https://v-olymp.ru")

    async def test_send_result_conversational_no_entities(self):
        """When user chats or asks a question (no entities), message must be conversational, not robotic."""
        bot = MagicMock()
        bot.send_message = AsyncMock()

        await _send_result(
            bot=bot,
            chat_id=12345,
            created=[],
            documents=[],
            summary="Я проверил список задач: активных задач сейчас нет. Хочешь я создам новую задачу для олимпиады?",
            warnings=[],
            errors=[],
            now=datetime.now(),
            parts=[],
        )

        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.call_args
        call_text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
        self.assertNotIn("Разобрал, но полезных сущностей не нашёл", call_text)
        self.assertIn("Я проверил список задач", call_text)

    def test_result_keyboard_has_reply_button(self):
        keyboard = _result_keyboard(
            tasks=[(1, 10)],
            events=[],
            reminders=[],
            parts=[],
        )
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        reply_btn = next((b for b in all_buttons if "Ответить" in b.text or "Дополнить" in b.text), None)
        self.assertIsNotNone(reply_btn)
        self.assertEqual(reply_btn.callback_data, "inbox:reply:10")


if __name__ == "__main__":
    unittest.main()
