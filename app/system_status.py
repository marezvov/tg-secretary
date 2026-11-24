"""Server & AI model status monitor.

Inspects:
1. Host hardware: CPU, RAM, Disk, Uptime, Host OS / Architecture
2. AI models: LLM endpoint ping (ms), loaded models list, Whisper STT memory state,
   VLM toggle, heavy semaphore queue and database size.

Works seamlessly across Windows, Linux (Docker/Host) and macOS with zero external
dependencies, with optional psutil acceleration when present.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import runtime
from .config import env, settings
from .runtime import HEAVY_SEMAPHORE
from .stt_whisper import whisper

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None


def _format_bytes(num_bytes: int | float) -> str:
    """Format bytes into human-readable string (KB, MB, GB)."""
    val = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(val) < 1024.0 or unit == "ТБ":
            return f"{val:.1f} {unit}" if unit != "Б" else f"{int(val)} Б"
        val /= 1024.0
    return f"{val:.1f} ТБ"


def _format_duration(seconds: float | int) -> str:
    """Format seconds into Russian duration string."""
    secs = int(max(0, seconds))
    days = secs // 86400
    hours = (secs % 86400) // 3600
    minutes = (secs % 3600) // 60
    rem_secs = secs % 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days} д.")
    if hours > 0:
        parts.append(f"{hours} ч.")
    if minutes > 0:
        parts.append(f"{minutes} мин.")
    if rem_secs > 0 and days == 0 and hours == 0:
        parts.append(f"{rem_secs} сек.")
    if not parts:
        parts.append("0 сек.")
    return " ".join(parts)


def get_hardware_status() -> dict[str, Any]:
    """Inspect CPU, RAM, Disk, Uptime and OS details."""
    cpu_cores = os.cpu_count() or 1
    cpu_percent: float | None = None

    ram_total: int = 0
    ram_used: int = 0
    ram_available: int = 0
    ram_percent: float = 0.0

    host_uptime_sec: float | None = None

    # 1. Check with psutil if installed
    if psutil is not None:
        try:
            cpu_percent = float(psutil.cpu_percent(interval=None))
            vmem = psutil.virtual_memory()
            ram_total = vmem.total
            ram_available = vmem.available
            ram_used = vmem.used
            ram_percent = vmem.percent
            host_uptime_sec = time.time() - psutil.boot_time()
        except Exception:
            pass

    # 2. Windows fallback via ctypes
    if ram_total == 0 and sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                ram_total = int(stat.ullTotalPhys)
                ram_available = int(stat.ullAvailPhys)
                ram_used = ram_total - ram_available
                ram_percent = float(stat.dwMemoryLoad)

            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            host_uptime_sec = uptime_ms / 1000.0
        except Exception:
            pass

    # 3. Linux fallback via /proc
    if ram_total == 0 and os.path.exists("/proc/meminfo"):
        try:
            meminfo: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip().split()[0]
                        if val.isdigit():
                            meminfo[key] = int(val) * 1024
            if "MemTotal" in meminfo:
                ram_total = meminfo["MemTotal"]
                ram_available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
                ram_used = max(0, ram_total - ram_available)
                ram_percent = round((ram_used / ram_total) * 100.0, 1) if ram_total else 0.0
        except Exception:
            pass

    if host_uptime_sec is None and os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as handle:
                first = handle.read().split()[0]
                host_uptime_sec = float(first)
        except Exception:
            pass

    # 4. Disk usage
    db_file = Path(env.db_path)
    target_dir = str(db_file.parent if db_file.parent.exists() else ".")
    disk_total, disk_used, disk_free = 0, 0, 0
    disk_percent = 0.0
    try:
        disk_usage = shutil.disk_usage(target_dir)
        disk_total = disk_usage.total
        disk_free = disk_usage.free
        disk_used = disk_usage.used
        disk_percent = round((disk_used / disk_total) * 100.0, 1) if disk_total else 0.0
    except Exception:
        pass

    # DB file size
    db_size = 0
    if db_file.exists():
        try:
            db_size = db_file.stat().st_size
        except OSError:
            pass

    # Bot uptime
    bot_uptime_sec = max(0.0, time.time() - runtime.START_TIME)

    return {
        "os_name": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "hostname": socket.gethostname(),
        "python_version": platform.python_version(),
        "cpu_cores": cpu_cores,
        "cpu_percent": cpu_percent,
        "ram_total": ram_total,
        "ram_used": ram_used,
        "ram_available": ram_available,
        "ram_percent": ram_percent,
        "ram_text": f"{_format_bytes(ram_used)} / {_format_bytes(ram_total)} ({ram_percent:.0f}%)"
        if ram_total else "Н/Д",
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_percent": disk_percent,
        "disk_text": f"{_format_bytes(disk_used)} / {_format_bytes(disk_total)} ({disk_percent:.0f}%)"
        if disk_total else "Н/Д",
        "db_size_text": _format_bytes(db_size),
        "bot_uptime": _format_duration(bot_uptime_sec),
        "host_uptime": _format_duration(host_uptime_sec) if host_uptime_sec else "Н/Д",
    }


async def check_llm_status() -> dict[str, Any]:
    """Ping LLM /models endpoint, measure latency, and list loaded models."""
    base_url = env.llm_base_url.rstrip("/")
    models_url = f"{base_url}/models"
    configured_model = env.llm_model_name
    headers = {}
    if env.llm_api_key:
        headers["Authorization"] = f"Bearer {env.llm_api_key}"

    start_time = time.monotonic()
    latency_ms: float = 0.0
    available_models: list[str] = []
    is_online = False
    error_msg: str | None = None

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(models_url, headers=headers)
            latency_ms = (time.monotonic() - start_time) * 1000.0
            if resp.status_code == 200:
                is_online = True
                data = resp.json()
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    available_models = [
                        m.get("id") or m.get("name")
                        for m in data["data"]
                        if isinstance(m, dict) and (m.get("id") or m.get("name"))
                    ]
                elif isinstance(data, list):
                    available_models = [str(m) for m in data]
            else:
                error_msg = f"HTTP {resp.status_code}: {resp.text[:120]}"
    except httpx.ConnectError:
        error_msg = "Нет подключения (сервер выключен или недоступен)"
    except httpx.TimeoutException:
        error_msg = "Таймаут ожидания ответа (> 4 сек)"
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:120]

    model_active = configured_model in available_models if available_models else is_online

    return {
        "online": is_online,
        "base_url": base_url,
        "configured_model": configured_model,
        "latency_ms": round(latency_ms, 1) if is_online else None,
        "available_models": available_models[:10],
        "model_active": model_active,
        "error": error_msg,
    }


async def get_full_system_status() -> dict[str, Any]:
    """Aggregate hardware, LLM, Whisper, and scheduler status."""
    hardware = get_hardware_status()
    llm = await check_llm_status()

    # Whisper status
    whisper_setting = str(await settings.get("whisper_model") or "small")
    whisper_in_ram = whisper._model is not None
    whisper_last_used = (
        _format_duration(time.monotonic() - whisper._last_used) + " назад"
        if whisper._last_used > 0
        else "ещё не вызывался"
    )

    # VLM status
    vlm_enabled = bool(await settings.get("vlm_enabled"))
    vlm_model = env.llm_model_name

    # Semaphore / queue
    semaphore_busy = HEAVY_SEMAPHORE.locked()

    # Scheduler status
    scheduler_running = False
    scheduled_jobs_count = 0
    if runtime.scheduler is not None:
        try:
            scheduler_running = bool(runtime.scheduler.running)
            scheduled_jobs_count = len(runtime.scheduler.get_jobs())
        except Exception:
            pass

    return {
        "hardware": hardware,
        "llm": llm,
        "whisper": {
            "model_configured": whisper_setting,
            "in_ram": whisper_in_ram,
            "last_used": whisper_last_used,
        },
        "vlm": {
            "enabled": vlm_enabled,
            "model": vlm_model,
        },
        "semaphore_busy": semaphore_busy,
        "scheduler": {
            "running": scheduler_running,
            "jobs_count": scheduled_jobs_count,
        },
    }
