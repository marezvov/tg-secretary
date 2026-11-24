"""Tests for app.database."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from app.database import Database


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db = Database(self.tmp.name)
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.tmp.name):
            try:
                os.unlink(self.tmp.name)
            except OSError:
                pass

    async def test_task_crud_and_status(self):
        task_id = await self.db.create_task(
            title="Подготовить отчёт",
            category="study",
            energy="high",
            est_minutes=45,
            first_step="Открыть файл",
        )
        self.assertGreater(task_id, 0)

        task = await self.db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Подготовить отчёт")
        self.assertEqual(task["status"], "todo")
        self.assertIsNone(task["completed_at"])

        # Complete task
        await self.db.complete_task(task_id)
        task_done = await self.db.get_task(task_id)
        self.assertEqual(task_done["status"], "done")
        self.assertIsNotNone(task_done["completed_at"])

        # Restore task
        await self.db.set_task_status(task_id, "todo")
        task_restored = await self.db.get_task(task_id)
        self.assertEqual(task_restored["status"], "todo")
        self.assertIsNone(task_restored["completed_at"])

        # Freeze task
        await self.db.set_task_status(task_id, "frozen")
        task_frozen = await self.db.get_task(task_id)
        self.assertEqual(task_frozen["status"], "frozen")

        # Delete task
        await self.db.delete_task(task_id)
        self.assertIsNone(await self.db.get_task(task_id))

    async def test_closed_tasks_listing_and_filtering(self):
        # Create active, done, and frozen tasks
        t1 = await self.db.create_task(title="Активная 1", status="todo")
        t2 = await self.db.create_task(title="Выполненная математика", status="todo")
        t3 = await self.db.create_task(title="Замороженная физика", status="frozen")
        t4 = await self.db.create_task(title="Выполненная физика", status="todo")

        await self.db.complete_task(t2)
        await self.db.complete_task(t4)

        # Filter: active
        active = await self.db.list_tasks(filter_name="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], t1)

        # Filter: done
        done = await self.db.list_tasks(filter_name="done")
        self.assertEqual(len(done), 2)
        self.assertEqual({d["id"] for d in done}, {t2, t4})

        # Filter: frozen
        frozen = await self.db.list_tasks(filter_name="frozen")
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["id"], t3)

        # Filter: closed (both done and frozen)
        closed = await self.db.list_tasks(filter_name="closed")
        self.assertEqual(len(closed), 3)
        self.assertEqual({c["id"] for c in closed}, {t2, t3, t4})

        # Count tasks with search
        count_fiz = await self.db.count_tasks(filter_name="closed", search="физика")
        self.assertEqual(count_fiz, 2)

        # Search closed tasks
        search_fiz = await self.db.list_tasks(filter_name="closed", search="физика")
        self.assertEqual(len(search_fiz), 2)
        self.assertEqual({s["id"] for s in search_fiz}, {t3, t4})

    async def test_notes_fts5_and_triggers(self):
        n1 = await self.db.create_note("Квантовая физика", "Лекция о фотонах и интерференции", tags="физика")
        n2 = await self.db.create_note("Список покупок", "Молоко, чай, хлеб", tags="дом")

        results = await self.db.search_notes("фотонах")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], n1)

        results_tag = await self.db.search_notes("физика")
        self.assertEqual(len(results_tag), 1)

        # Update note
        await self.db.update_note(n2, content="Молоко, кофе, печенье")
        res_coffee = await self.db.search_notes("кофе")
        self.assertEqual(len(res_coffee), 1)
        self.assertEqual(res_coffee[0]["id"], n2)

        # Delete note
        await self.db.delete_note(n1)
        res_deleted = await self.db.search_notes("фотонах")
        self.assertEqual(len(res_deleted), 0)

    async def test_stats_method(self):
        t1 = await self.db.create_task(title="T1", category="study", status="todo")
        t2 = await self.db.create_task(title="T2", category="study", status="todo")
        t3 = await self.db.create_task(title="T3", category="project", status="frozen")
        await self.db.complete_task(t2)

        st = await self.db.stats()
        self.assertEqual(st["tasks_todo"], 1)
        self.assertEqual(st["tasks_done"], 1)
        self.assertEqual(st["tasks_frozen"], 1)
        self.assertEqual(st["tasks_done_today"], 1)
        self.assertEqual(st["tasks_done_week"], 1)
        self.assertIn("study", st["tasks_done_by_category"])


if __name__ == "__main__":
    unittest.main()
