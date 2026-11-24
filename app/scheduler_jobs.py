"""APScheduler jobs: reminders, deadline cascades, briefings, /watch worker, power."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import browser_agent, llm_client, power_manager
from .config import settings
from .local_calendar import auto_schedule_tasks, day_overview, pick_task_for_energy
from .prompts import watch_prompt
from .schemas import WatchDecision
from .ui_formatter import (
    format_event,
    format_reminder,
    format_task_compact,
    format_today,
    send_long,
)
from .utils import (
    WEEKDAYS_RU,
    clip,
    esc,
    fmt_dt,
    human_delta,
    parse_datetime,
    parse_hhmm,
    sha256_text,
    to_db,
    to_db_date,
)
from .weather import weather_text_for_date

log = logging.getLogger(__name__)

MISFIRE_GRACE = 600


async def _owner() -> int | None:
    value = await settings.owner_id()
    return int(value) if value else None


async def _send(bot: Any, chat_id: int, text: str, keyboard: Any = None) -> bool:
    try:
        await send_long(bot, chat_id, text, keyboard)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)
        return False


# --------------------------------------------------------------------------- scheduler
async def create_scheduler(bot: Any, db: Any) -> AsyncIOScheduler:
    tz = await settings.timezone()
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        reminders_tick, "interval", minutes=1, args=[bot, db], id="reminders",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        deadline_tick, "interval", minutes=5, args=[bot, db], id="deadlines",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        watch_tick, "interval", minutes=5, args=[bot, db], id="watch",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        power_tick_job, "interval", minutes=1, args=[bot, db], id="power",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    morning_h, morning_m = await _brief_hm("morning_brief_time", 7, 15)
    evening_h, evening_m = await _brief_hm("evening_brief_time", 22, 30)
    scheduler.add_job(
        morning_brief_job,
        CronTrigger(hour=morning_h, minute=morning_m, timezone=tz),
        args=[bot, db], id="morning_brief",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        evening_brief_job,
        CronTrigger(hour=evening_h, minute=evening_m, timezone=tz),
        args=[bot, db], id="evening_brief",
        misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )
    return scheduler


async def _brief_hm(key: str, hour: int, minute: int) -> tuple[int, int]:
    parsed = parse_hhmm(await settings.get(key))
    if parsed is None:
        return hour, minute
    return parsed.hour, parsed.minute


async def reschedule_briefings(scheduler: AsyncIOScheduler) -> None:
    tz = await settings.timezone()
    mh, mm = await _brief_hm("morning_brief_time", 7, 15)
    eh, em = await _brief_hm("evening_brief_time", 22, 30)
    try:
        scheduler.reschedule_job(
            "morning_brief",
            trigger=CronTrigger(hour=mh, minute=mm, timezone=tz),
            misfire_grace_time=MISFIRE_GRACE,
            coalesce=True,
            max_instances=1,
        )
        scheduler.reschedule_job(
            "evening_brief",
            trigger=CronTrigger(hour=eh, minute=em, timezone=tz),
            misfire_grace_time=MISFIRE_GRACE,
            coalesce=True,
            max_instances=1,
        )
        log.info("Briefing jobs rescheduled: morning %02d:%02d, evening %02d:%02d", mh, mm, eh, em)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not reschedule briefings: %s", exc)


# --------------------------------------------------------------------------- reminders
async def reminders_tick(bot: Any, db: Any) -> None:
    owner_id = await _owner()
    if not owner_id:
        return
    now = await settings.now()
    due = await db.due_reminders(to_db(now))
    for reminder in due[:10]:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Сделано", callback_data=f"rem:done:{reminder['id']}:n"),
                    InlineKeyboardButton(text="⏰ +30 мин", callback_data=f"rem:snooze:30:{reminder['id']}:n"),
                ],
                [
                    InlineKeyboardButton(text="⏰ +2 часа", callback_data=f"rem:snooze:120:{reminder['id']}:n"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rem:del:{reminder['id']}:n"),
                ],
            ]
        )
        text = "⏰ <b>Напоминание</b>\n\n" + format_reminder(reminder, now)
        if reminder["task_id"]:
            task = await db.get_task(reminder["task_id"])
            if task is not None:
                text += f"\n\n📋 Задача: <b>{esc(task['title'])}</b>"
        if await _send(bot, owner_id, text, keyboard):
            await db.mark_reminder_sent(reminder["id"])


# --------------------------------------------------------------------------- deadlines
def _flags(row: Any) -> list[str]:
    try:
        value = json.loads(row["notified_flags"] or "[]")
        return [str(item) for item in value] if isinstance(value, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


async def _save_flags(db: Any, table: str, row_id: int, flags: list[str]) -> None:
    payload = json.dumps(sorted(set(flags)))
    if table == "tasks":
        await db.update_task(row_id, notified_flags=payload)
    else:
        await db.update_event(row_id, notified_flags=payload)


def _tier(deadline: datetime, now: datetime) -> tuple[str, str] | None:
    remaining = (deadline - now).total_seconds() / 60
    if remaining < 0:
        return "overdue", "просрочено"
    if remaining <= 90:
        return "1.5h", "осталось меньше 1,5 часа"
    if remaining <= 24 * 60:
        return "24h", "осталось меньше 24 часов"
    if remaining <= 48 * 60:
        return "48h", "осталось меньше 48 часов"
    return None


async def deadline_tick(bot: Any, db: Any) -> None:
    owner_id = await _owner()
    if not owner_id:
        return
    now = await settings.now()
    tz = now.tzinfo
    sent = 0

    tasks = await db.fetchall(
        "SELECT * FROM tasks WHERE status='todo' AND deadline_at IS NOT NULL "
        "AND deadline_at <= ? ORDER BY deadline_at LIMIT 80",
        (to_db(now + timedelta(days=3)),),
    )
    for task in tasks:
        if sent >= 8:
            break
        deadline = parse_datetime(task["deadline_at"], tz)
        if deadline is None:
            continue
        tier = _tier(deadline, now)
        if tier is None:
            continue
        key, phrase = tier
        flags = _flags(task)
        if key in flags:
            continue
        flags.append(key)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Готово", callback_data=f"t:done:{task['id']}:active:0"),
                    InlineKeyboardButton(text="⋯ Открыть", callback_data=f"t:view:{task['id']}:active:0"),
                ]
            ]
        )
        text = (
            f"⏳ <b>Дедлайн: {phrase}</b>\n\n"
            f"📋 <b>{esc(task['title'])}</b>\n"
            f"⏰ {fmt_dt(deadline)} · {human_delta(deadline, now)}"
        )
        if task["first_step"]:
            text += f"\n🪜 {esc(task['first_step'])}"
        if await _send(bot, owner_id, text, keyboard):
            await _save_flags(db, "tasks", task["id"], flags)
            sent += 1

    events = await db.fetchall(
        "SELECT * FROM events WHERE start_dt BETWEEN ? AND ? "
        "ORDER BY start_dt LIMIT 40",
        (to_db(now - timedelta(days=1)), to_db(now + timedelta(days=3))),
    )
    for event in events:
        if sent >= 10:
            break
        start = parse_datetime(event["start_dt"], tz)
        if start is None:
            continue
        flags = _flags(event)
        changed = False

        reg_deadline = parse_datetime(event["reg_deadline"], tz) if event["reg_deadline"] else None
        if reg_deadline is not None and str(event["reg_status"]) == "pending":
            tier = _tier(reg_deadline, now)
            if tier is not None:
                key, phrase = tier
                key = "reg_" + key
                if key not in flags:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Зарегистрировался",
                                    callback_data=f"event:reg:{event['id']}",
                                ),
                                InlineKeyboardButton(
                                    text="⋯ Открыть", callback_data=f"event:view:{event['id']}"
                                ),
                            ]
                        ]
                    )
                    text = (
                        f"📝 <b>Регистрация: {phrase}</b>\n\n"
                        f"🏆 <b>{esc(event['title'])}</b>\n"
                        f"⏳ до {fmt_dt(reg_deadline)} · {human_delta(reg_deadline, now)}"
                    )
                    if event["location_or_url"]:
                        text += f"\n📍 {esc(event['location_or_url'])}"
                    if await _send(bot, owner_id, text, keyboard):
                        flags.append(key)
                        changed = True
                        sent += 1

        tier = _tier(start, now)
        if tier is not None:
            key, phrase = tier
            key = "start_" + key
            if key not in flags:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⋯ Открыть", callback_data=f"event:view:{event['id']}")]
                    ]
                )
                text = (
                    f"📅 <b>Событие: {phrase}</b>\n\n"
                    + format_event(event, now, detailed=True)
                )
                if await _send(bot, owner_id, text, keyboard):
                    flags.append(key)
                    changed = True
                    sent += 1
        if changed:
            await _save_flags(db, "events", event["id"], flags)
        if sent >= 10:
            break


# --------------------------------------------------------------------------- briefings
async def morning_brief_job(bot: Any, db: Any) -> None:
    owner_id = await _owner()
    if not owner_id:
        return
    now = await settings.now()
    today = now.date().isoformat()
    if str(await settings.get("last_morning_brief") or "") == today:
        return
    overview = await day_overview(db, now.date())
    weather = await weather_text_for_date(
        now.date(), has_evening_event=bool(overview["events"])
    )
    tasks = await db.list_tasks("active", limit=30, now_db=to_db(now))
    top = []
    for task in tasks:
        if task["status"] == "todo":
            top.append(task)
        if len(top) >= 3:
            break
    text = (
        f"🌅 <b>Доброе утро!</b> {WEEKDAYS_RU[now.weekday()]}, {fmt_dt(now.date())}\n\n"
        + format_today(overview, weather, tasks)
    )
    if top:
        text += "\n\n<b>🎯 Главные задачи на сегодня</b>\n"
        for task in top:
            text += f"• {esc(task['title'])} · {esc(task['est_minutes'])} мин\n"
            if task["first_step"]:
                text += f"   🪜 {esc(clip(task['first_step'], 120))}\n"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 День", callback_data="menu:today"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
                InlineKeyboardButton(text="🧘 Сейчас", callback_data="now:menu"),
            ]
        ]
    )
    if await _send(bot, owner_id, text, keyboard):
        await settings.set("last_morning_brief", today)
        log.info("Morning brief sent")


async def evening_brief_job(bot: Any, db: Any) -> None:
    owner_id = await _owner()
    if not owner_id:
        return
    now = await settings.now()
    today = now.date().isoformat()
    if str(await settings.get("last_evening_brief") or "") == today:
        return
    tasks_today = await db.tasks_on_date(now.date())
    done = [task for task in tasks_today if task["status"] == "done"]
    todo = [task for task in tasks_today if task["status"] == "todo"]
    tomorrow = now.date() + timedelta(days=1)
    moved = 0
    for task in todo:
        await db.update_task(task["id"], scheduled_date=to_db_date(tomorrow))
        moved += 1
    if moved:
        await auto_schedule_tasks(db, days=7)
    tomorrow_profile = await db.get_day_profile(tomorrow.weekday())
    wake = tomorrow_profile["wake_time"] if tomorrow_profile is not None else None
    power = bool(await settings.get("power_enabled"))
    tomorrow_weather = await weather_text_for_date(tomorrow)
    lines = [
        f"🌙 <b>Вечерний чек-ин</b> · {fmt_dt(now.date())}",
        "",
        f"✅ Сделано сегодня: <b>{len(done)}</b>",
        f"↩️ Перенесено на завтра: <b>{moved}</b>",
    ]
    if todo:
        lines.append("")
        lines.append("<b>Хвосты:</b>")
        for task in todo[:5]:
            lines.append(f"• {esc(task['title'])}")
    if power and wake:
        lines.extend(["", f"⏰ Завтра ПК включится в <b>{esc(wake)}</b>."])
    if tomorrow_weather:
        lines.extend(["", tomorrow_weather])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Завтра", callback_data=f"day:{tomorrow.isoformat()}"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ]
        ]
    )
    if await _send(bot, owner_id, "\n".join(lines), keyboard):
        await settings.set("last_evening_brief", today)
        log.info("Evening check-in sent")


# --------------------------------------------------------------------------- watch
async def watch_tick(bot: Any, db: Any) -> None:
    owner_id = await _owner()
    if not owner_id:
        return
    items = await db.list_watched(active_only=True)
    if not items:
        return
    now = await settings.now()
    checked = 0
    for item in items:
        if checked >= 3:
            break
        last_checked = parse_datetime(item["last_checked"], now.tzinfo)
        interval = max(1, int(item["interval_hours"] or 2))
        if last_checked is not None and now - last_checked < timedelta(hours=interval):
            continue
        checked += 1
        try:
            await check_watch_entry(bot, db, int(item["id"]), notify=True)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
            log.warning("Watch check failed for #%s: %s", item["id"], exc)
            await db.update_watched(int(item["id"]), last_checked=to_db(now))


async def check_watch_entry(
    bot: Any,
    db: Any,
    watch_id: int,
    notify: bool = True,
) -> tuple[bool, str]:
    item = await db.get_watched(watch_id)
    if item is None:
        return False, "Мониторинг не найден."
    now = await settings.now()
    try:
        page = await browser_agent.extract_page(item["url"], goal=item["goal"])
    except Exception as exc:  # noqa: BLE001
        await db.update_watched(watch_id, last_checked=to_db(now))
        return False, f"❌ Не удалось прочитать страницу: {exc}"
    text = page.text
    if page.pdf_texts:
        text += "\n\n" + "\n\n".join(page.pdf_texts)
    new_hash = sha256_text(text)
    old_hash = item["last_hash"]
    await db.update_watched(watch_id, last_checked=to_db(now))
    if not old_hash:
        await db.update_watched(
            watch_id,
            last_hash=new_hash,
            last_summary=text[:3000],
        )
        return True, "✅ Первая проверка сохранена. Уведомлю только при изменениях."
    if old_hash == new_hash:
        return True, "🟢 Изменений нет."

    old_summary = str(item["last_summary"] or "")
    decision_summary = ""
    relevant = True
    try:
        decision = await llm_client.chat_json(
            [{"role": "user", "content": watch_prompt(str(item["goal"]), old_summary, text)}],
            model_cls=WatchDecision,
            max_tokens=500,
        )
        relevant = bool(decision.get("relevant"))
        decision_summary = str(decision.get("summary") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("Watch LLM comparison failed for #%s: %s", watch_id, exc)
        return False, f"⚠️ Страница изменилась, но не удалось оценить критерий: {exc}"

    await db.update_watched(
        watch_id,
        last_hash=new_hash,
        last_summary=text[:3000],
    )
    if not relevant:
        return True, "🟢 Страница изменилась, но критерию не соответствует."
    summary = decision_summary or clip(text, 800)
    message = (
        f"👁 <b>Изменение на странице</b>\n\n"
        f"🔗 {esc(item['url'])}\n"
        f"🎯 {esc(item['goal'])}\n\n"
        f"{esc(clip(summary, 2000))}"
    )
    if notify:
        owner_id = await _owner()
        if owner_id:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔗 Открыть", url=item["url"])],
                    [
                        InlineKeyboardButton(
                            text="⏸ Отключить", callback_data=f"watch:off:{watch_id}"
                        )
                    ],
                ]
            )
            await _send(bot, owner_id, message, keyboard)
    return True, message


async def run_manual_watch(bot: Any, chat_id: int, watch_id: int) -> None:
    await _send(bot, chat_id, "👁 Проверяю страницу… Это может занять до 90 секунд.")
    ok, message = await check_watch_entry(bot, db=runtime_db(), watch_id=watch_id, notify=False)
    await _send(bot, chat_id, message)


def runtime_db() -> Any:
    from . import runtime

    if runtime.database is None:
        raise RuntimeError("Database is not ready")
    return runtime.database


# --------------------------------------------------------------------------- power wrapper
async def power_tick_job(bot: Any, db: Any) -> None:
    try:
        await power_manager.power_tick(bot, db)
    except Exception:  # noqa: BLE001
        log.exception("Power tick failed")
