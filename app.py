"""Weather → Heltec Mesh bridge: Flask UI + scheduler.

Run:
    python app.py
Then open http://<raspberry-ip>:5000 from any device on the LAN.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, jsonify, render_template, request

import commands
# Mark process start time for /uptime — must be set before anything heavy runs.
commands.BOT_START_TS = time.time()

import weather
import weather_alerts
import rain_nowcast
import proxy_manager
import mqtt_publisher
from chat_db import ChatDb
from history_db import HistoryDb
from meshbridge import MeshBridge
import llm
from telegram_bridge import DEFAULTS as TG_DEFAULTS, TELETHON_AVAILABLE, TelegramBridge
from telegram_status_bot import DEFAULTS as TGS_DEFAULTS, TelegramStatusBot
from telegram_command_bot import TelegramCommandBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("weather-mesh-bridge")

VERSION = "2.12.0"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PRESETS_PATH = BASE_DIR / "presets.local.json"
BACKUPS_DIR = BASE_DIR / "backups"

DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Central outbound-proxy config. One URL, per-service on/off toggles, plus the
# managed-Xray fields (subscription + chosen exit) for the «Прокси» tab.
PROXY_DEFAULTS = {
    "url": "",             # socks5://host:port / http://host:port — empty = direct
    "use_weather": True,   # Open-Meteo (forecast/air/water/yesterday)
    "use_radar": True,     # RainViewer tiles
    "use_telegram": True,  # Telegram bridge (t.me scrape / MTProto)
    "use_llm": True,       # LLM API
    "use_tgstatus": True,  # Telegram status-bot
    # Managed Xray (optional): bot drives a local VLESS tunnel from a subscription.
    "subscription_url": "",
    "exit_index": None,
    "exit_name": "",
    "managed": False,
    "auto_switch": False,
}

SUB_CACHE_PATH = BASE_DIR / "xray_sub.txt"   # decoded subscription (has UUIDs, gitignored)


def _proxy_for(cfg: dict[str, Any], service: str) -> str:
    """Effective proxy URL for one service. The central `proxy` section is the
    single source of truth: if its `url` is set, each service is proxied only
    when its `use_<service>` toggle is on. Legacy configs without a central
    section fall back to the old per-section proxy fields."""
    p = cfg.get("proxy")
    if isinstance(p, dict):
        url = (p.get("url") or "").strip()
        if not url:
            return ""
        return url if p.get(f"use_{service}", True) else ""
    # Legacy fallback — older configs stored the proxy per section.
    if service == "telegram":
        return ((cfg.get("telegram") or {}).get("proxy") or "").strip()
    if service == "llm":
        return ((cfg.get("llm") or {}).get("proxy") or "").strip()
    if service == "tgstatus":
        return ((cfg.get("telegram_status") or {}).get("proxy") or "").strip()
    # weather / radar previously reused telegram's (then llm's) proxy.
    return ((cfg.get("telegram") or {}).get("proxy")
            or (cfg.get("llm") or {}).get("proxy") or "").strip()


def _propagate_proxy(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve the central proxy into each service's own `proxy` field so the
    Telegram bridge / LLM client / status-bot (which read their sub-config
    independently via load_config) transparently honour the per-service
    toggles, without those modules needing to know about the central section."""
    if isinstance(cfg.get("telegram"), dict):
        cfg["telegram"]["proxy"] = _proxy_for(cfg, "telegram")
    if isinstance(cfg.get("llm"), dict):
        cfg["llm"]["proxy"] = _proxy_for(cfg, "llm")
    if isinstance(cfg.get("telegram_status"), dict):
        cfg["telegram_status"]["proxy"] = _proxy_for(cfg, "tgstatus")
    return cfg


def _apply_local_presets() -> None:
    """Seed Telegram-bridge keyword/geo/blocklist defaults from an optional,
    git-ignored `presets.local.json`. The committed code ships these empty so
    the public repo stays topic-neutral; dropping the one preset file into the
    project folder activates a ready-made preset (keywords, geo filter, etc.).

    File format (all keys optional):
        { "telegram": { "keywords": [...], "geo_filter": [...],
                        "blocklist_lines": [...] } }
    Only seeds DEFAULTS — never overrides an existing config.json.
    """
    if not PRESETS_PATH.exists():
        return
    try:
        data = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read presets.local.json — ignoring")
        return
    tg = (data or {}).get("telegram") or {}
    for key in ("keywords", "geo_filter", "blocklist_lines"):
        if isinstance(tg.get(key), list):
            TG_DEFAULTS[key] = list(tg[key])
    log.info("Loaded local presets from %s", PRESETS_PATH.name)


_apply_local_presets()


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

_cfg_lock = threading.Lock()


_LEGACY_FIELD_MAP = {
    "humidity_pressure": ["humidity", "pressure"],
    "wind_precip": ["wind", "precipitation"],
}


def _migrate_fields(fields: list[str] | None) -> list[str]:
    """Expand legacy combined keys into the new split ones, preserving order."""
    if not fields:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for f in fields:
        for new in _LEGACY_FIELD_MAP.get(f, [f]):
            if new not in seen:
                seen.add(new)
                out.append(new)
    return out


def _migrate_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate legacy field keys in schedules + seed central proxy. Returns (cfg, changed)."""
    changed = False
    for slot in cfg.get("schedules", []) or []:
        old = slot.get("fields") or []
        new = _migrate_fields(old)
        if new != old:
            slot["fields"] = new
            changed = True
    # Seed the central proxy section from old per-section proxy fields, so an
    # upgraded config keeps working through the proxy it already had set.
    if not isinstance(cfg.get("proxy"), dict):
        legacy = ((cfg.get("telegram") or {}).get("proxy")
                  or (cfg.get("llm") or {}).get("proxy")
                  or (cfg.get("telegram_status") or {}).get("proxy") or "").strip()
        cfg["proxy"] = {**PROXY_DEFAULTS, "url": legacy}
        changed = True
    else:
        for k, v in PROXY_DEFAULTS.items():
            cfg["proxy"].setdefault(k, v)
    return cfg, changed


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {
            "location": {"name": "", "country": "", "latitude": None, "longitude": None, "timezone": "auto"},
            "mesh": {
                "connection_type": "serial",
                "device_path": "auto",
                "tcp_host": "",
                "tcp_port": 4403,
                "channel_index": 0,
                "destination": "broadcast",
                "chunk_delay": 10,
            },
            "message": {"language": "ru", "include_header": True, "use_emojis": False},
            "commands": {"enabled": True, "reply_delay_min_s": 5, "reply_delay_max_s": 10},
            "alerts": dict(weather_alerts.DEFAULTS),
            "nowcast": dict(rain_nowcast.DEFAULTS),
            "mqtt": dict(mqtt_publisher.DEFAULTS),
            "schedules": [],
            "telegram": dict(TG_DEFAULTS),
            "telegram_status": dict(TGS_DEFAULTS),
            "llm": dict(llm.DEFAULTS),
            "proxy": dict(PROXY_DEFAULTS),
        }
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg, changed = _migrate_config(cfg)
    if changed:
        save_config(cfg)
    _propagate_proxy(cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------
# Bridge + scheduler singletons
# ---------------------------------------------------------------------------

CONFIG = load_config()
_mesh_cfg = CONFIG.get("mesh", {}) or {}

# Persistent chat store — SQLite DB next to config.json.
CHAT_DB = ChatDb(BASE_DIR / "chat.db")

# Mesh-node history (telemetry time-series + traceroute log) — separate DB.
HISTORY_DB = HistoryDb(BASE_DIR / "history.db")

BRIDGE = MeshBridge(
    connection_type=_mesh_cfg.get("connection_type", "serial"),
    device_path=_mesh_cfg.get("device_path", "auto"),
    tcp_host=_mesh_cfg.get("tcp_host", ""),
    tcp_port=int(_mesh_cfg.get("tcp_port", 4403)),
    chat_db=CHAT_DB,
)


def _handle_mesh_command(msg: dict[str, Any]) -> Optional[str]:
    """MeshBridge callback for incoming text packets that look like commands."""
    cfg = load_config()
    enabled = bool((cfg.get("commands") or {}).get("enabled", True))
    if not enabled:
        return None
    return commands.handle(msg, bridge=BRIDGE, cfg=cfg)


BRIDGE.set_command_handler(_handle_mesh_command)


def _apply_command_settings(cfg: dict[str, Any]) -> None:
    """Push command-related runtime settings (reply back-off) into the bridge."""
    c = cfg.get("commands") or {}
    BRIDGE.set_command_reply_delay(
        c.get("reply_delay_min_s", 5),
        c.get("reply_delay_max_s", 10),
    )


def _apply_weather_proxy(cfg: dict[str, Any]) -> None:
    """Route Open-Meteo requests through the proxy when `use_weather` is on
    (some ISPs block api.open-meteo.com)."""
    weather.set_proxy(_proxy_for(cfg, "weather"))


_apply_command_settings(CONFIG)
_apply_weather_proxy(CONFIG)

# Persistent state for weather-alerts dedup, plus background worker.
ALERTS_STATE = weather_alerts.AlertsState(BASE_DIR / "alerts_state.json")
weather_alerts.start_background_worker(load_config, BRIDGE, ALERTS_STATE)

# Rain nowcast — "дождь идёт к тебе" via Open-Meteo minutely_15.
NOWCAST_STATE = rain_nowcast.NowcastState(BASE_DIR / "nowcast_state.json")
rain_nowcast.start_background_worker(load_config, BRIDGE, NOWCAST_STATE)


def _mqtt_weather_state() -> Optional[dict[str, Any]]:
    """Current weather flattened for MQTT/HA (None if no location/fetch fails)."""
    loc = load_config().get("location") or {}
    if loc.get("latitude") is None:
        return None
    try:
        data = weather.fetch_weather(loc["latitude"], loc["longitude"], loc.get("timezone") or "auto")
    except Exception:
        return None
    cur = data.get("current") or {}
    return {
        "temperature": cur.get("temperature_2m"),
        "apparent_temperature": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed": cur.get("wind_speed_10m"),
        "pressure": cur.get("surface_pressure") or cur.get("pressure_msl"),
        "precipitation": cur.get("precipitation"),
        "weather_code": cur.get("weather_code"),
    }


def _mqtt_last_alert() -> Optional[dict[str, Any]]:
    hist = ALERTS_STATE.status().get("history") or []
    return hist[-1] if hist else None


MQTT_PUB = mqtt_publisher.MqttPublisher(
    load_config, _mqtt_weather_state, BRIDGE.get_known_nodes, _mqtt_last_alert)
MQTT_PUB.start_worker()


def _start_telemetry_collector(interval_seconds: int = 600) -> threading.Thread:
    """Snapshot node telemetry into history.db every few minutes so even quiet
    nodes accumulate a time series for the charts; prune old rows each cycle."""
    def loop():
        time.sleep(120)   # let the interface populate its NodeDB first
        while True:
            try:
                n = HISTORY_DB.add_telemetry_snapshot(BRIDGE.get_known_nodes())
                HISTORY_DB.prune()
                if n:
                    log.debug("Telemetry snapshot: %d nodes recorded", n)
            except Exception:
                log.exception("Telemetry collector crashed (will retry)")
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, daemon=True, name="telemetry-collector")
    t.start()
    return t


_start_telemetry_collector()


# --- Packet stats: buffer received packets, flush to history.db periodically ---
_pkt_batch: list = []
_pkt_lock = threading.Lock()


def _on_packet(ts, portnum, from_num):
    with _pkt_lock:
        _pkt_batch.append((ts, portnum, from_num))


BRIDGE.set_packet_callback(_on_packet)


def _start_stats_flusher(interval_seconds: int = 45) -> threading.Thread:
    def loop():
        time.sleep(30)
        while True:
            try:
                with _pkt_lock:
                    batch = _pkt_batch[:]
                    _pkt_batch.clear()
                if batch:
                    HISTORY_DB.ingest_packets(batch)
                HISTORY_DB.prune_traffic(30)
            except Exception:
                log.exception("Stats flusher crashed (will retry)")
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, daemon=True, name="stats-flusher")
    t.start()
    return t


_start_stats_flusher()


def _telegram_forward(text: str, channel_index: int, destination: str) -> None:
    """Mesh-send callback handed to TelegramBridge. Lives in app.py so it can
    re-use the configured chunk delay and mesh settings."""
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    BRIDGE.send_text_chunked(
        text,
        channel_index=int(channel_index if channel_index is not None else mesh_cfg.get("channel_index", 0)),
        destination=destination or mesh_cfg.get("destination") or "broadcast",
        chunk_delay=_chunk_delay(mesh_cfg),
    )


def _telegram_summarize(text: str) -> Optional[str]:
    """Condense a long Telegram message via the LLM before it goes to mesh.
    Returns None (keep original) if the LLM isn't configured or fails."""
    cfg = load_config()
    if not llm.is_enabled(cfg):
        return None
    try:
        target = int((cfg.get("telegram") or {}).get("summarize_target_chars") or 100)
    except (TypeError, ValueError):
        target = 100
    target = max(40, min(target, 800))
    sys_prompt = (
        "Ты — фильтр-сжиматель. На вход даётся сообщение, на выход — его краткая "
        "суть на РУССКОМ языке одной фразой (что, где, когда, что делать). "
        f"Не длиннее {target} символов. "
        "ВЫВОДИ ТОЛЬКО готовую фразу-выжимку и больше ничего: без рассуждений, "
        "без пояснений, без преамбул вроде «The user wants…» или «Вот сводка», "
        "без кавычек, без эмодзи, без markdown. Сразу текст выжимки."
    )
    user_msg = f"Сожми это сообщение:\n\n{text}"
    try:
        summary = llm.ask(user_msg, cfg, system_override=sys_prompt)
    except Exception:
        log.exception("Telegram summarize via LLM failed — forwarding original")
        return None
    # Hard-cap in case the model overshoots the target.
    summary = (summary or "").strip()
    if summary and len(summary) > target:
        summary = summary[:target - 1].rstrip() + "…"
    return summary or None


