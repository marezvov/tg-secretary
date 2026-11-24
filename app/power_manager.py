"""Optional rtcwake power management with anti-loop safeguards."""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import shutil
from datetime import date, datetime, time, timedelta
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import env, settings
from .local_calendar import next_wake_datetime
from .utils import parse_datetime, parse_hhmm, to_db

log = logging.getLogger(__name__)

RTCWAKE_CANDIDATES = ("/usr/sbin/rtcwake", "/usr/bin/rtcwake")
SLEEP_WINDOW_MINUTES = 15
NOTICE_MINUTES = 5


def sudoers_instructions() -> str:
    user = getpass.getuser()
    return (
        "Для питания ПК нужен доступ к rtcwake без пароля.\n\n"
        "Создай файл <code>/etc/sudoers.d/tg-secretary</code>:\n"
        f"<pre>{user} ALL=(root) NOPASSWD: /usr/sbin/rtcwake</pre>\n"
        "Затем:\n"
        "<pre>sudo chmod 440 /etc/sudoers.d/tg-secretary\n"
        "sudo -n /usr/sbin/rtcwake --version</pre>"
    )


def _rtcwake_path() -> str | None:
    for candidate in RTCWAKE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("rtcwake")


async def check_rtcwake() -> tuple[bool, str]:
    if env.power_dry_run:
        return True, "POWER_DRY_RUN включён: команда сна будет только логироваться."
    path = _rtcwake_path()
    if path is None:
        if os.name == "nt":
            return False, "Модуль питания доступен только на Ubuntu/Linux (rtcwake)."
        return False, "rtcwake не найден. Установи util-linux."
    try:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except FileNotFoundError:
        return False, "sudo не найден или модуль запущен не на Linux."
    except asyncio.TimeoutError:
        return False, "Проверка rtcwake зависла. Проверь sudoers."
    except Exception as exc:  # noqa: BLE001
        return False, f"Не удалось проверить rtcwake: {exc}"
    if process.returncode == 0:
        return True, (stdout or stderr).decode(errors="replace").strip() or "rtcwake доступен."
    detail = (stderr or stdout).decode(errors="replace").strip()
    return False, detail or "sudo не разрешил запуск rtcwake."


async def enable_power(db: object) -> tuple[bool, str]:
    ok, message = await check_rtcwake()
    if not ok:
        return False, f"❌ {message}\n\n{sudoers_instructions()}"
    await settings.set("power_enabled", True)
    await settings.set("last_sleep_date", None)
    await settings.set("postponed_until", None)
    warning = "\n\n⚠️ " + message if env.power_dry_run else ""
    return True, "⚡ Питание ПК включено." + warning


async def disable_power() -> None:
    await settings.set("power_enabled", False)
    await settings.set("postponed_until", None)


def _sleep_datetime(now: datetime, sleep_at: time) -> datetime:
    candidate = datetime.combine(now.date(), sleep_at).replace(tzinfo=now.tzinfo)
    if candidate < now - timedelta(hours=2):
        candidate += timedelta(days=1)
    return candidate


async def _effective_sleep_dt(db: object, now: datetime) -> datetime | None:
    profile = await db.get_day_profile(now.weekday())
    if profile is None:
        return None
    sleep_at = parse_hhmm(profile["sleep_time"])
    if sleep_at is None:
        return None
    sleep_dt = _sleep_datetime(now, sleep_at)
    postponed_raw = await settings.get("postponed_until")
    postponed = parse_datetime(postponed_raw, now.tzinfo) if postponed_raw else None
    if postponed is not None and postponed > sleep_dt:
        return postponed
    return sleep_dt


async def power_tick(bot: object, db: object) -> None:
    if not bool(await settings.get("power_enabled")):
        return
    now = await settings.now()
    today = now.date().isoformat()
    last_sleep = str(await settings.get("last_sleep_date") or "")
    if last_sleep == today:
        return
    effective = await _effective_sleep_dt(db, now)
    if effective is None:
        return
    notice_date = str(await settings.get("sleep_notice_date") or "")
    notice_start = effective - timedelta(minutes=NOTICE_MINUTES)
    if notice_start <= now < effective and notice_date != today:
        await _send_sleep_notice(bot, db, now, effective)
        await settings.set("sleep_notice_date", today)
        return
    if effective <= now <= effective + timedelta(minutes=SLEEP_WINDOW_MINUTES):
        await _go_to_sleep(bot, db, now, effective)


async def _send_sleep_notice(bot: object, db: object, now: datetime, sleep_dt: datetime) -> None:
    wake = await next_wake_datetime(db, now)
    wake_text = wake.strftime("%d.%m %H:%M") if wake else "не настроено"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏸ Отложить на 1 час", callback_data="power:postpone")],
            [InlineKeyboardButton(text="🚫 Отменить на сегодня", callback_data="power:cancel")],
        ]
    )
    text = (
        "🌙 <b>Скоро сон ПК</b>\n\n"
        f"Выключение в <b>{sleep_dt.strftime('%H:%M')}</b>, "
        f"подъём в <b>{wake_text}</b>.\n\n"
        "Если ещё работаешь — отложи или отмени на сегодня."
    )
    try:
        from .ui_formatter import send_long

        await send_long(bot, int(await settings.owner_id() or 0), text, keyboard)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not send sleep notice: %s", exc)


async def _go_to_sleep(bot: object, db: object, now: datetime, sleep_dt: datetime) -> None:
    owner_id = int(await settings.owner_id() or 0)
    today = now.date().isoformat()
    wake = await next_wake_datetime(db, now)
    if wake is None:
        log.warning("No wake time configured; refusing to power off")
        if owner_id:
            await bot.send_message(owner_id, "⚠️ Не настроено время подъёма — сон отменён.")
        return
    # Set the guard before executing so a failed/partial shutdown cannot loop.
    await settings.set("last_sleep_date", today)
    timestamp = int(wake.timestamp())
    if owner_id:
        try:
            await bot.send_message(
                owner_id,
                f"💤 Ухожу в сон. Подъём в <b>{wake.strftime('%d.%m %H:%M')}</b>.",
            )
        except Exception:
            pass
    if env.power_dry_run:
        log.warning("POWER_DRY_RUN: would call rtcwake -m off -t %s", timestamp)
        if owner_id:
            await bot.send_message(owner_id, "🧪 DRY RUN: rtcwake не вызван.")
        return
    path = _rtcwake_path()
    if path is None:
        if owner_id:
            await bot.send_message(owner_id, "❌ rtcwake не найден, выключение отменено.")
        return
    try:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", path, "-m", "off", "-t", str(timestamp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except asyncio.TimeoutError:
            log.warning("rtcwake did not return within 45s (machine may be shutting down)")
            return
        if process.returncode != 0:
            detail = (stderr or stdout).decode(errors="replace").strip()
            log.error("rtcwake failed: %s", detail)
            if owner_id:
                await bot.send_message(
                    owner_id,
                    f"❌ rtcwake завершился с ошибкой: <code>{detail[:500]}</code>",
                )
    except Exception as exc:  # noqa: BLE001
        log.exception("Power off failed")
        if owner_id:
            await bot.send_message(owner_id, f"❌ Не удалось выключить ПК: {exc}")


async def postpone_one_hour() -> datetime:
    now = await settings.now()
    target = now + timedelta(hours=1)
    await settings.set("postponed_until", to_db(target))
    await settings.set("sleep_notice_date", None)
    return target


async def cancel_today() -> None:
    now = await settings.now()
    await settings.set("last_sleep_date", now.date().isoformat())
    await settings.set("sleep_notice_date", now.date().isoformat())
    await settings.set("postponed_until", None)
