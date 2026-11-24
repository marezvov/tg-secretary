"""Tests for app.schemas."""
import unittest

from app.schemas import (
    InboxItem,
    InboxResult,
    ScheduleImport,
    ScheduleSlot,
    WatchDecision,
    WebAction,
)


class TestSchemas(unittest.TestCase):
    def test_inbox_item_coercion(self):
        raw = {
            "type": "дело",
            "заголовок": "Сдать проект",
            "энергия": "высокая",
            "est_minutes": "45 минут",
            "step": "Создать репозиторий",
        }
        item = InboxItem.model_validate(raw)
        self.assertEqual(item.type, "task")
        self.assertEqual(item.title, "Сдать проект")
        self.assertEqual(item.energy, "high")
        self.assertEqual(item.est_minutes, 45)
        self.assertEqual(item.first_step, "Создать репозиторий")

    def test_inbox_result_list_coercion(self):
        raw = [
            {"type": "task", "title": "Задача 1"},
            {"type": "event", "title": "Олимпиада", "start_dt": "2026-11-26 10:00"},
        ]
        result = InboxResult.model_validate(raw)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].title, "Задача 1")
        self.assertEqual(result.items[1].type, "event")

    def test_schedule_import(self):
        raw = {
            "schedule": [
                {
                    "day": "понедельник",
                    "start": "09:00",
                    "end": "10:30",
                    "subject": "Математический анализ",
                    "place": "Ауд. 402",
                }
            ]
        }
        imported = ScheduleImport.model_validate(raw)
        self.assertEqual(len(imported.slots), 1)
        slot = imported.slots[0]
        self.assertEqual(slot.weekday, 0)
        self.assertEqual(slot.start_time, "09:00")
        self.assertEqual(slot.end_time, "10:30")
        self.assertEqual(slot.title, "Математический анализ")
        self.assertEqual(slot.location, "Ауд. 402")

    def test_web_action(self):
        raw = {
            "thought": "Нажать на кнопку входа",
            "action": "нажать",
            "id": "14",
        }
        action = WebAction.model_validate(raw)
        self.assertEqual(action.action, "click")
        self.assertEqual(action.id, 14)

    def test_watch_decision(self):
        raw = {"relevant": "да", "summary": "Появились новые даты олимпиады"}
        decision = WatchDecision.model_validate(raw)
        self.assertTrue(decision.relevant)
        self.assertEqual(decision.summary, "Появились новые даты олимпиады")


if __name__ == "__main__":
    unittest.main()
