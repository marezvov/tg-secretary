"""Configuration wizard: /start, /setup, /settings and schedule import."""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date
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

from .. import power_manager, runtime
from ..config import settings
from ..database import Database
from ..documents import DocumentError, extract_document_text
from ..llm_client import LLMError, vision_json
from ..prompts import schedule_prompt
from ..schemas import ScheduleImport
from ..scheduler_jobs import reschedule_briefings
from ..stt_whisper import STTError, whisper
from ..ui_formatter import (
    format_brief_settings,
    format_day_schedule,
    format_models_settings,
    format_power_settings,
    format_schedule_slot,
    format_settings_main,
    format_weekday_profile,
    safe_edit,
    send_long,
)
from ..utils import (
    WEEKDAYS_SHORT,
    WEEKDAYS_RU,
    esc,
    fmt_hhmm,
    mode_icon,
    mode_name,
    normalize_category,
    parse_date,
    parse_hhmm,
    weekday_ru,
)
from ..weather import WeatherError, geocode, timezone_for_coords

log = logging.getLogger(__name__)
router = Router(name="onboarding")


class SetupStates(StatesGroup):
    city = State()
    profile = State()
    schedule_import = State()
    schedule_slot = State()
    pause = State()
    day_bounds = State()
    wake_sleep = State()
    max_minutes = State()
    morning_time = State()
    evening_time = State()
    watch_interval = State()
    power_day = State()


def db() -> Database:
    if runtime.database is None:
        raise RuntimeError("Database is not ready")
    return runtime.database


# --------------------------------------------------------------------------- keyboards
def main_menu_keyboard() -> InlineKeyboardMarkup:
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


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📍 Город", callback_data="set:city"),
                InlineKeyboardButton(text="📅 Расписание", callback_data="set:schedule"),
            ],
            [
                InlineKeyboardButton(text="📆 Профили дней", callback_data="set:profiles"),
                InlineKeyboardButton(text="⏰ Брифинги", callback_data="set:brief"),
            ],
            [
                InlineKeyboardButton(text="⚡ Питание ПК", callback_data="set:power"),
                InlineKeyboardButton(text="🧠 Модели", callback_data="set:models"),
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
                InlineKeyboardButton(text="🗃 Архив", callback_data="arch:menu"),
            ],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="set:close")],
        ]
    )


def cancel_keyboard(target: str = "set:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data=target)]]
    )


async def _settings_text() -> str:
    values = await settings.all()
    counts = {"recurring": len(await db().list_recurring())}
    return format_settings_main(values, counts)


async def _show_settings(target: Any, edit: bool = False) -> None:
    text = await _settings_text()
    if edit and isinstance(target, CallbackQuery):
        await safe_edit(target, text, settings_keyboard())
    else:
        message = target.message if isinstance(target, CallbackQuery) else target
        await message.answer(text, reply_markup=settings_keyboard())


