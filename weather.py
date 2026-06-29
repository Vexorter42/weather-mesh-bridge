"""Open-Meteo wrapper: geocoding + current/daily/hourly forecast.

API key not required. Docs: https://open-meteo.com/en/docs
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# Unix ts of the last successful real Open-Meteo fetch — surfaced on the health
# page so the user can tell at a glance whether weather is actually flowing.
_LAST_SUCCESS_TS: float = 0.0


def last_success_ts() -> int:
    return int(_LAST_SUCCESS_TS)


def _ttl_cache(ttl_seconds: int):
    """Memoise a function's successful (non-None) result per-args for ttl_seconds.

    Drastically cuts Open-Meteo calls — the dashboard polls weather often and
    Open-Meteo rate-limits per IP (429), which bites hard behind a shared VPN
    exit node. Cached data is reused instead of re-fetching every time.
    """
    def deco(fn):
        store: dict = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            hit = store.get(key)
            if hit and now - hit[0] < ttl_seconds:
                return hit[1]                          # fresh cache hit
            try:
                val = fn(*args, **kwargs)
            except Exception:
                if hit:                                # serve stale on failure (429/timeout)
                    log.warning("%s failed; serving cached result (%.0fs old)",
                                fn.__name__, now - hit[0])
                    return hit[1]
                raise
            if val is not None:
                global _LAST_SUCCESS_TS
                _LAST_SUCCESS_TS = now
                store[key] = (now, val)
                if len(store) > 64:
                    for k in [k for k, v in store.items() if now - v[0] > ttl_seconds]:
                        store.pop(k, None)
                return val
            return hit[1] if hit else val              # best-effort None → stale if any
        return wrapper
    return deco

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Optional outbound proxy (Open-Meteo can be blocked by some ISPs). Set via
# set_proxy() from app.py — reuses the same SOCKS5/VLESS proxy as the rest.
_PROXIES: dict | None = None


def set_proxy(url: str | None) -> None:
    """Route all Open-Meteo requests through a SOCKS5/HTTP proxy. Empty = direct."""
    global _PROXIES
    url = (url or "").strip()
    if not url:
        _PROXIES = None
        return
    if url.startswith("socks5://"):
        url = url.replace("socks5://", "socks5h://", 1)   # DNS through proxy
    _PROXIES = {"http": url, "https": url}


def _rget(*args, **kwargs):
    """requests.get with the configured proxy applied (if any)."""
    kwargs.setdefault("proxies", _PROXIES)
    return requests.get(*args, **kwargs)

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
        r = _rget(
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


@_ttl_cache(300)            # 5 min — current weather changes slowly
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
                "visibility",
                "soil_temperature_0cm",
            ]
        ),
        "hourly": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "weather_code",
                "precipitation_probability",
                "wind_speed_10m",
                "relative_humidity_2m",
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
    r = _rget(FORECAST_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


@_ttl_cache(300)            # 5 min — nowcast refreshes often
def fetch_minutely(latitude: float, longitude: float, timezone: str = "auto") -> dict[str, Any]:
    """15-minutely precipitation nowcast for the next hours (Open-Meteo).

    Returns the raw payload; the caller reads `minutely_15.time` /
    `minutely_15.precipitation` and `utc_offset_seconds` to locate "now"."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone or "auto",
        "minutely_15": "precipitation,weather_code",
        "forecast_days": 1,
    }
    r = _rget(FORECAST_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


@_ttl_cache(1800)           # 30 min — water temp barely moves
def fetch_water_temperature(
    latitude: float, longitude: float, timezone: str = "auto"
) -> dict[str, Any] | None:
    """Best-effort water-surface temperature.

    1) Try Open-Meteo **Marine API** — accurate, works only for sea/ocean coords.
    2) Fall back to a **seasonal estimate** from the 7-day mean air temperature
       (rivers / lakes in temperate climate roughly follow the running air-temp
       average with a seasonal offset).

    Returns dict { "value": float_C, "source": "marine" | "estimated" } or None
    if even the air-temp history is unavailable.
    """
    # --- 1) Try the marine grid first
    try:
        r = _rget(
            MARINE_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone or "auto",
                "current": "sea_surface_temperature",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        t = (data.get("current") or {}).get("sea_surface_temperature")
        if t is not None:
            return {"value": float(t), "source": "marine"}
    except Exception as exc:
        log.info("Marine API has no data here (likely inland): %s", exc)

    # --- 2) Fallback: estimate from 7-day mean air temperature
    try:
        r = _rget(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone or "auto",
                "past_days": 7,
                "forecast_days": 1,
                "hourly": "temperature_2m",
            },
            timeout=12,
        )
        r.raise_for_status()
        hourly = (r.json().get("hourly") or {}).get("temperature_2m") or []
        temps = [float(t) for t in hourly if t is not None]
        if not temps:
            return None
        mean_air = sum(temps) / len(temps)
    except Exception:
        log.exception("Fallback water-temp estimate failed")
        return None

    # Seasonal offset: water lags air by ~1-3 weeks.
    # In summer water is a few °C colder than the running air mean; in autumn
    # it stays warmer; in winter it bottoms out near 0-2°C (under ice cover);
    # in spring it warms slowly. Numbers below are empirical rules of thumb
    # for mid-latitudes (Russia/EU temperate zone).
    import datetime as _dt
    month = _dt.date.today().month
    if month in (12, 1, 2):                # winter — ice / freezing
        water = max(0.5, mean_air * 0.2 + 1.5)
    elif month in (3, 4, 5):               # spring — water still cold
        water = mean_air - 4.0
    elif month in (6, 7, 8):               # summer — water a bit cooler
        water = mean_air - 2.0
    else:                                   # autumn — water still warm
        water = mean_air + 1.5

    # Clip to plausible inland range
    water = max(0.0, min(water, 32.0))
    return {"value": round(water, 1), "source": "estimated"}


@_ttl_cache(900)            # 15 min
def fetch_air_quality(
    latitude: float, longitude: float, timezone: str = "auto"
) -> dict[str, Any] | None:
    """Pull European AQI, PM2.5, PM10, ozone and UV index from Open-Meteo Air
    Quality API. Returns a dict with whatever fields the response had, or None.
    """
    try:
        r = _rget(
            AIR_QUALITY_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone or "auto",
                "current": "european_aqi,pm10,pm2_5,ozone,uv_index",
            },
            timeout=12,
        )
        r.raise_for_status()
        cur = (r.json().get("current") or {})
    except Exception:
        log.exception("Air quality fetch failed")
        return None

    def _f(k):
        v = cur.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    out = {
        "aqi": _f("european_aqi"),
        "pm2_5": _f("pm2_5"),
        "pm10": _f("pm10"),
        "ozone": _f("ozone"),
        "uv_index": _f("uv_index"),
    }
    # If everything's None, treat as no data.
    if all(v is None for v in out.values()):
        return None
    return out