TELEGRAM_BRIDGE = TelegramBridge(
    session_path=BASE_DIR / "telegram.session",
    mesh_send_callback=_telegram_forward,
    summarize_callback=_telegram_summarize,
)
# Apply the persisted config now; auto-start if it's enabled. Telethon is only
# required for the "С API" (MTProto) mode — the default "web" mode needs nothing,
# so we must NOT gate the auto-start on TELETHON_AVAILABLE (that bug meant the
# bridge stayed down after every restart in web mode).
_tg_cfg_init = CONFIG.get("telegram") or {}
TELEGRAM_BRIDGE.configure(_tg_cfg_init)
if _tg_cfg_init.get("enabled") and (
        (_tg_cfg_init.get("mode") or "web") != "telethon" or TELETHON_AVAILABLE):
    try:
        r = TELEGRAM_BRIDGE.start()
        if not r.get("ok"):
            log.warning("Telegram bridge auto-start skipped: %s", r.get("error"))
    except Exception:
        log.exception("Telegram bridge auto-start crashed")


# --- Telegram status-bot (pins/edits a single message in a chat) ---

def _tg_status_save_partial(partial: dict[str, Any]) -> None:
    """Merge a small dict into config.json under telegram_status — used by the
    bot to persist its message_id after sendMessage."""
    with _cfg_lock:
        cfg = load_config()
        existing = cfg.get("telegram_status") or {}
        # Shallow-merge only the telegram_status section
        if "telegram_status" in partial:
            existing.update(partial["telegram_status"] or {})
            cfg["telegram_status"] = existing
        save_config(cfg)


def _tg_status_stats() -> dict[str, Any]:
    """Cheap status snapshot for the status-bot to embed in messages."""
    try:
        s = CHAT_DB.stats()
    except Exception:
        s = {}
    try:
        mesh = BRIDGE.status() or {}
    except Exception:
        mesh = {}
    s["mesh_connected"] = bool(mesh.get("connected"))
    s["mesh_nodes_known"] = mesh.get("nodes_known")
    s["mesh_nodes_online_2h"] = mesh.get("nodes_online_2h")
    s["mesh_nodes_online_1h"] = mesh.get("nodes_online_1h")
    return s


def _tg_status_weather() -> Optional[dict[str, Any]]:
    """Build a current-weather snapshot for the status-bot. Returns None if
    the configured city has no coords."""
    cfg = load_config()
    loc = cfg.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    try:
        data = weather.fetch_weather(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    except Exception:
        log.exception("status-bot weather fetch failed")
        return None
    cur = data.get("current") or {}
    code = cur.get("weather_code")
    text, emoji = weather.WMO_CODES_RU.get(int(code) if code is not None else -1, ("—", ""))
    return {
        "city": loc.get("name", ""),
        "temperature_c": cur.get("temperature_2m"),
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed_ms": cur.get("wind_speed_10m"),
        "condition_text": text,
        "condition_emoji": emoji,
    }


TELEGRAM_STATUS_BOT = TelegramStatusBot(
    config_save=_tg_status_save_partial,
    config_load=load_config,
    stats_provider=_tg_status_stats,
    weather_provider=_tg_status_weather,
)
_tgs_cfg_init = CONFIG.get("telegram_status") or {}
if _tgs_cfg_init.get("enabled"):
    try:
        r = TELEGRAM_STATUS_BOT.start()
        if not r.get("ok"):
            log.warning("Telegram status-bot auto-start skipped: %s", r.get("error"))
    except Exception:
        log.exception("Telegram status-bot auto-start crashed")


def _web_url() -> str:
    """Best-effort LAN URL of this web UI (for the /map command)."""
    import socket
    port = int(os.environ.get("WMB_PORT", "5000"))
    scheme = "https" if os.environ.get("WMB_HTTPS", "1") not in ("0", "false") else "http"
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return f"{scheme}://{ip}:{port}/"


# Interactive Telegram command bot (same token as the status-bot).
TELEGRAM_COMMAND_BOT = TelegramCommandBot(load_config, {
    "stats": _tg_status_stats,
    "nodes": BRIDGE.get_known_nodes,
    "weather": _tg_status_weather,
    "traceroute": lambda dest: BRIDGE.traceroute(
        dest, hop_limit=5,
        channel_index=int((load_config().get("mesh") or {}).get("channel_index", 0)),
        timeout=60),
    "airtime": lambda: _airtime_data(),      # defined later in the file
    "web_url": _web_url,
    "recent_alerts": lambda: _recent_alerts(),
    "traffic": BRIDGE.traffic_stats,
    "daily_report": lambda: _daily_report_text(),
    "activity_report": lambda: _activity_report_text(),
    "record_command": HISTORY_DB.record_command,
}, subs_path=BASE_DIR / "tg_subscribers.json")
TELEGRAM_COMMAND_BOT.start_worker()


def _recent_alerts() -> list[dict]:
    """Merge recent weather-alert + nowcast history for DM subscribers."""
    out: list[dict] = []
    try:
        out += (ALERTS_STATE.status().get("history") or [])
    except Exception:
        pass
    try:
        out += (NOWCAST_STATE.status().get("history") or [])
    except Exception:
        pass
    return sorted(out, key=lambda a: a.get("ts") or 0)


_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_RU_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _fmt_num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)); d, dd = n % 10, n % 100
    if d == 1 and dd != 11:
        return one
    if 2 <= d <= 4 and not (12 <= dd <= 14):
        return few
    return many


def _pkts(n) -> str:
    return f"{_fmt_num(n)} {_plural(n, 'пакет', 'пакета', 'пакетов')}"


def _node_names() -> dict:
    return {n["num"]: (n.get("long_name") or n.get("short_name") or n.get("node_id") or f"!{n.get('num')}")
            for n in BRIDGE.get_known_nodes() if n.get("num") is not None}


def _region_label() -> str:
    """Region shown in the report header — the configured weather city, if any."""
    city = ((load_config().get("location") or {}).get("name") or "").strip()
    return f" ({city})" if city else ""