# --------------------------------------------------------------------------- start / setup
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    owner = await settings.owner_id()
    claimed = False
    if owner is None:
        await settings.set_owner(message.from_user.id)
        owner = message.from_user.id
        claimed = True
    values = await settings.all()
    city = values.get("city")
    lines = [
        "👋 <b>Привет! Я твой персональный ИИ-секретарь.</b>",
        "",
        "Я собираю входящие сообщения, веду календарь и задачи, ищу свободные окна, "
        "слежу за дедлайнами, читаю сайты и документы, расшифровываю голосовые.",
        "",
    ]
    if claimed:
        lines.append("🔐 Ты назначен владельцем этого бота. Остальные пользователи игнорируются.")
    if city:
        lines.append(f"📍 Город: <b>{esc(city)}</b> · 🕰 {esc(values.get('timezone'))}")
    else:
        lines.append("📍 Город ещё не настроен — начни с <b>/setup</b>, чтобы работали погода и часовой пояс.")
    lines.extend(
        [
            "",
            "<b>Что дальше:</b>",
            "• /today — карточка дня",
            "• /tasks — задачи и авто-планирование",
            "• /archive — закрытые задачи и прошлое",
            "• /settings — всё остальное",
            "",
            "Просто отправляй текст, голос, фото или файл — я разберу.",
        ]
    )
    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@router.message(Command("setup", "settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_settings(message)


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("✖️ Действие отменено.", reply_markup=main_menu_keyboard())
    else:
        await message.answer("Нечего отменять.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "set:menu")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_settings(callback, edit=True)


@router.callback_query(F.data == "set:close")
async def cb_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "state:cancel")
async def cb_cancel_state(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await _show_settings(callback, edit=True)


# --------------------------------------------------------------------------- city
@router.callback_query(F.data == "set:city")
async def cb_city(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    values = await settings.all()
    current = values.get("city") or "не настроен"
    await state.set_state(SetupStates.city)
    await safe_edit(
        callback,
        "📍 <b>Город и часовой пояс</b>\n\n"
        f"Сейчас: <b>{esc(current)}</b>\n\n"
        "Отправь название города текстом или геопозицию Telegram. "
        "Координаты и часовой пояс подтянутся автоматически (Open-Meteo).",
        cancel_keyboard(),
    )


@router.message(SetupStates.city, F.location)
async def city_location(message: Message, state: FSMContext) -> None:
    location = message.location
    try:
        timezone = await timezone_for_coords(location.latitude, location.longitude)
    except WeatherError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуй ввести город текстом.")
        return
    city = f"Геопозиция ({location.latitude:.3f}, {location.longitude:.3f})"
    await settings.set("city", city)
    await settings.set("lat", location.latitude)
    await settings.set("lon", location.longitude)
    await settings.set("timezone", timezone)
    await settings.set("setup_complete", True)
    await state.clear()
    await message.answer(
        f"✅ Сохранил: <b>{esc(city)}</b>\n🕰 Часовой пояс: <b>{esc(timezone)}</b>",
        reply_markup=settings_keyboard(),
    )


@router.message(SetupStates.city, F.text)
async def city_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if len(text) < 2:
        await message.answer("Название слишком короткое. Попробуй ещё раз.")
        return
    await message.chat.do("typing")
    try:
        geo = await geocode(text)
    except WeatherError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуй позже.")
        return
    if geo is None:
        await message.answer(
            "Не нашёл такой город. Напиши иначе, например: <i>Москва</i>, <i>Санкт-Петербург</i>, "
            "<i>Алматы</i>."
        )
        return
    await settings.set("city", geo.label)
    await settings.set("lat", geo.lat)
    await settings.set("lon", geo.lon)
    await settings.set("timezone", geo.timezone)
    await settings.set("setup_complete", True)
    await state.clear()
    await message.answer(
        f"✅ Город: <b>{esc(geo.label)}</b>\n"
        f"📍 {geo.lat:.3f}, {geo.lon:.3f}\n"
        f"🕰 Часовой пояс: <b>{esc(geo.timezone)}</b>",
        reply_markup=settings_keyboard(),
    )


# --------------------------------------------------------------------------- schedule
async def _show_schedule(target: CallbackQuery | Message, edit: bool = True) -> None:
    slots = await db().list_recurring()
    counts = {weekday: 0 for weekday in range(7)}
    for slot in slots:
        counts[int(slot["weekday"])] += 1
    values = await settings.all()
    pause = values.get("pause_recurring_until")
    lines = [
        "📅 <b>Регулярная сетка недели</b>",
        "",
        f"Всего слотов: <b>{len(slots)}</b>",
    ]
    if pause:
        lines.append(f"⏸ Пауза до <b>{esc(pause)}</b> (сетка не учитывается в календаре)")
    lines.extend(
        [
            "",
            "Выбери день, чтобы посмотреть и отредактировать занятия. "
            "Можно импортировать сетку фото/текстом/голосом.",
        ]
    )
    rows = []
    for weekday in range(7):
        rows.append(
            InlineKeyboardButton(
                text=f"{WEEKDAYS_SHORT[weekday]} ({counts[weekday]})",
                callback_data=f"sched:day:{weekday}",
            )
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            rows[:4],
            rows[4:],
            [
                InlineKeyboardButton(text="📥 Импорт сетки", callback_data="sched:import"),
                InlineKeyboardButton(
                    text="⏸ Пауза" if not pause else "▶️ Снять паузу",
                    callback_data="sched:pause",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")],
        ]
    )
    if edit and isinstance(target, CallbackQuery):
        await safe_edit(target, "\n".join(lines), keyboard)
    elif isinstance(target, CallbackQuery):
        await target.message.answer("\n".join(lines), reply_markup=keyboard)
    else:
        await target.answer("\n".join(lines), reply_markup=keyboard)


@router.message(Command("schedule_edit", "schedule"))
async def cmd_schedule_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_schedule(message, edit=False)


@router.callback_query(F.data == "set:schedule")
async def cb_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_schedule(callback)


@router.callback_query(F.data.startswith("sched:day:"))
async def cb_schedule_day(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    weekday = int(callback.data.split(":")[2])
    slots = await db().list_recurring(weekday)
    values = await settings.all()
    pause = values.get("pause_recurring_until")
    text = format_day_schedule(weekday, slots, pause)
    rows = []
    for slot in slots:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {fmt_hhmm(slot['start_time'])} {slot['title'][:20]}",
                    callback_data=f"sched:del:{slot['id']}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"sched:add:{weekday}"),
            InlineKeyboardButton(text="📥 Импорт", callback_data="sched:import"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ К сетке", callback_data="set:schedule")])
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sched:del:"))
async def cb_schedule_delete(callback: CallbackQuery) -> None:
    slot_id = int(callback.data.split(":")[2])
    slot = await db().get_recurring(slot_id)
    if slot is None:
        await callback.answer("Слот уже удалён", show_alert=True)
        return
    weekday = int(slot["weekday"])
    await db().delete_recurring(slot_id)
    await callback.answer("Удалено")
    slots = await db().list_recurring(weekday)
    values = await settings.all()
    text = format_day_schedule(weekday, slots, values.get("pause_recurring_until"))
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {fmt_hhmm(item['start_time'])} {item['title'][:20]}",
                callback_data=f"sched:del:{item['id']}",
            )
        ]
        for item in slots
    ]
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"sched:add:{weekday}"),
            InlineKeyboardButton(text="📥 Импорт", callback_data="sched:import"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ К сетке", callback_data="set:schedule")])
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sched:add:"))
async def cb_schedule_add(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":")[2])
    await state.set_state(SetupStates.schedule_slot)
    await state.update_data(weekday=weekday)
    await callback.answer()
    await safe_edit(
        callback,
        f"➕ <b>Новый слот · {WEEKDAYS_RU[weekday]}</b>\n\n"
        "Отправь в формате:\n"
        "<code>09:00-10:30 Математика | study | каб. 12</code>\n\n"
        "Категории: study, club, sport, other. "
        "Адрес и категория необязательны.",
        cancel_keyboard(f"sched:day:{weekday}"),
    )


def parse_slot_text(text: str, weekday: int) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст")
    chunks = [chunk.strip() for chunk in text.split("|")]
    body = chunks[0]
    range_match = re.search(
        r"(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})", body
    )
    if range_match:
        start_raw, end_raw = range_match.group(1), range_match.group(2)
        title = (body[: range_match.start()] + " " + body[range_match.end():]).strip()
    else:
        times = re.findall(r"\b(?:[01]?\d|2[0-3])[:.]\d{2}\b", body)
        if len(times) < 2:
            raise ValueError("Не вижу времени. Пример: 09:00-10:30 Математика")
        start_raw, end_raw = times[0], times[1]
        title = re.sub(r"\b(?:[01]?\d|2[0-3])[:.]\d{2}\b", " ", body)
    start = parse_hhmm(start_raw)
    end = parse_hhmm(end_raw)
    if start is None or end is None:
        raise ValueError("Некорректное время")
    if end <= start:
        raise ValueError("Конец должен быть позже начала")
    title = re.sub(r"[-–—,;]+", " ", title)
    title = " ".join(title.split()).strip(" .")
    if not title:
        raise ValueError("Не вижу названия занятия")
    category = normalize_category(chunks[1] if len(chunks) > 1 else "study")
    if category not in {"study", "club", "sport", "other"}:
        category = "study"
    location = chunks[2] if len(chunks) > 2 and chunks[2] else None
    return {
        "weekday": weekday,
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "title": title,
        "category": category,
        "location": location,
    }


@router.message(SetupStates.schedule_slot, F.text)
async def schedule_slot_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    data = await state.get_data()
    weekday = int(data.get("weekday", 0))
    try:
        slot = parse_slot_text(text, weekday)
    except ValueError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуй ещё раз или нажми «Отмена».")
        return
    await db().create_recurring(**slot)
    await state.clear()
    slots = await db().list_recurring(weekday)
    values = await settings.all()
    await message.answer(
        f"✅ Добавлено: {format_schedule_slot(slot)}\n\n"
        + format_day_schedule(weekday, slots, values.get("pause_recurring_until")),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К сетке", callback_data="set:schedule")]
            ]
        ),
    )


@router.callback_query(F.data == "sched:import")
async def cb_schedule_import(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SetupStates.schedule_import)
    await safe_edit(
        callback,
        "📥 <b>Импорт недельной сетки</b>\n\n"
        "Отправь одно из:\n"
        "• фото или скриншот таблицы расписания;\n"
        "• пересланный текст / список уроков;\n"
        "• голосовое сообщение;\n"
        "• документ PDF/DOCX/TXT.\n\n"
        "Я покажу превью, а запишу только после подтверждения.",
        cancel_keyboard("set:schedule"),
    )


def _slots_preview(slots: list[dict[str, Any]]) -> str:
    lines = [f"📚 <b>Найдено слотов: {len(slots)}</b>", ""]
    for slot in sorted(slots, key=lambda item: (int(item["weekday"]), item["start_time"])):
        lines.append(
            f"{WEEKDAYS_SHORT[int(slot['weekday'])]} "
            f"{slot['start_time']}–{slot['end_time']} · {esc(slot['title'])}"
            + (f" · {esc(slot.get('location'))}" if slot.get("location") else "")
        )
    return "\n".join(lines)


@router.message(SetupStates.schedule_import)
async def schedule_import_handler(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        return
    await message.chat.do("typing")
    slots: list[dict[str, Any]] = []
    try:
        if message.photo:
            values = await settings.all()
            if not values.get("vlm_enabled", True):
                await message.answer("VLM выключен в настройках — включи его или отправь текст.")
                return
            buffer = io.BytesIO()
            await message.bot.download(message.photo[-1], destination=buffer)
            now = await settings.now()
            tz = await settings.timezone()
            result = await vision_json(
                buffer.getvalue(),
                schedule_prompt(now, str(tz), message.caption or "Фото расписания", has_image=True),
                model_cls=ScheduleImport,
            )
            slots = result.get("slots", [])
        elif message.voice:
            buffer = io.BytesIO()
            await message.bot.download(message.voice, destination=buffer)
            transcript = await whisper.transcribe(buffer.getvalue(), suffix=".ogg")
            if not transcript:
                await message.answer("Whisper выключен или не распознал речь.")
                return
            await message.answer(f"🎙 <blockquote>{esc(transcript)}</blockquote>")
            now = await settings.now()
            tz = await settings.timezone()
            from ..llm_client import chat_json

            result = await chat_json(
                [{"role": "user", "content": schedule_prompt(now, str(tz), transcript)}],
                model_cls=ScheduleImport,
            )
            slots = result.get("slots", [])
        elif message.document:
            buffer = io.BytesIO()
            await message.bot.download(message.document, destination=buffer)
            title, text = extract_document_text(
                buffer.getvalue(), message.document.file_name or "schedule.txt"
            )
            now = await settings.now()
            tz = await settings.timezone()
            from ..llm_client import chat_json

            result = await chat_json(
                [{"role": "user", "content": schedule_prompt(now, str(tz), text)}],
                model_cls=ScheduleImport,
            )
            slots = result.get("slots", [])
        elif message.text:
            now = await settings.now()
            tz = await settings.timezone()
            from ..llm_client import chat_json

            result = await chat_json(
                [{"role": "user", "content": schedule_prompt(now, str(tz), message.text)}],
                model_cls=ScheduleImport,
            )
            slots = result.get("slots", [])
        else:
            await message.answer("Отправь фото, текст, голос или документ с расписанием.")
            return
    except (LLMError, STTError, DocumentError) as exc:
        await message.answer(f"❌ Не получилось разобрать: {esc(exc)}\n\nПопробуй ещё раз.")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Schedule import failed")
        await message.answer(f"❌ Ошибка импорта: {esc(exc)}")
        return
    if not slots:
        await message.answer("Не нашёл ни одного занятия. Попробуй более чёткий текст или фото.")
        return
    await state.update_data(import_slots=slots)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить к сетке", callback_data="sched:import:save:append")],
            [InlineKeyboardButton(text="♻️ Заменить всю сетку", callback_data="sched:import:save:replace")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="set:schedule")],
        ]
    )
    await message.answer(_slots_preview(slots), reply_markup=keyboard)


