"""Rain nowcast — "дождь идёт к тебе".

Uses Open-Meteo's 15-minutely precipitation forecast to detect rain/snow
arriving at the configured location within the next hour and pushes one short
mesh message ("🌧 Дождь через ~15 мин, ~40 мин"). The same approaching event is
announced only once (quiet window), so the channel doesn't get spammed.

Why minutely_15 instead of decoding RainViewer radar tiles: it's a single cached
HTTP call (reuses weather.py's proxy + TTL cache), needs no image decoding, and
gives a clean per-step precipitation amount over the point.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import weather

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "check_interval_minutes": 10,   # how often to poll the nowcast
    "lookahead_minutes": 60,        # how far ahead to watch for incoming rain
    "min_intensity_mm": 0.3,        # mm per 15-min step that counts as "rain"
    "quiet_minutes": 60,            # don't re-announce the same event within this
    "alert_ongoing": True,          # also warn if it's already raining now
}

# Intensity buckets, mm per 15-minute step.
_LIGHT_MAX = 0.75
_MODERATE_MAX = 2.5


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg.get("nowcast") or {})
    return out


def _intensity_word(mm: float) -> str:
    if mm < _LIGHT_MAX:
        return "слабый"
    if mm < _MODERATE_MAX:
        return "умеренный"
    return "сильный"


def _is_snow(code: Any) -> bool:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return False
    # WMO snow / snow grains / snow showers
    return c in {71, 73, 75, 77, 85, 86}


class NowcastState:
    """Tiny persistent state: which approaching event we last announced."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Failed to read nowcast state, starting fresh")
        return {"last_event_key": "", "last_sent_ts": 0, "last_check_ts": 0, "history": []}

    def _save_locked(self) -> None:
        hist = self._state.get("history") or []
        if len(hist) > 30:
            self._state["history"] = hist[-30:]
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def mark_check(self) -> None:
        with self._lock:
            self._state["last_check_ts"] = int(time.time())
            self._save_locked()

    def should_send(self, event_key: str, quiet_seconds: int) -> bool:
        with self._lock:
            now = int(time.time())
            if self._state.get("last_event_key") == event_key:
                # Same event — respect the quiet window.
                return (now - int(self._state.get("last_sent_ts") or 0)) >= quiet_seconds
            return True

    def mark_sent(self, event_key: str, text: str) -> None:
        with self._lock:
            self._state["last_event_key"] = event_key
            self._state["last_sent_ts"] = int(time.time())
            self._state.setdefault("history", []).append(
                {"key": event_key, "text": text, "ts": int(time.time())}
            )
            self._save_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_check_ts": self._state.get("last_check_ts", 0),
                "last_event_key": self._state.get("last_event_key", ""),
                "last_sent_ts": self._state.get("last_sent_ts", 0),
                "history": list((self._state.get("history") or [])[-10:]),
            }


def _now_index(times: list[str], utc_offset: int) -> int:
    """Index of the current 15-min step. Open-Meteo `time` strings are in the
    location's local time; we build "now" in that same local time via the
    response's utc_offset_seconds so it works regardless of the Pi's tz."""
    now_local = datetime.utcnow() + timedelta(seconds=int(utc_offset or 0))
    idx = 0
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt <= now_local:
            idx = i
        else:
            break
    return idx


def check(cfg: dict[str, Any], bridge: Any, state: NowcastState) -> Optional[dict[str, Any]]:
    """One nowcast cycle. Returns the sent alert dict, or None."""
    nc = _cfg(cfg)
    state.mark_check()
    if not nc.get("enabled"):
        return None

    loc = cfg.get("location") or {}
    if loc.get("latitude") is None:
        log.warning("Nowcast: location not configured, skipping")
        return None

    try:
        data = weather.fetch_minutely(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    except Exception:
        log.exception("Nowcast: minutely fetch failed")
        return None

    m = (data or {}).get("minutely_15") or {}
    times = m.get("time") or []
    precip = m.get("precipitation") or []
    codes = m.get("weather_code") or []
    if not times or not precip:
        return None

    utc_offset = data.get("utc_offset_seconds", 0)
    i0 = _now_index(times, utc_offset)

    min_mm = float(nc.get("min_intensity_mm", 0.3))
    lookahead_steps = max(1, int(nc.get("lookahead_minutes", 60)) // 15)
    quiet_seconds = max(0, int(nc.get("quiet_minutes", 60))) * 60
    city = loc.get("name") or ""
    suffix = f" в {city}" if city else ""

    def _p(i: int) -> float:
        try:
            return float(precip[i] or 0)
        except (TypeError, ValueError, IndexError):
            return 0.0

    current = _p(i0)

    # --- Case A: already raining now ---
    if current >= min_mm:
        if not nc.get("alert_ongoing", True):
            return None
        # How much longer (consecutive wet steps from now)?
        end = i0
        while end + 1 < len(precip) and _p(end + 1) >= min_mm:
            end += 1
        dur_min = (end - i0 + 1) * 15
        kind = "Снег" if _is_snow(codes[i0] if i0 < len(codes) else None) else "Дождь"
        word = _intensity_word(current)
        event_key = f"now:{times[i0]}"
        text = f"🌧 {kind} идёт сейчас{suffix} ({word}), ещё ~{dur_min} мин."
        if state.should_send(event_key, quiet_seconds):
            return _send(bridge, cfg, state, event_key, text)
        return None

    # --- Case B: dry now, rain approaching within lookahead ---
    for step in range(1, lookahead_steps + 1):
        i = i0 + step
        if i >= len(precip):
            break
        if _p(i) >= min_mm:
            eta_min = step * 15
            # Duration of the incoming event.
            end = i
            while end + 1 < len(precip) and _p(end + 1) >= min_mm:
                end += 1
            dur_min = (end - i + 1) * 15
            peak = max(_p(j) for j in range(i, end + 1))
            kind = "Снег" if _is_snow(codes[i] if i < len(codes) else None) else "Дождь"
            word = _intensity_word(peak)
            event_key = f"in:{times[i]}"
            text = f"🌧 {kind} через ~{eta_min} мин{suffix} ({word}), продлится ~{dur_min} мин."
            if state.should_send(event_key, quiet_seconds):
                return _send(bridge, cfg, state, event_key, text)
            return None

    return None


def _send(bridge: Any, cfg: dict[str, Any], state: NowcastState,
          event_key: str, text: str) -> Optional[dict[str, Any]]:
    mesh_cfg = cfg.get("mesh") or {}
    try:
        bridge.send_text_chunked(
            text,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            destination=mesh_cfg.get("destination", "broadcast"),
        )
        state.mark_sent(event_key, text)
        log.info("Nowcast sent: %s — %s", event_key, text)
        return {"key": event_key, "text": text}
    except Exception:
        log.exception("Failed to send nowcast")
        return None


def start_background_worker(get_cfg, bridge: Any, state: NowcastState) -> threading.Thread:
    """Run check() in a daemon thread every check_interval_minutes."""
    def loop():
        time.sleep(90)   # let the interface/scheduler settle first
        while True:
            interval = 10
            try:
                cfg = get_cfg()
                interval = max(2, int(_cfg(cfg).get("check_interval_minutes", 10)))
                check(cfg, bridge, state)
            except Exception:
                log.exception("Nowcast worker crashed (will retry)")
            time.sleep(interval * 60)

    t = threading.Thread(target=loop, daemon=True, name="rain-nowcast")
    t.start()
    return t


__all__ = ["NowcastState", "check", "start_background_worker", "DEFAULTS"]
