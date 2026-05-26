"""Weather → Heltec Mesh bridge: Flask UI + scheduler.

Run:
    python app.py
Then open http://<raspberry-ip>:5000 from any device on the LAN.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request

import commands
# Mark process start time for /uptime — must be set before anything heavy runs.
commands.BOT_START_TS = time.time()

import weather
import weather_alerts
from chat_db import ChatDb
from meshbridge import MeshBridge
from telegram_bridge import DEFAULTS as TG_DEFAULTS, TELETHON_AVAILABLE, TelegramBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("weather-mesh-bridge")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
    """Migrate legacy field keys in schedules. Returns (cfg, changed)."""
    changed = False
    for slot in cfg.get("schedules", []) or []:
        old = slot.get("fields") or []
        new = _migrate_fields(old)
        if new != old:
            slot["fields"] = new
            changed = True
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
            "commands": {"enabled": True},
            "alerts": dict(weather_alerts.DEFAULTS),
            "schedules": [],
            "telegram": dict(TG_DEFAULTS),
        }
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg, changed = _migrate_config(cfg)
    if changed:
        save_config(cfg)
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

# Persistent state for weather-alerts dedup, plus background worker.
ALERTS_STATE = weather_alerts.AlertsState(BASE_DIR / "alerts_state.json")
weather_alerts.start_background_worker(load_config, BRIDGE, ALERTS_STATE)


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


TELEGRAM_BRIDGE = TelegramBridge(
    session_path=BASE_DIR / "telegram.session",
    mesh_send_callback=_telegram_forward,
)
# Apply the persisted config now; auto-start if it's enabled and authorised.
_tg_cfg_init = CONFIG.get("telegram") or {}
TELEGRAM_BRIDGE.configure(_tg_cfg_init)
if _tg_cfg_init.get("enabled") and TELETHON_AVAILABLE:
    try:
        r = TELEGRAM_BRIDGE.start()
        if not r.get("ok"):
            log.warning("Telegram bridge auto-start skipped: %s", r.get("error"))
    except Exception:
        log.exception("Telegram bridge auto-start crashed")


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
        if "telegram" in payload:
            cfg["telegram"] = {**TG_DEFAULTS, **(cfg.get("telegram") or {}), **payload["telegram"]}
        save_config(cfg)
    BRIDGE.configure(cfg.get("mesh", {}) or {})
    # Push the freshest telegram config into the bridge. Reconfigure will
    # restart the worker if it's currently running.
    TELEGRAM_BRIDGE.configure(cfg.get("telegram") or {})
    reschedule_all()
    return jsonify(cfg)


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
    return jsonify(result)


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


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Aggregate counters for the main-page dashboard."""
    s = CHAT_DB.stats()
    mesh = BRIDGE.status()
    s["mesh_connected"] = bool(mesh.get("connected"))
    s["mesh_nodes_known"] = mesh.get("nodes_known")
    return jsonify(s)


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat() + "Z"})


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