@router.callback_query(F.data.startswith("sched:import:save:"))
async def cb_schedule_import_save(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":")[-1]
    data = await state.get_data()
    slots = data.get("import_slots") or []
    if not slots:
        await callback.answer("Данные потерялись, повтори импорт", show_alert=True)
        await state.clear()
        return
    if mode == "replace":
        await db().replace_recurring(slots)
        message = f"♻️ Сетка заменена: {len(slots)} слотов."
    else:
        await db().add_recurring_bulk(slots)
        message = f"✅ Добавлено слотов: {len(slots)}."
    await state.clear()
    await callback.answer("Сохранено")
    await _show_schedule(callback, edit=True)
    try:
        await callback.message.answer(message)
    except Exception:
        pass


@router.callback_query(F.data == "sched:pause")
async def cb_schedule_pause(callback: CallbackQuery, state: FSMContext) -> None:
    current = await settings.get("pause_recurring_until")
    if current:
        await settings.set("pause_recurring_until", None)
        await callback.answer("Пауза снята")
        await _show_schedule(callback, edit=True)
        return
    await callback.answer()
    await state.set_state(SetupStates.pause)
    await safe_edit(
        callback,
        "⏸ <b>Пауза регулярной сетки</b>\n\n"
        "Напиши дату, до которой сетка не учитывается (каникулы, болезнь, дистант): "
        "<code>2026-12-31</code> или <code>31.12.2026</code>.\n\n"
        "Сами слоты не удаляются. Чтобы снять паузу — нажми кнопку ещё раз.",
        cancel_keyboard("set:schedule"),
    )


