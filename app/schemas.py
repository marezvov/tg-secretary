"""Pydantic contracts for every JSON response produced by the LLM/VLM.

Validation is deliberately forgiving (synonyms, Russian words, numeric strings)
but still strict about the final stored shape.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .utils import normalize_category, normalize_energy, normalize_reg_status, normalize_weekday, parse_hhmm


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return str(value)


class InboxItem(BaseModel):
    type: str = "note"
    title: str = ""
    category: str = "study"
    energy: str = "medium"
    est_minutes: int = 30
    start_dt: Any = None
    end_dt: Any = None
    deadline_at: Any = None
    reminder_at: Any = None
    reg_status: str = "not_needed"
    location_or_url: str | None = None
    first_step: str | None = None
    prep_notes: str | None = None
    weekday: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    source_url: str | None = None
    content: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        aliases = {
            "summary": "title", "name": "title", "заголовок": "title", "название": "title",
            "when": "start_dt", "start": "start_dt", "date": "start_dt", "дата": "start_dt", "время": "start_dt",
            "end": "end_dt", "finish": "end_dt", "конец": "end_dt",
            "due": "deadline_at", "deadline": "deadline_at", "дедлайн": "deadline_at", "срок": "deadline_at",
            "remind_at": "reminder_at", "remind": "reminder_at", "напомнить": "reminder_at",
            "location": "location_or_url", "url": "location_or_url", "место": "location_or_url", "ссылка": "location_or_url",
            "step": "first_step", "notes": "prep_notes", "первый_шаг": "first_step", "заметки": "prep_notes",
            "text": "content", "описание": "content", "текст": "content",
            "тип": "type", "категория": "category", "энергия": "energy", "минуты": "est_minutes",
        }
        for source, target in aliases.items():
            if source in data and target not in data:
                data[target] = data.pop(source)
        return data

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> str:
        mapping = {
            "event": "event", "мероприятие": "event", "событие": "event",
            "task": "task", "задача": "task", "дело": "task",
            "reminder": "reminder", "напоминание": "reminder",
            "note": "note", "заметка": "note", "мысль": "note",
            "recurring_schedule": "recurring_schedule", "recurring": "recurring_schedule",
            "schedule": "recurring_schedule", "расписание": "recurring_schedule",
        }
        return mapping.get(str(value or "").strip().lower(), "note")

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: Any) -> str:
        return str(value or "").strip() or "Без названия"

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("energy", mode="before")
    @classmethod
    def _energy(cls, value: Any) -> str:
        return normalize_energy(value)

    @field_validator("reg_status", mode="before")
    @classmethod
    def _reg(cls, value: Any) -> str:
        return normalize_reg_status(value)

    @field_validator("est_minutes", mode="before")
    @classmethod
    def _est(cls, value: Any) -> int:
        if value is None:
            return 30
        if isinstance(value, (int, float)):
            return max(5, min(600, int(value)))
        match = re.search(r"\d+", str(value))
        return max(5, min(600, int(match.group(0)))) if match else 30

    @field_validator("weekday", mode="before")
    @classmethod
    def _weekday(cls, value: Any) -> int | None:
        return normalize_weekday(value)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _time(cls, value: Any) -> str | None:
        if value is None:
            return None
        parsed = parse_hhmm(value)
        return parsed.strftime("%H:%M") if parsed else _as_text(value)

    @field_validator("start_dt", "end_dt", "deadline_at", "reminder_at", mode="before")
    @classmethod
    def _date_text(cls, value: Any) -> str | None:
        return _as_text(value)

    @field_validator("location_or_url", "first_step", "prep_notes", "source_url", "content", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _as_text(value)


class InboxResult(BaseModel):
    items: list[InboxItem] = Field(default_factory=list)
    reply_summary: str = ""

    @field_validator("reply_summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"items": data}
        if isinstance(data, dict) and "items" not in data:
            for key in ("tasks", "events", "notes", "reminders"):
                if key in data and isinstance(data[key], list):
                    return {"items": data[key], "reply_summary": data.get("reply_summary", "")}
        return data


class ScheduleSlot(BaseModel):
    weekday: int = 0
    start_time: str = "09:00"
    end_time: str = "10:00"
    title: str = "Занятие"
    category: str = "study"
    location: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        aliases = {
            "day": "weekday", "день": "weekday",
            "start": "start_time", "from": "start_time", "начало": "start_time",
            "end": "end_time", "to": "end_time", "конец": "end_time",
            "name": "title", "subject": "title", "предмет": "title",
            "place": "location", "room": "location", "кабинет": "location",
        }
        for source, target in aliases.items():
            if source in data and target not in data:
                data[target] = data.pop(source)
        return data

    @field_validator("weekday", mode="before")
    @classmethod
    def _weekday(cls, value: Any) -> int:
        normalized = normalize_weekday(value)
        return normalized if normalized is not None else 0

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _time(cls, value: Any) -> str:
        parsed = parse_hhmm(value)
        return parsed.strftime("%H:%M") if parsed else str(value or "09:00")

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: Any) -> str:
        return str(value or "").strip() or "Занятие"

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        category = normalize_category(value)
        return category if category in {"study", "club", "sport", "other"} else "study"

    @field_validator("location", mode="before")
    @classmethod
    def _location(cls, value: Any) -> str | None:
        return _as_text(value)


class ScheduleImport(BaseModel):
    slots: list[ScheduleSlot] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"slots": data}
        if isinstance(data, dict) and "slots" not in data:
            for key in ("items", "schedule", "lessons", "classes"):
                if key in data and isinstance(data[key], list):
                    return {"slots": data[key]}
        return data


class WatchDecision(BaseModel):
    relevant: bool = False
    summary: str = ""

    @field_validator("relevant", mode="before")
    @classmethod
    def _relevant(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"true", "1", "yes", "да", "важно", "есть"}

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return str(value or "").strip()


class WebAction(BaseModel):
    thought: str = ""
    action: str = "extract"
    id: int | None = None
    direction: str | None = None
    url: str | None = None
    result: str | None = None

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, value: Any) -> str:
        mapping = {
            "click": "click", "scroll": "scroll", "goto": "goto", "extract": "extract",
            "нажать": "click", "прокрутить": "scroll", "перейти": "goto", "извлечь": "extract",
        }
        return mapping.get(str(value or "").strip().lower(), "extract")

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            match = re.search(r"\d+", str(value))
            return int(match.group(0)) if match else None
