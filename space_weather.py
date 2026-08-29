"""Space weather / HF-propagation watcher (NOAA SWPC, keyless).

Pulls the planetary K-index, F10.7 cm solar flux and solar-wind speed from
NOAA's Space Weather Prediction Center and:
  (a) answers the /космос command with the current state,
  (b) broadcasts a mesh alert on a geomagnetic storm (Kp>=5 => G1+).

Why it fits a LoRa/radio-mesh project: geomagnetic storms disturb HF/ionospheric
propagation, can degrade GPS, and push aurora down to mid latitudes. No API key.

Reuses weather_alerts.AlertsState for per-day alert dedup.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "kp_alert_threshold": 5,        # Kp>=5 == geomagnetic storm (G1)
    "check_interval_minutes": 30,
    "use_proxy": True,              # NOAA is geo-blocked from some regions
}

_SWPC = "https://services.swpc.noaa.gov"
_TIMEOUT = 12
_CACHE_TTL = 900  # 15 min — SWPC updates Kp ~every 3h anyway

_proxy_url = ""
_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_cache_lock = threading.Lock()

# Kp -> NOAA G-scale label
_G_LABEL = {
    0: "спокойно", 1: "G1 (слабая буря)", 2: "G2 (умеренная)",
    3: "G3 (сильная)", 4: "G4 (очень сильная)", 5: "G5 (экстремальная)",
}


def set_proxy(url: str) -> None:
    global _proxy_url
    _proxy_url = url or ""


def apply_proxy(cfg: dict[str, Any]) -> None:
    """Set the outbound proxy from config (NOAA needs it in geo-blocked regions)."""
    c = {**DEFAULTS, **(cfg.get("space_weather") or {})}
    prox = cfg.get("proxy") or {}
    set_proxy(prox.get("url") if c.get("use_proxy", True) and prox.get("url") else "")


def _proxies() -> Optional[dict]:
    if not _proxy_url:
        return None
    u = _proxy_url.strip()
    if u.startswith("socks5://"):
        u = u.replace("socks5://", "socks5h://", 1)
    return {"http": u, "https": u}


def _get_json(path: str) -> Any:
    r = requests.get(_SWPC + path, timeout=_TIMEOUT, proxies=_proxies(),
                     headers={"User-Agent": "weather-mesh-bridge"})
    r.raise_for_status()
    return r.json()


def _kp_to_g(kp: Optional[float]) -> int:
    if kp is None or kp < 5:
        return 0
    return min(5, int(kp) - 4)  # Kp5->G1 ... Kp9->G5


def fetch(force: bool = False) -> dict[str, Any]:
    """Current space-weather snapshot, TTL-cached with stale-on-failure."""
    with _cache_lock:
        if not force and _cache["data"] and (time.time() - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]

    out: dict[str, Any] = {"kp": None, "kp_time": None, "flux": None,
                           "wind_speed": None, "g_level": 0, "ok": False}
    try:
        ki = _get_json("/products/noaa-planetary-k-index.json")
        if isinstance(ki, list) and ki:
            last = ki[-1]
            out["kp_time"] = last.get("time_tag")
            try:
                out["kp"] = float(last.get("Kp"))
            except (TypeError, ValueError):
                pass
    except Exception:
        log.debug("SWPC Kp fetch failed", exc_info=True)
    try:
        fl = _get_json("/products/summary/10cm-flux.json")
        if isinstance(fl, list) and fl:
            out["flux"] = fl[-1].get("flux")
    except Exception:
        log.debug("SWPC flux fetch failed", exc_info=True)
    try:
        sw = _get_json("/products/summary/solar-wind-speed.json")
        if isinstance(sw, list) and sw:
            out["wind_speed"] = sw[-1].get("proton_speed")
    except Exception:
        log.debug("SWPC wind fetch failed", exc_info=True)

    out["g_level"] = _kp_to_g(out["kp"])
    out["ok"] = out["kp"] is not None
    if out["ok"]:
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["data"] = out
        return out
    # stale-on-failure — keep serving the last good snapshot
    with _cache_lock:
        if _cache["data"]:
            return _cache["data"]
    return out


def format_message(data: Optional[dict] = None) -> str:
    d = data or fetch()
    kp = d.get("kp")
    if kp is None:
        return "Космопогода: данные NOAA сейчас недоступны."
    g = d.get("g_level", 0)
    parts = [f"Kp={kp:.0f} ({_G_LABEL.get(g, '?')})"]
    if d.get("flux"):
        parts.append(f"F10.7={d['flux']}")
    if d.get("wind_speed"):
        try:
            parts.append(f"солн.ветер {float(d['wind_speed']):.0f} км/с")
        except (TypeError, ValueError):
            pass
    line = "☀️ Космопогода: " + ", ".join(parts) + "."
    if g >= 1:
        line += " Возможны помехи КВ-радиосвязи/GPS и полярное сияние в средних широтах."
    elif kp >= 4:
        line += " Магнитосфера возбуждена (Kp близок к буре)."
    return line


def check(cfg: dict[str, Any], bridge: Any, state: Any) -> list[dict[str, Any]]:
    """One check cycle; broadcast a mesh alert on a fresh geomagnetic storm."""
    c = {**DEFAULTS, **(cfg.get("space_weather") or {})}
    try:
        state.mark_check()
    except Exception:
        pass
    if not c.get("enabled"):
        return []

    apply_proxy(cfg)

    try:
        d = fetch()
    except Exception:
        log.exception("Space weather: fetch failed")
        return []
    kp = d.get("kp")
    if kp is None:
        return []

    threshold = float(c.get("kp_alert_threshold", 5))
    if kp < threshold:
        return []

    g = d.get("g_level", 0)
    today = datetime.now().strftime("%Y-%m-%d")
    # Dedup per G-level per day: re-alerts if the storm escalates to a higher G.
    key = f"geostorm_G{g}"
    if state.already_sent(key, today):
        return []

    text = (f"🌌 Геомагнитная буря: Kp={kp:.0f} ({_G_LABEL.get(g)}). "
            f"Возможны сбои КВ-радиосвязи и GPS, полярное сияние ниже обычного.")
    mesh_cfg = cfg.get("mesh") or {}
    try:
        bridge.send_text_chunked(
            text,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            destination=mesh_cfg.get("destination", "broadcast"),
        )
        state.mark_sent(key, today, text)
        log.info("Space weather alert sent: %s", text)
        return [{"key": key, "text": text}]
    except Exception:
        log.exception("Space weather: failed to send alert")
        return []


def start_background_worker(get_cfg, bridge: Any, state: Any) -> threading.Thread:
    def loop():
        time.sleep(90)  # stagger after startup
        while True:
            interval = 30
            try:
                cfg = get_cfg()
                interval = max(10, int({**DEFAULTS, **(cfg.get("space_weather") or {})}
                                       .get("check_interval_minutes", 30)))
                check(cfg, bridge, state)
            except Exception:
                log.exception("Space weather worker crashed (will retry)")
            time.sleep(interval * 60)

    t = threading.Thread(target=loop, daemon=True, name="space-weather")
    t.start()
    return t


__all__ = ["fetch", "format_message", "check", "start_background_worker", "DEFAULTS", "set_proxy"]