@_ttl_cache(1800)           # 30 min — yesterday's data is static
def fetch_yesterday(
    latitude: float, longitude: float, timezone: str = "auto"
) -> dict[str, Any] | None:
    """Pull yesterday's daily min/max/weather_code for the "vs yesterday" diff."""
    try:
        r = _rget(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone or "auto",
                "past_days": 1,
                "forecast_days": 0,
                "daily": "temperature_2m_min,temperature_2m_max,weather_code,precipitation_sum",
            },
            timeout=12,
        )
        r.raise_for_status()
        d = r.json().get("daily") or {}
    except Exception:
        log.exception("Yesterday fetch failed")
        return None

    def _first(arr_key, cast=float):
        arr = d.get(arr_key) or []
        if not arr or arr[0] is None:
            return None
        try:
            return cast(arr[0])
        except (TypeError, ValueError):
            return None

    return {
        "tmin": _first("temperature_2m_min"),
        "tmax": _first("temperature_2m_max"),
        "code": _first("weather_code", int),
        "precip": _first("precipitation_sum"),
    }


def aqi_label(aqi: float | None) -> str:
    """European AQI → human label. https://www.eea.europa.eu/themes/air/air-quality-index"""
    if aqi is None:
        return ""
    if aqi <= 20:  return "отлично"
    if aqi <= 40:  return "хорошо"
    if aqi <= 60:  return "средне"
    if aqi <= 80:  return "плохо"
    if aqi <= 100: return "очень плохо"
    return "крайне плохо"