@router.message(SetupStates.pause, F.text)
async def schedule_pause_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if text.lower() in {"off", "нет", "снять", "0", "-"}:
        await settings.set("pause_recurring_until", None)
        await state.clear()
        await message.answer("▶️ Пауза снята.", reply_markup=settings_keyboard())
        return
    tz = await settings.timezone()
    parsed = parse_date(text, tz)
    if parsed is None:
        await message.answer("Не понял дату. Пример: <code>2026-12-31</code>.")
        return
    await settings.set("pause_recurring_until", parsed.isoformat())
    await state.clear()
    await message.answer(
        f"⏸ Регулярная сетка на паузе до <b>{parsed.isoformat()}</b>. Слоты сохранены.",
        reply_markup=settings_keyboard(),
    )


# --------------------------------------------------------------------------- day profiles
@router.callback_query(F.data == "set:profiles")
async def cb_profiles(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    profiles = await db().list_day_profiles()
    lines = ["📆 <b>Профили дней недели</b>", ""]
    rows = []
    for profile in profiles:
        weekday = int(profile["weekday"])
        mode = str(profile["mode"])
        lines.append(
            f"{mode_icon(mode)} {WEEKDAYS_SHORT[weekday]} · {mode_name(mode)} · "
            f"{profile['day_start']}–{profile['day_end']}"
        )
        rows.append(
            InlineKeyboardButton(
                text=f"{WEEKDAYS_SHORT[weekday]} {mode_icon(mode)}",
                callback_data=f"profile:day:{weekday}",
            )
        )
    lines.extend(
        [
            "",
            "🟥 Тяжёлый — только мелкие дела до 15 минут.",
            "🟨 Средний — окна днём/вечером, высокую энергию ставлю в длинные окна.",
            "🟩 Свободный — глубокая работа и длинные блоки.",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[rows[:4], rows[4:], [InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")]]
    )
    await safe_edit(callback, "\n".join(lines), keyboard)


async def _show_profile(callback: CallbackQuery, weekday: int) -> None:
    profile = await db().get_day_profile(weekday)
    if profile is None:
        await callback.answer("Профиль не найден", show_alert=True)
        return
    mode = str(profile["mode"])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if mode == "heavy" else "") + "🟥 Тяжёлый",
                    callback_data=f"profile:mode:{weekday}:heavy",
                ),
                InlineKeyboardButton(
                    text=("✅ " if mode == "balanced" else "") + "🟨 Средний",
                    callback_data=f"profile:mode:{weekday}:balanced",
                ),
                InlineKeyboardButton(
                    text=("✅ " if mode == "free" else "") + "🟩 Свободный",
                    callback_data=f"profile:mode:{weekday}:free",
                ),
            ],
            [
                InlineKeyboardButton(text="⏱ Границы дня", callback_data=f"profile:bounds:{weekday}"),
                InlineKeyboardButton(text="⏱ Макс. блок", callback_data=f"profile:max:{weekday}"),
            ],
            [InlineKeyboardButton(text="🌅 Подъём / 🌙 сон", callback_data=f"profile:wake:{weekday}")],
            [InlineKeyboardButton(text="⬅️ К профилям", callback_data="set:profiles")],
        ]
    )
    await safe_edit(callback, format_weekday_profile(profile), keyboard)