def _gateway_line() -> str:
    """Our single receiver (the Heltec gateway) — analogue of OwearBot's S1/S2."""
    mesh = BRIDGE.status()
    name = "Heltec"
    num = mesh.get("my_node_num")
    if num is not None:
        name = _node_names().get(num, name)
    online = bool(mesh.get("connected"))
    return f"{'🟢' if online else '🔴'} {name} — {'Online' if online else 'Offline'}"


def _daily_report_text() -> str:
    d = HISTORY_DB.daily_report_data()
    if not d.get("total"):
        return ("📊 Ежедневный отчёт\n\n"
                "Пока нет данных за прошлые сутки — статистика копится с момента запуска бота.")
    names = _node_names()
    lt = time.localtime(d.get("date_ts") or time.time())
    date_str = f"{lt.tm_mday} {_RU_MONTHS[lt.tm_mon]} {lt.tm_year}"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    L = ["📊 Ежедневный отчёт", f"за {date_str}{_region_label()}\n",
         f"📦 {_pkts(d['total'])}",
         f"🛰 Активных узлов: {d['active_nodes']}",
         f"🆕 Новых узлов: {d['new_nodes']}",
         f"⏱ Среднее: {_fmt_num(d['avg_hour'])} пакетов/час\n"]
    if d.get("peak_hour") is not None:
        ph = d["peak_hour"]
        L += ["🔥 Пик активности", f"{ph:02d}:00–{(ph + 1) % 24:02d}:00 • {_pkts(d['peak_count'])}\n"]
    if d.get("min_hour") is not None:
        L += ["🌙 Минимум", f"{d['min_hour']:02d}:00 • {_pkts(d['min_count'])}\n"]
    if d.get("top_nodes"):
        L.append("🏆 Самые активные узлы")
        for i, n in enumerate(d["top_nodes"]):
            L.append(f"{medals[i]} {names.get(n['num'], '!'+str(n['num']))} — {_pkts(n['count'])}")
        L.append("")
    L += ["📻 Приёмник", _gateway_line(), ""]
    if d.get("top_users"):
        L.append(f"👥 Самые активные пользователи ({d['users_total']} всего)")
        for i, u in enumerate(d["top_users"]):
            L.append(f"{medals[i]} {u['user']} — {u['count']} {_plural(u['count'], 'команда', 'команды', 'команд')}")
        L.append("")
    # events of the day
    yt = d.get("yesterday_total") or 0
    diff = round(100 * (d["total"] - yt) / yt) if yt else None
    ev = []
    if d["new_nodes"]:
        ev.append(f"🆕 Сегодня сеть пополнилась {d['new_nodes']} новыми узлами!")
    if d.get("is_record"):
        ev.append("🏆 Новый рекорд суточного трафика!" + (f" ({diff:+d}%)" if diff is not None else ""))
    if d["top_nodes"]:
        tn = d["top_nodes"][0]
        ev.append(f"⭐ Узел дня: {names.get(tn['num'], '!'+str(tn['num']))} — {_pkts(tn['count'])}")
    if d.get("peak_hour") is not None and d["peak_hour"] <= 6:
        ev.append(f"🌙 Необычно высокая ночная активность в {d['peak_hour']:02d}:00.")
    if ev:
        L.append("💡 События дня")
        L += ev
        L.append("")
    if diff is not None:
        arrow = "📈" if diff > 0 else ("📉" if diff < 0 else "➡️")
        L.append(f"📊 Динамика: {arrow} {diff:+d}% к вчерашнему дню\n")
    L += ["📊 Полная аналитика: /activity",
          "⚙️ Настройки уведомлений: /settings",
          "📈 Популярные команды: /mesh • /seen • /nodes • /traffic\n",
          f"🔄 Обновлено: {time.strftime('%H:%M %Z')}"]
    return "\n".join(L)


def _bar(val: int, mx: int, width: int = 8) -> str:
    if mx <= 0:
        return "○" * width
    filled = max(0, min(width, round(val / mx * width)))
    return "●" * filled + "○" * (width - filled)


def _activity_report_text() -> str:
    a = HISTORY_DB.activity_data(7)
    if not a.get("total"):
        return ("📈 Активность сети\n\n"
                "Пока мало данных — статистика копится с момента запуска бота.")
    L = ["📈 Активность сети Meshtastic\n",
         f"📊 За последние {a['days']} дней",
         f"📦 Всего пакетов: {_fmt_num(a['total'])}",
         f"📈 Среднее за день: {_fmt_num(a['avg_day'])}\n",
         "📅 По дням недели"]
    dm = max(a["by_dow"]) or 1
    for i, v in enumerate(a["by_dow"]):
        L.append(f"{_RU_DOW[i]} {_bar(v, dm)} {round(100 * v / dm)}% {_fmt_num(v)}")
    L.append("\n📊 По часам")
    hm = max(a["by_hour"]) or 1
    for h, v in enumerate(a["by_hour"]):
        L.append(f"{h:02d}:00 {_bar(v, hm, 6)} {round(100 * v / hm)}% {_fmt_num(v)}")
    peak_h = max(range(24), key=lambda h: a["by_hour"][h])
    min_h = min(range(24), key=lambda h: a["by_hour"][h])
    L.append(f"\n🔥 Пик: {peak_h:02d}:00 • {_fmt_num(a['by_hour'][peak_h])}")
    L.append(f"🌙 Минимум: {min_h:02d}:00 • {_fmt_num(a['by_hour'][min_h])}")
    L.append(f"🔄 {time.strftime('%H:%M')}")
    return "\n".join(L)


def _mesh_healthcheck():
    """Periodic probe — keeps Heltec connection alive. If the underlying TCP
    or Serial link dropped silently (common with WiFi blips), we close and
    reopen so the next send/receive doesn't fail.
    """
    try:
        # Lazily open if we don't have a connection yet.
        with BRIDGE._lock:
            iface = BRIDGE._iface
        if iface is None:
            log.info("Healthcheck: no interface, opening")
            try:
                BRIDGE.connect()
            except Exception:
                log.exception("Healthcheck: initial connect failed")
            return

        # We have an interface — probe it with a heartbeat. The meshtastic
        # library exposes either sendHeartbeat or _sendHeartbeat depending
        # on version, so try both.
        ok = False
        try:
            send_hb = getattr(iface, "sendHeartbeat", None) or getattr(iface, "_sendHeartbeat", None)
            if callable(send_hb):
                send_hb()
                ok = True
            else:
                # Fallback: just touch nodes to make sure the obj still works
                _ = getattr(iface, "nodes", None)
                ok = True
        except Exception:
            log.warning("Healthcheck: heartbeat failed, will reconnect")
            ok = False

        if not ok:
            try:
                BRIDGE.close()
                BRIDGE.connect()
                log.info("Healthcheck: reconnected after failed probe")
            except Exception:
                log.exception("Healthcheck: reconnect failed")
    except Exception:
        log.exception("Healthcheck crashed")

scheduler = BackgroundScheduler(timezone="UTC")  # cron triggers carry their own tz
scheduler.start()


# How robust the scheduler is when a slot fires.
PRE_CHECK_OFFSET_SECONDS = 10     # за 10 сек до сработки прогреваем соединение
SLOT_SEND_RETRY_ATTEMPTS = 3      # сколько раз пытаться отправить
SLOT_SEND_RETRY_DELAY_SEC = 15    # пауза между попытками


def _job_id(slot_id: str) -> str:
    return f"slot-{slot_id}"


def _precheck_job_id(slot_id: str) -> str:
    return f"precheck-{slot_id}"


def _trigger_for_slot(slot: dict[str, Any]) -> CronTrigger:
    hh, mm = slot.get("time", "12:00").split(":")
    days = slot.get("days") or DAYS_ORDER
    day_of_week = ",".join(d for d in days if d in DAYS_ORDER) or "mon-sun"
    tz = slot.get("timezone") or "Europe/Moscow"
    return CronTrigger(hour=int(hh), minute=int(mm), day_of_week=day_of_week, timezone=tz)


