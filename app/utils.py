"""Small, dependency-free helpers shared by the whole application.

All date arithmetic lives here (code over prompts): the LLM only ever supplies
human text, never computed free windows or sleep timers.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

WEEKDAY_ALIASES = {
    "пн": 0, "понедельник": 0, "monday": 0, "mon": 0,
    "вт": 1, "вторник": 1, "tuesday": 1, "tue": 1,
    "ср": 2, "среда": 2, "среду": 2, "wednesday": 2, "wed": 2,
    "чт": 3, "четверг": 3, "thursday": 3, "thu": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "friday": 4, "fri": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "saturday": 5, "sat": 5,
    "вс": 6, "воскресенье": 6, "sunday": 6, "sun": 6,
}

MODE_ICONS = {"heavy": "🟥", "balanced": "🟨", "free": "🟩"}
MODE_NAMES = {"heavy": "Тяжёлый", "balanced": "Средний", "free": "Свободный"}
CATEGORY_ICONS = {
    "olympiad": "🏆", "study": "📚", "project": "🛠", "personal": "🏠",
    "deadline": "⏳", "meeting": "👥", "club": "🎨", "sport": "🏃", "other": "📌",
}
ENERGY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}
STATUS_ICONS = {"todo": "⏳", "done": "✅", "frozen": "🧊"}

MOOD_ICONS = {"great": "🔥", "good": "😊", "neutral": "😐", "low": "😔", "stressed": "🤯"}
MOOD_NAMES = {"great": "Отличное", "good": "Хорошее", "neutral": "Нормальное", "low": "Усталость", "stressed": "Стресс"}
ENERGY_NUM_ICONS = {1: "🪫", 2: "🔋", 3: "⚡️"}
ENERGY_NUM_NAMES = {1: "Низкая", 2: "Средняя", 3: "Высокая"}
STRESS_ICONS = {0: "🟢", 1: "🟡", 2: "🔴"}
STRESS_NAMES = {0: "Спокойно", 1: "Умеренный", 2: "Высокий"}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def esc(value: Any) -> str:
    """Escape user-controlled text for Telegram HTML parse mode."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def clip(value: Any, limit: int = 300, suffix: str = "…") -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def weekday_ru(day: date, short: bool = False) -> str:
    return (WEEKDAYS_SHORT if short else WEEKDAYS_RU)[day.weekday()]


def fmt_date(day: date, with_weekday: bool = True) -> str:
    base = f"{day.day} {MONTHS_GEN[day.month - 1]}"
    if with_weekday:
        return f"{WEEKDAYS_SHORT[day.weekday()]}, {base}"
    return base


def fmt_date_short(day: date) -> str:
    return f"{day.day:02d}.{day.month:02d}"


