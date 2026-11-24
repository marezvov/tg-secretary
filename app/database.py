"""SQLite persistence layer (WAL, FTS5, busy timeout, all CRUD helpers)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from .config import DEFAULT_SETTINGS, env
from .utils import fts_query, to_db, to_db_date

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_profiles (
    weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
    mode TEXT NOT NULL DEFAULT 'balanced',
    day_start TEXT NOT NULL DEFAULT '08:00',
    day_end TEXT NOT NULL DEFAULT '22:30',
    max_task_minutes INTEGER NOT NULL DEFAULT 90,
    wake_time TEXT DEFAULT '07:00',
    sleep_time TEXT DEFAULT '23:30'
);

CREATE TABLE IF NOT EXISTS recurring_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'study',
    location TEXT,
    buffer_before_min INTEGER NOT NULL DEFAULT 0,
    buffer_after_min INTEGER NOT NULL DEFAULT 15,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'olympiad',
    start_dt TEXT NOT NULL,
    end_dt TEXT,
    location_or_url TEXT,
    reg_deadline TEXT,
    reg_status TEXT NOT NULL DEFAULT 'not_needed',
    prep_notes TEXT,
    source_url TEXT,
    notified_flags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'study',
    energy TEXT NOT NULL DEFAULT 'medium',
    est_minutes INTEGER NOT NULL DEFAULT 30,
    deadline_at TEXT,
    scheduled_date TEXT,
    scheduled_time TEXT,
    first_step TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'todo',
    notified_flags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watched_urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    goal TEXT NOT NULL,
    last_hash TEXT,
    last_summary TEXT,
    interval_hours INTEGER NOT NULL DEFAULT 2,
    last_checked TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    cron_expr TEXT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    is_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    source_url TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, content, tags, content='notes', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS inbox_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_type TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_schedule ON tasks(scheduled_date, scheduled_time);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline_at);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_dt);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(is_sent, remind_at);
CREATE INDEX IF NOT EXISTS idx_watched_active ON watched_urls(is_active);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_log(status, created_at);
"""

