"""SQLite store for mesh-node history: telemetry snapshots + traceroute log.

Powers the per-node telemetry graphs and the traceroute-history view. Kept in a
separate file (history.db) so the chat schema / FTS index stays untouched.

A background collector (started from app.py) snapshots `BRIDGE.get_known_nodes()`
every few minutes, so even quiet nodes accumulate a time series for the charts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

TELEMETRY_RETENTION_DAYS = 7
TRACEROUTE_RETENTION_DAYS = 30


class HistoryDb:
    def __init__(self, path: Path,
                 telemetry_days: int = TELEMETRY_RETENTION_DAYS,
                 traceroute_days: int = TRACEROUTE_RETENTION_DAYS):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._telemetry_days = max(1, int(telemetry_days))
        self._traceroute_days = max(1, int(traceroute_days))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False, isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            c = self._connect()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS node_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_num INTEGER,
                    node_id TEXT,
                    time INTEGER NOT NULL,
                    battery REAL,
                    voltage REAL,
                    chan_util REAL,
                    air_tx REAL,
                    snr REAL,
                    lat REAL,
                    lon REAL,
                    long_name TEXT,
                    short_name TEXT
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_tele_node_time ON node_telemetry(node_num, time)")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS traceroute_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time INTEGER NOT NULL,
                    dest_id TEXT,
                    dest_name TEXT,
                    ok INTEGER NOT NULL DEFAULT 1,
                    hops INTEGER,
                    route_json TEXT,
                    route_back_json TEXT
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_trace_dest_time ON traceroute_history(dest_id, time)")

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def add_telemetry_snapshot(self, nodes: list[dict[str, Any]]) -> int:
        """Insert one row per node that currently exposes any telemetry/position.
        `nodes` is the list returned by MeshBridge.get_known_nodes()."""
        if not nodes:
            return 0
        now = int(time.time())
        rows = []
        for n in nodes:
            keys = ("battery_level", "voltage", "channel_utilization",
                    "air_util_tx", "snr", "latitude", "longitude")
            if not any(n.get(k) is not None for k in keys):
                continue
            rows.append((
                n.get("num"), n.get("node_id"), now,
                n.get("battery_level"), n.get("voltage"),
                n.get("channel_utilization"), n.get("air_util_tx"),
                n.get("snr"), n.get("latitude"), n.get("longitude"),
                n.get("long_name") or "", n.get("short_name") or "",
            ))
        if not rows:
            return 0
        with self._lock:
            c = self._connect()
            c.executemany(
                """
                INSERT INTO node_telemetry
                    (node_num, node_id, time, battery, voltage, chan_util,
                     air_tx, snr, lat, lon, long_name, short_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def telemetry(self, node_num: int, since_seconds: int = 86400) -> list[dict[str, Any]]:
        """Time series for one node (oldest→newest) within the window."""
        cutoff = int(time.time()) - max(60, int(since_seconds))
        with self._lock:
            c = self._connect()
            rows = c.execute(
                """
                SELECT time, battery, voltage, chan_util, air_tx, snr, lat, lon
                FROM node_telemetry
                WHERE node_num = ? AND time >= ?
                ORDER BY time ASC
                """,
                (int(node_num), cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Traceroute history
    # ------------------------------------------------------------------

    def add_traceroute(self, rec: dict[str, Any]) -> None:
        route = rec.get("route") or rec.get("route_to") or []
        route_back = rec.get("route_back") or []
        with self._lock:
            c = self._connect()
            c.execute(
                """
                INSERT INTO traceroute_history
                    (time, dest_id, dest_name, ok, hops, route_json, route_back_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(rec.get("time") or time.time()),
                    rec.get("dest_id") or rec.get("destination") or "",
                    rec.get("dest_name") or "",
                    1 if rec.get("ok", True) else 0,
                    rec.get("hops") if rec.get("hops") is not None else (len(route) or None),
                    json.dumps(route, ensure_ascii=False),
                    json.dumps(route_back, ensure_ascii=False),
                ),
            )

    def traceroute_history(self, dest_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            c = self._connect()
            if dest_id:
                rows = c.execute(
                    "SELECT * FROM traceroute_history WHERE dest_id = ? ORDER BY time DESC LIMIT ?",
                    (dest_id, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM traceroute_history ORDER BY time DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d.get("ok"))
            for src, dst in (("route_json", "route"), ("route_back_json", "route_back")):
                try:
                    d[dst] = json.loads(d.pop(src) or "[]")
                except Exception:
                    d[dst] = []
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self) -> None:
        now = int(time.time())
        with self._lock:
            c = self._connect()
            c.execute("DELETE FROM node_telemetry WHERE time < ?",
                      (now - self._telemetry_days * 86400,))
            c.execute("DELETE FROM traceroute_history WHERE time < ?",
                      (now - self._traceroute_days * 86400,))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