def _trigger_for_pre_check(slot: dict[str, Any], offset_seconds: int = PRE_CHECK_OFFSET_SECONDS):
    """Cron trigger that fires offset_seconds before the slot's main time.

    Returns None for slots so close to midnight that the pre-check would cross
    into the previous day (e.g. 00:00 slot → 23:59:50 previous day requires
    day-of-week shift, which we skip for simplicity).
    """
    hh, mm = slot.get("time", "12:00").split(":")
    days = slot.get("days") or DAYS_ORDER
    day_of_week = ",".join(d for d in days if d in DAYS_ORDER) or "mon-sun"
    tz = slot.get("timezone") or "Europe/Moscow"

    total = int(hh) * 3600 + int(mm) * 60 - int(offset_seconds)
    if total < 0:
        return None  # crosses midnight, skip pre-check

    return CronTrigger(
        hour=(total // 3600) % 24,
        minute=(total // 60) % 60,
        second=total % 60,
        day_of_week=day_of_week,
        timezone=tz,
    )


def _slot_pre_check(slot_id: str):
    """Warm up the mesh connection so the main send can fire immediately."""
    try:
        cfg = load_config()
        mesh_cfg = cfg.get("mesh", {}) or {}
        BRIDGE.configure(mesh_cfg)
        status = BRIDGE.connect()
        if status.get("connected"):
            log.info(
                "Slot %s pre-check OK (target=%s, nodes=%s)",
                slot_id,
                status.get("resolved_path") or status.get("tcp_host"),
                status.get("nodes_known", "?"),
            )
        else:
            log.warning(
                "Slot %s pre-check: связи нет (%s). Попробую переоткрыть на основной сработке.",
                slot_id, status.get("error", "unknown"),
            )
    except Exception:
        log.exception("Slot %s pre-check crashed", slot_id)


def _run_slot(slot_id: str):
    """APScheduler entry point — re-reads config so live edits stick.

    Tries up to SLOT_SEND_RETRY_ATTEMPTS times; between retries closes/reopens
    the mesh interface (most send failures are stale-socket / no-route-to-host).
    """
    cfg = load_config()
    slot = next((s for s in cfg.get("schedules", []) if s.get("id") == slot_id), None)
    if not slot or not slot.get("enabled", True):
        log.info("Slot %s skipped (missing or disabled)", slot_id)
        return

    fields = slot.get("fields") or []
    destination = slot.get("destination") or None  # None = use global mesh.destination
    last_err = None
    for attempt in range(1, SLOT_SEND_RETRY_ATTEMPTS + 1):
        try:
            send_now(cfg, fields, destination=destination)
            log.info("Slot %s sent (attempt %d/%d)", slot_id, attempt, SLOT_SEND_RETRY_ATTEMPTS)
            return
        except Exception as exc:
            last_err = exc
            log.warning(
                "Slot %s attempt %d/%d failed: %s",
                slot_id, attempt, SLOT_SEND_RETRY_ATTEMPTS, exc,
            )
            if attempt < SLOT_SEND_RETRY_ATTEMPTS:
                time.sleep(SLOT_SEND_RETRY_DELAY_SEC)
                # пересоздаём соединение перед следующей попыткой
                try:
                    BRIDGE.close()
                    BRIDGE.connect()
                except Exception:
                    log.exception("Slot %s reconnect before retry failed", slot_id)
    log.error(
        "Slot %s gave up after %d attempts. Last error: %s",
        slot_id, SLOT_SEND_RETRY_ATTEMPTS, last_err,
    )


def reschedule_all():
    cfg = load_config()
    # remove every job that we own (main + pre-check)
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("slot-") or job.id.startswith("precheck-"):
            scheduler.remove_job(job.id)

    for slot in cfg.get("schedules", []):
        if not slot.get("enabled", True):
            continue
        try:
            # Main send job at slot time
            scheduler.add_job(
                _run_slot,
                _trigger_for_slot(slot),
                args=[slot["id"]],
                id=_job_id(slot["id"]),
                replace_existing=True,
                # Если бот был оффлайн в момент срабатывания, всё равно отправь,
                # пока не прошло больше часа после запланированного времени.
                misfire_grace_time=3600,
                coalesce=True,
            )
            # Pre-check job 10 seconds before the slot — warms up mesh link
            pre_trigger = _trigger_for_pre_check(slot)
            if pre_trigger is not None:
                scheduler.add_job(
                    _slot_pre_check,
                    pre_trigger,
                    args=[slot["id"]],
                    id=_precheck_job_id(slot["id"]),
                    replace_existing=True,
                    misfire_grace_time=60,
                    coalesce=True,
                )
        except Exception:
            log.exception("Failed to schedule slot %s", slot.get("id"))


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def build_message(cfg: dict[str, Any], fields: list[str]) -> str:
    loc = cfg.get("location", {})
    if loc.get("latitude") is None or loc.get("longitude") is None:
        raise RuntimeError("Город не выбран. Открой UI и выбери город.")
    data = weather.fetch_weather(loc["latitude"], loc["longitude"], loc.get("timezone") or "auto")
    f = set(fields or [])
    # Separate endpoints — fetch only when the corresponding field is selected.
    if "water_temp" in f:
        data["_water_temp"] = weather.fetch_water_temperature(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    if "air_quality" in f or "uv_index" in f:
        data["_air_quality"] = weather.fetch_air_quality(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    if "vs_yesterday" in f:
        data["_yesterday"] = weather.fetch_yesterday(
            loc["latitude"], loc["longitude"], loc.get("timezone") or "auto"
        )
    msg_cfg = cfg.get("message", {}) or {}
    return weather.format_message(
        data,
        fields=fields or ["temp", "feels", "humidity", "pressure", "wind", "precipitation", "forecast"],
        location_name=loc.get("name", ""),
        include_header=msg_cfg.get("include_header", True),
        use_emojis=msg_cfg.get("use_emojis", False),
    )


def _chunk_delay(mesh_cfg: dict[str, Any]) -> float:
    """Read configured chunk delay, clamp to a sane range."""
    try:
        v = float(mesh_cfg.get("chunk_delay", 10))
    except (TypeError, ValueError):
        v = 10.0
    return max(0.0, min(v, 120.0))


def send_now(cfg: dict[str, Any], fields: list[str], destination: Optional[str] = None) -> dict[str, Any]:
    text = build_message(cfg, fields)
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    if not destination:
        destination = mesh_cfg.get("destination") or "broadcast"
    # send_text_chunked auto-splits sequences longer than the Meshtastic ~228-byte
    # limit and prefixes each chunk with "(i/N) ".
    result = BRIDGE.send_text_chunked(
        text,
        channel_index=int(mesh_cfg.get("channel_index", 0)),
        destination=destination,
        chunk_delay=_chunk_delay(mesh_cfg),
    )
    result["text"] = text
    return result


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    with _cfg_lock:
        return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_set_config():
    payload = request.get_json(force=True, silent=True) or {}
    with _cfg_lock:
        cfg = load_config()
        if "location" in payload:
            cfg["location"] = {**cfg.get("location", {}), **payload["location"]}
        if "mesh" in payload:
            cfg["mesh"] = {**cfg.get("mesh", {}), **payload["mesh"]}
        if "message" in payload:
            cfg["message"] = {**cfg.get("message", {}), **payload["message"]}
        if "commands" in payload:
            cfg["commands"] = {**cfg.get("commands", {}), **payload["commands"]}
        if "alerts" in payload:
            cfg["alerts"] = {**weather_alerts.DEFAULTS, **(cfg.get("alerts") or {}), **payload["alerts"]}
        if "nowcast" in payload:
            cfg["nowcast"] = {**rain_nowcast.DEFAULTS, **(cfg.get("nowcast") or {}), **payload["nowcast"]}
        if "mqtt" in payload:
            cfg["mqtt"] = {**mqtt_publisher.DEFAULTS, **(cfg.get("mqtt") or {}), **payload["mqtt"]}
        if "telegram" in payload:
            cfg["telegram"] = {**TG_DEFAULTS, **(cfg.get("telegram") or {}), **payload["telegram"]}
        if "telegram_status" in payload:
            cfg["telegram_status"] = {**TGS_DEFAULTS, **(cfg.get("telegram_status") or {}), **payload["telegram_status"]}
        if "llm" in payload:
            cfg["llm"] = {**llm.DEFAULTS, **(cfg.get("llm") or {}), **payload["llm"]}
        if "proxy" in payload:
            cfg["proxy"] = {**PROXY_DEFAULTS, **(cfg.get("proxy") or {}), **payload["proxy"]}
        # Resolve the central proxy into each service's own field before save,
        # so the bridge / LLM / status-bot pick up the per-service toggles.
        _propagate_proxy(cfg)
        save_config(cfg)
    BRIDGE.configure(cfg.get("mesh", {}) or {})
    _apply_command_settings(cfg)
    _apply_weather_proxy(cfg)
    # Push the freshest telegram config into the bridge. Reconfigure will
    # restart the worker if it's currently running.
    TELEGRAM_BRIDGE.configure(cfg.get("telegram") or {})
    MQTT_PUB.reconfigure()
    reschedule_all()
    return jsonify(cfg)


# ---------------------------------------------------------------------------
# Config backup / restore
# ---------------------------------------------------------------------------

_BACKUP_CORE = ("config.json", "presets.local.json", "alerts_state.json", "nowcast_state.json")
_BACKUP_DBS = ("chat.db", "history.db")
_RESTORE_ALLOWED = set(_BACKUP_CORE) | set(_BACKUP_DBS)


def _make_backup_zip(include_dbs: bool) -> bytes:
    """Zip of settings/state (+ optionally the SQLite DBs) as bytes."""
    names = list(_BACKUP_CORE) + (list(_BACKUP_DBS) if include_dbs else [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("wmb-backup-manifest.json", json.dumps(
            {"version": VERSION, "created": datetime.utcnow().isoformat() + "Z",
             "include_dbs": include_dbs}, ensure_ascii=False, indent=2))
        for name in names:
            p = BASE_DIR / name
            if p.exists():
                z.write(p, name)
    return buf.getvalue()


def _self_restart_later() -> None:
    """Restart the service shortly after the current response is flushed."""
    def go():
        time.sleep(1.2)
        try:
            subprocess.run(["sudo", "systemctl", "restart", "weather-mesh-bridge"], timeout=15)
        except Exception:
            log.exception("Self-restart via systemctl failed; exiting for systemd respawn")
            os._exit(0)
    threading.Thread(target=go, daemon=True).start()


@app.route("/api/backup/download", methods=["GET"])
def api_backup_download():
    include_dbs = request.args.get("dbs") in ("1", "true", "yes")
    data = _make_backup_zip(include_dbs)
    fname = f"wmb-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return Response(data, mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename={fname}",
        "Content-Length": str(len(data)),
    })


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Файл не передан"}), 400
    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    except Exception:
        return jsonify({"error": "Это не zip-архив бэкапа"}), 400
    names = set(zf.namelist())
    if "config.json" not in names:
        return jsonify({"error": "В архиве нет config.json — не похоже на бэкап WMB"}), 400
    try:
        json.loads(zf.read("config.json").decode("utf-8"))
    except Exception as exc:
        return jsonify({"error": f"config.json в архиве битый: {exc}"}), 400
    # Safety net: snapshot the current state before overwriting.
    try:
        BACKUPS_DIR.mkdir(exist_ok=True)
        (BACKUPS_DIR / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
         ).write_bytes(_make_backup_zip(True))
    except Exception:
        log.exception("Pre-restore snapshot failed (continuing anyway)")
    restored = []
    with _cfg_lock:
        for name in names:
            if name in _RESTORE_ALLOWED:
                (BASE_DIR / name).write_bytes(zf.read(name))
                restored.append(name)
    _self_restart_later()
    return jsonify({"ok": True, "restored": sorted(restored), "restarting": True})


def _start_autobackup(interval_seconds: int = 86400, keep: int = 14) -> threading.Thread:
    """Write a timestamped settings backup to backups/ daily; keep the last N."""
    def loop():
        time.sleep(300)
        while True:
            try:
                BACKUPS_DIR.mkdir(exist_ok=True)
                (BACKUPS_DIR / f"auto-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
                 ).write_bytes(_make_backup_zip(False))
                for old in sorted(BACKUPS_DIR.glob("auto-*.zip"))[:-keep]:
                    old.unlink(missing_ok=True)
            except Exception:
                log.exception("Auto-backup failed (will retry)")
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, daemon=True, name="auto-backup")
    t.start()
    return t


_start_autobackup()


def _rotate_proxy_exit() -> None:
    """Switch the managed Xray tunnel to the next exit in the subscription."""
    if not SUB_CACHE_PATH.exists():
        return
    exits = proxy_manager.public_exits(proxy_manager.parse_exits(
        SUB_CACHE_PATH.read_text(encoding="utf-8")))
    if not exits:
        return
    idxs = [e["index"] for e in exits]
    cur = (load_config().get("proxy") or {}).get("exit_index")
    pos = idxs.index(cur) if cur in idxs else -1
    nxt = idxs[(pos + 1) % len(idxs)]
    full = proxy_manager.parse_exits(SUB_CACHE_PATH.read_text(encoding="utf-8"))
    match = next((e for e in full if e["index"] == nxt), None)
    if not match:
        return
    proxy_manager.apply_exit(match["uri"])
    with _cfg_lock:
        c = load_config()
        c["proxy"] = {**PROXY_DEFAULTS, **(c.get("proxy") or {})}
        c["proxy"].update(exit_index=nxt, exit_name=match.get("name") or "")
        save_config(c)
    log.warning("Proxy auto-switched to exit #%s (%s)", nxt, match.get("name"))


def _start_proxy_autoswitch(interval_seconds: int = 300) -> threading.Thread:
    """When auto_switch is on and the managed tunnel is dead for two checks in
    a row, rotate to the next exit."""
    def loop():
        time.sleep(180)
        fails = 0
        while True:
            try:
                p = load_config().get("proxy") or {}
                if p.get("managed") and p.get("auto_switch") and SUB_CACHE_PATH.exists():
                    if proxy_manager.current_exit_ip() is None:
                        fails += 1
                        if fails >= 2:
                            _rotate_proxy_exit()
                            fails = 0
                    else:
                        fails = 0
                else:
                    fails = 0
            except Exception:
                log.exception("proxy auto-switch crashed (will retry)")
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, daemon=True, name="proxy-autoswitch")
    t.start()
    return t


_start_proxy_autoswitch()


@app.route("/api/proxy/test", methods=["POST"])
def api_proxy_test():
    """Check the proxy works by fetching our public IP through it. Body may
    carry {"url": "socks5://..."} to test the value being typed before saving;
    omit it to test the saved central proxy URL."""
    payload = request.get_json(force=True, silent=True) or {}
    url = ((payload.get("url") if "url" in payload
            else (load_config().get("proxy") or {}).get("url")) or "").strip()
    proxies = _proxies_dict(url)
    out: dict[str, Any] = {"url": url, "via_proxy": bool(proxies)}
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=12, proxies=proxies)
        r.raise_for_status()
        out["ok"] = True
        out["ip"] = (r.json() or {}).get("ip")
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return jsonify(out)


# --- Managed Xray: subscription → pick country → apply ---

@app.route("/api/proxy/subscription", methods=["POST"])
def api_proxy_subscription():
    """Fetch + parse a subscription URL into a list of exits. Caches the decoded
    body on the Pi (gitignored) and stores the URL in config."""
    payload = request.get_json(force=True, silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        url = ((load_config().get("proxy") or {}).get("subscription_url") or "").strip()
    if not url:
        return jsonify({"error": "Не задана ссылка-подписка"}), 400
    try:
        decoded = proxy_manager.fetch_subscription(url)
    except Exception as exc:
        return jsonify({"error": f"Не удалось получить подписку: {exc}"}), 502
    SUB_CACHE_PATH.write_text(decoded, encoding="utf-8")
    exits = proxy_manager.public_exits(proxy_manager.parse_exits(decoded))
    with _cfg_lock:
        cfg = load_config()
        cfg["proxy"] = {**PROXY_DEFAULTS, **(cfg.get("proxy") or {}), "subscription_url": url}
        save_config(cfg)
    return jsonify({"ok": True, "count": len(exits), "exits": exits})


@app.route("/api/proxy/exits", methods=["GET"])
def api_proxy_exits():
    """Cached exit list + which one is selected."""
    p = load_config().get("proxy") or {}
    exits = []
    if SUB_CACHE_PATH.exists():
        exits = proxy_manager.public_exits(proxy_manager.parse_exits(
            SUB_CACHE_PATH.read_text(encoding="utf-8")))
    return jsonify({
        "exits": exits,
        "selected": p.get("exit_index"),
        "selected_name": p.get("exit_name") or "",
        "managed": bool(p.get("managed")),
        "auto_switch": bool(p.get("auto_switch")),
        "has_subscription": bool(p.get("subscription_url")),
    })


@app.route("/api/proxy/ping", methods=["GET"])
def api_proxy_ping():
    """TCP latency (ms) from the Pi to every subscription exit."""
    if not SUB_CACHE_PATH.exists():
        return jsonify({"pings": []})
    exits = proxy_manager.public_exits(proxy_manager.parse_exits(
        SUB_CACHE_PATH.read_text(encoding="utf-8")))
    return jsonify({"pings": proxy_manager.ping_exits(exits)})


@app.route("/api/proxy/select", methods=["POST"])
def api_proxy_select():
    """Apply the chosen exit: rewrite the Xray config and restart it."""
    payload = request.get_json(force=True, silent=True) or {}
    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "Не указан индекс выхода"}), 400
    if not SUB_CACHE_PATH.exists():
        return jsonify({"error": "Сначала загрузи подписку"}), 400
    full = proxy_manager.parse_exits(SUB_CACHE_PATH.read_text(encoding="utf-8"))
    match = next((e for e in full if e["index"] == index), None)
    if not match:
        return jsonify({"error": "Выход не найден"}), 404
    try:
        proxy_manager.apply_exit(match["uri"])
    except Exception as exc:
        log.exception("apply_exit failed")
        return jsonify({"error": str(exc)}), 500
    with _cfg_lock:
        cfg = load_config()
        cfg["proxy"] = {**PROXY_DEFAULTS, **(cfg.get("proxy") or {})}
        cfg["proxy"].update(url=proxy_manager.PROXY_URL, managed=True,
                            exit_index=index, exit_name=match.get("name") or "")
        _propagate_proxy(cfg)
        save_config(cfg)
    _apply_weather_proxy(cfg)
    return jsonify({"ok": True, "exit_name": match.get("name"), "exit_ip": proxy_manager.current_exit_ip()})


