"""User-facing views: day/week, events, tasks, anti-stupor, notes, archive, watch."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import runtime
from ..config import settings
from ..database import Database
from ..local_calendar import (
    auto_schedule_tasks,
    day_overview,
    find_slot_for_task,
    pick_task_for_energy,
)
from ..scheduler_jobs import check_watch_entry
from ..ui_formatter import (
    format_archive_menu,
    format_event,
    format_events,
    format_note,
    format_note_search,
    format_now_task,
    format_reminder,
    format_stats,
    format_task_compact,
    format_task_detail,
    format_tasks_list,
    format_today,
    format_week,
    safe_edit,
    send_long,
)
from ..utils import (
    WEEKDAYS_SHORT,
    clip,
    esc,
    fmt_date,
    fmt_dt,
    human_delta,
    parse_date,
    parse_datetime,
    title_from_text,
    to_db,
    to_db_date,
    weekday_ru,
)
from ..weather import weather_text_for_date

log = logging.getLogger(__name__)
router = Router(name="views")


class ViewStates(StatesGroup):
    find_query = State()
    task_search = State()
    watch_criterion = State()
    task_plan = State()
    quick_task = State()
    quick_note = State()
    quick_remind = State()


def db() -> Database:
    if runtime.database is None:
        raise RuntimeError("Database is not ready")
    return runtime.database


def _background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda finished: log.debug("background task done: %s", finished))


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня", callback_data="menu:today"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ],
            [
                InlineKeyboardButton(text="🧘 Сейчас", callback_data="now:menu"),
                InlineKeyboardButton(text="🗃 Архив", callback_data="arch:menu"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="set:menu"),
                InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
            ],
        ]
    )


def _command_arg(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _task_filter(value: str) -> str:
    allowed = {"active", "today", "week", "overdue", "unscheduled", "done", "frozen", "all", "closed"}
    return value if value in allowed else "active"


TASK_FILTER_NAMES = {
    "active": "Активные", "today": "Сегодня", "week": "Неделя", "overdue": "Просрочено",
    "unscheduled": "Без слота", "done": "Выполненные", "frozen": "Замороженные",
    "all": "Все", "closed": "Закрытые",
}


# --------------------------------------------------------------------------- main menu
@router.message(Command("menu", "help"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_long(message.bot, message.chat.id, _help_text(), main_keyboard())


@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(callback, _welcome_text(), main_keyboard())


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback, _help_text(), main_keyboard())


def _welcome_text() -> str:
    return (
        "🧠 <b>Личный секретарь на связи</b>\n\n"
        "Нажми кнопку или отправь текст, голос, фото, документ. "
        "Я разложу это по задачам, событиям, заметкам и напоминаниям."
    )


def _help_text() -> str:
    return (
        "❓ <b>Команды</b>\n\n"
        "<b>День и планы</b>\n"
        "• /today [дата] — карточка дня\n"
        "• /week — сводка на 7 дней\n"
        "• /events — события и регистрации\n"
        "• /tasks — задачи, фильтры и авто-раскладка\n"
        "• /closed — просмотр закрытых/выполненных задач\n"
        "• /now — анти-ступор: одна задача под текущие силы\n"
        "• /schedule_edit — регулярная сетка занятий и импорт\n"
        "• /archive — закрытые задачи и прошлое\n\n"
        "<b>Память и контроль</b>\n"
        "• /notes — заметки\n"
        "• /find запрос — полнотекстовый поиск по заметкам и задачам\n"
        "• /reminders — напоминания\n"
        "• /inbox — история входящих\n"
        "• /watch — мониторинг страниц\n"
        "• /stats — статистика\n\n"
        "<b>Быстрое добавление</b>\n"
        "• /task купить хлеб\n"
        "• /note мысль или ссылка\n"
        "• /remind 25.11 18:00 позвонить\n\n"
        "<b>Настройка</b>\n"
        "• /settings — город, сетка, профили дней, питание ПК, модели\n"
        "• /cancel — отменить текущий ввод\n\n"
        "💡 Любое сообщение без команды автоматически попадёт в Inbox."
    )


# --------------------------------------------------------------------------- today / week
async def _today_payload(day: date) -> tuple[str, InlineKeyboardMarkup]:
    overview = await day_overview(db(), day)
    now = await settings.now()
    weather = None
    if day >= now.date():
        weather = await weather_text_for_date(day, has_evening_event=bool(overview["events"]))
    text = format_today(overview, weather, overview["tasks"])
    prev_day = day - timedelta(days=1)
    next_day = day + timedelta(days=1)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="◀️", callback_data=f"day:{prev_day.isoformat()}"),
                InlineKeyboardButton(text="Сегодня", callback_data=f"day:{now.date().isoformat()}"),
                InlineKeyboardButton(text="▶️", callback_data=f"day:{next_day.isoformat()}"),
            ],
            [
                InlineKeyboardButton(text="📆 Неделя", callback_data="week:today"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
                InlineKeyboardButton(text="🧘 Сейчас", callback_data="now:menu"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
        ]
    )
    return text, keyboard


@router.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext) -> None:
    await state.clear()
    tz = await settings.timezone()
    arg = _command_arg(message)
    day = parse_date(arg, tz) if arg else (await settings.now()).date()
    if day is None:
        day = (await settings.now()).date()
    text, keyboard = await _today_payload(day)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data.startswith("day:"))
async def cb_day(callback: CallbackQuery) -> None:
    try:
        day = date.fromisoformat(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    await callback.answer()
    text, keyboard = await _today_payload(day)
    await safe_edit(callback, text, keyboard)


async def _week_payload(start: date) -> tuple[str, InlineKeyboardMarkup]:
    data = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        overview = await day_overview(db(), day)
        free_minutes = sum(
            int((end - begin).total_seconds() // 60)
            for begin, end in overview["free_windows"]
        )
        open_tasks = [task for task in overview["tasks"] if task["status"] == "todo"]
        data.append(
            {
                "date": day,
                "profile": overview["profile"],
                "free_minutes": free_minutes,
                "event_count": len(overview["events"]),
                "task_count": len(open_tasks),
                "events": overview["events"],
                "paused": overview["paused"],
            }
        )
    text = format_week(start, data)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️", callback_data=f"week:start:{(start - timedelta(days=7)).isoformat()}"
                ),
                InlineKeyboardButton(text="Сегодня", callback_data="week:today"),
                InlineKeyboardButton(
                    text="▶️", callback_data=f"week:start:{(start + timedelta(days=7)).isoformat()}"
                ),
            ],
            [
                InlineKeyboardButton(text="📅 День", callback_data="menu:today"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
        ]
    )
    return text, keyboard


@router.message(Command("week"))
async def cmd_week(message: Message, state: FSMContext) -> None:
    await state.clear()
    tz = await settings.timezone()
    arg = _command_arg(message)
    start = parse_date(arg, tz) if arg else (await settings.now()).date()
    if start is None:
        start = (await settings.now()).date()
    text, keyboard = await _week_payload(start)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data == "week:today")
async def cb_week_today(callback: CallbackQuery) -> None:
    await callback.answer()
    now = await settings.now()
    text, keyboard = await _week_payload(now.date())
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("week:start:"))
async def cb_week_start(callback: CallbackQuery) -> None:
    try:
        start = date.fromisoformat(callback.data.split(":")[2])
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    await callback.answer()
    text, keyboard = await _week_payload(start)
    await safe_edit(callback, text, keyboard)


# --------------------------------------------------------------------------- events
async def _events_payload(scope: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    now = await settings.now()
    scope = scope if scope in {"upcoming", "past", "all"} else "upcoming"
    limit = 5
    total = await db().count_events(scope, to_db(now))
    rows = await db().list_events(scope, limit=limit, offset=offset, now_db=to_db(now))
    text = format_events(scope, rows, now, total)
    if total > limit:
        pages = (total + limit - 1) // limit
        page = offset // limit + 1
        text += f"\n\n<i>Страница {page}/{pages}</i>"
    filters = []
    for value, label in (("upcoming", "Предстоящие"), ("past", "Прошедшие"), ("all", "Все")):
        filters.append(
            InlineKeyboardButton(
                text=("✅ " if scope == value else "") + label,
                callback_data=f"events:list:{value}:0",
            )
        )
    keyboard_rows = [filters]
    for index, event in enumerate(rows, start=offset + 1):
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {index}. {clip(event['title'], 34)}",
                    callback_data=f"event:view:{event['id']}",
                )
            ]
        )
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"events:list:{scope}:{max(0, offset - limit)}"
            )
        )
    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"events:list:{scope}:{offset + limit}"
            )
        )
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append(
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="menu:today"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@router.message(Command("events"))
async def cmd_events(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _events_payload("upcoming", 0)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data.startswith("events:list:"))
async def cb_events_list(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    scope = parts[2]
    offset = max(0, int(parts[3])) if len(parts) > 3 and parts[3].isdigit() else 0
    await callback.answer()
    text, keyboard = await _events_payload(scope, offset)
    await safe_edit(callback, text, keyboard)


async def _event_detail(event_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    event = await db().get_event(event_id)
    if event is None:
        return None
    now = await settings.now()
    reg_labels = {
        "not_needed": "⚪️ Регистрация не нужна",
        "pending": "🟡 Регистрация ожидает",
        "done": "✅ Зарегистрирован",
    }
    rows = [
        [InlineKeyboardButton(text=reg_labels.get(str(event["reg_status"]), "⚪️ Регистрация"), callback_data=f"event:reg:{event_id}")],
    ]
    if event["location_or_url"] and str(event["location_or_url"]).startswith("http"):
        rows.append(
            [InlineKeyboardButton(text="🔗 Открыть ссылку", url=str(event["location_or_url"]))]
        )
    if event["source_url"] and str(event["source_url"]).startswith("http"):
        rows.append([InlineKeyboardButton(text="🌐 Источник", url=str(event["source_url"]))])
    rows.append(
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"event:delask:{event_id}"),
            InlineKeyboardButton(text="⬅️ К событиям", callback_data="events:list:upcoming:0"),
        ]
    )
    return f"📅 <b>Событие</b>\n\n{format_event(event, now, detailed=True)}", InlineKeyboardMarkup(
        inline_keyboard=rows
    )


@router.callback_query(F.data.startswith("event:view:"))
async def cb_event_view(callback: CallbackQuery) -> None:
    event_id = int(callback.data.split(":")[2])
    payload = await _event_detail(event_id)
    if payload is None:
        await callback.answer("Событие не найдено", show_alert=True)
        return
    await callback.answer()
    text, keyboard = payload
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("event:reg:"))
async def cb_event_reg(callback: CallbackQuery) -> None:
    event_id = int(callback.data.split(":")[2])
    event = await db().get_event(event_id)
    if event is None:
        await callback.answer("Событие не найдено", show_alert=True)
        return
    order = ["not_needed", "pending", "done"]
    current = str(event["reg_status"])
    new_status = order[(order.index(current) + 1) % len(order)] if current in order else "pending"
    await db().update_event(event_id, reg_status=new_status)
    await callback.answer(f"Регистрация: {new_status}")
    payload = await _event_detail(event_id)
    if payload:
        await safe_edit(callback, payload[0], payload[1])


@router.callback_query(F.data.startswith("event:delask:"))
async def cb_event_delask(callback: CallbackQuery) -> None:
    event_id = int(callback.data.split(":")[2])
    event = await db().get_event(event_id)
    if event is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback,
        f"🗑 Удалить событие <b>{esc(event['title'])}</b>?\n\n"
        "Это действие нельзя отменить.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"event:del:{event_id}")],
                [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"event:view:{event_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("event:del:"))
async def cb_event_del(callback: CallbackQuery) -> None:
    event_id = int(callback.data.split(":")[2])
    await db().delete_event(event_id)
    await callback.answer("Удалено")
    text, keyboard = await _events_payload("upcoming", 0)
    await safe_edit(callback, "🗑 Событие удалено.\n\n" + text, keyboard)


# --------------------------------------------------------------------------- tasks
async def _counts_context() -> tuple[dict[str, int], str, str, str]:
    now = await settings.now()
    counts = await db().task_counts_by_status()
    now_db = to_db(now)
    today_db = to_db_date(now.date())
    week_end = to_db_date(now.date() + timedelta(days=7))
    return counts, now_db, today_db, week_end


async def _tasks_payload(
    filter_name: str, offset: int, search: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    filter_name = _task_filter(filter_name)
    offset = max(0, int(offset))
    limit = 6
    counts, now_db, today_db, week_end_db = await _counts_context()
    now = await settings.now()
    total = await db().count_tasks(
        filter_name, now_db, today_db, week_end_db, search=search
    )
    tasks = await db().list_tasks(
        filter_name, limit=limit, offset=offset,
        now_db=now_db, today_db=today_db, week_end_db=week_end_db,
        search=search,
    )
    closed_count = counts.get("done", 0) + counts.get("frozen", 0)
    text = (
        f"📋 <b>Задачи · {TASK_FILTER_NAMES.get(filter_name, filter_name)}</b>\n"
        f"Активных: <b>{counts.get('todo', 0)}</b> · выполнено: <b>{counts.get('done', 0)}</b> · "
        f"заморожено: <b>{counts.get('frozen', 0)}</b> · закрытых: <b>{closed_count}</b>\n"
    )
    if search:
        text += f"🔍 Фильтр: «<b>{esc(search)}</b>» (найдено: {total})\n"
    elif total:
        text += f"Показано: {offset + 1}–{offset + len(tasks)} из {total}\n"
    text += "\n" + format_tasks_list(filter_name, tasks, now)

    filter_rows = [
        [
            InlineKeyboardButton(
                text=("• " if filter_name == "active" else "") + "Активные",
                callback_data="t:list:active:0",
            ),
            InlineKeyboardButton(
                text=("• " if filter_name == "today" else "") + "Сегодня",
                callback_data="t:list:today:0",
            ),
            InlineKeyboardButton(
                text=("• " if filter_name == "overdue" else "") + "Просрочено",
                callback_data="t:list:overdue:0",
            ),
        ],
        [
            InlineKeyboardButton(
                text=("• " if filter_name == "done" else "") + "Выполненные",
                callback_data="t:list:done:0",
            ),
            InlineKeyboardButton(
                text=("• " if filter_name == "frozen" else "") + "Замороженные",
                callback_data="t:list:frozen:0",
            ),
            InlineKeyboardButton(
                text=("• " if filter_name == "closed" else "") + "Все закрытые",
                callback_data="t:list:closed:0",
            ),
        ],
        [
            InlineKeyboardButton(
                text=("• " if filter_name == "all" else "") + "Все задачи",
                callback_data="t:list:all:0",
            ),
            InlineKeyboardButton(
                text="🔍 Поиск",
                callback_data=f"t:search:{filter_name}",
            ),
        ],
    ]
    task_rows = []
    if search:
        task_rows.append(
            [InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data=f"t:clear:{filter_name}:0")]
        )
    for index, task in enumerate(tasks, start=offset + 1):
        task_rows.append(
            [
                InlineKeyboardButton(
                    text=f"⋯ {index}. {clip(task['title'], 28)}",
                    callback_data=f"t:view:{task['id']}:{filter_name}:{offset}",
                ),
                InlineKeyboardButton(
                    text="✅" if task["status"] == "todo" else "↩️",
                    callback_data=(
                        f"t:done:{task['id']}:{filter_name}:{offset}"
                        if task["status"] == "todo"
                        else f"t:restore:{task['id']}:{filter_name}:{offset}"
                    ),
                ),
            ]
        )
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"t:list:{filter_name}:{max(0, offset - limit)}"
            )
        )
    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"t:list:{filter_name}:{offset + limit}"
            )
        )
    if nav:
        task_rows.append(nav)
    task_rows.append(
        [
            InlineKeyboardButton(text="📅 Авто-раскидать", callback_data=f"t:autoall:{filter_name}:{offset}"),
            InlineKeyboardButton(text="🗃 Архив", callback_data="arch:menu"),
        ]
    )
    back = "arch:menu" if filter_name in {"done", "frozen", "closed"} else "menu:main"
    task_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data=back)])
    return text, InlineKeyboardMarkup(inline_keyboard=filter_rows + task_rows)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    arg = _command_arg(message).strip()
    filter_name = "active"
    search = None
    if arg:
        parts = arg.split(maxsplit=1)
        first = parts[0].lower()
        if first in {"active", "today", "week", "overdue", "unscheduled", "done", "frozen", "all", "closed"}:
            filter_name = _task_filter(first)
            if len(parts) > 1:
                search = parts[1].strip()
        else:
            search = arg
    if search:
        await state.update_data(task_search=search)
    text, keyboard = await _tasks_payload(filter_name, 0, search=search)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.message(Command("closed", "done"))
async def cmd_closed_tasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    arg = _command_arg(message).strip()
    search = arg if arg else None
    if search:
        await state.update_data(task_search=search)
    text, keyboard = await _tasks_payload("done", 0, search=search)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data.startswith("t:clear:"))
async def cb_tasks_clear_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(task_search=None)
    parts = callback.data.split(":")
    filter_name = _task_filter(parts[2])
    await callback.answer()
    text, keyboard = await _tasks_payload(filter_name, 0, search=None)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("t:list:"))
async def cb_tasks_list(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    filter_name = _task_filter(parts[2])
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    data = await state.get_data()
    search = data.get("task_search")
    await callback.answer()
    text, keyboard = await _tasks_payload(filter_name, offset, search=search)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("t:search:"))
async def cb_task_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    filter_name = _task_filter(callback.data.split(":")[2])
    await state.set_state(ViewStates.task_search)
    await state.update_data(filter_name=filter_name)
    await callback.answer()
    await safe_edit(
        callback,
        f"🔍 <b>Поиск по задачам ({TASK_FILTER_NAMES.get(filter_name, filter_name)})</b>\n\n"
        "Отправь поисковый запрос (по названию или первому шагу):",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"t:list:{filter_name}:0")]
            ]
        ),
    )


@router.message(ViewStates.task_search, F.text)
async def task_search_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    filter_name = _task_filter(str(data.get("filter_name", "active")))
    query = (message.text or "").strip()
    await state.set_state(None)
    await state.update_data(task_search=query, filter_name=filter_name)
    text, keyboard = await _tasks_payload(filter_name, 0, search=query)
    await send_long(message.bot, message.chat.id, text, keyboard)


def _task_detail_keyboard(task_id: int, status: str, filter_name: str, offset: int) -> InlineKeyboardMarkup:
    ctx = f"{filter_name}:{offset}"
    if status == "todo":
        rows = [
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"t:done:{task_id}:{ctx}"),
                InlineKeyboardButton(text="⏭ На завтра", callback_data=f"t:tom:{task_id}:{ctx}"),
            ],
            [
                InlineKeyboardButton(text="📅 В свободное окно", callback_data=f"t:auto:{task_id}:{ctx}"),
                InlineKeyboardButton(text="🧊 Заморозить", callback_data=f"t:freeze:{task_id}:{ctx}"),
            ],
            [
                InlineKeyboardButton(text="🗓 Перенести", callback_data=f"t:plan:{task_id}:{ctx}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"t:delask:{task_id}:{ctx}"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="♻️ Вернуть в работу", callback_data=f"t:restore:{task_id}:{ctx}"),
                InlineKeyboardButton(text="📋 Повторить задачу", callback_data=f"t:clone:{task_id}:{ctx}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"t:delask:{task_id}:{ctx}"),
            ],
        ]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ К задачам", callback_data=f"t:list:{filter_name}:{offset}"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _task_detail_payload(task_id: int, filter_name: str, offset: int) -> tuple[str, InlineKeyboardMarkup] | None:
    task = await db().get_task(task_id)
    if task is None:
        return None
    now = await settings.now()
    return (
        format_task_detail(task, now),
        _task_detail_keyboard(task_id, str(task["status"]), filter_name, offset),
    )


@router.callback_query(F.data.startswith("t:view:"))
async def cb_task_view(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    payload = await _task_detail_payload(task_id, filter_name, offset)
    if payload is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, payload[0], payload[1])


@router.callback_query(F.data.startswith("t:done:"))
async def cb_task_done(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await db().complete_task(task_id)
    await callback.answer("✅ Готово")
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, f"✅ <b>{esc(task['title'])}</b> — закрыто.\n\n{text}", keyboard)


@router.callback_query(F.data.startswith("t:restore:"))
async def cb_task_restore(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await db().set_task_status(task_id, "todo")
    await callback.answer("♻️ Возвращено в работу")
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, f"♻️ <b>{esc(task['title'])}</b> снова активна.\n\n{text}", keyboard)


@router.callback_query(F.data.startswith("t:clone:"))
async def cb_task_clone(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    new_id = await db().create_task(
        title=task["title"],
        category=task["category"],
        energy=task["energy"],
        est_minutes=task["est_minutes"],
        first_step=task["first_step"],
        source_url=task["source_url"],
        status="todo",
    )
    await callback.answer("📋 Задача повторена!")
    text, keyboard = await _tasks_payload("active", 0)
    await safe_edit(
        callback,
        f"📋 <b>Создана новая задача</b> на основе закрытой:\n«{esc(task['title'])}» (ID: #{new_id})\n\n{text}",
        keyboard,
    )


@router.callback_query(F.data.startswith("t:freeze:"))
async def cb_task_freeze(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await db().set_task_status(task_id, "frozen")
    await callback.answer("🧊 Заморожено")
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, f"🧊 <b>{esc(task['title'])}</b> заморожена.\n\n{text}", keyboard)


@router.callback_query(F.data.startswith("t:tom:"))
async def cb_task_tomorrow(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    now = await settings.now()
    tomorrow = now.date() + timedelta(days=1)
    await db().update_task(task_id, scheduled_date=to_db_date(tomorrow), scheduled_time=None)
    await callback.answer(f"⏭ На {fmt_date(tomorrow, False)}")
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("t:autoall:"))
async def cb_task_autoall(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    filter_name = _task_filter(parts[2]) if len(parts) > 2 else "active"
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    await callback.answer("Раскладываю…")
    count = await auto_schedule_tasks(db(), days=14)
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, f"📅 Разложено задач: <b>{count}</b>.\n\n{text}", keyboard)


@router.callback_query(F.data.startswith("t:auto:"))
async def cb_task_auto(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    slot = await find_slot_for_task(db(), task_id)
    if slot is None:
        await callback.answer("Не нашёл подходящее окно за 14 дней", show_alert=True)
        return
    await callback.answer(f"📅 {slot.strftime('%d.%m %H:%M')}", show_alert=True)
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("t:plan:"))
async def cb_task_plan(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await state.set_state(ViewStates.task_plan)
    await state.update_data(task_id=task_id, filter=filter_name, offset=offset)
    await callback.answer()
    await safe_edit(
        callback,
        f"🗓 <b>Перенос задачи</b>\n\n<b>{esc(task['title'])}</b>\n\n"
        "Отправь дату и время: <code>25.11 18:00</code> или <code>2026-11-25 18:00</code>.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✖️ Отмена",
                        callback_data=f"t:view:{task_id}:{filter_name}:{offset}",
                    )
                ]
            ]
        ),
    )


@router.message(ViewStates.task_plan, F.text)
async def task_plan_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    tz = await settings.timezone()
    parsed = parse_datetime(text, tz)
    if parsed is None:
        await message.answer("Не понял дату. Пример: <code>25.11 18:00</code>.")
        return
    data = await state.get_data()
    task_id = int(data.get("task_id", 0))
    filter_name = _task_filter(str(data.get("filter", "active")))
    offset = int(data.get("offset", 0))
    await db().update_task(
        task_id,
        scheduled_date=to_db_date(parsed.date()),
        scheduled_time=parsed.strftime("%H:%M"),
    )
    await state.clear()
    payload = await _task_detail_payload(task_id, filter_name, offset)
    if payload:
        await send_long(message.bot, message.chat.id, payload[0], payload[1])


@router.callback_query(F.data.startswith("t:delask:"))
async def cb_task_delask(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback,
        f"🗑 Удалить задачу <b>{esc(task['title'])}</b>?\n\nЭто действие нельзя отменить.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить",
                        callback_data=f"t:del:{task_id}:{filter_name}:{offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✖️ Отмена",
                        callback_data=f"t:view:{task_id}:{filter_name}:{offset}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("t:del:"))
async def cb_task_del(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[2])
    filter_name = _task_filter(parts[3]) if len(parts) > 3 else "active"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    await db().delete_task(task_id)
    await callback.answer("Удалено")
    text, keyboard = await _tasks_payload(filter_name, offset)
    await safe_edit(callback, "🗑 Задача удалена.\n\n" + text, keyboard)


# --------------------------------------------------------------------------- anti-stupor
def _now_menu() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "🧘 <b>Анти-ступор</b>\n\n"
        "Как ты сейчас? Выбери честно — я дам ровно одну задачу "
        "и конкретный первый шаг на 15 минут.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🟢 Голова варит — сложное", callback_data="now:pick:high")],
                [InlineKeyboardButton(text="🟡 Устал — среднее", callback_data="now:pick:medium")],
                [InlineKeyboardButton(text="🔴 Нет сил — маленькое", callback_data="now:pick:low")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
            ]
        ),
    )


@router.message(Command("now"))
async def cmd_now(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = _now_menu()
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "now:menu")
async def cb_now_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = _now_menu()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("now:pick:"))
async def cb_now_pick(callback: CallbackQuery) -> None:
    level = callback.data.split(":")[2]
    task = await pick_task_for_energy(db(), level)
    await callback.answer()
    if task is None:
        await safe_edit(
            callback,
            "🎉 <b>Активных задач нет.</b>\n\nМожно отдохнуть, погулять или заглянуть в архив.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🗃 Архив", callback_data="arch:menu")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
                ]
            ),
        )
        return
    now = await settings.now()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"now:done:{task['id']}"),
                InlineKeyboardButton(text="⏱ Таймер 20 мин", callback_data=f"now:timer:{task['id']}"),
            ],
            [
                InlineKeyboardButton(text="⏭ Другую", callback_data=f"now:other:{level}:{task['id']}"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ],
        ]
    )
    await safe_edit(callback, format_now_task(task, now), keyboard)


@router.callback_query(F.data.startswith("now:other:"))
async def cb_now_other(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    level = parts[2]
    exclude = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    task = await pick_task_for_energy(db(), level, exclude_id=exclude)
    await callback.answer()
    if task is None:
        await safe_edit(
            callback,
            "Больше подходящих задач нет. Отдохни 10 минут — это тоже план.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Все задачи", callback_data="menu:tasks")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
                ]
            ),
        )
        return
    now = await settings.now()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"now:done:{task['id']}"),
                InlineKeyboardButton(text="⏱ Таймер 20 мин", callback_data=f"now:timer:{task['id']}"),
            ],
            [
                InlineKeyboardButton(text="⏭ Другую", callback_data=f"now:other:{level}:{task['id']}"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ],
        ]
    )
    await safe_edit(callback, format_now_task(task, now), keyboard)


@router.callback_query(F.data.startswith("now:done:"))
async def cb_now_done(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await db().complete_task(task_id)
    await callback.answer("✅ Отлично!")
    await safe_edit(
        callback,
        f"✅ <b>{esc(task['title'])}</b> — сделано.\n\n"
        "Один шаг — уже движение. Что дальше?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧘 Ещё одну", callback_data="now:menu")],
                [InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
            ]
        ),
    )


async def _timer_finished(bot: Any, chat_id: int, task_id: int) -> None:
    task = await db().get_task(task_id)
    if task is None:
        return
    await send_long(
        bot,
        chat_id,
        f"⏱ <b>20 минут прошли.</b>\n\nЗадача: <b>{esc(task['title'])}</b>\n\n"
        "Как успехи?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сделано", callback_data=f"now:done:{task_id}")],
                [InlineKeyboardButton(text="🔁 Ещё 20 минут", callback_data=f"now:timer:{task_id}")],
                [InlineKeyboardButton(text="🧘 Выбрать другую", callback_data="now:menu")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("now:timer:"))
async def cb_now_timer(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await db().get_task(task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    now = await settings.now()
    if runtime.scheduler is not None:
        runtime.scheduler.add_job(
            _timer_finished,
            "date",
            run_date=now + timedelta(minutes=20),
            args=[callback.bot, callback.message.chat.id, task_id],
            id=f"timer:{task_id}:{int(now.timestamp())}",
            replace_existing=True,
            misfire_grace_time=600,
        )
    await callback.answer("⏱ Таймер на 20 минут пошёл", show_alert=True)


# --------------------------------------------------------------------------- watch
def _normalize_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


async def _watch_payload() -> tuple[str, InlineKeyboardMarkup]:
    items = await db().list_watched()
    now = await settings.now()
    lines = ["👁 <b>Мониторинг страниц</b>", ""]
    if not items:
        lines.append(
            "Пока пусто. Добавь страницу:\n"
            "<code>/watch https://example.com/olympiads новые олимпиады</code>"
        )
    for item in items:
        status = "🟢" if item["is_active"] else "⏸"
        lines.append(f"{status} <b>#{item['id']}</b>")
        lines.append(
            f"🔗 {esc(item['url'])}\n"
            f"🎯 {esc(item['goal'])}\n"
            f"🕒 каждые {item['interval_hours']} ч · {esc(item['last_checked'] or 'ещё не проверялось')}"
        )
        if item["last_summary"]:
            lines.append(f"📝 {esc(clip(item['last_summary'], 200))}")
        lines.append("")
    keyboard_rows = []
    for item in items[:10]:
        toggle = "⏸" if item["is_active"] else "▶️"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{toggle} #{item['id']}", callback_data=f"watch:toggle:{item['id']}"
                ),
                InlineKeyboardButton(text="🔄 Проверить", callback_data=f"watch:check:{item['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"watch:delask:{item['id']}"),
            ]
        )
    keyboard_rows.append(
        [
            InlineKeyboardButton(text="📥 Inbox", callback_data="inbox:list:0"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


async def _add_watch(url: str, criterion: str, chat_id: int, bot: Any) -> None:
    interval = int(await settings.get("watch_interval_hours") or 2)
    watch_id = await db().create_watched(url, criterion, interval_hours=interval)
    await send_long(
        bot,
        chat_id,
        f"👁 <b>Взял под наблюдение #{watch_id}</b>\n\n"
        f"🔗 {esc(url)}\n🎯 {esc(criterion)}\n"
        f"🕒 Проверка каждые {interval} ч.\n\n"
        "Первая проверка создаст baseline, уведомление придёт только при изменении, "
        "соответствующем критерию.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data=f"watch:check:{watch_id}")],
                [InlineKeyboardButton(text="👁 Все страницы", callback_data="watch:list")],
            ]
        ),
    )


@router.message(Command("watch"))
async def cmd_watch(message: Message, state: FSMContext) -> None:
    await state.clear()
    arg = _command_arg(message)
    if not arg:
        text, keyboard = await _watch_payload()
        await send_long(message.bot, message.chat.id, text, keyboard)
        return
    parts = arg.split(maxsplit=1)
    url = _normalize_url(parts[0])
    if len(parts) < 2 or not parts[1].strip():
        await state.set_state(ViewStates.watch_criterion)
        await state.update_data(watch_url=url)
        await message.answer(
            f"👁 <b>Страница:</b> {esc(url)}\n\n"
            "Теперь отправь критерий: что именно отслеживать? "
            "Например: <i>новые олимпиады по физике</i> или <i>изменение даты регистрации</i>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="watch:list")]
                ]
            ),
        )
        return
    await _add_watch(url, parts[1].strip(), message.chat.id, message.bot)


@router.message(ViewStates.watch_criterion, F.text)
async def watch_criterion_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    data = await state.get_data()
    url = str(data.get("watch_url") or "")
    if not url:
        await state.clear()
        await message.answer("Ссылка потерялась. Начни заново: /watch <url> <критерий>")
        return
    if len(text) < 3:
        await message.answer("Опиши критерий чуть подробнее.")
        return
    await state.clear()
    await _add_watch(url, text, message.chat.id, message.bot)


@router.callback_query(F.data == "watch:list")
async def cb_watch_list(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _watch_payload()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("watch:toggle:"))
async def cb_watch_toggle(callback: CallbackQuery) -> None:
    watch_id = int(callback.data.split(":")[2])
    item = await db().get_watched(watch_id)
    if item is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await db().set_watched_active(watch_id, not bool(item["is_active"]))
    await callback.answer("Включено" if not item["is_active"] else "Приостановлено")
    text, keyboard = await _watch_payload()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data.startswith("watch:off:"))
async def cb_watch_off(callback: CallbackQuery) -> None:
    watch_id = int(callback.data.split(":")[2])
    await db().set_watched_active(watch_id, False)
    await callback.answer("Мониторинг отключён")
    await safe_edit(
        callback,
        "⏸ Мониторинг отключён. Включить можно в /watch.",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="👁 Все страницы", callback_data="watch:list")]]
        ),
    )


@router.callback_query(F.data.startswith("watch:delask:"))
async def cb_watch_delask(callback: CallbackQuery) -> None:
    watch_id = int(callback.data.split(":")[2])
    await callback.answer()
    await safe_edit(
        callback,
        f"🗑 Удалить мониторинг <b>#{watch_id}</b>?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Удалить", callback_data=f"watch:del:{watch_id}")],
                [InlineKeyboardButton(text="✖️ Отмена", callback_data="watch:list")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("watch:del:"))
async def cb_watch_del(callback: CallbackQuery) -> None:
    watch_id = int(callback.data.split(":")[2])
    await db().delete_watched(watch_id)
    await callback.answer("Удалено")
    text, keyboard = await _watch_payload()
    await safe_edit(callback, text, keyboard)


async def _manual_watch(bot: Any, chat_id: int, watch_id: int) -> None:
    await send_long(bot, chat_id, "👁 Проверяю страницу… Это может занять до 90 секунд.")
    try:
        _ok, message = await check_watch_entry(bot, db(), watch_id, notify=False)
        await send_long(bot, chat_id, message)
    except Exception as exc:  # noqa: BLE001
        await send_long(bot, chat_id, f"❌ Ошибка проверки: {esc(exc)}")


@router.callback_query(F.data.startswith("watch:check:"))
async def cb_watch_check(callback: CallbackQuery) -> None:
    watch_id = int(callback.data.split(":")[2])
    await callback.answer("Проверяю в фоне…")
    _background(_manual_watch(callback.bot, callback.message.chat.id, watch_id))


# --------------------------------------------------------------------------- notes / find
async def _notes_payload(offset: int) -> tuple[str, InlineKeyboardMarkup]:
    limit = 8
    offset = max(0, offset)
    total = await db().count_notes()
    notes = await db().list_notes(limit=limit, offset=offset)
    lines = [f"🗒 <b>Заметки</b> · {total}", ""]
    if not notes:
        lines.append("Пока пусто. Добавь: <code>/note текст</code> или отправь документ.")
    for index, note in enumerate(notes, start=offset + 1):
        tags = f" · 🏷 {esc(note['tags'])}" if note["tags"] else ""
        lines.append(f"{index}. <b>{esc(note['title'])}</b>{tags}\n   <i>{esc(note['created_at'])}</i>")
    if total > limit:
        pages = (total + limit - 1) // limit
        lines.append(f"\n<i>Страница {offset // limit + 1}/{pages}</i>")
    rows = []
    for note in notes:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {clip(note['title'], 34)}",
                    callback_data=f"note:view:{note['id']}",
                )
            ]
        )
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"notes:list:{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"notes:list:{offset + limit}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="🔎 Поиск", callback_data="find:new"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("notes"))
async def cmd_notes(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _notes_payload(0)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data.startswith("notes:list:"))
async def cb_notes_list(callback: CallbackQuery) -> None:
    offset = int(callback.data.split(":")[2]) if callback.data.split(":")[2].isdigit() else 0
    await callback.answer()
    text, keyboard = await _notes_payload(offset)
    await safe_edit(callback, text, keyboard)


async def _note_detail(note_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    note = await db().get_note(note_id)
    if note is None:
        return None
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"note:delask:{note_id}"),
                InlineKeyboardButton(text="⬅️ К заметкам", callback_data="notes:list:0"),
            ]
        ]
    )
    return format_note(note, full=True), keyboard


@router.callback_query(F.data.startswith("note:view:"))
async def cb_note_view(callback: CallbackQuery) -> None:
    note_id = int(callback.data.split(":")[2])
    payload = await _note_detail(note_id)
    if payload is None:
        await callback.answer("Заметка не найдена", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, payload[0], payload[1])


@router.callback_query(F.data.startswith("note:delask:"))
async def cb_note_delask(callback: CallbackQuery) -> None:
    note_id = int(callback.data.split(":")[2])
    note = await db().get_note(note_id)
    if note is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback,
        f"🗑 Удалить заметку <b>{esc(note['title'])}</b>?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Удалить", callback_data=f"note:del:{note_id}")],
                [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"note:view:{note_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("note:del:"))
async def cb_note_del(callback: CallbackQuery) -> None:
    note_id = int(callback.data.split(":")[2])
    await db().delete_note(note_id)
    await callback.answer("Удалено")
    text, keyboard = await _notes_payload(0)
    await safe_edit(callback, text, keyboard)


async def _find_payload(query: str) -> tuple[str, InlineKeyboardMarkup]:
    notes = await db().search_notes(query, limit=8)
    tasks = await db().list_tasks("all", search=query, limit=6)
    total_found = len(notes) + len(tasks)
    lines = [f"🔎 <b>Поиск:</b> {esc(query)}", f"Найдено совпадений: <b>{total_found}</b>", ""]
    if not notes and not tasks:
        lines.append("Ничего не найдено. Попробуй другие слова или проверь /tasks и /notes.")
    rows: list[list[InlineKeyboardButton]] = []
    if notes:
        lines.append("<b>🗒 Заметки:</b>")
        for index, note in enumerate(notes, start=1):
            lines.append(f"{index}. " + format_note_search(note))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📄 {index}. {clip(note['title'], 28)}",
                        callback_data=f"note:view:{note['id']}",
                    )
                ]
            )
        lines.append("")
    if tasks:
        now = await settings.now()
        lines.append("<b>📋 Задачи (активные и закрытые):</b>")
        for index, task in enumerate(tasks, start=1):
            lines.append(format_task_compact(task, now))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📋 {index}. {clip(task['title'], 28)}",
                        callback_data=f"t:view:{task['id']}:all:0",
                    )
                ]
            )
        lines.append("")
    rows.append(
        [
            InlineKeyboardButton(text="🔎 Новый поиск", callback_data="find:new"),
            InlineKeyboardButton(text="🗒 Заметки", callback_data="notes:list:0"),
            InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
        ]
    )
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext) -> None:
    arg = _command_arg(message)
    if not arg:
        await state.set_state(ViewStates.find_query)
        await message.answer(
            "🔎 <b>Поиск по заметкам</b>\n\nОтправь запрос — ищу по заголовкам, тексту и тегам (FTS5).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:main")]
                ]
            ),
        )
        return
    await state.clear()
    text, keyboard = await _find_payload(arg)
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data == "find:new")
async def cb_find_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ViewStates.find_query)
    await safe_edit(
        callback,
        "🔎 <b>Новый поиск</b>\n\nОтправь запрос по заметкам.",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="notes:list:0")]]
        ),
    )


@router.message(ViewStates.find_query, F.text)
async def find_query_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if len(text) < 2:
        await message.answer("Запрос слишком короткий.")
        return
    await state.clear()
    text_result, keyboard = await _find_payload(text)
    await send_long(message.bot, message.chat.id, text_result, keyboard)


# --------------------------------------------------------------------------- reminders
async def _reminders_payload() -> tuple[str, InlineKeyboardMarkup]:
    now = await settings.now()
    pending = await db().list_reminders(pending_only=True, limit=10)
    sent = await db().fetchall(
        "SELECT * FROM reminders WHERE is_sent=1 ORDER BY remind_at DESC LIMIT 5"
    )
    lines = [f"⏰ <b>Напоминания</b> · активных: {len(pending)}", ""]
    if not pending:
        lines.append("Активных напоминаний нет. Добавь через Inbox или /remind.")
    for reminder in pending:
        lines.append(format_reminder(reminder, now))
        lines.append("")
    if sent:
        lines.extend(["<b>Недавно отправленные</b>"])
        for reminder in sent:
            lines.append("✅ " + esc(clip(reminder["text"], 80)) + f" · {esc(reminder['remind_at'])}")
    rows = []
    for reminder in pending:
        rows.append(
            [
                InlineKeyboardButton(text="✅", callback_data=f"rem:done:{reminder['id']}:l"),
                InlineKeyboardButton(text="⏰ +30", callback_data=f"rem:snooze:30:{reminder['id']}:l"),
                InlineKeyboardButton(text="⏰ +2ч", callback_data=f"rem:snooze:120:{reminder['id']}:l"),
                InlineKeyboardButton(text="🗑", callback_data=f"rem:del:{reminder['id']}:l"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📥 Inbox", callback_data="inbox:list:0"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _reminders_payload()
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data == "reminders:list")
async def cb_reminders_list(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _reminders_payload()
    await safe_edit(callback, text, keyboard)


async def _finish_reminder_message(callback: CallbackQuery, text: str) -> None:
    await safe_edit(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⏰ Все напоминания", callback_data="reminders:list")]]
        ),
    )


@router.callback_query(F.data.startswith("rem:done:"))
async def cb_rem_done(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    reminder_id = int(parts[2])
    context = parts[3] if len(parts) > 3 else "n"
    reminder = await db().get_reminder(reminder_id)
    if reminder is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    if reminder["task_id"]:
        await db().complete_task(int(reminder["task_id"]))
    await db().mark_reminder_sent(reminder_id)
    await callback.answer("✅ Готово")
    if context == "l":
        text, keyboard = await _reminders_payload()
        await safe_edit(callback, text, keyboard)
    else:
        await _finish_reminder_message(callback, f"✅ Напоминание закрыто: {esc(reminder['text'])}")


@router.callback_query(F.data.startswith("rem:snooze:"))
async def cb_rem_snooze(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    minutes = int(parts[2])
    reminder_id = int(parts[3])
    context = parts[4] if len(parts) > 4 else "n"
    reminder = await db().get_reminder(reminder_id)
    if reminder is None:
        await callback.answer("Уже удалено", show_alert=True)
        return
    now = await settings.now()
    target = now + timedelta(minutes=minutes)
    await db.snooze_reminder(reminder_id, to_db(target))
    await callback.answer(f"⏰ +{minutes} мин")
    if context == "l":
        text, keyboard = await _reminders_payload()
        await safe_edit(callback, text, keyboard)
    else:
        await _finish_reminder_message(
            callback,
            f"⏰ Напомню снова в <b>{target.strftime('%H:%M')}</b>.\n\n{esc(reminder['text'])}",
        )


@router.callback_query(F.data.startswith("rem:del:"))
async def cb_rem_del(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    reminder_id = int(parts[2])
    context = parts[3] if len(parts) > 3 else "n"
    await db().delete_reminder(reminder_id)
    await callback.answer("Удалено")
    if context == "l":
        text, keyboard = await _reminders_payload()
        await safe_edit(callback, text, keyboard)
    else:
        await _finish_reminder_message(callback, "🗑 Напоминание удалено.")


# --------------------------------------------------------------------------- archive / stats
async def _archive_stats() -> dict[str, Any]:
    stats = await db().stats()
    now = await settings.now()
    stats["events_past"] = await db().count_events("past", to_db(now))
    return stats


async def _archive_payload() -> tuple[str, InlineKeyboardMarkup]:
    stats = await _archive_stats()
    text = format_archive_menu(stats)
    closed_all = stats.get("tasks_done", 0) + stats.get("tasks_frozen", 0)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Выполненные ({stats.get('tasks_done', 0)})",
                    callback_data="t:list:done:0",
                ),
                InlineKeyboardButton(
                    text=f"🧊 Замороженные ({stats.get('tasks_frozen', 0)})",
                    callback_data="t:list:frozen:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"📁 Все закрытые ({closed_all})",
                    callback_data="t:list:closed:0",
                ),
                InlineKeyboardButton(
                    text=f"📅 Прошедшие ({stats.get('events_past', 0)})",
                    callback_data="events:list:past:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🗒 Заметки ({stats.get('notes', 0)})",
                    callback_data="notes:list:0",
                ),
                InlineKeyboardButton(
                    text=f"📥 Inbox ({stats.get('inbox_total', 0)})",
                    callback_data="inbox:list:0",
                ),
            ],
            [
                InlineKeyboardButton(text="🔍 Поиск в архиве", callback_data="arch:find"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
            ],
            [InlineKeyboardButton(text="🧹 Очистить старые (>30д)", callback_data="arch:cleanup")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
        ]
    )
    return text, keyboard


@router.callback_query(F.data == "arch:find")
async def cb_arch_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ViewStates.find_query)
    await callback.answer()
    await safe_edit(
        callback,
        "🔍 <b>Поиск в архиве</b>\n\nОтправь поисковый запрос (поиск по заметкам и задачам, включая закрытые):",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✖️ Отмена", callback_data="arch:menu")]
            ]
        ),
    )


@router.message(Command("archive"))
async def cmd_archive(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _archive_payload()
    await send_long(message.bot, message.chat.id, text, keyboard)


@router.callback_query(F.data == "arch:menu")
async def cb_archive(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _archive_payload()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data == "arch:cleanup")
async def cb_archive_cleanup(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(
        callback,
        "🧹 <b>Очистка архива</b>\n\n"
        "Удалить выполненные задачи, закрытые более 30 дней назад?\n"
        "Прошедшие события и заметки останутся.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, очистить", callback_data="arch:cleanup:confirm")],
                [InlineKeyboardButton(text="✖️ Отмена", callback_data="arch:menu")],
            ]
        ),
    )


@router.callback_query(F.data == "arch:cleanup:confirm")
async def cb_archive_cleanup_confirm(callback: CallbackQuery) -> None:
    cutoff = (await settings.now()).date() - timedelta(days=30)
    count = await db().delete_old_done_tasks(cutoff.isoformat() + " 00:00")
    await callback.answer(f"Удалено: {count}")
    text, keyboard = await _archive_payload()
    await safe_edit(callback, f"🧹 Удалено старых задач: <b>{count}</b>.\n\n{text}", keyboard)


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _stats_payload()
    await send_long(message.bot, message.chat.id, text, keyboard)


async def _stats_payload() -> tuple[str, InlineKeyboardMarkup]:
    stats = await _archive_stats()
    text = format_stats(stats)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗃 Архив", callback_data="arch:menu"),
                InlineKeyboardButton(text="📋 Задачи", callback_data="menu:tasks"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
        ]
    )
    return text, keyboard


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _stats_payload()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data == "menu:today")
async def cb_menu_today(callback: CallbackQuery) -> None:
    await callback.answer()
    now = await settings.now()
    text, keyboard = await _today_payload(now.date())
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data == "menu:tasks")
async def cb_menu_tasks(callback: CallbackQuery) -> None:
    await callback.answer()
    text, keyboard = await _tasks_payload("active", 0)
    await safe_edit(callback, text, keyboard)


# --------------------------------------------------------------------------- quick capture
async def _create_quick_task(title: str, chat_id: int, bot: Any) -> None:
    clean = title.strip()
    if not clean:
        return
    task_id = await db().create_task(
        title=clip(clean, 160),
        category="personal",
        energy="medium",
        est_minutes=30,
        first_step=None,
    )
    payload = await _task_detail_payload(task_id, "active", 0)
    if payload:
        await send_long(bot, chat_id, payload[0], payload[1])


@router.message(Command("task"))
async def cmd_task(message: Message, state: FSMContext) -> None:
    arg = _command_arg(message)
    if not arg:
        await state.set_state(ViewStates.quick_task)
        await message.answer(
            "📋 <b>Новая задача</b>\n\nОтправь название задачи. Время и детали можно добавить позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:tasks")]]
            ),
        )
        return
    await state.clear()
    await _create_quick_task(arg, message.chat.id, message.bot)


@router.message(ViewStates.quick_task, F.text)
async def quick_task_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    await state.clear()
    await _create_quick_task(text, message.chat.id, message.bot)


async def _create_quick_note(text: str, chat_id: int, bot: Any) -> None:
    content = text.strip()
    title = title_from_text(content)
    note_id = await db().create_note(title=title, content=content)
    payload = await _note_detail(note_id)
    if payload:
        await send_long(bot, chat_id, payload[0], payload[1])


@router.message(Command("note"))
async def cmd_note(message: Message, state: FSMContext) -> None:
    arg = _command_arg(message)
    if not arg:
        await state.set_state(ViewStates.quick_note)
        await message.answer(
            "🗒 <b>Новая заметка</b>\n\nОтправь текст — сохраню и проиндексирую для поиска.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="notes:list:0")]]
            ),
        )
        return
    await state.clear()
    await _create_quick_note(arg, message.chat.id, message.bot)


@router.message(ViewStates.quick_note, F.text)
async def quick_note_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    await state.clear()
    await _create_quick_note(text, message.chat.id, message.bot)


REMIND_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}|\d{2}\.\d{2}(?:\.\d{4})?\s+\d{1,2}:\d{2}|\d{1,2}:\d{2})\s+(.+)$"
)


def parse_remind_arg(text: str, tz: Any) -> tuple[datetime, str] | None:
    match = REMIND_RE.match((text or "").strip())
    if not match:
        return None
    when_raw, body = match.group(1), match.group(2).strip()
    parsed = parse_datetime(when_raw, tz)
    if parsed is None:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", when_raw):
        now = datetime.now(tz)
        parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        if parsed <= now:
            parsed += timedelta(days=1)
    return parsed, body


async def _create_reminder(parsed: datetime, text: str, chat_id: int, bot: Any) -> None:
    reminder_id = await db().create_reminder(text=text, remind_at=to_db(parsed))
    now = datetime.now(parsed.tzinfo)
    payload = await db().get_reminder(reminder_id)
    await send_long(
        bot,
        chat_id,
        f"⏰ <b>Напоминание создано</b>\n\n"
        f"<b>{esc(text)}</b>\n"
        f"🕒 {parsed.strftime('%d.%m %H:%M')} · {human_delta(parsed, now)}",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⏰ Все", callback_data="reminders:list"),
                    InlineKeyboardButton(
                        text="🗑 Удалить", callback_data=f"rem:del:{reminder_id}:l"
                    ),
                ]
            ]
        ),
    )


@router.message(Command("remind"))
async def cmd_remind(message: Message, state: FSMContext) -> None:
    arg = _command_arg(message)
    tz = await settings.timezone()
    parsed = parse_remind_arg(arg, tz) if arg else None
    if parsed is None:
        await state.set_state(ViewStates.quick_remind)
        await message.answer(
            "⏰ <b>Новое напоминание</b>\n\n"
            "Отправь в формате: <code>25.11 18:00 позвонить</code> "
            "или <code>18:30 выпить воды</code>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="reminders:list")]]
            ),
        )
        return
    await state.clear()
    when, body = parsed
    await _create_reminder(when, body, message.chat.id, message.bot)


@router.message(ViewStates.quick_remind, F.text)
async def quick_remind_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    tz = await settings.timezone()
    parsed = parse_remind_arg(text, tz)
    if parsed is None:
        await message.answer("Не понял. Пример: <code>25.11 18:00 позвонить</code>.")
        return
    await state.clear()
    when, body = parsed
    await _create_reminder(when, body, message.chat.id, message.bot)
