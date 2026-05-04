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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request

import weather
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
BRIDGE = MeshBridge(
    connection_type=_mesh_cfg.get("connection_type", "serial"),
    device_path=_mesh_cfg.get("device_path", "auto"),
    tcp_host=_mesh_cfg.get("tcp_host", ""),
    tcp_port=int(_mesh_cfg.get("tcp_port", 4403)),
)

scheduler = BackgroundScheduler(timezone="UTC")  # cron triggers carry their own tz
scheduler.start()


def _job_id(slot_id: str) -> str:
    return f"slot-{slot_id}"


def _trigger_for_slot(slot: dict[str, Any]) -> CronTrigger:
    hh, mm = slot.get("time", "12:00").split(":")
    days = slot.get("days") or DAYS_ORDER
    day_of_week = ",".join(d for d in days if d in DAYS_ORDER) or "mon-sun"
    tz = slot.get("timezone") or "Europe/Moscow"
    return CronTrigger(hour=int(hh), minute=int(mm), day_of_week=day_of_week, timezone=tz)


def _run_slot(slot_id: str):
    """APScheduler entry point — re-reads config so live edits stick."""
    cfg = load_config()
    slot = next((s for s in cfg.get("schedules", []) if s.get("id") == slot_id), None)
    if not slot or not slot.get("enabled", True):
        log.info("Slot %s skipped (missing or disabled)", slot_id)
        return
    try:
        send_now(cfg, slot.get("fields") or [])
        log.info("Slot %s sent", slot_id)
    except Exception:
        log.exception("Slot %s failed", slot_id)


def reschedule_all():
    cfg = load_config()
    # remove every job that we own
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("slot-"):
            scheduler.remove_job(job.id)
    for slot in cfg.get("schedules", []):
        if not slot.get("enabled", True):
            continue
        try:
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


def send_now(cfg: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    text = build_message(cfg, fields)
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    # send_text_chunked auto-splits sequences longer than the Meshtastic ~228-byte
    # limit and prefixes each chunk with "(i/N) ".
    result = BRIDGE.send_text_chunked(
        text,
        channel_index=int(mesh_cfg.get("channel_index", 0)),
        destination=mesh_cfg.get("destination", "broadcast"),
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
        for k in ("time", "enabled", "days", "fields", "timezone"):
            if k in payload:
                if k == "fields":
                    target[k] = _migrate_fields(payload[k])
                else:
                    target[k] = payload[k]
        save_config(cfg)
    reschedule_all()
    return jsonify(target)


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
    """Send a free-form text message into the mesh (broadcast on configured channel)."""
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Пустое сообщение"}), 400
    cfg = load_config()
    mesh_cfg = cfg.get("mesh", {}) or {}
    BRIDGE.configure(mesh_cfg)
    try:
        result = BRIDGE.send_text_chunked(
            text,
            channel_index=int(mesh_cfg.get("channel_index", 0)),
            destination=mesh_cfg.get("destination", "broadcast"),
            chunk_delay=_chunk_delay(mesh_cfg),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@app.route("/api/scheduler/jobs", methods=["GET"])
def api_jobs():
    out = []
    for job in scheduler.get_jobs():
        if not job.id.startswith("slot-"):
            continue
        out.append(
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return jsonify(out)


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