TASK_FIELDS = {
    "title", "category", "energy", "est_minutes", "deadline_at", "scheduled_date",
    "scheduled_time", "first_step", "source_url", "status", "notified_flags", "completed_at",
}
EVENT_FIELDS = {
    "title", "category", "start_dt", "end_dt", "location_or_url", "reg_deadline",
    "reg_status", "prep_notes", "source_url", "notified_flags",
}
PROFILE_FIELDS = {"mode", "day_start", "day_end", "max_task_minutes", "wake_time", "sleep_time"}
WATCH_FIELDS = {"url", "goal", "last_hash", "last_summary", "interval_hours", "last_checked", "is_active"}


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or env.db_path)
        self._connection: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock | None = None

    @property
    def write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        self._write_lock = asyncio.Lock()
        for pragma in (
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA foreign_keys = ON",
            "PRAGMA busy_timeout = 5000",
        ):
            await self._connection.execute(pragma)
        await self._connection.commit()
        log.info("Connected to SQLite database: %s", self.path)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self._write_lock = None

    async def init_schema(self) -> None:
        conn = self.conn
        await conn.executescript(SCHEMA)
        await conn.executescript(INDEXES)
        columns = {row["name"] for row in await self.fetchall("PRAGMA table_info(tasks)")}
        if "completed_at" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        await conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        await conn.commit()
        await self.seed_defaults()

    async def seed_defaults(self) -> None:
        now = datetime.now()
        for key, value in DEFAULT_SETTINGS.items():
            if key == "owner_id":
                continue
            await self.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        if env.admin_id is not None:
            row = await self.fetchone("SELECT value FROM settings WHERE key='owner_id'")
            current = None
            if row is not None:
                try:
                    current = json.loads(row["value"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    current = None
            if current is None:
                await self.execute(
                    "INSERT INTO settings(key, value) VALUES('owner_id', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(env.admin_id),),
                )
        defaults = {
            0: ("balanced", "08:00", "22:30", 90, "07:00", "23:30"),
            1: ("balanced", "08:00", "22:30", 90, "07:00", "23:30"),
            2: ("balanced", "08:00", "22:30", 90, "07:00", "23:30"),
            3: ("balanced", "08:00", "22:30", 90, "07:00", "23:30"),
            4: ("balanced", "08:00", "22:30", 90, "07:00", "23:30"),
            5: ("free", "09:00", "23:00", 150, "08:30", "23:59"),
            6: ("free", "09:00", "23:00", 150, "08:30", "23:59"),
        }
        for weekday, values in defaults.items():
            await self.execute(
                """
                INSERT OR IGNORE INTO day_profiles
                    (weekday, mode, day_start, day_end, max_task_minutes, wake_time, sleep_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (weekday, *values),
            )
        log.info("Database schema/defaults ready (%s)", to_db(now))

    # ------------------------------------------------------------------ low level
    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.write_lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            last_id = cursor.lastrowid or 0
            await cursor.close()
            return last_id

    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        async with self.write_lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.write_lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            count = cursor.rowcount or 0
            await cursor.close()
            return count

    async def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return None
        return row[0]

    async def get_setting_raw(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    # ------------------------------------------------------------------ tasks
    async def create_task(
        self,
        title: str,
        category: str = "study",
        energy: str = "medium",
        est_minutes: int = 30,
        deadline_at: str | None = None,
        scheduled_date: str | None = None,
        scheduled_time: str | None = None,
        first_step: str | None = None,
        source_url: str | None = None,
        status: str = "todo",
        completed_at: str | None = None,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO tasks
                (title, category, energy, est_minutes, deadline_at, scheduled_date,
                 scheduled_time, first_step, source_url, status, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title, category, energy, max(5, int(est_minutes)), deadline_at,
                scheduled_date, scheduled_time, first_step, source_url, status,
                to_db(datetime.now()), completed_at,
            ),
        )

    async def get_task(self, task_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))

    async def update_task(self, task_id: int, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if key in TASK_FIELDS}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        await self.execute(
            f"UPDATE tasks SET {assignments} WHERE id=?",
            (*clean.values(), task_id),
        )

    async def complete_task(self, task_id: int) -> None:
        await self.update_task(
            task_id, status="done", completed_at=to_db(datetime.now())
        )

    async def set_task_status(self, task_id: int, status: str) -> None:
        if status == "done":
            await self.complete_task(task_id)
        else:
            await self.update_task(task_id, status=status, completed_at=None)

    async def delete_task(self, task_id: int) -> None:
        await self.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    @staticmethod
    def _task_filter(
        filter_name: str,
        now_db: str,
        today_db: str,
        week_end_db: str,
    ) -> tuple[str, list[Any]]:
        filter_name = (filter_name or "active").lower()
        if filter_name == "active":
            return "status='todo'", []
        if filter_name == "done":
            return "status='done'", []
        if filter_name == "frozen":
            return "status='frozen'", []
        if filter_name == "all":
            return "1=1", []
        if filter_name == "today":
            return "status='todo' AND scheduled_date=?", [today_db]
        if filter_name == "week":
            return "status='todo' AND scheduled_date BETWEEN ? AND ?", [today_db, week_end_db]
        if filter_name == "overdue":
            return "status='todo' AND deadline_at IS NOT NULL AND deadline_at < ?", [now_db]
        if filter_name == "unscheduled":
            return "status='todo' AND scheduled_date IS NULL", []
        if filter_name == "closed":
            return "status IN ('done','frozen')", []
        return "status='todo'", []

    async def list_tasks(
        self,
        filter_name: str = "active",
        limit: int = 50,
        offset: int = 0,
        now_db: str | None = None,
        today_db: str | None = None,
        week_end_db: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> list[aiosqlite.Row]:
        now_db = now_db or to_db(datetime.now())
        today_db = today_db or date.today().isoformat()
        week_end_db = week_end_db or date.today().isoformat()
        where, params = self._task_filter(filter_name, now_db, today_db, week_end_db)
        if category:
            where += " AND category=?"
            params.append(category)
        if search:
            where += " AND (title LIKE ? OR first_step LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        order = (
            "COALESCE(completed_at, created_at) DESC, id DESC"
            if filter_name in {"done", "closed"}
            else "CASE WHEN status='todo' THEN 0 WHEN status='frozen' THEN 1 ELSE 2 END, "
                 "(deadline_at IS NULL), deadline_at, "
                 "(scheduled_date IS NULL), scheduled_date, COALESCE(scheduled_time,'99:99'), id DESC"
        )
        return await self.fetchall(
            f"SELECT * FROM tasks WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    async def count_tasks(
        self,
        filter_name: str = "active",
        now_db: str | None = None,
        today_db: str | None = None,
        week_end_db: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> int:
        now_db = now_db or to_db(datetime.now())
        today_db = today_db or date.today().isoformat()
        week_end_db = week_end_db or date.today().isoformat()
        where, params = self._task_filter(filter_name, now_db, today_db, week_end_db)
        if category:
            where += " AND category=?"
            params.append(category)
        if search:
            where += " AND (title LIKE ? OR first_step LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        value = await self.scalar(f"SELECT COUNT(*) FROM tasks WHERE {where}", params)
        return int(value or 0)

    async def tasks_on_date(self, day: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM tasks WHERE scheduled_date=? ORDER BY scheduled_time, id",
            (to_db_date(day),),
        )

    async def unscheduled_todo_tasks(self, limit: int = 200) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM tasks WHERE status='todo' AND "
            "(scheduled_date IS NULL OR scheduled_date < ?) "
            "ORDER BY (deadline_at IS NULL), deadline_at, id",
            (to_db_date(date.today()),),
        )

    async def task_counts_by_status(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    async def delete_old_done_tasks(self, before_date: str) -> int:
        return await self.execute_rowcount(
            "DELETE FROM tasks WHERE status='done' AND COALESCE(completed_at, created_at) < ?",
            (before_date,),
        )

    # ------------------------------------------------------------------ events
    async def create_event(
        self,
        title: str,
        category: str = "olympiad",
        start_dt: str = "",
        end_dt: str | None = None,
        location_or_url: str | None = None,
        reg_deadline: str | None = None,
        reg_status: str = "not_needed",
        prep_notes: str | None = None,
        source_url: str | None = None,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO events
                (title, category, start_dt, end_dt, location_or_url, reg_deadline,
                 reg_status, prep_notes, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title, category, start_dt, end_dt, location_or_url, reg_deadline,
                reg_status, prep_notes, source_url, to_db(datetime.now()),
            ),
        )

    async def get_event(self, event_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM events WHERE id=?", (event_id,))

    async def update_event(self, event_id: int, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if key in EVENT_FIELDS}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        await self.execute(
            f"UPDATE events SET {assignments} WHERE id=?", (*clean.values(), event_id)
        )

    async def delete_event(self, event_id: int) -> None:
        await self.execute("DELETE FROM events WHERE id=?", (event_id,))

    async def list_events(
        self,
        scope: str = "upcoming",
        limit: int = 20,
        offset: int = 0,
        now_db: str | None = None,
    ) -> list[aiosqlite.Row]:
        now_db = now_db or to_db(datetime.now())
        if scope == "past":
            where, params, order = "COALESCE(end_dt, start_dt) < ?", [now_db], "start_dt DESC"
        elif scope == "all":
            where, params, order = "1=1", [], "start_dt DESC"
        else:
            where, params, order = "COALESCE(end_dt, start_dt) >= ?", [now_db], "start_dt ASC"
        return await self.fetchall(
            f"SELECT * FROM events WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    async def count_events(self, scope: str = "upcoming", now_db: str | None = None) -> int:
        now_db = now_db or to_db(datetime.now())
        if scope == "past":
            where, params = "COALESCE(end_dt, start_dt) < ?", [now_db]
        elif scope == "all":
            where, params = "1=1", []
        else:
            where, params = "COALESCE(end_dt, start_dt) >= ?", [now_db]
        value = await self.scalar(f"SELECT COUNT(*) FROM events WHERE {where}", params)
        return int(value or 0)

    async def events_between(self, start: date, end: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM events WHERE date(start_dt) <= date(?) "
            "AND date(COALESCE(end_dt, start_dt)) >= date(?) ORDER BY start_dt",
            (to_db_date(end), to_db_date(start)),
        )

    async def events_on_date(self, day: date) -> list[aiosqlite.Row]:
        return await self.events_between(day, day)

    # ------------------------------------------------------------------ recurring
    async def create_recurring(
        self,
        weekday: int,
        start_time: str,
        end_time: str,
        title: str,
        category: str = "study",
        location: str | None = None,
        buffer_before_min: int = 0,
        buffer_after_min: int = 15,
        is_active: int = 1,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO recurring_schedule
                (weekday, start_time, end_time, title, category, location,
                 buffer_before_min, buffer_after_min, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(weekday), start_time, end_time, title, category, location,
                int(buffer_before_min), int(buffer_after_min), int(is_active),
            ),
        )

    async def list_recurring(
        self, weekday: int | None = None, active_only: bool = False
    ) -> list[aiosqlite.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if weekday is not None:
            clauses.append("weekday=?")
            params.append(weekday)
        if active_only:
            clauses.append("is_active=1")
        where = " AND ".join(clauses) if clauses else "1=1"
        return await self.fetchall(
            f"SELECT * FROM recurring_schedule WHERE {where} ORDER BY weekday, start_time",
            params,
        )

    async def get_recurring(self, slot_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM recurring_schedule WHERE id=?", (slot_id,))

    async def delete_recurring(self, slot_id: int) -> None:
        await self.execute("DELETE FROM recurring_schedule WHERE id=?", (slot_id,))

    async def replace_recurring(self, slots: Sequence[dict[str, Any]]) -> None:
        async with self._write_lock:
            await self.conn.execute("DELETE FROM recurring_schedule")
            await self.conn.executemany(
                """
                INSERT INTO recurring_schedule
                    (weekday, start_time, end_time, title, category, location, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                [
                    (
                        int(slot.get("weekday", 0)), slot.get("start_time", "09:00"),
                        slot.get("end_time", "10:00"), slot.get("title", "Занятие"),
                        slot.get("category", "study"), slot.get("location"),
                    )
                    for slot in slots
                ],
            )
            await self.conn.commit()

    async def add_recurring_bulk(self, slots: Sequence[dict[str, Any]]) -> None:
        await self.execute_many(
            """
            INSERT INTO recurring_schedule
                (weekday, start_time, end_time, title, category, location, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    int(slot.get("weekday", 0)), slot.get("start_time", "09:00"),
                    slot.get("end_time", "10:00"), slot.get("title", "Занятие"),
                    slot.get("category", "study"), slot.get("location"),
                )
                for slot in slots
            ],
        )

    # ------------------------------------------------------------------ day profiles
    async def get_day_profile(self, weekday: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM day_profiles WHERE weekday=?", (int(weekday),))

    async def list_day_profiles(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM day_profiles ORDER BY weekday")

    async def update_day_profile(self, weekday: int, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if key in PROFILE_FIELDS}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        await self.execute(
            f"UPDATE day_profiles SET {assignments} WHERE weekday=?",
            (*clean.values(), int(weekday)),
        )

    # ------------------------------------------------------------------ reminders
    async def create_reminder(
        self,
        text: str,
        remind_at: str,
        cron_expr: str | None = None,
        task_id: int | None = None,
    ) -> int:
        return await self.execute(
            "INSERT INTO reminders(text, remind_at, cron_expr, task_id, is_sent) VALUES (?, ?, ?, ?, 0)",
            (text, remind_at, cron_expr, task_id),
        )

    async def get_reminder(self, reminder_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM reminders WHERE id=?", (reminder_id,))

    async def list_reminders(
        self, pending_only: bool = True, limit: int = 50, offset: int = 0
    ) -> list[aiosqlite.Row]:
        where = "is_sent=0" if pending_only else "1=1"
        order = "remind_at ASC" if pending_only else "is_sent ASC, remind_at DESC"
        return await self.fetchall(
            f"SELECT * FROM reminders WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def count_reminders(self, pending_only: bool = True) -> int:
        where = "is_sent=0" if pending_only else "1=1"
        value = await self.scalar(f"SELECT COUNT(*) FROM reminders WHERE {where}")
        return int(value or 0)

    async def due_reminders(self, now_db: str) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM reminders WHERE is_sent=0 AND remind_at <= ? ORDER BY remind_at",
            (now_db,),
        )

    async def mark_reminder_sent(self, reminder_id: int, sent: bool = True) -> None:
        await self.execute(
            "UPDATE reminders SET is_sent=? WHERE id=?", (1 if sent else 0, reminder_id)
        )

    async def snooze_reminder(self, reminder_id: int, remind_at: str) -> None:
        await self.execute(
            "UPDATE reminders SET remind_at=?, is_sent=0 WHERE id=?", (remind_at, reminder_id)
        )

    async def delete_reminder(self, reminder_id: int) -> None:
        await self.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))

    # ------------------------------------------------------------------ notes
    async def create_note(
        self,
        title: str,
        content: str,
        tags: str | None = None,
        source_url: str | None = None,
    ) -> int:
        return await self.execute(
            "INSERT INTO notes(title, content, tags, source_url, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, content, tags, source_url, to_db(datetime.now())),
        )

    async def get_note(self, note_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM notes WHERE id=?", (note_id,))

    async def list_notes(self, limit: int = 20, offset: int = 0) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM notes ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def count_notes(self) -> int:
        return int(await self.scalar("SELECT COUNT(*) FROM notes") or 0)

    async def search_notes(self, query: str, limit: int = 20) -> list[aiosqlite.Row]:
        match = fts_query(query)
        if not match:
            return []
        try:
            return await self.fetchall(
                """
                SELECT n.*, snippet(notes_fts, 1, '⟦', '⟧', '…', 24) AS snippet,
                       bm25(notes_fts) AS rank
                FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
                WHERE notes_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (match, limit),
            )
        except aiosqlite.OperationalError:
            pattern = f"%{query}%"
            return await self.fetchall(
                "SELECT * FROM notes WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, pattern, limit),
            )

    async def update_note(self, note_id: int, **fields: Any) -> None:
        clean = {k: v for k, v in fields.items() if k in {"title", "content", "tags", "source_url"}}
        if not clean:
            return
        assignments = ", ".join(f"{k}=?" for k in clean)
        await self.execute(
            f"UPDATE notes SET {assignments} WHERE id=?",
            (*clean.values(), note_id),
        )

    async def delete_note(self, note_id: int) -> None:
        await self.execute("DELETE FROM notes WHERE id=?", (note_id,))

    # ------------------------------------------------------------------ watch
    async def create_watched(self, url: str, goal: str, interval_hours: int = 2) -> int:
        return await self.execute(
            "INSERT INTO watched_urls(url, goal, interval_hours, is_active) VALUES (?, ?, ?, 1)",
            (url, goal, max(1, int(interval_hours))),
        )

    async def list_watched(self, active_only: bool = False) -> list[aiosqlite.Row]:
        where = "WHERE is_active=1" if active_only else ""
        return await self.fetchall(f"SELECT * FROM watched_urls {where} ORDER BY id DESC")

    async def get_watched(self, watch_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM watched_urls WHERE id=?", (watch_id,))

    async def update_watched(self, watch_id: int, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if key in WATCH_FIELDS}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        await self.execute(
            f"UPDATE watched_urls SET {assignments} WHERE id=?",
            (*clean.values(), watch_id),
        )

    async def delete_watched(self, watch_id: int) -> None:
        await self.execute("DELETE FROM watched_urls WHERE id=?", (watch_id,))

    async def set_watched_active(self, watch_id: int, active: bool) -> None:
        await self.execute(
            "UPDATE watched_urls SET is_active=? WHERE id=?", (1 if active else 0, watch_id)
        )

    # ------------------------------------------------------------------ inbox
    async def create_inbox(
        self, msg_type: str, raw_content: str, status: str = "pending"
    ) -> int:
        return await self.execute(
            "INSERT INTO inbox_log(msg_type, raw_content, status, created_at) VALUES (?, ?, ?, ?)",
            (msg_type, raw_content, status, to_db(datetime.now())),
        )

    async def update_inbox(
        self, inbox_id: int, status: str | None = None, raw_content: str | None = None
    ) -> None:
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if raw_content is not None:
            fields["raw_content"] = raw_content
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        await self.execute(
            f"UPDATE inbox_log SET {assignments} WHERE id=?", (*fields.values(), inbox_id)
        )

    async def get_inbox(self, inbox_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM inbox_log WHERE id=?", (inbox_id,))

    async def list_inbox(
        self, limit: int = 15, offset: int = 0, status: str | None = None
    ) -> list[aiosqlite.Row]:
        where, params = ("WHERE status=?", [status]) if status else ("", [])
        return await self.fetchall(
            f"SELECT * FROM inbox_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    async def count_inbox(self, status: str | None = None) -> int:
        if status:
            return int(await self.scalar("SELECT COUNT(*) FROM inbox_log WHERE status=?", (status,)) or 0)
        return int(await self.scalar("SELECT COUNT(*) FROM inbox_log") or 0)

    async def delete_inbox(self, inbox_id: int) -> None:
        await self.execute("DELETE FROM inbox_log WHERE id=?", (inbox_id,))

    # ------------------------------------------------------------------ stats
    async def stats(self) -> dict[str, Any]:
        task_counts = await self.task_counts_by_status()
        now_db = to_db(datetime.now())
        today_date = date.today().isoformat()
        week_ago_date = (date.today() - timedelta(days=7)).isoformat()
        month_ago_date = (date.today() - timedelta(days=30)).isoformat()
        done_today = await self.scalar(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND "
            "COALESCE(completed_at, created_at) >= ?",
            (today_date + " 00:00",),
        )
        done_week = await self.scalar(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND "
            "COALESCE(completed_at, created_at) >= ?",
            (week_ago_date + " 00:00",),
        )
        done_month = await self.scalar(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND "
            "COALESCE(completed_at, created_at) >= ?",
            (month_ago_date + " 00:00",),
        )
        upcoming_events = await self.count_events("upcoming", now_db)
        categories = await self.fetchall(
            "SELECT category, COUNT(*) AS n FROM tasks WHERE status='todo' GROUP BY category"
        )
        done_categories = await self.fetchall(
            "SELECT category, COUNT(*) AS n FROM tasks WHERE status='done' GROUP BY category"
        )
        return {
            "tasks_todo": task_counts.get("todo", 0),
            "tasks_done": task_counts.get("done", 0),
            "tasks_frozen": task_counts.get("frozen", 0),
            "tasks_done_today": int(done_today or 0),
            "tasks_done_week": int(done_week or 0),
            "tasks_done_month": int(done_month or 0),
            "events_upcoming": upcoming_events,
            "reminders_pending": await self.count_reminders(True),
            "notes": await self.count_notes(),
            "watched_active": int(
                await self.scalar("SELECT COUNT(*) FROM watched_urls WHERE is_active=1") or 0
            ),
            "inbox_processed": await self.count_inbox("processed"),
            "inbox_failed": await self.count_inbox("pending_retry"),
            "inbox_total": await self.count_inbox(),
            "tasks_by_category": {row["category"]: int(row["n"]) for row in categories},
            "tasks_done_by_category": {row["category"]: int(row["n"]) for row in done_categories},
        }
