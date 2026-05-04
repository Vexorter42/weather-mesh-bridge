"""Open-Meteo wrapper: geocoding + current/daily/hourly forecast.

API key not required. Docs: https://open-meteo.com/en/docs
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes split into (label, emoji) so emoji can be turned off.
WMO_CODES_RU: dict[int, tuple[str, str]] = {
    0:  ("ясно",                       "☀️"),
    1:  ("в основном ясно",            "🌤"),
    2:  ("переменная облачность",      "⛅"),
    3:  ("пасмурно",                   "☁️"),
    45: ("туман",                      "🌫"),
    48: ("изморозь",                   "🌫"),
    51: ("мелкая морось",              "🌦"),
    53: ("морось",                     "🌦"),
    55: ("сильная морось",             "🌧"),
    56: ("ледяная морось",             "🌧"),
    57: ("сильная ледяная морось",     "🌧"),
    61: ("небольшой дождь",            "🌦"),
    63: ("дождь",                      "🌧"),
    65: ("сильный дождь",              "🌧"),
    66: ("ледяной дождь",              "🌧"),
    67: ("сильный ледяной дождь",      "🌧"),
    71: ("небольшой снег",             "🌨"),
    73: ("снег",                       "🌨"),
    75: ("сильный снег",               "❄️"),
    77: ("снежные зёрна",              "❄️"),
    80: ("ливни",                      "🌦"),
    81: ("сильные ливни",              "🌧"),
    82: ("очень сильные ливни",        "🌧"),
    85: ("снегопад",                   "🌨"),
    86: ("сильный снегопад",           "❄️"),
    95: ("гроза",                      "⛈"),
    96: ("гроза с градом",             "⛈"),
    99: ("сильная гроза с градом",     "⛈"),
}

WIND_DIRECTIONS_RU = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]


def wmo_text(code: int | None, use_emojis: bool = True) -> str:
    if code is None:
        return "—"
    label, emoji = WMO_CODES_RU.get(int(code), (f"код {code}", ""))
    if use_emojis and emoji:
        return f"{label} {emoji}"
    return label


def wind_direction(deg: float | None) -> str:
    if deg is None:
        return "—"
    idx = int((float(deg) + 22.5) // 45) % 8
    return WIND_DIRECTIONS_RU[idx]


def search_city(query: str, language: str = "ru", count: int = 8) -> list[dict[str, Any]]:
    """Search city by name. Returns list of candidates with lat/lon."""
    if not query or not query.strip():
        return []
    try:
        r = requests.get(
            GEOCODE_URL,
            params={"name": query.strip(), "count": count, "language": language, "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.error("Geocoding failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for item in data.get("results", []) or []:
        out.append(
            {
                "name": item.get("name", ""),
                "country": item.get("country", ""),
                "admin1": item.get("admin1", ""),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "timezone": item.get("timezone", "auto"),
            }
        )
    return out


def fetch_weather(latitude: float, longitude: float, timezone: str = "auto") -> dict[str, Any]:
    """Fetch current weather + 2 days forecast (today + tomorrow) + hourly data."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone or "auto",
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "is_day",
                "precipitation",
                "weather_code",
                "cloud_cover",
                "pressure_msl",
                "surface_pressure",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
            ]
        ),
        "hourly": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "weather_code",
                "precipitation_probability",
                "wind_speed_10m",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "apparent_temperature_max",
                "apparent_temperature_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
            ]
        ),
        "forecast_days": 2,
        "wind_speed_unit": "ms",
    }
    r = requests.get(FORECAST_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def hpa_to_mmhg(hpa: float | None) -> float | None:
    if hpa is None:
        return None
    return round(float(hpa) * 0.7500617, 1)


def _hour_index(hourly_times: list[str], date_iso: str, hour: int) -> int | None:
    """Find index of `<date>T<hour:02d>:00` in Open-Meteo hourly time array."""
    target = f"{date_iso}T{hour:02d}:00"
    for i, t in enumerate(hourly_times):
        if t == target:
            return i
    return None


def _hourly_at(hourly: dict[str, Any], idx: int | None, key: str):
    if idx is None:
        return None
    arr = hourly.get(key)
    if not arr or idx >= len(arr):
        return None
    return arr[idx]


def format_message(
    weather: dict[str, Any],
    fields: list[str],
    location_name: str = "",
    include_header: bool = True,
    use_emojis: bool = True,
) -> str:
    """Build a compact text message based on chosen fields.

    Supported field keys:
      - temp                       current temperature + condition
      - feels                      apparent (feels-like) temperature
      - humidity_pressure          humidity % + pressure mmHg
      - wind_precip                wind speed/direction + precipitation
      - forecast                   today's daily min/max + condition + precip prob
      - tomorrow_morning_evening   tomorrow at 09:00 and 21:00 (location tz)
    """
    cur = weather.get("current", {}) or {}
    daily = weather.get("daily", {}) or {}
    hourly = weather.get("hourly", {}) or {}
    lines: list[str] = []

    def emoji(e: str, with_space: bool = True) -> str:
        if not use_emojis:
            return ""
        return f"{e} " if with_space else e

    if include_header:
        head = f"{emoji('🌍')}Погода"
        if location_name:
            head += f" — {location_name}"
        lines.append(head)

    if "temp" in fields:
        t = cur.get("temperature_2m")
        code = cur.get("weather_code")
        line = emoji("🌡")
        if t is not None:
            line += f"{t:+.1f}°C"
        line += f", {wmo_text(code, use_emojis)}"
        lines.append(line)

    if "feels" in fields:
        a = cur.get("apparent_temperature")
        if a is not None:
            lines.append(f"{emoji('🤔')}Ощущается как {a:+.1f}°C")

    # Backward compat: humidity_pressure / wind_precip used to be combined fields.
    has_humidity = "humidity" in fields or "humidity_pressure" in fields
    has_pressure = "pressure" in fields or "humidity_pressure" in fields
    has_wind = "wind" in fields or "wind_precip" in fields
    has_precip = "precipitation" in fields or "wind_precip" in fields

    if has_humidity or has_pressure:
        parts: list[str] = []
        if has_humidity:
            h = cur.get("relative_humidity_2m")
            if h is not None:
                parts.append(f"{emoji('💧')}влажность {int(h)}%")
        if has_pressure:
            p = hpa_to_mmhg(cur.get("pressure_msl"))
            if p is not None:
                parts.append(f"{emoji('⏲')}давление {p} мм рт.ст.")
        if parts:
            lines.append(" · ".join(parts))

    if has_wind or has_precip:
        wparts: list[str] = []
        if has_wind:
            ws = cur.get("wind_speed_10m")
            wd = cur.get("wind_direction_10m")
            wg = cur.get("wind_gusts_10m")
            if ws is not None:
                w_str = f"{emoji('💨')}ветер {ws:.1f} м/с"
                if wd is not None:
                    w_str += f" {wind_direction(wd)}"
                if wg is not None and wg > (ws or 0) * 1.3:
                    w_str += f" (порывы {wg:.0f})"
                wparts.append(w_str)
        if has_precip:
            precip = cur.get("precipitation")
            if precip is not None and precip > 0:
                wparts.append(f"{emoji('🌧')}осадки {precip} мм")
        if wparts:
            lines.append(" · ".join(wparts))

    if "forecast" in fields:
        try:
            tmin = daily.get("temperature_2m_min", [None])[0]
            tmax = daily.get("temperature_2m_max", [None])[0]
            code = daily.get("weather_code", [None])[0]
            pprob = daily.get("precipitation_probability_max", [None])[0]
        except IndexError:
            tmin = tmax = code = pprob = None

        fparts: list[str] = []
        if tmin is not None and tmax is not None:
            fparts.append(f"{emoji('📅')}сегодня {tmin:+.0f}…{tmax:+.0f}°C")
        if code is not None:
            fparts.append(wmo_text(code, use_emojis))
        if pprob is not None and pprob > 0:
            fparts.append(f"{emoji('☔')}вероятность осадков {int(pprob)}%")
        if fparts:
            lines.append(" · ".join(fparts))

    if "tomorrow_morning_evening" in fields:
        # daily.time = ["YYYY-MM-DD today", "YYYY-MM-DD tomorrow"]
        tomorrow_date = None
        try:
            tomorrow_date = daily.get("time", [None, None])[1]
        except IndexError:
            pass

        morning_idx = evening_idx = None
        if tomorrow_date:
            times = hourly.get("time", []) or []
            morning_idx = _hour_index(times, tomorrow_date, 9)
            evening_idx = _hour_index(times, tomorrow_date, 21)

        seg_parts: list[str] = []

        def _block(label: str, idx: int | None) -> str | None:
            if idx is None:
                return None
            t = _hourly_at(hourly, idx, "temperature_2m")
            code = _hourly_at(hourly, idx, "weather_code")
            pprob = _hourly_at(hourly, idx, "precipitation_probability")
            chunks: list[str] = [label]
            if t is not None:
                chunks.append(f"{t:+.0f}°C")
            if code is not None:
                chunks.append(wmo_text(code, use_emojis))
            if pprob is not None and pprob > 0:
                chunks.append(f"{int(pprob)}%")
            return " ".join(chunks)

        m = _block("утро", morning_idx)
        e = _block("вечер", evening_idx)
        if m:
            seg_parts.append(m)
        if e:
            seg_parts.append(e)

        # Also append tomorrow's daily min/max as a brief summary line
        try:
            tmin = daily.get("temperature_2m_min", [None, None])[1]
            tmax = daily.get("temperature_2m_max", [None, None])[1]
            tcode = daily.get("weather_code", [None, None])[1]
        except IndexError:
            tmin = tmax = tcode = None

        head_parts: list[str] = [f"{emoji('🌅')}Завтра"]
        if tmin is not None and tmax is not None:
            head_parts.append(f"{tmin:+.0f}…{tmax:+.0f}°C")
        if tcode is not None:
            head_parts.append(wmo_text(tcode, use_emojis))
        if head_parts:
            lines.append(" · ".join(head_parts))
        if seg_parts:
            lines.append(" · ".join(seg_parts))

    return "\n".join(lines)


ALL_FIELDS = [
    {"key": "temp",                       "label": "Температура + состояние"},
    {"key": "feels",                      "label": "Температура по ощущению"},
    {"key": "humidity",                   "label": "Влажность"},
    {"key": "pressure",                   "label": "Давление"},
    {"key": "wind",                       "label": "Ветер"},
    {"key": "precipitation",              "label": "Осадки"},
    {"key": "forecast",                   "label": "Прогноз на сегодня"},
    {"key": "tomorrow_morning_evening",   "label": "Завтра: утро и вечер"},
]
