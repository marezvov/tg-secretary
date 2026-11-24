"""Tests for app.ui_formatter."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ui_formatter import (
    format_archive_menu,
    format_stats,
    format_task_compact,
    format_task_detail,
)


class TestUIFormatter(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Moscow")
        self.now = datetime(2026, 11, 23, 14, 0, tzinfo=self.tz)

    def test_format_task_compact_active(self):
        task = {
            "title": "Купить билеты",
            "category": "personal",
            "energy": "low",
            "est_minutes": 15,
            "status": "todo",
            "scheduled_date": "2026-11-23",
            "scheduled_time": "15:00",
            "deadline_at": None,
            "completed_at": None,
        }
        text = format_task_compact(task, self.now)
        self.assertIn("Купить билеты", text)
        self.assertIn("15 мин", text)
        self.assertIn("15:00", text)

    def test_format_task_compact_done(self):
        task = {
            "title": "Сдать эссе",
            "category": "study",
            "energy": "medium",
            "est_minutes": 60,
            "status": "done",
            "scheduled_date": None,
            "scheduled_time": None,
            "deadline_at": None,
            "completed_at": "2026-11-23 12:00",
        }
        text = format_task_compact(task, self.now)
        self.assertIn("Сдать эссе", text)
        self.assertIn("Закрыта: 2026-11-23 12:00", text)

    def test_format_task_detail(self):
        task = {
            "title": "Подготовка к олимпиаде",
            "category": "olympiad",
            "energy": "high",
            "est_minutes": 120,
            "status": "done",
            "created_at": "2026-11-20 10:00",
            "completed_at": "2026-11-23 11:30",
            "scheduled_date": "2026-11-23",
            "scheduled_time": "10:00",
            "deadline_at": "2026-11-25 23:59",
            "first_step": "Решить вариант 2024 года",
            "source_url": "https://olympiad.ru",
        }
        text = format_task_detail(task, self.now)
        self.assertIn("Подготовка к олимпиаде", text)
        self.assertIn("Закрыта:", text)
        self.assertIn("Решить вариант 2024 года", text)
        self.assertIn("https://olympiad.ru", text)

    def test_format_stats(self):
        stats = {
            "tasks_todo": 3,
            "tasks_done": 12,
            "tasks_frozen": 1,
            "tasks_done_today": 2,
            "tasks_done_week": 8,
            "tasks_done_month": 12,
            "events_upcoming": 2,
            "reminders_pending": 1,
            "notes": 5,
            "watched_active": 1,
            "inbox_total": 20,
            "inbox_processed": 19,
            "inbox_failed": 1,
            "tasks_by_category": {"study": 2, "personal": 1},
            "tasks_done_by_category": {"study": 10, "project": 2},
        }
        text = format_stats(stats)
        self.assertIn("Активных задач: <b>3</b>", text)
        self.assertIn("Выполнено всего: <b>12</b>", text)
        self.assertIn("Сегодня: <b>2</b>", text)
        self.assertIn("За 7 дней: <b>8</b>", text)
        self.assertIn("учёба: 2", text)
        self.assertIn("Закрытые задачи по категориям", text)

    def test_format_archive_menu(self):
        stats = {
            "tasks_done": 15,
            "tasks_frozen": 2,
            "events_past": 4,
            "notes": 7,
            "inbox_total": 30,
        }
        text = format_archive_menu(stats)
        self.assertIn("Выполненных задач: <b>15</b>", text)
        self.assertIn("Замороженных: <b>2</b>", text)
        self.assertIn("Прошедших событий: <b>4</b>", text)


if __name__ == "__main__":
    unittest.main()
