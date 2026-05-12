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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request

import commands
import weather
from chat_db import ChatDb
from meshbridge import MeshBridge

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
            "schedules": [],
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
        save_config(cfg)
    BRIDGE.configure(cfg.get("mesh", {}) or {})
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


@app.route("/api/chat/messages", methods=["GET"])
def api_chat_messages():
    """Return chat messages newer than ?since=<id>."""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    return jsonify({"messages": BRIDGE.get_messages(since)})


@app.route("/api/chat/reply", methods=["POST"])
def api_chat_reply():
    """Send a text reply to a previous mesh message."""
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
    try:
        result = BRIDGE.send_reply(
            text,
            reply_to,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            destination=mesh_cfg.get("destination", "broadcast"),
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


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    """Send a free-form text message into the mesh.

    payload:
      - text (str, required)
      - destination (str, optional): "broadcast" or node id (e.g. "!a1b2c3d4").
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
    try:
        result = BRIDGE.send_text_chunked(
            text,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
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


# ---------------------------------------------------------------------------

reschedule_all()


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