@router.callback_query(F.data.startswith("profile:day:"))
async def cb_profile_day(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_profile(callback, int(callback.data.split(":")[2]))


@router.callback_query(F.data.startswith("profile:mode:"))
async def cb_profile_mode(callback: CallbackQuery) -> None:
    _prefix, _profile, weekday, mode = callback.data.split(":")
    if mode not in {"heavy", "balanced", "free"}:
        await callback.answer("Неизвестный режим", show_alert=True)
        return
    await db().update_day_profile(int(weekday), mode=mode)
    await callback.answer(f"{mode_icon(mode)} {mode_name(mode)}")
    await _show_profile(callback, int(weekday))


@router.callback_query(F.data.startswith("profile:bounds:"))
async def cb_profile_bounds(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":")[2])
    profile = await db().get_day_profile(weekday)
    await state.set_state(SetupStates.day_bounds)
    await state.update_data(weekday=weekday)
    await callback.answer()
    await safe_edit(
        callback,
        f"⏱ <b>Границы активного дня · {WEEKDAYS_RU[weekday]}</b>\n\n"
        f"Сейчас: <b>{profile['day_start']}–{profile['day_end']}</b>\n\n"
        "Отправь новый интервал: <code>08:00-22:30</code>",
        cancel_keyboard(f"profile:day:{weekday}"),
    )


