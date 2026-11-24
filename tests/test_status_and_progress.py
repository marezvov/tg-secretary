"""Unit tests for Server Status monitor and StatusReporter progress tracker."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.progress import StatusReporter
from app.system_status import (
    _format_bytes,
    _format_duration,
    check_llm_status,
    get_full_system_status,
    get_hardware_status,
)
from app.ui_formatter import format_progress_settings, format_server_status


class TestFormatHelpers(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(_format_bytes(500), "500 Б")
        self.assertEqual(_format_bytes(1024), "1.0 КБ")
        self.assertEqual(_format_bytes(1024 * 1024 * 5), "5.0 МБ")
        self.assertEqual(_format_bytes(1024 * 1024 * 1024 * 2.5), "2.5 ГБ")

    def test_format_duration(self) -> None:
        self.assertEqual(_format_duration(30), "30 сек.")
        self.assertEqual(_format_duration(120), "2 мин.")
        self.assertEqual(_format_duration(3665), "1 ч. 1 мин.")
        self.assertEqual(_format_duration(86400 * 2 + 3600 * 3 + 60 * 10), "2 д. 3 ч. 10 мин.")


class TestSystemStatus(unittest.IsolatedAsyncioTestCase):
    def test_get_hardware_status(self) -> None:
        hw = get_hardware_status()
        self.assertIn("os_name", hw)
        self.assertIn("cpu_cores", hw)
        self.assertGreaterEqual(hw["cpu_cores"], 1)
        self.assertIn("ram_text", hw)
        self.assertIn("disk_text", hw)
        self.assertIn("bot_uptime", hw)
        self.assertIn("db_size_text", hw)

    async def test_check_llm_status_offline(self) -> None:
        with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
            status = await check_llm_status()
            self.assertFalse(status["online"])
            self.assertIn("Connection refused", status["error"])

    async def test_check_llm_status_online(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"id": "qwen2.5:7b-instruct"}, {"id": "llava:7b"}]
        }
        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            status = await check_llm_status()
            self.assertTrue(status["online"])
            self.assertIn("qwen2.5:7b-instruct", status["available_models"])
            self.assertIsNotNone(status["latency_ms"])

    async def test_get_full_system_status(self) -> None:
        full = await get_full_system_status()
        self.assertIn("hardware", full)
        self.assertIn("llm", full)
        self.assertIn("whisper", full)
        self.assertIn("vlm", full)
        self.assertIn("semaphore_busy", full)
        self.assertIn("scheduler", full)

    def test_format_server_status(self) -> None:
        mock_data = {
            "hardware": {
                "os_name": "Windows 11",
                "architecture": "AMD64",
                "hostname": "test-host",
                "python_version": "3.11.0",
                "cpu_cores": 8,
                "cpu_percent": 15.0,
                "ram_text": "4.0 ГБ / 16.0 ГБ (25%)",
                "disk_text": "100.0 ГБ / 500.0 ГБ (20%)",
                "db_size_text": "2.5 МБ",
                "bot_uptime": "1 ч. 15 мин.",
                "host_uptime": "2 д. 4 ч.",
            },
            "llm": {
                "online": True,
                "base_url": "http://127.0.0.1:11434/v1",
                "configured_model": "qwen2.5:7b",
                "latency_ms": 45.2,
                "available_models": ["qwen2.5:7b", "llama3"],
                "model_active": True,
                "error": None,
            },
            "whisper": {
                "model_configured": "small",
                "in_ram": False,
                "last_used": "ещё не вызывался",
            },
            "vlm": {
                "enabled": True,
                "model": "llava:7b",
            },
            "semaphore_busy": False,
            "scheduler": {
                "running": True,
                "jobs_count": 4,
            },
        }
        text = format_server_status(mock_data)
        self.assertIn("Статус сервера и моделей", text)
        self.assertIn("Windows 11", text)
        self.assertIn("🟢 Онлайн (45 мс)", text)
        self.assertIn("qwen2.5:7b", text)
        self.assertIn("Whisper STT", text)
        self.assertIn("Очередь тяжелых задач", text)


class TestStatusReporter(unittest.IsolatedAsyncioTestCase):
    def test_format_progress_settings(self) -> None:
        values = {
            "progress_mode": "verbose",
            "progress_notify_browser": True,
            "progress_notify_voice": False,
            "progress_notify_doc": True,
            "progress_notify_llm": True,
            "progress_notify_calendar": True,
        }
        text = format_progress_settings(values)
        self.assertIn("Ход выполнения задач", text)
        self.assertIn("Подробный", text)
        self.assertIn("Ссылки и браузер: <b>✅ Включено</b>", text)
        self.assertIn("Голосовые и аудио: <b>❌ Выключено</b>", text)

    def test_reporter_modes_and_categories(self) -> None:
        bot = MagicMock()
        # Off mode
        rep_off = StatusReporter(bot, 12345, mode="off")
        self.assertFalse(rep_off.is_enabled("browser"))
        self.assertFalse(rep_off.is_enabled("llm"))

        # Compact mode with selective categories
        rep_compact = StatusReporter(
            bot, 12345, mode="compact",
            categories={"browser": True, "voice": False, "general": True}
        )
        self.assertTrue(rep_compact.is_enabled("browser"))
        self.assertFalse(rep_compact.is_enabled("voice"))

    async def test_reporter_step_and_render_verbose(self) -> None:
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 999
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()

        rep = StatusReporter(bot, 12345, mode="verbose")
        await rep.step("browser", "🌐 Открываю ссылку: example.com...")
        self.assertEqual(rep.message_id, 999)
        self.assertIn("Открываю ссылку", rep._render_text())

        await rep.step_done("🌐 Страница загружена")
        self.assertIn("Страница загружена", rep._render_text())

        await rep.step("llm", "🧠 Анализирую через нейросеть...")
        rendered = rep._render_text()
        self.assertIn("Страница загружена", rendered)
        self.assertIn("Анализирую через нейросеть", rendered)

        await rep.finish(delete=True)
        bot.delete_message.assert_awaited_once_with(chat_id=12345, message_id=999)

    async def test_reporter_step_compact(self) -> None:
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 888
        bot.send_message = AsyncMock(return_value=sent_msg)

        rep = StatusReporter(bot, 12345, mode="compact")
        await rep.step("doc", "📄 Извлекаю текст из doc.pdf...")
        self.assertEqual(rep._render_text(), "⏳ <i>📄 Извлекаю текст из doc.pdf...</i>")


if __name__ == "__main__":
    unittest.main()