@app.route("/api/fields", methods=["GET"])
def api_fields():
    return jsonify(weather.ALL_FIELDS)


@app.route("/api/cities", methods=["GET"])
def api_cities():
    q = request.args.get("q", "").strip()
    return jsonify(weather.search_city(q))


@app.route("/api/schedules", methods=["GET"])
def api_list_schedules():
    return jsonify(load_config().get("schedules", []))


@app.route("/api/schedules", methods=["POST"])
def api_create_schedule():
    payload = request.get_json(force=True, silent=True) or {}
    slot = {
        "id": uuid.uuid4().hex[:8],
        "time": payload.get("time", "12:00"),
        "enabled": bool(payload.get("enabled", True)),
        "days": payload.get("days") or DAYS_ORDER,
        "fields": _migrate_fields(payload.get("fields")) or ["temp", "feels", "humidity", "pressure", "wind", "precipitation", "forecast"],
        "timezone": payload.get("timezone") or "Europe/Moscow",
        "destination": payload.get("destination") or "broadcast",
    }
    with _cfg_lock:
        cfg = load_config()
        cfg.setdefault("schedules", []).append(slot)
        save_config(cfg)
    reschedule_all()
    return jsonify(slot)


@app.route("/api/schedules/<slot_id>", methods=["PATCH"])
def api_update_schedule(slot_id: str):
    payload = request.get_json(force=True, silent=True) or {}
    with _cfg_lock:
        cfg = load_config()
        slots = cfg.get("schedules", [])
        target = next((s for s in slots if s.get("id") == slot_id), None)
        if not target:
            return jsonify({"error": "not found"}), 404
        for k in ("time", "enabled", "days", "fields", "timezone", "destination"):
            if k in payload:
                if k == "fields":
                    target[k] = _migrate_fields(payload[k])
                else:
                    target[k] = payload[k]
        save_config(cfg)
    reschedule_all()
    return jsonify(target)


@app.route("/api/schedules/<slot_id>/run", methods=["POST"])
def api_run_schedule_now(slot_id: str):
    """Run a slot's payload immediately, regardless of its scheduled time."""
    cfg = load_config()
    slot = next((s for s in cfg.get("schedules", []) if s.get("id") == slot_id), None)
    if not slot:
        return jsonify({"error": "Слот не найден"}), 404
    try:
        result = send_now(cfg, slot.get("fields") or [], destination=slot.get("destination") or None)
    except Exception as exc:
        log.exception("Manual run of slot %s failed", slot_id)
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/schedules/<slot_id>", methods=["DELETE"])
def api_delete_schedule(slot_id: str):
    with _cfg_lock:
        cfg = load_config()
        before = len(cfg.get("schedules", []))
        cfg["schedules"] = [s for s in cfg.get("schedules", []) if s.get("id") != slot_id]
        save_config(cfg)
        removed = before - len(cfg["schedules"])
    reschedule_all()
    return jsonify({"removed": removed})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    payload = request.get_json(force=True, silent=True) or {}
    fields = payload.get("fields") or ["temp", "feels", "humidity", "pressure", "wind", "precipitation", "forecast"]
    cfg = load_config()
    try:
        text = build_message(cfg, fields)
    except Exception as exc:
        log.exception("Preview failed")
        return jsonify({"error": str(exc)}), 400
    return jsonify({"text": text})


@app.route("/api/send", methods=["POST"])
def api_send():
    payload = request.get_json(force=True, silent=True) or {}
    fields = payload.get("fields") or ["temp", "feels", "humidity", "pressure", "wind", "precipitation", "forecast"]
    cfg = load_config()
    try:
        result = send_now(cfg, fields)
    except Exception as exc:
        log.exception("Send failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/mesh/status", methods=["GET"])
def api_mesh_status():
    return jsonify(BRIDGE.status())


