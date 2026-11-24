"""Tests for end-to-end features and closed task workflow."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from app.config import settings
from app.database import Database
from app.local_calendar import auto_schedule_tasks, day_overview, get_free_windows
from app.ui_formatter import format_task_compact, format_task_detail
from app.utils import to_db, to_db_date


class TestFeatures(unittest.IsolatedAsyncioTestCase):
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

    async def test_closed_task_workflow(self):
        # 1. Create a task
        task_id = await self.db.create_task(
            title="Курсовая работа",
            category="study",
            energy="high",
            est_minutes=90,
            first_step="Составить план",
        )
        task = await self.db.get_task(task_id)
        self.assertEqual(task["status"], "todo")
        self.assertIsNone(task["completed_at"])

        # 2. Complete the task
        await self.db.complete_task(task_id)
        task_done = await self.db.get_task(task_id)
        self.assertEqual(task_done["status"], "done")
        self.assertIsNotNone(task_done["completed_at"])

        # 3. Check compact formatting has completed_at
        compact = format_task_compact(task_done, datetime.now())
        self.assertIn("Закрыта:", compact)

        # 4. Filter by done
        done_list = await self.db.list_tasks(filter_name="done")
        self.assertEqual(len(done_list), 1)
        self.assertEqual(done_list[0]["id"], task_id)

        # 5. Clone/Repeat task (the "t:clone:" workflow)
        new_id = await self.db.create_task(
            title=task_done["title"],
            category=task_done["category"],
            energy=task_done["energy"],
            est_minutes=task_done["est_minutes"],
            first_step=task_done["first_step"],
            status="todo",
        )
        new_task = await self.db.get_task(new_id)
        self.assertEqual(new_task["status"], "todo")
        self.assertEqual(new_task["title"], "Курсовая работа")
        self.assertIsNone(new_task["completed_at"])

        # 6. Verify stats reflect both
        st = await self.db.stats()
        self.assertEqual(st["tasks_todo"], 1)
        self.assertEqual(st["tasks_done"], 1)

    async def test_search_closed_and_active_tasks(self):
        t1 = await self.db.create_task(title="Физика ДЗ 1", category="study")
        t2 = await self.db.create_task(title="Физика ДЗ 2", category="study")
        await self.db.complete_task(t1)

        # Search active
        active_fiz = await self.db.list_tasks(filter_name="active", search="Физика")
        self.assertEqual(len(active_fiz), 1)
        self.assertEqual(active_fiz[0]["id"], t2)

        # Search done
        done_fiz = await self.db.list_tasks(filter_name="done", search="Физика")
        self.assertEqual(len(done_fiz), 1)
        self.assertEqual(done_fiz[0]["id"], t1)

        # Search all
        all_fiz = await self.db.list_tasks(filter_name="all", search="Физика")
        self.assertEqual(len(all_fiz), 2)


if __name__ == "__main__":
    unittest.main()