@router.message(SetupStates.day_bounds, F.text)
async def profile_bounds_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    match = re.fullmatch(r"\s*(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*", text)
    if not match:
        await message.answer("Формат: <code>08:00-22:30</code>")
        return
    start = parse_hhmm(match.group(1))
    end = parse_hhmm(match.group(2))
    if start is None or end is None or end <= start:
        await message.answer("Некорректный интервал: конец должен быть позже начала.")
        return
    data = await state.get_data()
    weekday = int(data.get("weekday", 0))
    await db().update_day_profile(
        weekday, day_start=start.strftime("%H:%M"), day_end=end.strftime("%H:%M")
    )
    await state.clear()
    await message.answer(
        f"✅ Границы дня обновлены: <b>{start.strftime('%H:%M')}–{end.strftime('%H:%M')}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К профилю", callback_data=f"profile:day:{weekday}")]
            ]
        ),
    )


@router.callback_query(F.data.startswith("profile:max:"))
async def cb_profile_max(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":")[2])
    profile = await db().get_day_profile(weekday)
    await state.set_state(SetupStates.max_minutes)
    await state.update_data(weekday=weekday)
    await callback.answer()
    await safe_edit(
        callback,
        f"⏱ <b>Максимальный блок задачи · {WEEKDAYS_RU[weekday]}</b>\n\n"
        f"Сейчас: <b>{profile['max_task_minutes']} мин</b>\n\n"
        "Отправь число минут (15–480).",
        cancel_keyboard(f"profile:day:{weekday}"),
    )


@router.message(SetupStates.max_minutes, F.text)
async def profile_max_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if not text.isdigit() or not (15 <= int(text) <= 480):
        await message.answer("Нужно число от 15 до 480.")
        return
    data = await state.get_data()
    weekday = int(data.get("weekday", 0))
    await db().update_day_profile(weekday, max_task_minutes=int(text))
    await state.clear()
    await message.answer(
        f"✅ Максимальный блок: <b>{int(text)} мин</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К профилю", callback_data=f"profile:day:{weekday}")]
            ]
        ),
    )


@router.callback_query(F.data.startswith("profile:wake:"))
async def cb_profile_wake(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":")[2])
    profile = await db().get_day_profile(weekday)
    await state.set_state(SetupStates.wake_sleep)
    await state.update_data(weekday=weekday)
    await callback.answer()
    await safe_edit(
        callback,
        f"🌅 <b>Подъём и сон · {WEEKDAYS_RU[weekday]}</b>\n\n"
        f"Сейчас: 🌅 {profile['wake_time']} → 🌙 {profile['sleep_time']}\n\n"
        "Отправь: <code>07:00-23:30</code>",
        cancel_keyboard(f"profile:day:{weekday}"),
    )


@router.message(SetupStates.wake_sleep, F.text)
async def profile_wake_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    match = re.fullmatch(r"\s*(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*", text)
    if not match:
        await message.answer("Формат: <code>07:00-23:30</code>")
        return
    wake = parse_hhmm(match.group(1))
    sleep = parse_hhmm(match.group(2))
    if wake is None or sleep is None:
        await message.answer("Некорректное время.")
        return
    data = await state.get_data()
    weekday = int(data.get("weekday", 0))
    await db().update_day_profile(
        weekday, wake_time=wake.strftime("%H:%M"), sleep_time=sleep.strftime("%H:%M")
    )
    await state.clear()
    await message.answer(
        f"✅ 🌅 {wake.strftime('%H:%M')} → 🌙 {sleep.strftime('%H:%M')}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К профилю", callback_data=f"profile:day:{weekday}")]
            ]
        ),
    )


