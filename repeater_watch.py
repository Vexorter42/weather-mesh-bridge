"""MeshCore repeater watch — alert when a repeater goes silent.

Passively watches the MeshCore contact table (no admin login needed): repeaters
advertise periodically, so if `now - last_advert` exceeds a threshold the
repeater has likely gone offline. Fires a single alert into a MeshCore channel
when a repeater goes silent, and (optionally) a recovery notice when it returns.

Repeaters are contacts with type == 2. Config under "repeater_watch".
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPEATER_TYPE = 2

DEFAULTS = {
    "enabled": False,
    "gone_after_minutes": 120,       # silent this long → "gone"
    "check_interval_minutes": 15,
    "channel_name": "",              # MeshCore channel for alerts ("" = default channel)
    "notify_recovery": True,         # also announce when a repeater comes back
    "only": [],                      # optional whitelist of repeater names ([] = all type-2)
}


class RepeaterState:
    """Per-repeater gone/ok state, persisted so we don't re-alert after restart."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read repeater state, starting fresh")
        return {"status": {}, "last_check_ts": 0}

    def get(self, name: str):
        with self._lock:
            return (self._data.get("status") or {}).get(name)

    def set(self, name: str, value: str):
        with self._lock:
            self._data.setdefault("status", {})[name] = value

    def clear(self, name: str):
        with self._lock:
            (self._data.get("status") or {}).pop(name, None)

    def is_seeded(self) -> bool:
        with self._lock:
            return bool(self._data.get("seeded"))

    def mark_seeded(self):
        with self._lock:
            self._data["seeded"] = True

    def save(self):
        with self._lock:
            self._data["last_check_ts"] = int(time.time())
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"last_check_ts": self._data.get("last_check_ts", 0),
                    "gone": [n for n, v in (self._data.get("status") or {}).items() if v == "gone"]}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.get("repeater_watch") or {})}


def _fmt_age(sec: int) -> str:
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}ч {m}м" if h else f"{m}м"


def check(cfg: dict[str, Any], mc, state: RepeaterState, dry: bool = False) -> list[dict[str, Any]]:
    """One watch cycle. Returns list of events {name, kind, age}. dry=True skips
    sending (for diagnostics)."""
    c = _cfg(cfg)
    if not c.get("enabled"):
        return []
    if not mc or not mc.status().get("connected"):
        return []

    gone_after = max(60, int(c.get("gone_after_minutes", 120)) * 60)
    channel = (c.get("channel_name") or "").strip()
    only = {str(x).strip() for x in (c.get("only") or []) if str(x).strip()}
    now = int(time.time())

    try:
        contacts = mc.list_contacts(refresh=True)
    except Exception:
        log.exception("Repeater watch: list_contacts failed")
        return []

    # First real run: record current reality WITHOUT alerting, so we don't spam
    # about repeaters that were already silent before we started watching.
    seeding = (not dry) and (not state.is_seeded())

    events: list[dict[str, Any]] = []
    seeded_n = 0
    for ct in contacts:
        if ct.get("type") != REPEATER_TYPE:
            continue
        name = (ct.get("adv_name") or "").strip()
        if not name or (only and name not in only):
            continue
        last = int(ct.get("last_advert") or 0)
        if last <= 0:
            continue
        age = now - last
        gone = age >= gone_after
        prev = state.get(name)

        if seeding:
            state.set(name, "gone" if gone else "ok")
            seeded_n += 1
            continue
        if gone and prev != "gone":
            events.append({"name": name, "kind": "gone", "age": age})
            if not dry:
                text = f"⚠️ Репитер {name} пропал: молчит {_fmt_age(age)} (нет адвертов)."
                try:
                    mc.send_named(channel, text)
                    log.info("Repeater watch: %s GONE (%s)", name, _fmt_age(age))
                except Exception:
                    log.exception("Repeater watch: send failed for %s", name)
                state.set(name, "gone")
        elif (not gone) and prev == "gone":
            events.append({"name": name, "kind": "back", "age": age})
            if not dry:
                if c.get("notify_recovery", True):
                    try:
                        mc.send_named(channel, f"✅ Репитер {name} снова на связи.")
                    except Exception:
                        log.exception("Repeater watch: recovery send failed for %s", name)
                state.set(name, "ok")
                log.info("Repeater watch: %s BACK", name)

    if seeding:
        state.mark_seeded()
        log.info("Repeater watch: seeded baseline for %d repeaters", seeded_n)
    if not dry:
        state.save()
    return events


def start_background_worker(get_cfg, mc, state: RepeaterState) -> threading.Thread:
    def loop():
        time.sleep(120)  # let MeshCore connect + load contacts first
        while True:
            interval = 15
            try:
                cfg = get_cfg()
                interval = max(5, int(_cfg(cfg).get("check_interval_minutes", 15)))
                check(cfg, mc, state)
            except Exception:
                log.exception("Repeater watch crashed (will retry)")
            time.sleep(interval * 60)

    t = threading.Thread(target=loop, daemon=True, name="repeater-watch")
    t.start()
    return t


__all__ = ["RepeaterState", "check", "start_background_worker", "DEFAULTS"]
