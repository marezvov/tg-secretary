"""Tests for app.local_calendar."""
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

from app.config import settings
from app.database import Database
from app.local_calendar import (
    auto_schedule_tasks,
    day_overview,
    get_free_windows,
    next_wake_datetime,
    pick_task_for_energy,
)
from app.utils import to_db, to_db_date


class TestCalendar(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db = Database(self.tmp.name)
        await self.db.connect()
        await self.db.init_schema()
        settings.bind(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.tmp.name):
            try:
                os.unlink(self.tmp.name)
            except OSError:
                pass

    async def test_get_free_windows_and_pause(self):
        # Monday: weekday 0
        target = date(2026, 11, 23)  # Monday
        # Add recurring lesson: 10:00 - 12:00
        await self.db.create_recurring(
            weekday=0,
            start_time="10:00",
            end_time="12:00",
            title="Математика",
            category="study",
            buffer_before_min=0,
            buffer_after_min=0,
        )

        windows = await get_free_windows(self.db, target)
        # Default balanced Monday bounds are 08:00 - 22:30
        # Free windows should be 08:00 - 10:00 and 12:00 - 22:30
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0][0].strftime("%H:%M"), "08:00")
        self.assertEqual(windows[0][1].strftime("%H:%M"), "10:00")
        self.assertEqual(windows[1][0].strftime("%H:%M"), "12:00")
        self.assertEqual(windows[1][1].strftime("%H:%M"), "22:30")

        # Now pause recurring schedule until 2026-11-25
        await settings.set("pause_recurring_until", "2026-11-25")
        windows_paused = await get_free_windows(self.db, target)
        # With pause active, the recurring slot is ignored: one single big window 08:00 - 22:30
        self.assertEqual(len(windows_paused), 1)
        self.assertEqual(windows_paused[0][0].strftime("%H:%M"), "08:00")
        self.assertEqual(windows_paused[0][1].strftime("%H:%M"), "22:30")

    async def test_auto_schedule_tasks(self):
        target = date(2026, 11, 23)
        t1 = await self.db.create_task(
            title="Сделать домашку",
            category="study",
            energy="medium",
            est_minutes=60,
        )
        t2 = await self.db.create_task(
            title="Прочитать статью",
            category="study",
            energy="low",
            est_minutes=20,
        )

        count = await auto_schedule_tasks(self.db, days=3)
        self.assertGreaterEqual(count, 1)

        task1 = await self.db.get_task(t1)
        self.assertIsNotNone(task1["scheduled_date"])
        self.assertIsNotNone(task1["scheduled_time"])

    async def test_pick_task_for_energy(self):
        await self.db.create_task(title="Сложный проект", energy="high", est_minutes=90)
        await self.db.create_task(title="Лёгкая задача", energy="low", est_minutes=15)

        chosen_green = await pick_task_for_energy(self.db, "green")
        self.assertEqual(chosen_green["title"], "Сложный проект")

        chosen_red = await pick_task_for_energy(self.db, "red")
        self.assertEqual(chosen_red["title"], "Лёгкая задача")

    async def test_next_wake_datetime(self):
        now = datetime(2026, 11, 23, 10, 0)
        wake = await next_wake_datetime(self.db, now)
        self.assertIsNotNone(wake)
        self.assertGreater(wake, now)


if __name__ == "__main__":
    unittest.main()