# --------------------------------------------------------------------------- power
async def _power_text() -> tuple[str, InlineKeyboardMarkup]:
    values = await settings.all()
    profiles = await db().list_day_profiles()
    lines = [format_power_settings(values, profiles), ""]
    keyboard_rows = [
        [
            InlineKeyboardButton(
                text="Выключить" if values.get("power_enabled") else "Включить",
                callback_data="power:toggle",
            ),
            InlineKeyboardButton(text="🔍 Проверить права", callback_data="power:check"),
        ]
    ]
    weekday_row = []
    for profile in profiles:
        weekday_row.append(
            InlineKeyboardButton(
                text=WEEKDAYS_SHORT[int(profile["weekday"])],
                callback_data=f"power:day:{int(profile['weekday'])}",
            )
        )
    keyboard_rows.append(weekday_row[:4])
    keyboard_rows.append(weekday_row[4:])
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@router.callback_query(F.data == "set:power")
async def cb_power(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    text, keyboard = await _power_text()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data == "power:toggle")
async def cb_power_toggle(callback: CallbackQuery) -> None:
    if bool(await settings.get("power_enabled")):
        await power_manager.disable_power()
        await callback.answer("Питание выключено")
    else:
        ok, message = await power_manager.enable_power(db())
        await callback.answer("Включено" if ok else "Не удалось включить", show_alert=not ok)
        if not ok:
            await send_long(callback.bot, callback.message.chat.id, message)
    text, keyboard = await _power_text()
    await safe_edit(callback, text, keyboard)


@router.callback_query(F.data == "power:check")
async def cb_power_check(callback: CallbackQuery) -> None:
    ok, message = await power_manager.check_rtcwake()
    if ok:
        await callback.answer("rtcwake доступен ✅", show_alert=True)
    else:
        await callback.answer("rtcwake недоступен", show_alert=True)
        await send_long(
            callback.bot,
            callback.message.chat.id,
            f"❌ {esc(message)}\n\n{power_manager.sudoers_instructions()}",
        )


@router.callback_query(F.data.startswith("power:day:"))
async def cb_power_day(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":")[2])
    profile = await db().get_day_profile(weekday)
    await state.set_state(SetupStates.power_day)
    await state.update_data(weekday=weekday)
    await callback.answer()
    await safe_edit(
        callback,
        f"⚡ <b>{WEEKDAYS_RU[weekday]}</b>\n\n"
        f"Сейчас: 🌅 {profile['wake_time']} → 🌙 {profile['sleep_time']}\n\n"
        "Отправь подъём и сон: <code>07:00-23:30</code>",
        cancel_keyboard("set:power"),
    )


@router.message(SetupStates.power_day, F.text)
async def power_day_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    match = re.fullmatch(r"\s*(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*", text)
    if not match:
        await message.answer("Формат: <code>07:00-23:30</code>")
        return
    wake = parse_hhmm(match.group(1))
    sleep = parse_hhmm(match.group(2))
    if wake is None or sleep is None:
        await message.answer("Некорректное время.")
        return
    data = await state.get_data()
    weekday = int(data.get("weekday", 0))
    await db().update_day_profile(
        weekday, wake_time=wake.strftime("%H:%M"), sleep_time=sleep.strftime("%H:%M")
    )
    await state.clear()
    text, keyboard = await _power_text()
    await message.answer(
        f"✅ {WEEKDAYS_RU[weekday]}: 🌅 {wake.strftime('%H:%M')} → 🌙 {sleep.strftime('%H:%M')}\n\n{text}",
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------- briefings
@router.callback_query(F.data == "set:brief")
async def cb_brief(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    values = await settings.all()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌅 Утренний", callback_data="brief:morning"),
                InlineKeyboardButton(text="🌙 Вечерний", callback_data="brief:evening"),
            ],
            [InlineKeyboardButton(text="👁 Интервал мониторинга", callback_data="brief:watch")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")],
        ]
    )
    await safe_edit(callback, format_brief_settings(values), keyboard)


@router.callback_query(F.data.startswith("brief:"))
async def cb_brief_edit(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[1]
    await callback.answer()
    if kind == "morning":
        await state.set_state(SetupStates.morning_time)
        prompt = "🌅 Время утреннего брифинга: <code>07:15</code>"
    elif kind == "evening":
        await state.set_state(SetupStates.evening_time)
        prompt = "🌙 Время вечернего чек-ина: <code>22:30</code>"
    else:
        await state.set_state(SetupStates.watch_interval)
        prompt = "👁 Интервал проверки /watch в часах (1–48): <code>2</code>"
    await safe_edit(callback, prompt, cancel_keyboard("set:brief"))


async def _save_time_and_show(message: Message, state: FSMContext, key: str) -> None:
    text = (message.text or "").strip()
    parsed = parse_hhmm(text)
    if parsed is None:
        await message.answer("Формат времени: <code>07:15</code>")
        return
    await settings.set(key, parsed.strftime("%H:%M"))
    await state.clear()
    if runtime.scheduler is not None:
        await reschedule_briefings(runtime.scheduler)
    values = await settings.all()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К брифингам", callback_data="set:brief")]
        ]
    )
    await message.answer(f"✅ Сохранено: <b>{parsed.strftime('%H:%M')}</b>", reply_markup=keyboard)


