"""Open-Meteo geocoding + forecast (no API keys)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import settings
from .utils import esc, parse_hhmm

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = 1800

WMO: dict[int, tuple[str, str]] = {
    0: ("☀️", "Ясно"),
    1: ("🌤", "В основном ясно"),
    2: ("⛅️", "Переменная облачность"),
    3: ("☁️", "Пасмурно"),
    45: ("🌫", "Туман"),
    48: ("🌫", "Изморозь"),
    51: ("🌦", "Слабая морось"),
    53: ("🌦", "Морось"),
    55: ("🌧", "Сильная морось"),
    56: ("🌧", "Ледяная морось"),
    57: ("🌧", "Сильная ледяная морось"),
    61: ("🌧", "Небольшой дождь"),
    63: ("🌧", "Дождь"),
    65: ("🌧", "Сильный дождь"),
    66: ("🌧", "Ледяной дождь"),
    67: ("🌧", "Сильный ледяной дождь"),
    71: ("🌨", "Небольшой снег"),
    73: ("🌨", "Снег"),
    75: ("❄️", "Сильный снег"),
    77: ("🌨", "Снежные зёрна"),
    80: ("🌦", "Ливень"),
    81: ("🌧", "Сильный ливень"),
    82: ("⛈", "Очень сильный ливень"),
    85: ("🌨", "Снегопад"),
    86: ("❄️", "Сильный снегопад"),
    95: ("⛈", "Гроза"),
    96: ("⛈", "Гроза с градом"),
    99: ("⛈", "Сильная гроза с градом"),
}

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class WeatherError(RuntimeError):
    pass


@dataclass
class GeoResult:
    name: str
    country: str
    admin1: str
    lat: float
    lon: float
    timezone: str

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


async def geocode(city: str) -> GeoResult | None:
    query = (city or "").strip()
    if not query:
        return None
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            response = await http.get(
                GEOCODE_URL,
                params={"name": query, "count": 1, "language": "ru", "format": "json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - network/API failures are expected
            raise WeatherError(f"Геокодер недоступен: {exc}") from exc
    results = payload.get("results") or []
    if not results:
        return None
    item = results[0]
    return GeoResult(
        name=str(item.get("name") or query),
        country=str(item.get("country") or ""),
        admin1=str(item.get("admin1") or ""),
        lat=float(item["latitude"]),
        lon=float(item["longitude"]),
        timezone=str(item.get("timezone") or "UTC"),
    )


async def timezone_for_coords(lat: float, lon: float) -> str:
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            response = await http.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            response.raise_for_status()
            return str(response.json().get("timezone") or "UTC")
        except Exception as exc:  # noqa: BLE001
            raise WeatherError(f"Не удалось определить часовой пояс: {exc}") from exc


async def _forecast(lat: float, lon: float) -> dict[str, Any]:
    key = f"{lat:.3f},{lon:.3f}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": 4,
    }
    async with httpx.AsyncClient(timeout=18.0) as http:
        try:
            response = await http.get(FORECAST_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise WeatherError(f"Open-Meteo недоступен: {exc}") from exc
    _cache[key] = (time.time(), data)
    return data


def _hour_at(data: dict[str, Any], date_str: str, hour: int, field: str) -> Any:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get(field) or []
    needle = f"{date_str}T{hour:02d}:00"
    for index, value in enumerate(times):
        if value == needle and index < len(values):
            return values[index]
    return None


def _day_index(data: dict[str, Any], target: date) -> int | None:
    daily = data.get("daily") or {}
    days = daily.get("time") or []
    needle = target.isoformat()
    for index, value in enumerate(days):
        if value == needle:
            return index
    return None


def _advice(
    temp_min: float | None,
    temp_max: float | None,
    rain: int,
    wind: float | None,
    code: int,
    has_evening_event: bool,
) -> list[str]:
    tips: list[str] = []
    if rain >= 60:
        tips.append("☔️ Возьми зонт — дождь вероятен")
    elif rain >= 30:
        tips.append("🌦 Возможен дождь, зонт не помешает")
    if code in {71, 73, 75, 77, 85, 86}:
        tips.append("❄️ Снег: обувь нескользящая, перчатки")
    if temp_max is not None and temp_max >= 27:
        tips.append("🥵 Жарко: вода и головной убор")
    elif temp_min is not None and temp_min <= -5:
        tips.append("🧥 Морозно: тёплый слой и перчатки")
    elif temp_min is not None and temp_min <= 7:
        tips.append("🧥 Прохладно: куртка или свитер")
    if wind is not None and wind >= 30:
        tips.append("💨 Ветрено: капюшон вместо зонта")
    if has_evening_event:
        tips.append("🌙 Вечером мероприятие — проверь время выхода")
    if not tips:
        tips.append("🙂 Комфортная погода, одевайся по сезону")
    return tips


async def weather_text_for_date(
    target: date,
    has_evening_event: bool = False,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    if lat is None or lon is None:
        coords = await settings.coords()
        if coords is None:
            return None
        lat, lon = coords
    try:
        data = await _forecast(lat, lon)
    except WeatherError as exc:
        log.warning("Weather unavailable: %s", exc)
        return "🌦 <i>Прогноз погоды временно недоступен.</i>"
    index = _day_index(data, target)
    if index is None:
        return "🌦 <i>Прогноз доступен только на ближайшие дни.</i>"
    daily = data.get("daily") or {}
    code = int((daily.get("weather_code") or [0])[index] or 0)
    icon, description = WMO.get(code, ("🌡", "Погода"))
    t_max = _float_at(daily.get("temperature_2m_max"), index)
    t_min = _float_at(daily.get("temperature_2m_min"), index)
    rain = int(_float_at(daily.get("precipitation_probability_max"), index) or 0)
    wind = _float_at(daily.get("wind_speed_10m_max"), index)
    date_str = target.isoformat()
    points: list[str] = []
    for hour, label in ((7, "утро"), (13, "день"), (18, "вечер"), (21, "поздно")):
        temp = _hour_at(data, date_str, hour, "temperature_2m")
        if temp is not None:
            points.append(f"{label} {round(float(temp))}°")
    temp_line = f"🌡 {round(t_min) if t_min is not None else '?'}…{round(t_max) if t_max is not None else '?'}°C"
    if points:
        temp_line += f" · {' · '.join(points)}"
    lines = [
        f"<b>{icon} Погода: {esc(description)}</b>",
        temp_line,
        f"☔️ Осадки: {rain}%" + (f" · 💨 ветер {round(wind)} км/ч" if wind is not None else ""),
    ]
    for tip in _advice(t_min, t_max, rain, wind, code, has_evening_event):
        lines.append(tip)
    return "\n".join(lines)


def _float_at(values: Any, index: int) -> float | None:
    try:
        value = values[index]
        return float(value) if value is not None else None
    except (TypeError, IndexError, ValueError):
        return None


async def weather_for_today() -> str | None:
    now = await settings.now()
    return await weather_text_for_date(now.date())


async def weather_for_tomorrow() -> str | None:
    now = await settings.now()
    return await weather_text_for_date(now.date() + timedelta(days=1))


async def city_for_coords(lat: float, lon: float) -> str:
    """Open-Meteo has no reverse geocoding; use a readable fallback label."""
    return f"Геопозиция ({lat:.3f}, {lon:.3f})"


async def sunset_for_date(target: date) -> str | None:
    coords = await settings.coords()
    if not coords:
        return None
    try:
        data = await _forecast(*coords)
        index = _day_index(data, target)
        if index is None:
            return None
        values = (data.get("daily") or {}).get("sunset") or []
        if index < len(values) and values[index]:
            return str(values[index]).split("T")[-1]
    except WeatherError:
        return None
    return None


def cached_weather_keys() -> list[str]:
    return list(_cache)
