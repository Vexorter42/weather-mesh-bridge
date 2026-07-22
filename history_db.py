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
            # --- traffic analytics (daily report + /activity) ---
            c.execute("CREATE TABLE IF NOT EXISTS traffic_hourly (hour INTEGER PRIMARY KEY, total INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS node_seen (num INTEGER PRIMARY KEY, first_ts INTEGER, last_ts INTEGER, total INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS node_daily (day TEXT, num INTEGER, count INTEGER, PRIMARY KEY(day, num))")
            c.execute("CREATE TABLE IF NOT EXISTS cmd_usage (day TEXT, user TEXT, count INTEGER, PRIMARY KEY(day, user))")

    # ------------------------------------------------------------------
    # Traffic analytics
    # ------------------------------------------------------------------

    @staticmethod
    def _daykey(ts: int) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def ingest_packets(self, events: list) -> None:
        """Aggregate a batch of received packets. events = [(ts, portnum, from_num)]."""
        if not events:
            return
        hourly: dict[int, int] = {}
        daily: dict[tuple, int] = {}
        seen: dict[int, list] = {}          # num -> [first_ts, last_ts, count]
        for ts, _port, num in events:
            ts = int(ts)
            hourly[ts // 3600] = hourly.get(ts // 3600, 0) + 1
            if num is not None:
                daily[(self._daykey(ts), num)] = daily.get((self._daykey(ts), num), 0) + 1
                s = seen.setdefault(num, [ts, ts, 0])
                s[0] = min(s[0], ts); s[1] = max(s[1], ts); s[2] += 1
        with self._lock:
            c = self._connect()
            c.execute("BEGIN")
            try:
                for h, n in hourly.items():
                    c.execute("INSERT INTO traffic_hourly(hour,total) VALUES(?,?) "
                              "ON CONFLICT(hour) DO UPDATE SET total=total+?", (h, n, n))
                for (day, num), n in daily.items():
                    c.execute("INSERT INTO node_daily(day,num,count) VALUES(?,?,?) "
                              "ON CONFLICT(day,num) DO UPDATE SET count=count+?", (day, num, n, n))
                for num, (fts, lts, n) in seen.items():
                    c.execute("INSERT INTO node_seen(num,first_ts,last_ts,total) VALUES(?,?,?,?) "
                              "ON CONFLICT(num) DO UPDATE SET last_ts=max(last_ts,?), total=total+?",
                              (num, fts, lts, n, lts, n))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def record_command(self, user: str) -> None:
        if not user:
            return
        with self._lock:
            self._connect().execute(
                "INSERT INTO cmd_usage(day,user,count) VALUES(?,?,1) "
                "ON CONFLICT(day,user) DO UPDATE SET count=count+1",
                (self._daykey(int(time.time())), user))

    def _hours(self, from_hour: int, to_hour: int) -> dict[int, int]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT hour,total FROM traffic_hourly WHERE hour>=? AND hour<? ORDER BY hour",
                (from_hour, to_hour)).fetchall()
        return {int(r["hour"]): int(r["total"]) for r in rows}

    def daily_report_data(self, day_ts: Optional[int] = None) -> dict[str, Any]:
        """Everything the OwearBot-style daily report needs for the given day."""
        now = day_ts or int(time.time())
        day = self._daykey(now)
        # local midnight of that day → hour range
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        h0, h24 = midnight // 3600, midnight // 3600 + 24
        hours = self._hours(h0, h24)
        total = sum(hours.values())
        # per-hour counts aligned 0..23 (local hour)
        by_hour = {}
        for h, n in hours.items():
            by_hour[time.localtime(h * 3600).tm_hour] = by_hour.get(time.localtime(h * 3600).tm_hour, 0) + n
        peak_h = max(by_hour, key=by_hour.get) if by_hour else None
        min_h = min(by_hour, key=by_hour.get) if by_hour else None
        # yesterday total
        y0, y24 = h0 - 24, h0
        y_total = sum(self._hours(y0, y24).values())
        with self._lock:
            c = self._connect()
            active = c.execute("SELECT COUNT(*) FROM node_daily WHERE day=?", (day,)).fetchone()[0]
            top = c.execute("SELECT num,count FROM node_daily WHERE day=? ORDER BY count DESC LIMIT 5",
                            (day,)).fetchall()
            new_nodes = c.execute("SELECT COUNT(*) FROM node_seen WHERE first_ts>=? AND first_ts<?",
                                  (midnight, midnight + 86400)).fetchone()[0]
            users = c.execute("SELECT user,count FROM cmd_usage WHERE day=? ORDER BY count DESC LIMIT 5",
                              (day,)).fetchall()
            users_total = c.execute("SELECT COUNT(*) FROM cmd_usage WHERE day=?", (day,)).fetchone()[0]
        return {
            "day": day, "total": total, "active_nodes": int(active),
            "new_nodes": int(new_nodes), "avg_hour": round(total / 24) if total else 0,
            "peak_hour": peak_h, "peak_count": by_hour.get(peak_h, 0) if peak_h is not None else 0,
            "min_hour": min_h, "min_count": by_hour.get(min_h, 0) if min_h is not None else 0,
            "top_nodes": [{"num": r["num"], "count": r["count"]} for r in top],
            "top_users": [{"user": r["user"], "count": r["count"]} for r in users],
            "users_total": int(users_total),
            "yesterday_total": y_total,
        }

    def activity_data(self, days: int = 7) -> dict[str, Any]:
        """7-day activity: totals + by weekday + by hour-of-day."""
        now = int(time.time())
        cur_hour = now // 3600
        from_hour = cur_hour - days * 24
        hours = self._hours(from_hour, cur_hour + 1)
        total = sum(hours.values())
        by_dow = [0] * 7      # Mon..Sun
        by_hod = [0] * 24
        for h, n in hours.items():
            lt = time.localtime(h * 3600)
            by_dow[lt.tm_wday] += n
            by_hod[lt.tm_hour] += n
        return {"days": days, "total": total, "avg_day": round(total / days) if total else 0,
                "by_dow": by_dow, "by_hour": by_hod}

    def prune_traffic(self, days: int = 30) -> None:
        cutoff_hour = (int(time.time()) - days * 86400) // 3600
        cutoff_day = self._daykey(int(time.time()) - days * 86400)
        with self._lock:
            c = self._connect()
            c.execute("DELETE FROM traffic_hourly WHERE hour < ?", (cutoff_hour,))
            c.execute("DELETE FROM node_daily WHERE day < ?", (cutoff_day,))
            c.execute("DELETE FROM cmd_usage WHERE day < ?", (cutoff_day,))

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
