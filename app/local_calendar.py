"""Local calendar engine: merge schedule/events/tasks, find windows, auto-plan."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import aiosqlite

from .config import settings
from .utils import (
    combine,
    date_range,
    merge_intervals,
    minutes_between,
    parse_date,
    parse_datetime,
    parse_hhmm,
    subtract_intervals,
    to_db,
    to_db_date,
)

log = logging.getLogger(__name__)

DEFAULT_START = "08:00"
DEFAULT_END = "22:30"


def profile_bounds(profile: aiosqlite.Row | None, day: date, tz: Any) -> tuple[datetime, datetime]:
    start_time = parse_hhmm(profile["day_start"] if profile is not None else DEFAULT_START) or time(8, 0)
    end_time = parse_hhmm(profile["day_end"] if profile is not None else DEFAULT_END) or time(22, 30)
    start = combine(day, start_time).replace(tzinfo=tz)
    end = combine(day, end_time).replace(tzinfo=tz)
    if end <= start:
        end = start + timedelta(hours=12)
    return start, end


def _event_interval(event: aiosqlite.Row, day: date, tz: Any) -> tuple[datetime, datetime] | None:
    start = parse_datetime(event["start_dt"], tz)
    if start is None:
        return None
    end = parse_datetime(event["end_dt"], tz) if event["end_dt"] else None
    if end is None or end <= start:
        end = start + timedelta(minutes=60)
    if end.date() < day or start.date() > day:
        return None
    return start, end


async def day_overview(db: Any, day: date) -> dict[str, Any]:
    tz = await settings.timezone()
    profile = await db.get_day_profile(day.weekday())
    day_start, day_end = profile_bounds(profile, day, tz)
    paused = await settings.is_recurring_paused(day)
    pause_until = str(await settings.get("pause_recurring_until") or "") or None
    recurring = [] if paused else await db.list_recurring(day.weekday(), active_only=True)
    events = await db.events_on_date(day)
    tasks = await db.tasks_on_date(day)

    entries: list[dict[str, Any]] = []
    busy: list[tuple[datetime, datetime]] = []

    for slot in recurring:
        start_t = parse_hhmm(slot["start_time"])
        end_t = parse_hhmm(slot["end_time"])
        if start_t is None or end_t is None:
            continue
        start = combine(day, start_t).replace(tzinfo=tz)
        end = combine(day, end_t).replace(tzinfo=tz)
        if end <= start:
            continue
        before = int(slot["buffer_before_min"] or 0)
        after = int(slot["buffer_after_min"] or 0)
        busy.append((start - timedelta(minutes=before), end + timedelta(minutes=after)))
        entries.append(
            {
                "kind": "recurring", "row": slot, "id": slot["id"],
                "start": start, "end": end,
                "title": slot["title"], "category": slot["category"],
                "location": slot["location"], "status": "active",
            }
        )

    for event in events:
        interval = _event_interval(event, day, tz)
        if interval is None:
            continue
        start, end = interval
        busy.append((max(start, day_start), min(end, day_end)))
        entries.append(
            {
                "kind": "event", "row": event, "id": event["id"],
                "start": start, "end": end,
                "title": event["title"], "category": event["category"],
                "location": event["location_or_url"], "status": event["reg_status"],
            }
        )

    for task in tasks:
        if not task["scheduled_time"]:
            continue
        start_t = parse_hhmm(task["scheduled_time"])
        if start_t is None:
            continue
        start = combine(day, start_t).replace(tzinfo=tz)
        end = start + timedelta(minutes=int(task["est_minutes"] or 30))
        busy.append((start, end))
        entries.append(
            {
                "kind": "task", "row": task, "id": task["id"],
                "start": start, "end": end,
                "title": task["title"], "category": task["category"],
                "location": task["first_step"], "status": task["status"],
            }
        )

    entries.sort(key=lambda item: (item["start"], item["end"]))
    busy = [
        (max(start, day_start), min(end, day_end))
        for start, end in merge_intervals(busy)
        if end > day_start and start < day_end
    ]
    windows = subtract_intervals([(day_start, day_end)], busy)
    windows = [(start, end) for start, end in windows if minutes_between(start, end) >= 15]

    return {
        "date": day,
        "timezone": tz,
        "profile": profile,
        "day_start": day_start,
        "day_end": day_end,
        "paused": paused,
        "pause_until": pause_until if paused else None,
        "recurring": list(recurring),
        "events": list(events),
        "tasks": list(tasks),
        "entries": entries,
        "busy": busy,
        "free_windows": windows,
    }


async def get_free_windows(
    db: Any, day: date, min_minutes: int = 15, future_only: bool = False
) -> list[tuple[datetime, datetime]]:
    overview = await day_overview(db, day)
    windows = overview["free_windows"]
    if future_only:
        now = await settings.now()
        result = []
        for start, end in windows:
            if end <= now:
                continue
            result.append((max(start, now), end))
        windows = result
    return [(start, end) for start, end in windows if minutes_between(start, end) >= min_minutes]


async def find_free_windows(
    db: Any, day: date, min_minutes: int = 15, future_only: bool = False
) -> list[tuple[datetime, datetime]]:
    return await get_free_windows(db, day, min_minutes=min_minutes, future_only=future_only)


def _allowed_by_mode(mode: str, energy: str, est: int, window_minutes: int, max_task_minutes: int) -> bool:
    mode = (mode or "balanced").lower()
    if mode == "heavy":
        return energy == "low" and est <= min(15, max_task_minutes)
    if mode == "balanced":
        if est > max_task_minutes and max_task_minutes > 0:
            return False
        if energy == "high":
            return window_minutes >= est + 15
        return True
    # free mode still respects the profile's maximum block unless it is 0.
    return max_task_minutes <= 0 or est <= max_task_minutes


def _task_deadline(task: aiosqlite.Row, tz: Any) -> datetime | None:
    return parse_datetime(task["deadline_at"], tz) if task["deadline_at"] else None


def _task_sort_key(task: aiosqlite.Row, tz: Any) -> tuple[Any, ...]:
    deadline = _task_deadline(task, tz)
    energy_rank = {"high": 0, "medium": 1, "low": 2}.get(task["energy"], 1)
    return (
        deadline is None,
        deadline or datetime.max.replace(tzinfo=tz),
        energy_rank,
        -int(task["est_minutes"] or 30),
        task["id"],
    )


def _fit_task_into_window(
    task: aiosqlite.Row,
    target_day: date,
    window_start: datetime,
    window_end: datetime,
    mode: str,
    max_task_minutes: int,
    tz: Any,
) -> tuple[datetime, datetime] | None:
    est = max(5, int(task["est_minutes"] or 30))
    window_minutes = minutes_between(window_start, window_end)
    if est > window_minutes:
        return None
    if not _allowed_by_mode(mode, str(task["energy"]), est, window_minutes, max_task_minutes):
        return None
    deadline = _task_deadline(task, tz)
    start = window_start
    end = start + timedelta(minutes=est)
    if deadline is not None and end > deadline:
        return None
    return start, end


async def auto_schedule_tasks(db: Any, days: int = 14) -> int:
    """Distribute unscheduled todo tasks into free windows, respecting energy/mode."""
    now = await settings.now()
    tz = now.tzinfo
    today = now.date()
    tasks = await db.fetchall(
        "SELECT * FROM tasks WHERE status='todo' AND "
        "(scheduled_date IS NULL OR scheduled_date < ?) "
        "ORDER BY (deadline_at IS NULL), deadline_at, id",
        (to_db_date(today),),
    )
    if not tasks:
        return 0
    pending = sorted(tasks, key=lambda task: _task_sort_key(task, tz))
    scheduled_count = 0
    oversized: list[aiosqlite.Row] = []

    for offset, target_day in enumerate(date_range(today, max(1, days))):
        if not pending:
            break
        overview = await day_overview(db, target_day)
        profile = overview["profile"]
        mode = str(profile["mode"] if profile is not None else "balanced")
        max_task_minutes = int(profile["max_task_minutes"] if profile is not None else 90)
        windows = list(overview["free_windows"])
        if target_day == today:
            adjusted: list[tuple[datetime, datetime]] = []
            for start, end in windows:
                start = max(start, now + timedelta(minutes=5))
                if end - start >= timedelta(minutes=15):
                    adjusted.append((start, end))
            windows = adjusted
        if not windows:
            continue

        for task in list(pending):
            deadline = _task_deadline(task, tz)
            if deadline is not None and target_day > deadline.date():
                continue
            placed = False
            for index, (window_start, window_end) in enumerate(windows):
                fit = _fit_task_into_window(
                    task, target_day, window_start, window_end, mode, max_task_minutes, tz
                )
                if fit is None:
                    continue
                start, end = fit
                await db.update_task(
                    task["id"],
                    scheduled_date=to_db_date(target_day),
                    scheduled_time=start.strftime("%H:%M"),
                )
                pending.remove(task)
                scheduled_count += 1
                placed = True
                # Consume the used slice plus a small transition buffer.
                new_start = end + timedelta(minutes=5)
                if minutes_between(new_start, window_end) >= 15:
                    windows[index] = (new_start, window_end)
                else:
                    windows.pop(index)
                break
            if placed:
                continue
            est = int(task["est_minutes"] or 30)
            if est > max_task_minutes and mode != "heavy":
                oversized.append(task)

    # Long tasks that do not fit any profile block: first suitable window,
    # ignoring max_task_minutes, so they are never silently lost.
    if pending and oversized:
        for task in oversized:
            if task not in pending:
                continue
            est = int(task["est_minutes"] or 30)
            for offset in range(max(1, days)):
                target_day = today + timedelta(days=offset)
                profile = await db.get_day_profile(target_day.weekday())
                mode = str(profile["mode"] if profile is not None else "balanced")
                if mode == "heavy":
                    continue
                windows = await get_free_windows(db, target_day, min_minutes=est, future_only=offset == 0)
                if not windows:
                    continue
                window_start, window_end = windows[0]
                deadline = _task_deadline(task, tz)
                if deadline is not None and window_start + timedelta(minutes=est) > deadline:
                    continue
                await db.update_task(
                    task["id"],
                    scheduled_date=to_db_date(target_day),
                    scheduled_time=window_start.strftime("%H:%M"),
                )
                pending.remove(task)
                scheduled_count += 1
                break
    log.info("Auto-scheduled %s task(s)", scheduled_count)
    return scheduled_count


async def find_slot_for_task(db: Any, task_id: int, days: int = 14) -> datetime | None:
    task = await db.get_task(task_id)
    if task is None:
        return None
    now = await settings.now()
    tz = now.tzinfo
    today = now.date()
    est = max(5, int(task["est_minutes"] or 30))
    deadline = _task_deadline(task, tz)
    for offset in range(max(1, days)):
        target_day = today + timedelta(days=offset)
        if deadline is not None and target_day > deadline.date():
            break
        profile = await db.get_day_profile(target_day.weekday())
        mode = str(profile["mode"] if profile is not None else "balanced")
        max_task_minutes = int(profile["max_task_minutes"] if profile is not None else 90)
        windows = await get_free_windows(db, target_day, min_minutes=est, future_only=offset == 0)
        for window_start, window_end in windows:
            fit = _fit_task_into_window(
                task, target_day, window_start, window_end, mode, max_task_minutes, tz
            )
            if fit is None:
                continue
            start, end = fit
            await db.update_task(
                task_id,
                scheduled_date=to_db_date(target_day),
                scheduled_time=start.strftime("%H:%M"),
            )
            return start
    return None


async def pick_task_for_energy(db: Any, level: str, exclude_id: int | None = None) -> aiosqlite.Row | None:
    now = await settings.now()
    tz = now.tzinfo
    tasks = await db.fetchall(
        "SELECT * FROM tasks WHERE status='todo' ORDER BY (deadline_at IS NULL), deadline_at, id"
    )
    if not tasks:
        return None
    level = {"green": "high", "yellow": "medium", "red": "low"}.get(level, level)
    best: tuple[int, aiosqlite.Row] | None = None
    for task in tasks:
        if exclude_id is not None and int(task["id"]) == int(exclude_id):
            continue
        energy = str(task["energy"] or "medium")
        est = int(task["est_minutes"] or 30)
        score = 0
        if level == "high":
            score += {"high": 4, "medium": 2, "low": -1}.get(energy, 0)
        elif level == "medium":
            score += {"high": -1, "medium": 3, "low": 2}.get(energy, 0)
        else:
            score += {"high": -3, "medium": 1, "low": 4}.get(energy, 0)
            if est <= 15:
                score += 3
            elif est <= 30:
                score += 1
        if task["scheduled_date"] == to_db_date(now.date()):
            score += 3
        deadline = _task_deadline(task, tz)
        if deadline is not None:
            if deadline <= now:
                score += 5
            elif deadline <= now + timedelta(days=1):
                score += 2
        if task["scheduled_date"] is None:
            score += 1
        if best is None or score > best[0]:
            best = (score, task)
    if best is not None:
        return best[1]
    if exclude_id is not None:
        return await pick_task_for_energy(db, level, exclude_id=None)
    return None


def first_step_for(task: aiosqlite.Row) -> str:
    step = (task["first_step"] or "").strip()
    if step:
        return step
    return f"Открой материалы по теме «{task['title']}» и поработай 15 минут без переключений."


async def next_wake_datetime(db: Any, now: datetime | None = None) -> datetime | None:
    """Nearest future wake_time from day_profiles (used by rtcwake)."""
    now = now or await settings.now()
    tz = now.tzinfo
    for offset in (1, 0, 2):
        day = (now + timedelta(days=offset)).date() if offset else now.date()
        profile = await db.get_day_profile(day.weekday())
        if profile is None:
            continue
        wake_time = parse_hhmm(profile["wake_time"])
        if wake_time is None:
            continue
        candidate = combine(day, wake_time).replace(tzinfo=tz)
        if candidate > now + timedelta(minutes=1):
            return candidate
    return None


def day_mode_label(profile: aiosqlite.Row | None) -> str:
    from .utils import mode_icon, mode_name

    mode = str(profile["mode"] if profile is not None else "balanced")
    return f"{mode_icon(mode)} {mode_name(mode)}"


def date_from_argument(value: str | None) -> date | None:
    if not value:
        return None
    return parse_date(value)