def fmt_dt(value: datetime | date | str | None, with_year: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        dt = parse_datetime(value)
        if dt is None:
            return value
        value = dt
    if isinstance(value, datetime):
        if with_year:
            return value.strftime("%d.%m.%Y %H:%M")
        return value.strftime("%d.%m %H:%M")
    if with_year:
        return value.strftime("%d.%m.%Y")
    return value.strftime("%d.%m")


def to_db(value: datetime) -> str:
    """Serialise a local datetime for SQLite text columns."""
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M")


def to_db_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def parse_datetime(value: Any, tz: ZoneInfo | None = None) -> datetime | None:
    """Parse many human/LLM date formats into an aware local datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if not text:
            return None
        text = re.sub(r"\s+", " ", text)
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            pass
        if dt is None:
            formats = (
                "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%d.%m %H:%M", "%d.%m", "%Y-%m-%d", "%H:%M",
            )
            for fmt in formats:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        # Bare HH:MM without a date: anchor to today.
        if dt.year == 1900 and dt.month == 1 and dt.day == 1:
            today = (datetime.now(tz) if tz else datetime.now()).date()
            dt = datetime.combine(today, dt.time())
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    elif dt.tzinfo is not None and tz is not None:
        dt = dt.astimezone(tz)
    return dt


def parse_date(value: Any, tz: ZoneInfo | None = None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    today = (datetime.now(tz) if tz else datetime.now()).date()
    if text in {"сегодня", "today"}:
        return today
    if text in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)
    if text in {"послезавтра", "day after tomorrow"}:
        return today + timedelta(days=2)
    dt = parse_datetime(text, tz)
    return dt.date() if dt else None


def parse_hhmm(value: Any) -> time | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace(".", ":")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def fmt_hhmm(value: Any) -> str:
    parsed = parse_hhmm(value)
    if parsed is None:
        return str(value or "—")
    return parsed.strftime("%H:%M")


def minutes_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def human_duration(minutes: int) -> str:
    minutes = max(0, int(minutes))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def human_delta(target: datetime, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
    delta = target - now
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return f"просрочено на {human_duration(abs(seconds) // 60)}"
    if seconds < 60:
        return "меньше минуты"
    if seconds < 3600:
        return f"через {seconds // 60} мин"
    if seconds < 86400:
        hours, mins = divmod(seconds // 60, 60)
        return f"через {hours} ч {mins} мин" if mins else f"через {hours} ч"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if days <= 3:
        return f"через {days} д {hours} ч" if hours else f"через {days} д"
    return f"через {days} дн"


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    n = abs(int(number)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    found = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!?)»\"'")
        if url not in found:
            found.append(url)
    return found


def fts_query(text: str) -> str | None:
    tokens = re.findall(r"[\w\-]{2,}", text, flags=re.UNICODE)
    if not tokens:
        return None
    # Quote every token so punctuation cannot make MATCH invalid.
    safe = [f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens[:12]]
    return " ".join(safe)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def json_loads_safe(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def title_from_text(text: str, limit: int = 60) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return "Без названия"
    first = re.split(r"(?<=[.!?])\s", clean)[0]
    return clip(first, limit)


def day_bounds_from_profile(profile: Any, fallback_start: str = "08:00", fallback_end: str = "22:30") -> tuple[time, time]:
    start = parse_hhmm(profile["day_start"] if profile is not None else fallback_start) or parse_hhmm(fallback_start)
    end = parse_hhmm(profile["day_end"] if profile is not None else fallback_end) or parse_hhmm(fallback_end)
    return start, end  # type: ignore[return-value]


def combine(day: date, value: time) -> datetime:
    return datetime.combine(day, value)


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    items = sorted((start, end) for start, end in intervals if end > start)
    if not items:
        return []
    merged = [items[0]]
    for start, end in items[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(
    windows: Iterable[tuple[datetime, datetime]],
    busy: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result = list(windows)
    for busy_start, busy_end in merge_intervals(busy):
        updated: list[tuple[datetime, datetime]] = []
        for start, end in result:
            if busy_end <= start or busy_start >= end:
                updated.append((start, end))
                continue
            if start < busy_start:
                updated.append((start, min(busy_start, end)))
            if busy_end < end:
                updated.append((max(busy_end, start), end))
        result = updated
    return [(s, e) for s, e in result if e > s]


def clamp_datetime(value: datetime, lower: datetime, upper: datetime) -> datetime:
    return max(lower, min(upper, value))


def date_range(start: date, days: int) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(days)]


def normalize_weekday(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        if value == 7:
            return 6
        return None
    text = str(value).strip().lower()
    if text.isdigit():
        number = int(text)
        if 0 <= number <= 6:
            return number
        if number == 7:
            return 6
        return None
    return WEEKDAY_ALIASES.get(text.replace(".", ""))


def normalize_category(value: Any) -> str:
    mapping = {
        "олимпиада": "olympiad", "олимпиад": "olympiad", "olympiad": "olympiad",
        "учёба": "study", "учеба": "study", "study": "study", "школа": "study",
        "проект": "project", "project": "project",
        "личное": "personal", "personal": "personal",
        "дедлайн": "deadline", "deadline": "deadline",
        "встреча": "meeting", "meeting": "meeting",
        "кружок": "club", "club": "club",
        "спорт": "sport", "sport": "sport",
        "другое": "other", "other": "other",
    }
    return mapping.get(str(value or "").strip().lower(), "study")


def normalize_energy(value: Any) -> str:
    mapping = {
        "high": "high", "высокая": "high", "высокий": "high", "🔴": "high",
        "medium": "medium", "средняя": "medium", "средний": "medium", "🟡": "medium",
        "low": "low", "низкая": "low", "низкий": "low", "🟢": "low",
    }
    return mapping.get(str(value or "").strip().lower(), "medium")


def normalize_reg_status(value: Any) -> str:
    mapping = {
        "not_needed": "not_needed", "не нужно": "not_needed", "не требуется": "not_needed",
        "pending": "pending", "ожидает": "pending", "в процессе": "pending",
        "done": "done", "готово": "done", "зарегистрирован": "done",
    }
    return mapping.get(str(value or "").strip().lower(), "not_needed")


def mode_icon(mode: str | None) -> str:
    return MODE_ICONS.get(str(mode or "balanced"), "🟨")


def mode_name(mode: str | None) -> str:
    return MODE_NAMES.get(str(mode or "balanced"), "Средний")


def category_icon(category: str | None) -> str:
    return CATEGORY_ICONS.get(str(category or "study"), "📌")


def mood_icon(mood: str | None) -> str:
    return MOOD_ICONS.get(str(mood or "neutral"), "😐")


def mood_name(mood: str | None) -> str:
    return MOOD_NAMES.get(str(mood or "neutral"), "Нормальное")


def energy_num_label(energy: int | None) -> str:
    val = int(energy or 2)
    return f"{ENERGY_NUM_ICONS.get(val, '🔋')} {ENERGY_NUM_NAMES.get(val, 'Средняя')}"

