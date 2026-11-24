"""HTML cards and inline-safe message splitting for Telegram."""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions

from .utils import (
    CATEGORY_ICONS,
    ENERGY_ICONS,
    MODE_ICONS,
    MODE_NAMES,
    STATUS_ICONS,
    WEEKDAYS_SHORT,
    WEEKDAY_ALIASES,
    clip,
    esc,
    fmt_date,
    fmt_dt,
    fmt_hhmm,
    human_delta,
    human_duration,
    mode_icon,
    mode_name,
    parse_date,
    parse_datetime,
    parse_hhmm,
    plural_ru,
    weekday_ru,
)

log = logging.getLogger(__name__)

MAX_MESSAGE = 3800
TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w:-]*)([^>]*)>")
VOID_TAGS = {"br", "hr", "img"}


def split_html(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split HTML without cutting tags, closing/reopening them per chunk."""
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    guard = 0
    while len(remaining) > limit and guard < 100:
        guard += 1
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        last_open = remaining.rfind("<", 0, cut)
        last_close = remaining.rfind(">", 0, cut)
        if last_open > last_close:
            cut = last_open if last_open > 0 else cut
        chunk = remaining[:cut].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            cut = limit
        balanced, reopen = _balance_tags(chunk)
        chunks.append(balanced)
        remaining = (reopen + remaining[cut:].lstrip("\n")).strip()
    if remaining:
        chunks.append(remaining)
    return chunks or [text]


def _balance_tags(chunk: str) -> tuple[str, str]:
    stack: list[tuple[str, str]] = []
    for match in TAG_RE.finditer(chunk):
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if name in VOID_TAGS or attrs.rstrip().endswith("/"):
            continue
        if closing:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    stack.pop(index)
                    break
        else:
            stack.append((name, match.group(0)))
    closing_tags = "".join(f"</{name}>" for name, _ in reversed(stack))
    reopening = "".join(tag for _, tag in stack)
    return chunk + closing_tags, reopening


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


async def send_long(
    bot: Any,
    chat_id: int,
    text: str,
    reply_markup: Any = None,
    disable_preview: bool = True,
) -> None:
    chunks = split_html(text)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            await bot.send_message(
                chat_id,
                chunk,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
            )
        except TelegramBadRequest as exc:
            log.warning("HTML send failed (%s), falling back to plain text", exc)
            plain = strip_tags(chunk)
            for part in _plain_chunks(plain):
                await bot.send_message(
                    chat_id,
                    part,
                    reply_markup=markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
                )


def _plain_chunks(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def safe_edit(callback: Any, text: str, reply_markup: Any = None) -> None:
    chunks = split_html(text)
    message = callback.message
    try:
        if len(chunks) == 1:
            await message.edit_text(
                chunks[0],
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await message.edit_text(
                chunks[0], reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            for index, chunk in enumerate(chunks[1:], start=1):
                await callback.bot.send_message(
                    callback.message.chat.id,
                    chunk,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        log.debug("edit failed, sending a new message: %s", exc)
        await send_long(callback.bot, message.chat.id, text, reply_markup=reply_markup)


def _task_icon(task: Any) -> str:
    return STATUS_ICONS.get(str(task["status"]), "⏳")


def _deadline_line(task: Any, now: datetime) -> str:
    if not task["deadline_at"]:
        return ""
    deadline = parse_datetime(task["deadline_at"], now.tzinfo)
    if deadline is None:
        return ""
    return f"⏰ до {fmt_dt(deadline)} · {human_delta(deadline, now)}"


def format_task_compact(task: Any, now: datetime) -> str:
    icon = _task_icon(task)
    energy = ENERGY_ICONS.get(str(task["energy"]), "🟡")
    category = CATEGORY_ICONS.get(str(task["category"]), "📌")
    line = f"{icon} <b>{esc(task['title'])}</b> — {category} {esc(task['est_minutes'])} мин {energy}"
    if task["status"] == "done":
        if task["completed_at"]:
            completed_dt = parse_datetime(task["completed_at"], now.tzinfo)
            delta_str = f" ({human_delta(completed_dt, now)})" if completed_dt else ""
            line += f"\n   ✅ Закрыта: {esc(task['completed_at'])}{delta_str}"
        else:
            line += "\n   ✅ Выполнена"
    elif task["status"] == "frozen":
        line += "\n   🧊 Заморожена"
    else:
        if task["scheduled_date"]:
            when = esc(task["scheduled_date"])
            if task["scheduled_time"]:
                when += f" {esc(task['scheduled_time'])}"
            line += f"\n   📅 {when}"
        if task["deadline_at"]:
            deadline = parse_datetime(task["deadline_at"], now.tzinfo)
            if deadline:
                line += f"\n   ⏰ {fmt_dt(deadline)} · {human_delta(deadline, now)}"
    return line


def format_task_detail(task: Any, now: datetime) -> str:
    icon = _task_icon(task)
    category = CATEGORY_ICONS.get(str(task["category"]), "📌")
    energy = ENERGY_ICONS.get(str(task["energy"]), "🟡")
    lines = [
        f"{icon} <b>{esc(task['title'])}</b>",
        f"{category} {esc(task['category'])} · {energy} энергия · ⏱ {esc(task['est_minutes'])} мин",
        f"Статус: <b>{esc(task['status'])}</b> · создана {esc(task['created_at'])}",
    ]
    if task["completed_at"]:
        completed_dt = parse_datetime(task["completed_at"], now.tzinfo)
        delta_str = f" ({human_delta(completed_dt, now)})" if completed_dt else ""
        lines.append(f"✅ Закрыта: <b>{esc(task['completed_at'])}</b>{delta_str}")
    if task["scheduled_date"]:
        when = str(task["scheduled_date"])
        if task["scheduled_time"]:
            when += f" {task['scheduled_time']}"
        lines.append(f"📅 Запланировано: <b>{esc(when)}</b>")
    if task["deadline_at"]:
        deadline = parse_datetime(task["deadline_at"], now.tzinfo)
        lines.append(
            f"⏰ Дедлайн: <b>{fmt_dt(deadline or task['deadline_at'])}</b>"
            + (f" ({human_delta(deadline, now)})" if deadline else "")
        )
    if task["first_step"]:
        lines.append(f"🪜 Первый шаг: {esc(task['first_step'])}")
    if task["source_url"]:
        lines.append(f"🔗 {esc(task['source_url'])}")
    return "\n".join(lines)


def format_tasks_header(filter_name: str, counts: dict[str, int], total: int, offset: int) -> str:
    names = {
        "active": "Активные", "today": "На сегодня", "week": "На неделю",
        "overdue": "Просроченные", "unscheduled": "Без слота", "done": "Выполненные",
        "frozen": "Замороженные", "all": "Все", "closed": "Закрытые",
    }
    title = names.get(filter_name, filter_name)
    return (
        f"📋 <b>Задачи · {title}</b>\n"
        f"Активных: <b>{counts.get('todo', 0)}</b> · выполнено: <b>{counts.get('done', 0)}</b> · "
        f"заморожено: <b>{counts.get('frozen', 0)}</b>\n"
        f"Показано: {offset + 1}–{offset + total}" if total else
        f"📋 <b>Задачи · {title}</b>\nАктивных: <b>{counts.get('todo', 0)}</b> · "
        f"выполнено: <b>{counts.get('done', 0)}</b> · заморожено: <b>{counts.get('frozen', 0)}</b>"
    )


def format_tasks_list(filter_name: str, tasks: Iterable[Any], now: datetime) -> str:
    tasks = list(tasks)
    names = {
        "active": "Активные", "today": "На сегодня", "week": "На неделю",
        "overdue": "Просроченные", "unscheduled": "Без слота", "done": "Выполненные",
        "frozen": "Замороженные", "all": "Все", "closed": "Закрытые",
    }
    lines = [f"<b>📋 Задачи · {names.get(filter_name, filter_name)}</b>", ""]
    if not tasks:
        lines.append("Здесь пока пусто.")
    for task in tasks:
        lines.append(format_task_compact(task, now))
        lines.append("")
    return "\n".join(lines).strip()


def format_event(event: Any, now: datetime, detailed: bool = True) -> str:
    start = parse_datetime(event["start_dt"], now.tzinfo)
    end = parse_datetime(event["end_dt"], now.tzinfo) if event["end_dt"] else None
    icon = CATEGORY_ICONS.get(str(event["category"]), "📌")
    lines = [f"{icon} <b>{esc(event['title'])}</b>"]
    if start:
        when = fmt_dt(start)
        if end:
            when += f"–{end.strftime('%H:%M')}"
        lines.append(f"🕒 {when} · {human_delta(start, now)}")
    else:
        lines.append(f"🕒 {esc(event['start_dt'])}")
    if event["location_or_url"]:
        lines.append(f"📍 {esc(event['location_or_url'])}")
    reg = str(event["reg_status"])
    if reg != "not_needed":
        reg_icon = {"pending": "🟡", "done": "✅"}.get(reg, "⚪️")
        lines.append(f"{reg_icon} Регистрация: {esc(reg)}")
    if detailed and event["reg_deadline"]:
        deadline = parse_datetime(event["reg_deadline"], now.tzinfo)
        lines.append(f"⏳ Регистрация до: {fmt_dt(deadline or event['reg_deadline'])}")
    if detailed and event["prep_notes"]:
        lines.append(f"📝 {esc(clip(event['prep_notes'], 500))}")
    if detailed and event["source_url"]:
        lines.append(f"🔗 {esc(event['source_url'])}")
    return "\n".join(lines)


def format_events(scope: str, events: Iterable[Any], now: datetime, total: int) -> str:
    events = list(events)
    title = {"upcoming": "Предстоящие", "past": "Прошедшие", "all": "Все"}.get(scope, scope)
    lines = [f"<b>📅 События · {title}</b> · {total}", ""]
    if not events:
        lines.append("Ничего не найдено.")
    for event in events:
        lines.append(format_event(event, now))
        lines.append("")
    return "\n".join(lines).strip()


def format_today(overview: dict[str, Any], weather_text: str | None, task_rows: list[Any] | None = None) -> str:
    day = overview["date"]
    profile = overview["profile"]
    mode = str(profile["mode"] if profile is not None else "balanced")
    paused_until = None
    if overview.get("paused"):
        raw_pause = overview.get("pause_until")
        if raw_pause:
            parsed_pause = parse_date(str(raw_pause))
            paused_until = fmt_date(parsed_pause, with_weekday=False) if parsed_pause else str(raw_pause)
    lines = [
        f"📅 <b>{weekday_ru(day)}, {fmt_date(day, with_weekday=False)}</b>",
        f"{mode_icon(mode)} {mode_name(mode)} · "
        f"{overview['day_start'].strftime('%H:%M')}–{overview['day_end'].strftime('%H:%M')}",
    ]
    if paused_until:
        lines.append(f"⏸ Регулярная сетка на паузе до {esc(paused_until)}")
    if weather_text:
        lines.extend(["", weather_text])
    lines.extend(["", "<b>🗓 Таймлайн</b>"])
    entries = overview["entries"]
    if not entries:
        lines.append("Событий и занятий нет — день свободен.")
    now = overview["timezone"] and datetime.now(overview["timezone"])
    for entry in entries:
        start = entry["start"]
        end = entry["end"]
        icon = CATEGORY_ICONS.get(str(entry["category"]), "📌")
        if entry["kind"] == "task":
            icon = STATUS_ICONS.get(str(entry["status"]), "⏳")
        title = esc(entry["title"])
        extra = ""
        if entry["location"] and entry["kind"] in {"recurring", "event"}:
            extra = f" · {esc(clip(entry['location'], 60))}"
        if entry["kind"] == "event":
            delta = human_delta(start, now) if now else ""
            extra += f" · {delta}" if delta and start > (now or start) else ""
        lines.append(f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} {icon} <b>{title}</b>{extra}")
    lines.extend(["", "<b>🟢 Свободные окна</b>"])
    windows = overview["free_windows"]
    if not windows:
        lines.append("Свободных окон нет.")
    for start, end in windows:
        minutes = int((end - start).total_seconds() // 60)
        lines.append(f"• {start.strftime('%H:%M')}–{end.strftime('%H:%M')} · {human_duration(minutes)}")
    unscheduled = [task for task in (task_rows or []) if not task["scheduled_time"] and task["status"] == "todo"]
    if unscheduled:
        lines.extend(["", "<b>📋 Без времени на сегодня</b>"])
        for task in unscheduled:
            lines.append(f"• {esc(task['title'])} · {esc(task['est_minutes'])} мин")
    return "\n".join(lines)


def format_week(start: date, days: list[dict[str, Any]]) -> str:
    end = start + timedelta(days=len(days) - 1)
    lines = [f"📆 <b>Неделя {fmt_date(start, False)} — {fmt_date(end, False)}</b>", ""]
    for item in days:
        day = item["date"]
        profile = item["profile"]
        mode = str(profile["mode"] if profile is not None else "balanced")
        free_minutes = item["free_minutes"]
        event_count = item["event_count"]
        task_count = item["task_count"]
        lines.append(
            f"{mode_icon(mode)} <b>{WEEKDAYS_SHORT[day.weekday()]} {fmt_date(day, False)}</b> · "
            f"{human_duration(free_minutes)} свободно · "
            f"{event_count} {plural_ru(event_count, 'событие', 'события', 'событий')} · "
            f"{task_count} {plural_ru(task_count, 'задача', 'задачи', 'задач')}"
        )
        for event in item.get("events", [])[:2]:
            icon = CATEGORY_ICONS.get(str(event["category"]), "📌")
            start_dt = parse_datetime(event["start_dt"])
            time_text = start_dt.strftime("%H:%M") if start_dt else ""
            lines.append(f"   {icon} {time_text} {esc(clip(event['title'], 50))}")
        if item.get("paused"):
            lines.append("   ⏸ сетка на паузе")
    return "\n".join(lines)


def format_now_task(task: Any, now: datetime) -> str:
    energy = ENERGY_ICONS.get(str(task["energy"]), "🟡")
    category = CATEGORY_ICONS.get(str(task["category"]), "📌")
    from .local_calendar import first_step_for

    lines = [
        "🎯 <b>Одна задача на сейчас</b>",
        "",
        f"<b>{esc(task['title'])}</b>",
        f"{category} {esc(task['category'])} · {energy} · ⏱ {esc(task['est_minutes'])} мин",
    ]
    if task["deadline_at"]:
        deadline = parse_datetime(task["deadline_at"], now.tzinfo)
        if deadline:
            lines.append(f"⏰ {fmt_dt(deadline)} · {human_delta(deadline, now)}")
    lines.extend(
        [
            "",
            f"🪜 <b>Первый шаг:</b> {esc(first_step_for(task))}",
            "",
            "Поработай 15 минут. Дальше будет видно.",
        ]
    )
    return "\n".join(lines)


def format_note(note: Any, full: bool = False) -> str:
    lines = [
        f"🗒 <b>{esc(note['title'])}</b>",
        f"<i>{esc(note['created_at'])}</i>",
    ]
    if note["tags"]:
        lines.append(f"🏷 {esc(note['tags'])}")
    content = str(note["content"] or "")
    lines.extend(["", esc(content if full else clip(content, 700))])
    if note["source_url"]:
        lines.append(f"\n🔗 {esc(note['source_url'])}")
    return "\n".join(lines)


def format_note_search(note: Any) -> str:
    lines = [f"🔎 <b>{esc(note['title'])}</b>", f"<i>{esc(note['created_at'])}</i>"]
    snippet = note["snippet"] if "snippet" in note.keys() and note["snippet"] else clip(note["content"], 300)
    lines.append(esc(snippet))
    return "\n".join(lines)


def format_reminder(reminder: Any, now: datetime, detailed: bool = False) -> str:
    remind_at = parse_datetime(reminder["remind_at"], now.tzinfo)
    icon = "✅" if reminder["is_sent"] else "⏰"
    line = f"{icon} <b>{esc(reminder['text'])}</b>"
    if remind_at:
        line += f"\n   {fmt_dt(remind_at)} · {human_delta(remind_at, now)}"
    else:
        line += f"\n   {esc(reminder['remind_at'])}"
    if detailed and reminder["cron_expr"]:
        line += f"\n   🔁 {esc(reminder['cron_expr'])}"
    return line


def format_watch(item: Any, now: datetime) -> str:
    status = "🟢" if item["is_active"] else "⏸"
    lines = [
        f"{status} <b>{esc(clip(item['url'], 100))}</b>",
        f"🎯 {esc(item['goal'])}",
        f"🕒 каждые {esc(item['interval_hours'])} ч · проверено: {esc(item['last_checked'] or 'ещё нет')}",
    ]
    if item["last_summary"]:
        lines.append(f"📝 {esc(clip(item['last_summary'], 300))}")
    return "\n".join(lines)


def format_inbox_item(item: Any, now: datetime) -> str:
    status_icons = {
        "processed": "✅", "pending": "🕓", "pending_retry": "⚠️",
        "failed": "❌", "ignored": "⚪️",
    }
    icon = status_icons.get(str(item["status"]), "•")
    lines = [
        f"{icon} <b>#{item['id']}</b> · {esc(item['msg_type'])} · {esc(item['created_at'])}",
    ]
    try:
        import json

        payload = json.loads(item["raw_content"])
        parts = payload.get("parts") if isinstance(payload, dict) else None
        if parts:
            preview = " | ".join(
                clip(part.get("text") or part.get("caption") or part.get("file_name") or part.get("kind"), 120)
                for part in parts
            )
            lines.append(f"<i>{esc(preview)}</i>")
        else:
            lines.append(f"<i>{esc(clip(item['raw_content'], 250))}</i>")
    except Exception:
        lines.append(f"<i>{esc(clip(item['raw_content'], 250))}</i>")
    return "\n".join(lines)


def format_stats(stats: dict[str, Any]) -> str:
    categories = stats.get("tasks_by_category") or {}
    done_categories = stats.get("tasks_done_by_category") or {}
    cat_names = {
        "study": "📚 учёба", "olympiad": "🏆 олимпиады", "project": "🛠 проекты",
        "personal": "🏠 личное", "club": "🎨 кружки", "sport": "🏃 спорт",
        "deadline": "⏳ дедлайны", "meeting": "👥 встречи", "other": "📌 другое",
    }
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"⏳ Активных задач: <b>{stats.get('tasks_todo', 0)}</b>",
        f"✅ Выполнено всего: <b>{stats.get('tasks_done', 0)}</b>",
        f"   • Сегодня: <b>{stats.get('tasks_done_today', 0)}</b>",
        f"   • За 7 дней: <b>{stats.get('tasks_done_week', 0)}</b>",
        f"   • За 30 дней: <b>{stats.get('tasks_done_month', 0)}</b>",
        f"🧊 Заморожено: <b>{stats.get('tasks_frozen', 0)}</b>",
        f"📅 Предстоящих событий: <b>{stats.get('events_upcoming', 0)}</b>",
        f"⏰ Активных напоминаний: <b>{stats.get('reminders_pending', 0)}</b>",
        f"🗒 Заметок: <b>{stats.get('notes', 0)}</b>",
        f"👁 Мониторинг страниц: <b>{stats.get('watched_active', 0)}</b>",
        f"📥 Inbox: <b>{stats.get('inbox_total', 0)}</b> · обработано: <b>{stats.get('inbox_processed', 0)}</b>"
        + (f" · ждут повтора: <b>{stats.get('inbox_failed', 0)}</b>" if stats.get("inbox_failed") else ""),
    ]
    if categories:
        lines.extend(["", "<b>Активные задачи по категориям:</b>"])
        for key, count in sorted(categories.items(), key=lambda pair: -pair[1]):
            lines.append(f"• {cat_names.get(key, key)}: {count}")
    if done_categories:
        lines.extend(["", "<b>Закрытые задачи по категориям:</b>"])
        for key, count in sorted(done_categories.items(), key=lambda pair: -pair[1]):
            lines.append(f"• {cat_names.get(key, key)}: {count}")
    return "\n".join(lines)


def format_archive_menu(stats: dict[str, Any]) -> str:
    return (
        "🗃 <b>Архив</b>\n\n"
        "Здесь всё закрытое и прошлое — ничего не теряется.\n\n"
        f"✅ Выполненных задач: <b>{stats.get('tasks_done', 0)}</b>\n"
        f"🧊 Замороженных: <b>{stats.get('tasks_frozen', 0)}</b>\n"
        f"📅 Прошедших событий: <b>{stats.get('events_past', 0)}</b>\n"
        f"🗒 Заметок: <b>{stats.get('notes', 0)}</b>\n"
        f"📥 Сообщений в Inbox: <b>{stats.get('inbox_total', 0)}</b>"
    )


def format_schedule_slot(slot: Any) -> str:
    icon = CATEGORY_ICONS.get(str(slot["category"]), "📌")
    location = f" · {esc(slot['location'])}" if slot["location"] else ""
    return (
        f"• {fmt_hhmm(slot['start_time'])}–{fmt_hhmm(slot['end_time'])} "
        f"{icon} <b>{esc(slot['title'])}</b>{location}"
    )


def format_day_schedule(weekday: int, slots: list[Any], pause_until: str | None) -> str:
    lines = [f"📅 <b>{weekday_ru(date(2024, 1, 1 + weekday))}</b> — регулярная сетка", ""]
    if pause_until:
        lines.append(f"⏸ Вся сетка на паузе до <b>{esc(pause_until)}</b>")
        lines.append("")
    if not slots:
        lines.append("Слотов нет. Добавь занятие или импортируй сетку.")
    for slot in slots:
        lines.append(format_schedule_slot(slot))
    return "\n".join(lines)


def format_settings_main(values: dict[str, Any], counts: dict[str, int]) -> str:
    coords = ""
    if values.get("lat") is not None and values.get("lon") is not None:
        coords = f" ({float(values['lat']):.3f}, {float(values['lon']):.3f})"
    pause = values.get("pause_recurring_until")
    return (
        "⚙️ <b>Настройки секретаря</b>\n\n"
        f"📍 Город: <b>{esc(values.get('city') or 'не настроен')}</b>{coords}\n"
        f"🕰 Часовой пояс: <b>{esc(values.get('timezone') or '—')}</b>\n"
        f"📚 Слотов в сетке: <b>{counts.get('recurring', 0)}</b>"
        + (f" · ⏸ пауза до {esc(pause)}" if pause else "") + "\n"
        f"⏰ Брифинги: <b>{esc(values.get('morning_brief_time', '07:15'))}</b> / "
        f"<b>{esc(values.get('evening_brief_time', '22:30'))}</b>\n"
        f"🧠 Whisper: <b>{esc(values.get('whisper_model', 'small'))}</b> · "
        f"VLM: <b>{'вкл' if values.get('vlm_enabled', True) else 'выкл'}</b>\n"
        f"⚡ Питание ПК: <b>{'вкл' if values.get('power_enabled') else 'выкл'}</b>\n"
        f"👁 Мониторинг: каждые <b>{esc(values.get('watch_interval_hours', 2))}</b> ч\n\n"
        "Меняй что угодно без правки файлов и перезапуска."
    )


def format_power_settings(values: dict[str, Any], profiles: list[Any]) -> str:
    lines = [
        "⚡ <b>Питание ПК (rtcwake)</b>",
        "",
        f"Тумблер: <b>{'включён' if values.get('power_enabled') else 'выключен'}</b>",
        "",
    ]
    for profile in profiles:
        lines.append(
            f"{WEEKDAYS_SHORT[int(profile['weekday'])]}: "
            f"🌅 {esc(profile['wake_time'] or '—')} → 🌙 {esc(profile['sleep_time'] or '—')}"
        )
    lines.extend(
        [
            "",
            "За 5 минут до сна бот пришлёт кнопки отсрочки. "
            "Для работы нужен sudoers-доступ к rtcwake.",
        ]
    )
    return "\n".join(lines)


def format_brief_settings(values: dict[str, Any]) -> str:
    return (
        "⏰ <b>Брифинги и мониторинг</b>\n\n"
        f"🌅 Утренний брифинг: <b>{esc(values.get('morning_brief_time', '07:15'))}</b>\n"
        f"🌙 Вечерний чек-ин: <b>{esc(values.get('evening_brief_time', '22:30'))}</b>\n"
        f"👁 Интервал /watch: каждые <b>{esc(values.get('watch_interval_hours', 2))}</b> ч\n\n"
        "Брифинги приходят с погодой, таймлайном, свободными окнами и главными задачами."
    )


def format_models_settings(values: dict[str, Any]) -> str:
    profile = str(values.get("user_profile") or "")
    return (
        "🧠 <b>Модели и профиль</b>\n\n"
        f"🎙 Whisper: <b>{esc(values.get('whisper_model', 'small'))}</b>\n"
        f"👁 VLM: <b>{'включён' if values.get('vlm_enabled', True) else 'выключен'}</b>\n"
        f"⚙️ LLM: смотри .env (LLM_BASE_URL / LLM_MODEL_NAME)\n\n"
        f"<b>Профиль владельца</b>\n{esc(profile) if profile else '<i>не заполнен</i>'}"
    )


def format_weekday_profile(profile: Any) -> str:
    mode = str(profile["mode"])
    return (
        f"📆 <b>{weekday_ru(date(2024, 1, 1 + int(profile['weekday'])))}</b>\n\n"
        f"Режим: <b>{mode_icon(mode)} {mode_name(mode)}</b>\n"
        f"Активный день: <b>{esc(profile['day_start'])}–{esc(profile['day_end'])}</b>\n"
        f"Максимальный блок задачи: <b>{esc(profile['max_task_minutes'])} мин</b>\n"
        f"Подъём: <b>{esc(profile['wake_time'] or '—')}</b> · Сон: <b>{esc(profile['sleep_time'] or '—')}</b>"
    )


def event_button_label(event: Any) -> str:
    start = parse_datetime(event["start_dt"])
    return f"{CATEGORY_ICONS.get(str(event['category']), '📌')} {start.strftime('%d.%m %H:%M') if start else ''} {clip(event['title'], 22)}"


def format_server_status(data: dict[str, Any]) -> str:
    hw = data["hardware"]
    llm = data["llm"]
    whisper_info = data["whisper"]
    vlm_info = data["vlm"]
    scheduler_info = data["scheduler"]
    semaphore_busy = data["semaphore_busy"]

    if llm["online"]:
        llm_status = f"🟢 Онлайн ({llm['latency_ms']:.0f} мс)"
    else:
        llm_status = f"🔴 Офлайн ({esc(llm.get('error') or 'недоступен')})"

    cpu_str = f"{hw['cpu_cores']} ядер"
    if hw.get("cpu_percent") is not None:
        cpu_str += f" · {hw['cpu_percent']:.0f}%"

    lines = [
        "🖥 <b>Статус сервера и моделей</b>",
        "",
        "<b>💻 Оборудование ПК:</b>",
        f"• ОС: <code>{esc(hw['os_name'])} ({esc(hw['architecture'])})</code>",
        f"• Хост: <code>{esc(hw['hostname'])}</code> · Python {esc(hw['python_version'])}",
        f"• CPU: <b>{cpu_str}</b>",
        f"• RAM: <b>{esc(hw['ram_text'])}</b>",
        f"• Диск: <b>{esc(hw['disk_text'])}</b>",
        f"• База данных SQLite: <b>{esc(hw['db_size_text'])}</b>",
        f"• Аптайм бота: <b>{esc(hw['bot_uptime'])}</b> (хост: {esc(hw['host_uptime'])})",
        "",
        "<b>🧠 Нейросети и модели:</b>",
        f"• LLM эндпоинт: <code>{esc(llm['base_url'])}</code>",
        f"• Статус LLM: <b>{llm_status}</b>",
        f"• Модель LLM: <code>{esc(llm['configured_model'])}</code> "
        f"{'✅' if llm.get('model_active') else '⚠️ не найдена в списке'}",
    ]

    if llm.get("available_models"):
        models_preview = ", ".join(f"<code>{esc(m)}</code>" for m in llm["available_models"][:4])
        if len(llm["available_models"]) > 4:
            models_preview += f" и ещё {len(llm['available_models']) - 4}"
        lines.append(f"• Доступно моделей: {models_preview}")

    whisper_state = "🟢 В памяти RAM" if whisper_info["in_ram"] else "💤 Выгружен (lazy)"
    if whisper_info["model_configured"].lower() == "off":
        whisper_state = "⚪ Отключён"

    lines.extend(
        [
            f"• Whisper STT: <b>{esc(whisper_info['model_configured'])}</b> · {whisper_state} (активность: {esc(whisper_info['last_used'])})",
            f"• VLM (Vision): <b>{'🟢 Включён' if vlm_info['enabled'] else '⚪ Отключён'}</b> (<code>{esc(vlm_info['model'])}</code>)",
            "",
            "<b>⚙️ Очередь и планировщик:</b>",
            f"• Очередь тяжелых задач: <b>{'🟡 Занята обработкой' if semaphore_busy else '🟢 Свободна'}</b>",
            f"• Фоновый планировщик: <b>{'🟢 Работает' if scheduler_info['running'] else '🔴 Остановлен'}</b> ({scheduler_info['jobs_count']} задач)",
        ]
    )
    return "\n".join(lines)


def format_progress_settings(values: dict[str, Any]) -> str:
    mode = str(values.get("progress_mode") or "verbose")
    mode_titles = {
        "verbose": "Подробный (по шагам) 📝",
        "compact": "Компактный (одна строка) ⏱",
        "off": "Выключен (тихий) 🔇",
    }
    mode_text = mode_titles.get(mode, mode)

    def _chk(key: str) -> str:
        return "✅ Включено" if values.get(key, True) else "❌ Выключено"

    return (
        "⚡️ <b>Ход выполнения задач (Live Status)</b>\n\n"
        "Бот сообщает в реальном времени, что он сейчас делает при разборе входящих "
        "(открывает ссылку, расшифровывает аудио, отправляет запрос в нейросеть, планирует в календарь).\n\n"
        f"<b>Текущий режим:</b> {mode_text}\n\n"
        "<b>Категории уведомлений:</b>\n"
        f"• 🌐 Ссылки и браузер: <b>{_chk('progress_notify_browser')}</b>\n"
        f"• 🎧 Голосовые и аудио: <b>{_chk('progress_notify_voice')}</b>\n"
        f"• 📄 Документы и файлы: <b>{_chk('progress_notify_doc')}</b>\n"
        f"• 🧠 Запросы к нейросети: <b>{_chk('progress_notify_llm')}</b>\n"
        f"• 📅 Задачи и календарь: <b>{_chk('progress_notify_calendar')}</b>\n\n"
        "Нажимай на кнопки ниже, чтобы переключить режим или настроить категории."
    )

