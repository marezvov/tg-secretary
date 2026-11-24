"""Tests for app.utils."""
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.utils import (
    clip,
    combine,
    esc,
    fmt_dt,
    fts_query,
    human_delta,
    human_duration,
    merge_intervals,
    minutes_between,
    normalize_category,
    normalize_energy,
    normalize_reg_status,
    normalize_weekday,
    parse_date,
    parse_datetime,
    parse_hhmm,
    sha256_text,
    subtract_intervals,
)


class TestUtils(unittest.TestCase):
    def test_esc(self):
        self.assertEqual(esc("Hello <world> & 'quotes'"), "Hello &lt;world&gt; &amp; 'quotes'")
        self.assertEqual(esc(None), "")
        self.assertEqual(esc(123), "123")

    def test_clip(self):
        self.assertEqual(clip("short text", 20), "short text")
        self.assertEqual(clip("very long long text that exceeds limit", 15), "very long long…")

    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("09:30"), time(9, 30))
        self.assertEqual(parse_hhmm("9:05"), time(9, 5))
        self.assertEqual(parse_hhmm("23.45"), time(23, 45))
        self.assertIsNone(parse_hhmm("25:00"))
        self.assertIsNone(parse_hhmm("invalid"))
        self.assertIsNone(parse_hhmm(None))

    def test_parse_date(self):
        today = date.today()
        self.assertEqual(parse_date("сегодня"), today)
        self.assertEqual(parse_date("завтра"), today + timedelta(days=1))
        self.assertEqual(parse_date("послезавтра"), today + timedelta(days=2))
        self.assertEqual(parse_date("2026-11-25"), date(2026, 11, 25))
        self.assertIsNone(parse_date(None))

    def test_parse_datetime(self):
        tz = ZoneInfo("Europe/Moscow")
        dt = parse_datetime("2026-11-25 14:30", tz=tz)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.day, 25)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 30)
        self.assertEqual(dt.tzinfo, tz)

    def test_merge_intervals(self):
        d = date(2026, 11, 25)
        i1 = (combine(d, time(9, 0)), combine(d, time(10, 30)))
        i2 = (combine(d, time(10, 0)), combine(d, time(11, 0)))
        i3 = (combine(d, time(13, 0)), combine(d, time(14, 0)))

        merged = merge_intervals([i1, i2, i3])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], (combine(d, time(9, 0)), combine(d, time(11, 0))))
        self.assertEqual(merged[1], (combine(d, time(13, 0)), combine(d, time(14, 0))))

    def test_subtract_intervals(self):
        d = date(2026, 11, 25)
        day_window = [(combine(d, time(8, 0)), combine(d, time(20, 0)))]
        busy = [
            (combine(d, time(10, 0)), combine(d, time(12, 0))),
            (combine(d, time(15, 0)), combine(d, time(16, 30))),
        ]
        free = subtract_intervals(day_window, busy)
        self.assertEqual(len(free), 3)
        self.assertEqual(free[0], (combine(d, time(8, 0)), combine(d, time(10, 0))))
        self.assertEqual(free[1], (combine(d, time(12, 0)), combine(d, time(15, 0))))
        self.assertEqual(free[2], (combine(d, time(16, 30)), combine(d, time(20, 0))))

    def test_minutes_between(self):
        d = date(2026, 11, 25)
        t1 = combine(d, time(10, 0))
        t2 = combine(d, time(11, 45))
        self.assertEqual(minutes_between(t1, t2), 105)

    def test_human_duration(self):
        self.assertEqual(human_duration(25), "25 мин")
        self.assertEqual(human_duration(60), "1 ч")
        self.assertEqual(human_duration(95), "1 ч 35 мин")

    def test_human_delta(self):
        now = datetime(2026, 11, 25, 12, 0)
        future = datetime(2026, 11, 25, 12, 45)
        past = datetime(2026, 11, 25, 11, 30)
        self.assertEqual(human_delta(future, now), "через 45 мин")
        self.assertIn("просрочено на 30 мин", human_delta(past, now))

    def test_normalizers(self):
        self.assertEqual(normalize_weekday("пн"), 0)
        self.assertEqual(normalize_weekday("пятница"), 4)
        self.assertEqual(normalize_weekday("7"), 6)
        self.assertEqual(normalize_category("олимпиада"), "olympiad")
        self.assertEqual(normalize_category("учёба"), "study")
        self.assertEqual(normalize_category("спорт"), "sport")
        self.assertEqual(normalize_energy("высокая"), "high")
        self.assertEqual(normalize_energy("🔴"), "high")
        self.assertEqual(normalize_energy("низкая"), "low")
        self.assertEqual(normalize_reg_status("готово"), "done")
        self.assertEqual(normalize_reg_status("в процессе"), "pending")

    def test_fts_query(self):
        self.assertEqual(fts_query("физика олимпиада"), '"физика"* "олимпиада"*')
        self.assertIsNone(fts_query(""))

    def test_sha256_text(self):
        h = sha256_text("secret text")
        self.assertEqual(len(h), 64)


if __name__ == "__main__":
    unittest.main()
