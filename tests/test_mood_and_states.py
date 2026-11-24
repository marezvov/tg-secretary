"""Tests for user mood and state tracking, formatters, and schedule deduplication."""
import unittest
from datetime import datetime
from app.utils import (
    mood_icon,
    mood_name,
    energy_num_label,
    normalize_weekday,
)
from app.ui_formatter import (
    format_mood_card,
    format_mood_menu,
    format_mood_history,
)


class TestMoodAndStates(unittest.TestCase):
    def test_utils_helpers(self):
        self.assertEqual(mood_icon("great"), "🔥")
        self.assertEqual(mood_icon("low"), "😔")
        self.assertEqual(mood_name("stressed"), "Стресс")
        self.assertIn("🪫", energy_num_label(1))
        self.assertIn("⚡️", energy_num_label(3))

    def test_normalize_weekday(self):
        # 0..6 numeric
        self.assertEqual(normalize_weekday(0), 0)
        self.assertEqual(normalize_weekday(1), 1)
        self.assertEqual(normalize_weekday(6), 6)
        self.assertEqual(normalize_weekday(7), 6)  # Sunday
        self.assertIsNone(normalize_weekday(8))

        # string digits
        self.assertEqual(normalize_weekday("0"), 0)
        self.assertEqual(normalize_weekday("1"), 1)
        self.assertEqual(normalize_weekday("6"), 6)
        self.assertEqual(normalize_weekday("7"), 6)

        # aliases
        self.assertEqual(normalize_weekday("понедельник"), 0)
        self.assertEqual(normalize_weekday("пн"), 0)
        self.assertEqual(normalize_weekday("вторник"), 1)
        self.assertEqual(normalize_weekday("суббота"), 5)
        self.assertEqual(normalize_weekday("вс"), 6)

    def test_format_mood_card(self):
        mock_state = {
            "created_at": "2026-11-24 21:00",
            "mood": "great",
            "energy": 3,
            "stress": 0,
            "note": "Сдал проект на отлично!",
        }
        card = format_mood_card(mock_state)
        self.assertIn("🔥", card)
        self.assertIn("Отличное", card)
        self.assertIn("⚡️", card)
        self.assertIn("Сдал проект на отлично!", card)

    def test_format_mood_menu(self):
        mock_latest = {
            "created_at": "2026-11-24 21:00",
            "mood": "good",
            "energy": 2,
            "stress": 0,
            "note": "Спокойный вечер",
        }
        mock_stats = {
            "count": 5,
            "days": 7,
            "avg_energy": 2.4,
            "dominant_mood": "good",
            "mood_counts": {"good": 3, "great": 2},
        }
        text = format_mood_menu(mock_latest, mock_stats)
        self.assertIn("😊", text)
        self.assertIn("Хорошее", text)
        self.assertIn("2.4 / 3.0", text)

    def test_format_mood_history(self):
        states = [
            {"created_at": "2026-11-24 21:00", "mood": "good", "energy": 2, "note": "Ок"},
            {"created_at": "2026-11-23 20:00", "mood": "low", "energy": 1, "note": "Устал"},
        ]
        stats = {
            "count": 2,
            "avg_energy": 1.5,
            "dominant_mood": "good",
        }
        text = format_mood_history(states, stats, days=7)
        self.assertIn("История состояния за 7 дней", text)
        self.assertIn("Всего чек-инов: <b>2</b>", text)
        self.assertIn("1.5 / 3.0", text)
        self.assertIn("Устал", text)


if __name__ == "__main__":
    unittest.main()
