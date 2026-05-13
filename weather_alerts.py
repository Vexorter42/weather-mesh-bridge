"""Weather alerts watcher.

Periodically pulls the configured city's forecast from Open-Meteo and broadcasts
a single mesh message when conditions cross user-set thresholds (thunderstorm,
strong wind, high precipitation chance, severe frost). Each alert type is
deduplicated per calendar day so the channel doesn't get spammed.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import weather

log = logging.getLogger(__name__)

# Default thresholds — overridable via config["alerts"]
DEFAULTS = {
    "enabled": False,
    "wind_threshold_ms": 15,        # порывы > 15 м/с считаются опасными
    "rain_prob_threshold": 80,      # вероятность осадков ≥ 80%
    "frost_threshold_c": -5,        # ночные заморозки до −5 и ниже
    "thunderstorm_alerts": True,    # текущая гроза
    "check_interval_minutes": 15,
}

# WMO weather codes that count as thunderstorm
THUNDERSTORM_CODES = {95, 96, 99}


class AlertsState:
    """Persistent state for alert deduplication and last-check timestamp."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state = self._load()
        self._history: list[dict[str, Any]] = list(self._state.get("history") or [])

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Failed to read alerts state, starting fresh")
        return {"sent_today": {}, "last_check_ts": 0, "history": []}

    def _save_locked(self) -> None:
        # Trim history to last 50 entries.
        if len(self._history) > 50:
            self._history = self._history[-50:]
        self._state["history"] = self._history
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---------------------------------------------------------------------

    def already_sent(self, key: str, today: str) -> bool:
        with self._lock:
            return self._state.get("sent_today", {}).get(key) == today

    def mark_sent(self, key: str, today: str, text: str) -> None:
        with self._lock:
            self._state.setdefault("sent_today", {})[key] = today
            self._history.append({
                "key": key, "text": text,
                "ts": int(time.time()),
            })
            self._save_locked()

    def mark_check(self) -> None:
        with self._lock:
            self._state["last_check_ts"] = int(time.time())
            self._save_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_check_ts": self._state.get("last_check_ts", 0),
                "sent_today": dict(self._state.get("sent_today") or {}),
                "history": list(self._history[-20:]),
            }


def _alerts_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg.get("alerts") or {})
    return out


def check(cfg: dict[str, Any], bridge: Any, state: AlertsState) -> list[dict[str, Any]]:
    """Run one check cycle; send all freshly-triggered alerts. Returns list of
    sent alerts {"key", "text"}.
    """
    alerts_cfg = _alerts_cfg(cfg)
    state.mark_check()

    if not alerts_cfg.get("enabled"):
        return []

    loc = cfg.get("location") or {}
    if loc.get("latitude") is None:
        log.warning("Alerts: city not configured, skipping")
        return []

    try:
        data = weather.fetch_weather(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    except Exception:
        log.exception("Alerts: weather fetch failed")
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    city = loc.get("name") or ""

    to_send: list[tuple[str, str]] = []

    # Текущая гроза
    if alerts_cfg.get("thunderstorm_alerts"):
        cur_code = (data.get("current") or {}).get("weather_code")
        try:
            cur_code = int(cur_code) if cur_code is not None else None
        except (TypeError, ValueError):
            cur_code = None
        if cur_code in THUNDERSTORM_CODES and not state.already_sent("thunderstorm", today):
            to_send.append((
                "thunderstorm",
                f"⚠️ ГРОЗА сейчас{' в ' + city if city else ''}. Будьте осторожны.",
            ))

    daily = data.get("daily") or {}

    # Сильный ветер на сегодня
    try:
        wind_max = (daily.get("wind_speed_10m_max") or [None])[0]
        wind_max = float(wind_max) if wind_max is not None else None
    except (TypeError, ValueError, IndexError):
        wind_max = None
    threshold = float(alerts_cfg.get("wind_threshold_ms", 15))
    if wind_max is not None and wind_max >= threshold and not state.already_sent("strong_wind", today):
        to_send.append((
            "strong_wind",
            f"⚠️ Сильный ветер сегодня{' в ' + city if city else ''}: до {wind_max:.0f} м/с.",
        ))

    # Высокая вероятность осадков
    try:
        prob = (daily.get("precipitation_probability_max") or [None])[0]
        prob = int(prob) if prob is not None else None
    except (TypeError, ValueError, IndexError):
        prob = None
    rain_threshold = int(alerts_cfg.get("rain_prob_threshold", 80))
    if prob is not None and prob >= rain_threshold and not state.already_sent("heavy_rain", today):
        to_send.append((
            "heavy_rain",
            f"⚠️ Высокая вероятность осадков сегодня{' в ' + city if city else ''}: {prob}%.",
        ))

    # Заморозки
    try:
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        tmin = float(tmin) if tmin is not None else None
    except (TypeError, ValueError, IndexError):
        tmin = None
    frost_threshold = float(alerts_cfg.get("frost_threshold_c", -5))
    if tmin is not None and tmin <= frost_threshold and not state.already_sent("frost", today):
        to_send.append((
            "frost",
            f"⚠️ Заморозки{' в ' + city if city else ''}: минимум {tmin:+.0f}°C.",
        ))

    if not to_send:
        return []

    mesh_cfg = cfg.get("mesh") or {}
    sent: list[dict[str, Any]] = []
    for key, text in to_send:
        try:
            bridge.send_text_chunked(
                text,
                channel_index=int(mesh_cfg.get("channel_index", 0)),
                destination=mesh_cfg.get("destination", "broadcast"),
            )
            state.mark_sent(key, today, text)
            sent.append({"key": key, "text": text})
            log.info("Weather alert sent: %s — %s", key, text)
        except Exception:
            log.exception("Failed to send weather alert %s", key)
    return sent


def start_background_worker(get_cfg, bridge: Any, state: AlertsState) -> threading.Thread:
    """Run check() in a daemon thread every check_interval_minutes."""
    def loop():
        # Stagger first run so we don't fire before scheduler/UI are ready.
        time.sleep(60)
        while True:
            try:
                cfg = get_cfg()
                interval = max(2, int(_alerts_cfg(cfg).get("check_interval_minutes", 15)))
                check(cfg, bridge, state)
            except Exception:
                log.exception("Alerts worker crashed (will retry)")
                interval = 15
            time.sleep(interval * 60)

    t = threading.Thread(target=loop, daemon=True, name="weather-alerts")
    t.start()
    return t


__all__ = ["AlertsState", "check", "start_background_worker", "DEFAULTS"]