@app.route("/api/mesh/connect", methods=["POST"])
def api_mesh_connect():
    """Force-open the connection (serial or TCP) and report status."""
    cfg = load_config()
    BRIDGE.configure(cfg.get("mesh", {}) or {})
    return jsonify(BRIDGE.connect())


@app.route("/api/mesh/disconnect", methods=["POST"])
def api_mesh_disconnect():
    """Force-close the current Heltec connection.

    The next outbound action (manual send, scheduled slot, healthcheck) will
    automatically re-open it — this button is mostly useful for forcing a
    reconnect, freeing a stuck serial/TCP socket, or cleanly stepping aside
    so you can flash the Heltec from another tool.
    """
    try:
        BRIDGE.close()
    except Exception as exc:
        log.exception("Manual disconnect failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "connected": False})


@app.route("/api/chat/messages", methods=["GET"])
def api_chat_messages():
    """Return chat messages newer than ?since=<id> plus delivery-status
    updates for any pending outgoing messages the client passes via
    ?status_for=<id>,<id>,...
    """
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    # On the first load the client passes ?tail=N to fetch only the newest N
    # messages instead of replaying the whole history forward from id 0.
    tail_raw = request.args.get("tail")
    if since == 0 and tail_raw:
        try:
            tail_n = max(1, min(500, int(tail_raw)))
        except (TypeError, ValueError):
            tail_n = 200
        messages = CHAT_DB.get_recent(tail_n)
    else:
        messages = BRIDGE.get_messages(since)

    status_updates: list[dict[str, Any]] = []
    raw = (request.args.get("status_for") or "").strip()
    if raw:
        try:
            ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        except Exception:
            ids = []
        if ids:
            try:
                rows = CHAT_DB.get_by_ids(ids)
                status_updates = [
                    {
                        "id": r["id"],
                        "msg_id": r.get("msg_id"),
                        "delivery_status": r.get("delivery_status"),
                        "delivery_hops": r.get("delivery_hops"),
                    }
                    for r in rows
                ]
            except Exception:
                log.exception("status_for lookup failed")

    return jsonify({"messages": messages, "status_updates": status_updates})


@app.route("/api/chat/search", methods=["GET"])
def api_chat_search():
    """Full-text search across stored chat history. ?q=<query>&limit=<N>"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"messages": []})
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except (TypeError, ValueError):
        limit = 100
    try:
        results = CHAT_DB.search(q, limit=limit)
    except Exception as exc:
        log.exception("Chat search failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"messages": results, "query": q, "count": len(results)})


@app.route("/api/chat/reply", methods=["POST"])
def api_chat_reply():
    """Send a text reply to a previous mesh message.

    payload:
      - text, reply_to (required)
      - destination (str, optional) — broadcast / node id
      - channel (int, optional) — override channel index
    """
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    reply_to = payload.get("reply_to")
    if not text:
        return jsonify({"error": "Пустое сообщение"}), 400
    try:
        reply_to = int(reply_to)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный reply_to"}), 400
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    destination = (payload.get("destination") or mesh_cfg.get("destination") or "broadcast")
    channel_index = _resolve_channel(payload, mesh_cfg)
    try:
        result = BRIDGE.send_reply(
            text,
            reply_to,
            channel_index=channel_index,
            destination=destination,
            chunk_delay=_chunk_delay(mesh_cfg),
        )
    except Exception as exc:
        log.exception("Reply send failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/chat/react", methods=["POST"])
def api_chat_react():
    """Send an emoji reaction targeting a previous mesh message."""
    payload = request.get_json(force=True, silent=True) or {}
    emoji_text = (payload.get("emoji") or "").strip()
    reply_to = payload.get("reply_to")
    if not emoji_text:
        return jsonify({"error": "Пустой эмодзи"}), 400
    try:
        reply_to = int(reply_to)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный reply_to"}), 400
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    try:
        result = BRIDGE.send_reaction(
            emoji_text,
            reply_to,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            destination=mesh_cfg.get("destination", "broadcast"),
        )
    except Exception as exc:
        log.exception("Reaction send failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


def _resolve_channel(payload: dict[str, Any], mesh_cfg: dict[str, Any]) -> int:
    """Pick channel index from payload (per-message) or fall back to global."""
    raw = payload.get("channel")
    if raw is None:
        return int(mesh_cfg.get("channel_index", 0))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(mesh_cfg.get("channel_index", 0))


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    """Send a free-form text message into the mesh.

    payload:
      - text (str, required)
      - destination (str, optional): "broadcast" or node id (e.g. "!a1b2c3d4").
      - channel (int, optional): channel index; if omitted, uses global default.
    """
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Пустое сообщение"}), 400
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    destination = (payload.get("destination") or mesh_cfg.get("destination") or "broadcast").strip()
    if not destination:
        destination = "broadcast"
    channel_index = _resolve_channel(payload, mesh_cfg)
    try:
        result = BRIDGE.send_text_chunked(
            text,
            channel_index=channel_index,
            destination=destination,
            chunk_delay=_chunk_delay(mesh_cfg),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/nodes", methods=["GET"])
def api_nodes():
    """List nodes known by the connected Heltec — for DM picker and stats."""
    return jsonify(BRIDGE.get_known_nodes())


@app.route("/api/nodes/<int:num>/telemetry", methods=["GET"])
def api_node_telemetry(num: int):
    """Telemetry time-series for one node (battery/voltage/util/SNR over time)."""
    try:
        hours = max(1, min(168, int(request.args.get("hours", 24))))
    except (TypeError, ValueError):
        hours = 24
    return jsonify(HISTORY_DB.telemetry(num, since_seconds=hours * 3600))


@app.route("/api/channels", methods=["GET"])
def api_channels():
    """List channels configured on the connected Heltec — for chat sidebar."""
    return jsonify(BRIDGE.get_known_channels())


@app.route("/api/mesh/traceroute", methods=["POST"])
def api_mesh_traceroute():
    """Send a traceroute request to a node and return the discovered path.

    payload: { destination: "!a1b2c3d4", hop_limit?: int, timeout?: int }
    """
    payload = request.get_json(force=True, silent=True) or {}
    dest = (payload.get("destination") or "").strip()
    if not dest:
        return jsonify({"error": "Не указан адресат"}), 400
    try:
        hop_limit = int(payload.get("hop_limit") or 5)
        timeout = float(payload.get("timeout") or 60)
    except (TypeError, ValueError):
        hop_limit, timeout = 5, 60.0
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    try:
        result = BRIDGE.traceroute(
            dest,
            hop_limit=hop_limit,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            timeout=timeout,
        )
    except Exception as exc:
        log.exception("Traceroute crashed")
        return jsonify({"error": str(exc)}), 500
    # Log the attempt so the user can see how the route to this node changes.
    try:
        HISTORY_DB.add_traceroute({
            "dest_id": dest,
            "dest_name": result.get("from_name") or "",
            "ok": not result.get("error"),
            "route": [h.get("node_id") for h in (result.get("hops_forward") or [])],
            "route_back": [h.get("node_id") for h in (result.get("hops_back") or [])],
        })
    except Exception:
        log.exception("Failed to log traceroute history")
    return jsonify(result)


@app.route("/api/mesh/traceroute/history", methods=["GET"])
def api_traceroute_history():
    """Past traceroutes (optionally to one node) — to spot route changes."""
    dest = (request.args.get("dest") or "").strip() or None
    return jsonify(HISTORY_DB.traceroute_history(dest, limit=30))


@app.route("/api/heltec/info", methods=["GET"])
def api_heltec_info():
    """Current Heltec settings (name, region, role, hop limit, modem preset)."""
    try:
        return jsonify(BRIDGE.get_device_info())
    except Exception as exc:
        log.exception("Failed to read Heltec info")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/heltec/settings", methods=["POST"])
def api_heltec_settings():
    """Apply a partial update to Heltec settings.

    payload: any subset of { long_name, short_name, region, role, hop_limit,
                             modem_preset, tx_enabled, tx_power }
    """
    payload = request.get_json(force=True, silent=True) or {}
    try:
        result = BRIDGE.set_device_settings(payload)
    except Exception as exc:
        log.exception("Heltec settings update failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/heltec/reboot", methods=["POST"])
def api_heltec_reboot():
    """Tell the Heltec to reboot."""
    payload = request.get_json(force=True, silent=True) or {}
    try:
        delay = int(payload.get("delay", 5))
    except (TypeError, ValueError):
        delay = 5
    try:
        result = BRIDGE.reboot_device(delay_seconds=delay)
    except Exception as exc:
        log.exception("Heltec reboot failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/telegram/status", methods=["GET"])
def api_telegram_status():
    """Current state of the Telegram bridge + recent matches."""
    return jsonify(TELEGRAM_BRIDGE.status())


@app.route("/api/telegram/start", methods=["POST"])
def api_telegram_start():
    """Start the Telegram listening worker (also flips telegram.enabled=true)."""
    with _cfg_lock:
        cfg = load_config()
        cfg.setdefault("telegram", dict(TG_DEFAULTS))["enabled"] = True
        save_config(cfg)
    TELEGRAM_BRIDGE.configure(cfg.get("telegram") or {})
    result = TELEGRAM_BRIDGE.start()
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/telegram/stop", methods=["POST"])
def api_telegram_stop():
    """Stop the worker (also flips telegram.enabled=false)."""
    with _cfg_lock:
        cfg = load_config()
        cfg.setdefault("telegram", dict(TG_DEFAULTS))["enabled"] = False
        save_config(cfg)
    result = TELEGRAM_BRIDGE.stop()
    return jsonify(result)


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    """Fetch one t.me preview page via the configured proxy to verify the
    bridge can reach Telegram. Doesn't change persistent state."""
    payload = request.get_json(force=True, silent=True) or {}
    channel = (payload.get("channel") or "durov").strip()
    try:
        return jsonify(TELEGRAM_BRIDGE.test_fetch(channel))
    except Exception as exc:
        log.exception("Telegram test_fetch crashed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/tg-status/status", methods=["GET"])
def api_tg_status_get():
    """Return TG status-bot runtime info + the persisted config block."""
    cfg = load_config().get("telegram_status") or {}
    return jsonify({
        **TELEGRAM_STATUS_BOT.status(),
        "config": {
            "enabled":        bool(cfg.get("enabled")),
            "bot_token_set":  bool(cfg.get("bot_token")),
            "chat_id":        cfg.get("chat_id") or "",
            "update_seconds": cfg.get("update_seconds") or 60,
            "auto_pin":       cfg.get("auto_pin") is not False,
            "commands_enabled": bool(cfg.get("commands_enabled")),
            "daily_time":     cfg.get("daily_time") or "09:00",
            "proxy":          cfg.get("proxy") or "",
            "message_id":     cfg.get("message_id"),
            "show_mesh_stats": cfg.get("show_mesh_stats") is not False,
            "show_weather":   cfg.get("show_weather") is not False,
            "extra_text":     cfg.get("extra_text") or "",
        },
    })


@app.route("/api/tg-status/start", methods=["POST"])
def api_tg_status_start():
    with _cfg_lock:
        cfg = load_config()
        cfg.setdefault("telegram_status", dict(TGS_DEFAULTS))["enabled"] = True
        save_config(cfg)
    r = TELEGRAM_STATUS_BOT.start()
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/tg-status/stop", methods=["POST"])
def api_tg_status_stop():
    with _cfg_lock:
        cfg = load_config()
        cfg.setdefault("telegram_status", dict(TGS_DEFAULTS))["enabled"] = False
        save_config(cfg)
    return jsonify(TELEGRAM_STATUS_BOT.stop())


@app.route("/api/tg-status/update-now", methods=["POST"])
def api_tg_status_update_now():
    """Force an immediate edit — used by the «Обновить сейчас» button."""
    return jsonify(TELEGRAM_STATUS_BOT.update_now())


@app.route("/api/tg-status/mark-offline", methods=["POST"])
def api_tg_status_mark_offline():
    """Overwrite the pinned message with '🔴 OFFLINE'. Called by a systemd
    ExecStopPost hook or external cron when the main service goes down."""
    return jsonify(TELEGRAM_STATUS_BOT.mark_offline())


@app.route("/api/tg-status/reset-message", methods=["POST"])
def api_tg_status_reset_message():
    """Forget the stored message_id so the next tick sends a fresh message."""
    with _cfg_lock:
        cfg = load_config()
        s = cfg.setdefault("telegram_status", dict(TGS_DEFAULTS))
        s["message_id"] = None
        save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/telegram/test_send", methods=["POST"])
def api_telegram_test_send():
    """Send a TEST mesh-message using the bridge's destination/channel/prefix
    settings — full end-to-end pipeline check without waiting for a real
    Telegram event. Body: {text?: "..."} — optional custom text.
    """
    payload = request.get_json(force=True, silent=True) or {}
    text = payload.get("text")
    try:
        return jsonify(TELEGRAM_BRIDGE.test_send(text))
    except Exception as exc:
        log.exception("Telegram test_send crashed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/scheduler/jobs", methods=["GET"])
def api_jobs():
    out = []
    for job in scheduler.get_jobs():
        if not (job.id.startswith("slot-") or job.id.startswith("precheck-")):
            continue
        out.append(
            {
                "id": job.id,
                "kind": "precheck" if job.id.startswith("precheck-") else "slot",
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return jsonify(out)


@app.route("/api/alerts/status", methods=["GET"])
def api_alerts_status():
    """Returns last-check time + recent triggered alerts."""
    return jsonify(ALERTS_STATE.status())


@app.route("/api/alerts/check", methods=["POST"])
def api_alerts_check():
    """Force one alerts-check cycle right now — for manual testing."""
    cfg = load_config()
    try:
        sent = weather_alerts.check(cfg, BRIDGE, ALERTS_STATE)
    except Exception as exc:
        log.exception("Manual alerts check failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"sent": sent, "count": len(sent)})


@app.route("/api/nowcast/status", methods=["GET"])
def api_nowcast_status():
    """Nowcast config snapshot + last-run state for the UI."""
    nc = {**rain_nowcast.DEFAULTS, **(load_config().get("nowcast") or {})}
    return jsonify({"config": nc, "state": NOWCAST_STATE.status()})


@app.route("/api/nowcast/check", methods=["POST"])
def api_nowcast_check():
    """Force one nowcast cycle right now — for manual testing."""
    cfg = load_config()
    try:
        sent = rain_nowcast.check(cfg, BRIDGE, NOWCAST_STATE)
    except Exception as exc:
        log.exception("Manual nowcast check failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"sent": sent, "ok": True})


@app.route("/api/mqtt/status", methods=["GET"])
def api_mqtt_status():
    return jsonify(MQTT_PUB.status())


@app.route("/api/mqtt/test", methods=["POST"])
def api_mqtt_test():
    """Test-connect to the broker with posted (or saved) settings."""
    payload = request.get_json(force=True, silent=True) or {}
    section = payload.get("mqtt") if "mqtt" in payload else (load_config().get("mqtt") or {})
    return jsonify(mqtt_publisher.test_connection(section))


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Aggregate counters for the main-page dashboard."""
    s = CHAT_DB.stats()
    mesh = BRIDGE.status()
    s["mesh_connected"] = bool(mesh.get("connected"))
    s["mesh_nodes_known"] = mesh.get("nodes_known")
    return jsonify(s)


@app.route("/api/airtime", methods=["GET"])
def _airtime_data() -> dict[str, Any]:
    """LoRa channel-load: local node's channel utilization + air-util-TX (from
    telemetry) + our outgoing/incoming packet counts. Shared by the API and the
    Telegram command bot."""
    mesh = BRIDGE.status()
    my_num = mesh.get("my_node_num")
    chan_util = air_tx = None
    if my_num is not None:
        for n in BRIDGE.get_known_nodes():
            if n.get("num") == my_num:
                chan_util = n.get("channel_utilization")
                air_tx = n.get("air_util_tx")
                break
    out = {
        "connected": bool(mesh.get("connected")),
        "channel_utilization": chan_util,
        "air_util_tx": air_tx,
        "nodes_online_2h": mesh.get("nodes_online_2h"),
    }
    out.update(CHAT_DB.airtime_counts())
    return out


def api_airtime():
    """LoRa channel-load monitor endpoint."""
    return jsonify(_airtime_data())


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat() + "Z"})


# Cache the proxy exit-IP probe so the health page stays snappy.
_PROXY_EXIT_CACHE: dict[str, Any] = {"ts": 0.0, "ip": None, "via": False}


def _proxy_exit_ip(cfg: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    if now - _PROXY_EXIT_CACHE["ts"] < 60:
        return _PROXY_EXIT_CACHE
    proxies = _proxies_dict(_proxy_for(cfg, "weather"))
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=6, proxies=proxies)
        r.raise_for_status()
        _PROXY_EXIT_CACHE.update(ts=now, ip=(r.json() or {}).get("ip"), via=bool(proxies))
    except Exception:
        _PROXY_EXIT_CACHE.update(ts=now, ip=None, via=bool(proxies))
    return _PROXY_EXIT_CACHE


def _service_active(name: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr or "unknown").strip()
    except Exception:
        return "unknown"


@app.route("/api/health/full", methods=["GET"])
def api_health_full():
    """One-shot self-diagnostics for the health card."""
    cfg = load_config()
    mesh = BRIDGE.status()
    exit_info = _proxy_exit_ip(cfg)
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        disk = {
            "free_gb": round(du.free / 1e9, 1),
            "total_gb": round(du.total / 1e9, 1),
            "used_pct": round(100 * (du.total - du.free) / du.total) if du.total else None,
        }
    except Exception:
        disk = {"free_gb": None, "total_gb": None, "used_pct": None}
    return jsonify({
        "version": VERSION,
        "uptime_seconds": int(time.time() - commands.BOT_START_TS),
        "mesh_connected": bool(mesh.get("connected")),
        "nodes_known": mesh.get("nodes_known"),
        "nodes_online_2h": mesh.get("nodes_online_2h"),
        "weather_last_ok_ts": weather.last_success_ts() or None,
        "location_set": (cfg.get("location") or {}).get("latitude") is not None,
        "proxy_url": _proxy_for(cfg, "weather") or "",
        "proxy_exit_ip": exit_info.get("ip"),
        "proxy_via": exit_info.get("via"),
        "xray_active": _service_active("xray"),
        "disk": disk,
    })


# ---------------------------------------------------------------------------
# RainViewer radar proxy
# ---------------------------------------------------------------------------
# Many ISPs (esp. RU) reset the browser's connection to RainViewer's CDN. We
# proxy both the frames-manifest and the PNG tiles through this server so the
# browser only ever talks to the Pi (LAN), and the Pi fetches RainViewer —
# optionally via the same SOCKS5/VLESS proxy used for the Telegram bridge / LLM.

RAINVIEWER_MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_HOST = "https://tilecache.rainviewer.com"


def _proxies_dict(proxy: str) -> Optional[dict]:
    """requests-style proxies dict from a URL. socks5:// → socks5h:// so DNS
    resolves through the proxy. Empty → None (direct)."""
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://", 1)
    return {"http": proxy, "https": proxy}


def _outbound_proxies() -> Optional[dict]:
    """Proxy for RainViewer radar fetches — honours the `use_radar` toggle."""
    return _proxies_dict(_proxy_for(load_config(), "radar"))


@app.route("/api/radar/maps", methods=["GET"])
def api_radar_maps():
    """Proxy the RainViewer frames manifest (past + nowcast)."""
    try:
        r = requests.get(RAINVIEWER_MAPS_URL, timeout=12, proxies=_outbound_proxies())
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as exc:
        log.warning("Radar maps proxy failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


@app.route("/api/radar/tile/<int:z>/<int:x>/<int:y>", methods=["GET"])
def api_radar_tile(z: int, x: int, y: int):
    """Proxy a single radar PNG tile. ?path=/v2/radar/<hash>&color=2"""
    path = (request.args.get("path") or "").strip()
    color = request.args.get("color", "2")
    # Basic validation — only allow RainViewer radar/satellite paths.
    if not path.startswith("/v2/"):
        return Response(status=400)
    if not color.isdigit():
        color = "2"
    url = f"{RAINVIEWER_HOST}{path}/256/{z}/{x}/{y}/{color}/1_1.png"
    try:
        r = requests.get(url, timeout=12, proxies=_outbound_proxies())
        if r.status_code != 200:
            return Response(status=r.status_code)
        resp = Response(r.content, mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp
    except Exception as exc:
        log.debug("Radar tile proxy failed (%s): %s", url, exc)
        return Response(status=502)


@app.route("/api/llm/status", methods=["GET"])
def api_llm_status():
    """LLM config (without leaking the api_key) for the settings UI."""
    c = (load_config().get("llm") or {})
    return jsonify({
        "enabled": bool(c.get("enabled")),
        "api_key_set": bool(c.get("api_key")),
        "base_url": c.get("base_url") or llm.DEFAULTS["base_url"],
        "model": c.get("model") or llm.DEFAULTS["model"],
        "system_prompt": c.get("system_prompt") or llm.DEFAULTS["system_prompt"],
        "max_tokens": c.get("max_tokens") or llm.DEFAULTS["max_tokens"],
        "temperature": c.get("temperature", llm.DEFAULTS["temperature"]),
        "proxy": c.get("proxy") or "",
        "max_reply_chars": c.get("max_reply_chars") or llm.DEFAULTS["max_reply_chars"],
        "fallback_models": (", ".join(c["fallback_models"])
                            if isinstance(c.get("fallback_models"), list)
                            else (c.get("fallback_models") or "")),
        "context_memory": c.get("context_memory", True),
    })


@app.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """Verify connectivity/auth — asks the model to reply 'ok'."""
    return jsonify(llm.test_connection(load_config()))


@app.route("/api/llm/ask", methods=["POST"])
def api_llm_ask():
    """Ask the LLM a question from the web UI (doesn't touch the mesh)."""
    payload = request.get_json(force=True, silent=True) or {}
    q = (payload.get("question") or "").strip()
    if not q:
        return jsonify({"error": "Пустой вопрос"}), 400
    try:
        return jsonify({"answer": llm.ask(q, load_config())})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/system/info", methods=["GET"])
def api_system_info():
    """Version info — current commit, upstream HEAD, dirty flag."""
    import subprocess
    info = {"git_available": False}
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        cur = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, env=env, stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BASE_DIR, env=env, stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s"],
            cwd=BASE_DIR, env=env, stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        info.update({"git_available": True, "commit": cur, "branch": branch, "message": msg})
        try:
            subprocess.check_output(
                ["git", "fetch", "--quiet"],
                cwd=BASE_DIR, env=env, stderr=subprocess.DEVNULL, timeout=15,
            )
            ahead = subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD..@{u}"],
                cwd=BASE_DIR, env=env, stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            info["behind_count"] = int(ahead or 0)
        except Exception:
            info["behind_count"] = None
    except Exception as exc:
        info["error"] = str(exc)
    return jsonify(info)


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    """Restart the systemd service (passwordless sudo). Responds first, then
    restarts a beat later so the reply reaches the browser."""
    _self_restart_later()
    return jsonify({"ok": True, "restarting": True})


@app.route("/api/system/update", methods=["POST"])
def api_system_update():
    """Pull latest from git, install pip deps if requirements changed, and
    return diagnostic info. The systemd service is configured with
    Restart=on-failure so the user can manually restart via SSH after this
    completes, or we exit the Python process which systemd will respawn.
    """
    import subprocess
    payload = request.get_json(force=True, silent=True) or {}
    do_restart = bool(payload.get("restart"))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result: dict[str, Any] = {"steps": [], "ok": False}

    def _run(cmd, **kw):
        try:
            out = subprocess.check_output(
                cmd, cwd=BASE_DIR, env=env, stderr=subprocess.STDOUT,
                timeout=kw.get("timeout", 60),
            ).decode(errors="replace")
            result["steps"].append({"cmd": " ".join(cmd), "ok": True, "out": out[-600:]})
            return out, 0
        except subprocess.CalledProcessError as exc:
            result["steps"].append({
                "cmd": " ".join(cmd), "ok": False,
                "out": (exc.output or b"").decode(errors="replace")[-600:],
                "rc": exc.returncode,
            })
            return None, exc.returncode
        except Exception as exc:
            result["steps"].append({"cmd": " ".join(cmd), "ok": False, "out": str(exc)})
            return None, -1

    # 1. Save current commit for rollback hint
    head_before, _ = _run(["git", "rev-parse", "--short", "HEAD"])
    # 2. Pull
    out, rc = _run(["git", "pull", "--ff-only"], timeout=60)
    if rc != 0:
        result["error"] = "git pull failed — рабочая директория грязная или конфликты"
        return jsonify(result), 500
    # 3. Detect requirement changes
    head_after, _ = _run(["git", "rev-parse", "--short", "HEAD"])
    head_before = (head_before or "").strip()
    head_after = (head_after or "").strip()
    result["before"] = head_before
    result["after"] = head_after
    result["changed"] = head_before != head_after

    if result["changed"]:
        diff, _ = _run(["git", "diff", "--name-only", head_before, head_after], timeout=10)
        changed_files = (diff or "").strip().split("\n") if diff else []
        result["changed_files"] = changed_files
        if "requirements.txt" in changed_files:
            # Reinstall deps using the venv's pip
            venv_pip = str(BASE_DIR / ".venv" / "bin" / "pip")
            if Path(venv_pip).exists():
                _run([venv_pip, "install", "-r", str(BASE_DIR / "requirements.txt")], timeout=180)
            else:
                result["steps"].append({"cmd": "pip install", "ok": False, "out": "venv not found"})

    result["ok"] = True

    if do_restart and result["changed"]:
        # Schedule an in-process exit so systemd respawns us with the new code.
        # If the service isn't configured with Restart=on-failure / always, the
        # bot will simply stop. We respond first.
        def _exit_soon():
            time.sleep(1.0)
            log.info("Exiting for systemd-restart after update")
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()
        result["restarting"] = True

    return jsonify(result)


@app.route("/api/weather/current", methods=["GET"])
def api_weather_current():
    """Compact current-weather snapshot for the dashboard widget."""
    cfg = load_config()
    loc = cfg.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return jsonify({"error": "Город не выбран"}), 400
    try:
        data = weather.fetch_weather(loc["latitude"], loc["longitude"], loc.get("timezone") or "auto")
    except Exception as exc:
        log.exception("Current weather fetch failed")
        return jsonify({"error": str(exc)}), 500
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = cur.get("weather_code")
    text, emoji = weather.WMO_CODES_RU.get(int(code) if code is not None else -1, ("—", ""))
    try:
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        tmax = (daily.get("temperature_2m_max") or [None])[0]
    except Exception:
        tmin = tmax = None
    out = {
        "city": loc.get("name", ""),
        "temperature_c": cur.get("temperature_2m"),
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "pressure_mmhg": weather.hpa_to_mmhg(cur.get("pressure_msl")),
        "wind_speed_ms": cur.get("wind_speed_10m"),
        "wind_direction": weather.wind_direction(cur.get("wind_direction_10m")),
        "wind_gusts_ms": cur.get("wind_gusts_10m"),
        "precipitation_mm": cur.get("precipitation"),
        "weather_code": code,
        "condition_text": text,
        "condition_emoji": emoji,
        "today_min": tmin,
        "today_max": tmax,
        "is_day": bool(cur.get("is_day")),
    }
    return jsonify(out)


@app.route("/api/weather/hourly", methods=["GET"])
def api_weather_hourly():
    """Next 24h hourly arrays for the dashboard chart."""
    cfg = load_config()
    loc = cfg.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return jsonify({"error": "Город не выбран"}), 400
    try:
        data = weather.fetch_weather(loc["latitude"], loc["longitude"], loc.get("timezone") or "auto")
    except Exception as exc:
        log.exception("Hourly weather fetch failed")
        return jsonify({"error": str(exc)}), 500
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    # Find current hour position so we only return the next 24 entries
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:00")
    start = 0
    for i, t in enumerate(times):
        if t >= now_iso:
            start = i
            break
    end = min(start + 24, len(times))

    def _slice(key):
        arr = hourly.get(key) or []
        return arr[start:end]

    return jsonify({
        "city": loc.get("name", ""),
        "time": _slice("time"),
        "temperature_2m": _slice("temperature_2m"),
        "precipitation_probability": _slice("precipitation_probability"),
        "wind_speed_10m": _slice("wind_speed_10m"),
        "weather_code": _slice("weather_code"),
        "relative_humidity_2m": _slice("relative_humidity_2m"),
    })


# ---------------------------------------------------------------------------

reschedule_all()

# Keep Heltec link healthy: every 10 minutes ping it, reconnect on failure.
# First run shifted 90 seconds after boot to let the initial connection settle.
scheduler.add_job(
    _mesh_healthcheck,
    "interval",
    minutes=10,
    id="mesh-healthcheck",
    replace_existing=True,
    next_run_time=datetime.utcnow() + timedelta(seconds=90),
    misfire_grace_time=60,
    coalesce=True,
)


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


if __name__ == "__main__":
    host = os.environ.get("WMB_HOST", "0.0.0.0")
    port = int(os.environ.get("WMB_PORT", "5000"))
    use_https = _truthy_env("WMB_HTTPS", "1")

    ssl_context = None
    scheme = "http"
    if use_https:
        try:
            from tls_certs import ensure_self_signed_cert

            tls_dir = BASE_DIR / "tls"
            cert_path = tls_dir / "cert.pem"
            key_path = tls_dir / "key.pem"
            ensure_self_signed_cert(cert_path, key_path)
            ssl_context = (str(cert_path), str(key_path))
            scheme = "https"
        except Exception:
            log.exception("HTTPS setup failed; falling back to plain HTTP")

    log.info("Starting Weather → Mesh bridge on %s://%s:%d", scheme, host, port)
    # debug=False because the reloader spawns two schedulers — use systemd to restart
    app.run(host=host, port=port, debug=False, threaded=True, ssl_context=ssl_context)
