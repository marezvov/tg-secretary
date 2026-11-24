"""Environment configuration and hot settings stored in SQLite."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _env_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Env:
    bot_token: str
    llm_base_url: str
    llm_model_name: str
    llm_api_key: str
    admin_id: int | None
    db_path: str
    log_level: str
    power_dry_run: bool
    default_timezone: str


def _load_env() -> Env:
    return Env(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1").strip(),
        llm_model_name=os.getenv("LLM_MODEL_NAME", "local-model").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "sk-local").strip() or "sk-local",
        admin_id=_env_int("ADMIN_ID"),
        db_path=os.getenv("DB_PATH", "data/secretary.db").strip() or "data/secretary.db",
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        power_dry_run=_env_bool("POWER_DRY_RUN", False),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
    )


env = _load_env()

# These values are only defaults.  Everything can be changed at runtime via
# /settings and is persisted in the `settings` table as JSON.
DEFAULT_SETTINGS: dict[str, Any] = {
    "owner_id": None,
    "city": None,
    "lat": None,
    "lon": None,
    "timezone": env.default_timezone,
    "user_profile": "",
    "pause_recurring_until": None,
    "whisper_model": "small",
    "vlm_enabled": True,
    "morning_brief_time": "07:15",
    "evening_brief_time": "22:30",
    "watch_interval_hours": 2,
    "power_enabled": False,
    "last_sleep_date": None,
    "postponed_until": None,
    "sleep_notice_date": None,
    "setup_complete": False,
}

MISSING = object()


class SettingsService:
    """Async access to hot configuration backed by the settings table."""

    def __init__(self) -> None:
        self._db: Any = None

    def bind(self, db: Any) -> None:
        self._db = db

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("SettingsService is not bound to a database")
        return self._db

    async def get(self, key: str, default: Any = MISSING) -> Any:
        try:
            row = await self.db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        except Exception:
            row = None
        if row is not None:
            try:
                return json.loads(row["value"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return row["value"]
        if default is not MISSING:
            return default
        return DEFAULT_SETTINGS.get(key)

    async def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, payload),
        )

    async def all(self) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError, json.JSONDecodeError):
                result[row["key"]] = row["value"]
        return result

    async def owner_id(self) -> int | None:
        value = await self.get("owner_id")
        if value is None and env.admin_id is not None:
            return env.admin_id
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def set_owner(self, user_id: int) -> None:
        await self.set("owner_id", int(user_id))

    async def timezone(self) -> ZoneInfo:
        name = await self.get("timezone") or env.default_timezone
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            try:
                return ZoneInfo(env.default_timezone)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                return ZoneInfo("UTC")

    async def now(self) -> datetime:
        return datetime.now(await self.timezone())

    async def today(self) -> date:
        return (await self.now()).date()

    async def is_recurring_paused(self, target: date) -> bool:
        raw = await self.get("pause_recurring_until")
        if not raw:
            return False
        text = str(raw).strip()
        try:
            pause_until = date.fromisoformat(text[:10])
        except ValueError:
            return False
        return target <= pause_until

    async def user_profile(self) -> str:
        return str(await self.get("user_profile") or "")

    async def coords(self) -> tuple[float, float] | None:
        lat = await self.get("lat")
        lon = await self.get("lon")
        if lat is None or lon is None:
            return None
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None


settings = SettingsService()