@router.message(SetupStates.morning_time, F.text)
async def morning_time_text(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    await _save_time_and_show(message, state, "morning_brief_time")


@router.message(SetupStates.evening_time, F.text)
async def evening_time_text(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    await _save_time_and_show(message, state, "evening_brief_time")


@router.message(SetupStates.watch_interval, F.text)
async def watch_interval_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if not text.isdigit() or not (1 <= int(text) <= 48):
        await message.answer("Нужно число от 1 до 48.")
        return
    await settings.set("watch_interval_hours", int(text))
    await state.clear()
    await message.answer(
        f"✅ Новый интервал мониторинга: <b>{int(text)} ч</b> (для новых страниц).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К брифингам", callback_data="set:brief")]
            ]
        ),
    )


# --------------------------------------------------------------------------- models
@router.callback_query(F.data == "set:models")
async def cb_models(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    values = await settings.all()
    current = str(values.get("whisper_model") or "small")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if current == "off" else "") + "off",
                    callback_data="set:whisper:off",
                ),
                InlineKeyboardButton(
                    text=("✅ " if current == "small" else "") + "small",
                    callback_data="set:whisper:small",
                ),
                InlineKeyboardButton(
                    text=("✅ " if current == "medium" else "") + "medium",
                    callback_data="set:whisper:medium",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅ " if current == "large-v3-turbo" else "") + "large-v3-turbo",
                    callback_data="set:whisper:large-v3-turbo",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"👁 VLM: {'вкл' if values.get('vlm_enabled', True) else 'выкл'}",
                    callback_data="set:vlm",
                ),
                InlineKeyboardButton(text="✏️ Профиль", callback_data="set:profile"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")],
        ]
    )
    await safe_edit(callback, format_models_settings(values), keyboard)


@router.callback_query(F.data.startswith("set:whisper:"))
async def cb_set_whisper(callback: CallbackQuery) -> None:
    model = callback.data.split(":")[2]
    if model not in {"off", "small", "medium", "large-v3-turbo"}:
        await callback.answer("Неизвестная модель", show_alert=True)
        return
    await settings.set("whisper_model", model)
    await callback.answer(f"Whisper: {model}")
    values = await settings.all()
    await safe_edit(callback, format_models_settings(values), await _models_keyboard(values))


async def _models_keyboard(values: dict[str, Any]) -> InlineKeyboardMarkup:
    current = str(values.get("whisper_model") or "small")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if current == "off" else "") + "off",
                    callback_data="set:whisper:off",
                ),
                InlineKeyboardButton(
                    text=("✅ " if current == "small" else "") + "small",
                    callback_data="set:whisper:small",
                ),
                InlineKeyboardButton(
                    text=("✅ " if current == "medium" else "") + "medium",
                    callback_data="set:whisper:medium",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅ " if current == "large-v3-turbo" else "") + "large-v3-turbo",
                    callback_data="set:whisper:large-v3-turbo",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"👁 VLM: {'вкл' if values.get('vlm_enabled', True) else 'выкл'}",
                    callback_data="set:vlm",
                ),
                InlineKeyboardButton(text="✏️ Профиль", callback_data="set:profile"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set:menu")],
        ]
    )


@router.callback_query(F.data == "set:vlm")
async def cb_set_vlm(callback: CallbackQuery) -> None:
    current = bool(await settings.get("vlm_enabled", True))
    await settings.set("vlm_enabled", not current)
    await callback.answer("VLM включён" if not current else "VLM выключен")
    values = await settings.all()
    await safe_edit(callback, format_models_settings(values), await _models_keyboard(values))


@router.callback_query(F.data == "set:profile")
async def cb_set_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SetupStates.profile)
    current = await settings.user_profile()
    await safe_edit(
        callback,
        "🧠 <b>Профиль владельца</b>\n\n"
        "Опиши себя: класс/курс, предметы, цели, особенности здоровья и энергии, "
        "любимые форматы работы. Это помогает классифицировать входящие и выбирать задачи.\n\n"
        f"<b>Сейчас:</b>\n{esc(current) if current else '<i>пусто</i>'}\n\n"
        "Отправь новый текст профиля.",
        cancel_keyboard("set:models"),
    )


@router.message(SetupStates.profile, F.text)
async def profile_text_handler(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if len(text) < 3:
        await message.answer("Слишком коротко. Напиши чуть подробнее.")
        return
    await settings.set("user_profile", text[:4000])
    await state.clear()
    await message.answer(
        "✅ Профиль сохранён. Учту его при разборе входящих и в брифингах.",
        reply_markup=settings_keyboard(),
    )