def uv_label(uv: float | None) -> str:
    if uv is None:
        return ""
    if uv < 3:  return "низкий"
    if uv < 6:  return "средний"
    if uv < 8:  return "высокий"
    if uv < 11: return "очень высокий"
    return "экстремальный"


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

    if "vs_yesterday" in fields:
        # Injected by app.build_message as weather["_yesterday"] dict.
        y = weather.get("_yesterday")
        try:
            today_tmax = (daily.get("temperature_2m_max") or [None])[0]
            today_tmax = float(today_tmax) if today_tmax is not None else None
        except (TypeError, ValueError, IndexError):
            today_tmax = None
        if y and y.get("tmax") is not None and today_tmax is not None:
            diff = today_tmax - float(y["tmax"])
            if abs(diff) < 1.0:
                lines.append(f"{emoji('📊')}как вчера")
            else:
                word = "теплее" if diff > 0 else "холоднее"
                lines.append(f"{emoji('📈' if diff > 0 else '📉')}на {abs(diff):.0f}°C {word}, чем вчера")

    if "air_quality" in fields:
        aq = weather.get("_air_quality")
        if aq:
            parts: list[str] = []
            if aq.get("aqi") is not None:
                lbl = aqi_label(aq["aqi"])
                parts.append(f"AQI {int(aq['aqi'])}" + (f" ({lbl})" if lbl else ""))
            if aq.get("pm2_5") is not None:
                parts.append(f"PM2.5 {aq['pm2_5']:.0f}")
            if aq.get("pm10") is not None:
                parts.append(f"PM10 {aq['pm10']:.0f}")
            if parts:
                lines.append(f"{emoji('🌫')}воздух: " + ", ".join(parts))

    if "uv_index" in fields:
        aq = weather.get("_air_quality")
        uv = aq.get("uv_index") if aq else None
        if uv is not None:
            lbl = uv_label(uv)
            lines.append(f"{emoji('☀️')}УФ {uv:.0f}" + (f" ({lbl})" if lbl else ""))

    if "water_temp" in fields:
        # Injected by app.build_message via fetch_water_temperature.
        # Now a dict {"value", "source"} — "marine" is real, "estimated" is
        # derived from 7-day air-temp mean for inland rivers/lakes.
        wt = weather.get("_water_temp")
        if isinstance(wt, dict) and wt.get("value") is not None:
            v = float(wt["value"])
            prefix = "≈ " if wt.get("source") == "estimated" else ""
            lines.append(f"{emoji('🌊')}Вода {prefix}{v:+.1f}°C")
        elif isinstance(wt, (int, float)):
            # Backwards-compat in case old code path still returns a plain number
            lines.append(f"{emoji('🌊')}Вода {float(wt):+.1f}°C")

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
    {"key": "vs_yesterday",               "label": "Сравнение со вчера 📊"},
    {"key": "water_temp",                 "label": "Температура воды 🌊"},
    {"key": "humidity",                   "label": "Влажность"},
    {"key": "pressure",                   "label": "Давление"},
    {"key": "wind",                       "label": "Ветер"},
    {"key": "precipitation",              "label": "Осадки"},
    {"key": "air_quality",                "label": "Качество воздуха 🌫"},
    {"key": "uv_index",                   "label": "УФ-индекс ☀️"},
    {"key": "forecast",                   "label": "Прогноз на сегодня"},
    {"key": "tomorrow_morning_evening",   "label": "Завтра: утро и вечер"},
]
