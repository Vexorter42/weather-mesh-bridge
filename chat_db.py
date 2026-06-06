"""SQLite-backed chat message store.

Replaces the in-memory deque so:
  - chat history survives bot restarts
  - we can compute statistics over arbitrary time windows
  - message lookup by mesh msg_id is O(log n)

The schema is intentionally simple; one wide table with the same fields the UI
already consumes from the API.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30   # старые сообщения удаляются автоматически


class ChatDb:
    def __init__(self, path: Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._retention = max(1, int(retention_days))
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                isolation_level=None,  # autocommit; we use BEGIN explicitly when needed
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_schema(self):
        with self._lock:
            c = self._connect()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time INTEGER NOT NULL,
                    from_id TEXT,
                    from_name TEXT,
                    to_id TEXT,
                    channel INTEGER,
                    text TEXT,
                    incoming INTEGER NOT NULL,
                    msg_id INTEGER,
                    reply_to INTEGER,
                    is_reaction INTEGER NOT NULL DEFAULT 0,
                    hops_taken INTEGER,
                    rx_rssi REAL,
                    rx_snr REAL,
                    delivery_status TEXT,
                    delivery_hops INTEGER,
                    via_mqtt INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(time)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_mesh_id ON messages(msg_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_reply ON messages(reply_to)")
            # Lightweight migration for older DBs.
            cols = {row[1] for row in c.execute("PRAGMA table_info(messages)")}
            if "to_id" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN to_id TEXT")
            if "delivery_status" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN delivery_status TEXT")
            if "delivery_hops" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN delivery_hops INTEGER")
            if "via_mqtt" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN via_mqtt INTEGER NOT NULL DEFAULT 0")

            # Full-text search index over the text column. FTS5 ships with
            # stock SQLite on Debian/Raspberry Pi OS. Triggers keep it in
            # sync with the main table.
            try:
                c.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        text, content='messages', content_rowid='id', tokenize = 'unicode61'
                    )
                    """
                )
                # Triggers
                c.execute("""
                    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
                    END
                """)
                c.execute("""
                    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
                    END
                """)
                c.execute("""
                    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
                        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
                    END
                """)
                # If the FTS index is empty but messages exist — backfill once.
                row = c.execute("SELECT COUNT(*) FROM messages_fts").fetchone()
                if row and row[0] == 0:
                    mrow = c.execute("SELECT COUNT(*) FROM messages").fetchone()
                    if mrow and mrow[0] > 0:
                        c.execute("INSERT INTO messages_fts(rowid, text) SELECT id, text FROM messages")
                        log.info("FTS5 backfilled with %s existing messages", mrow[0])
            except sqlite3.OperationalError as exc:
                log.warning("FTS5 init failed (likely unsupported by this sqlite build): %s", exc)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["incoming"] = bool(d.get("incoming"))
        d["is_reaction"] = bool(d.get("is_reaction"))
        d["via_mqtt"] = bool(d.get("via_mqtt"))
        return d

    def add(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Insert one message, return it with the assigned `id`."""
        with self._lock:
            c = self._connect()
            cursor = c.execute(
                """
                INSERT INTO messages (time, from_id, from_name, to_id, channel, text, incoming,
                                      msg_id, reply_to, is_reaction,
                                      hops_taken, rx_rssi, rx_snr,
                                      delivery_status, delivery_hops, via_mqtt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(msg.get("time") or time.time()),
                    str(msg.get("from_id") or ""),
                    msg.get("from_name") or "",
                    msg.get("to_id"),
                    int(msg.get("channel") or 0),
                    msg.get("text") or "",
                    1 if msg.get("incoming") else 0,
                    msg.get("msg_id"),
                    msg.get("reply_to"),
                    1 if msg.get("is_reaction") else 0,
                    msg.get("hops_taken"),
                    msg.get("rx_rssi"),
                    msg.get("rx_snr"),
                    msg.get("delivery_status"),
                    msg.get("delivery_hops"),
                    1 if msg.get("via_mqtt") else 0,
                ),
            )
            new_id = cursor.lastrowid
            row = c.execute("SELECT * FROM messages WHERE id = ?", (new_id,)).fetchone()
            return self._row_to_dict(row)

    def update_delivery_by_mesh_id(
        self, mesh_msg_id: int, status: str, hops: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        """Update delivery status of an outgoing message by its Meshtastic packet ID.

        Returns the updated row (or None if no such outgoing message exists).
        """
        with self._lock:
            c = self._connect()
            # Limit to outgoing — we only track delivery for our own messages
            cur = c.execute(
                """
                UPDATE messages
                SET delivery_status = ?, delivery_hops = COALESCE(?, delivery_hops)
                WHERE msg_id = ? AND incoming = 0
                """,
                (status, hops, int(mesh_msg_id)),
            )
            if cur.rowcount == 0:
                return None
            row = c.execute(
                "SELECT * FROM messages WHERE msg_id = ? AND incoming = 0 ORDER BY id DESC LIMIT 1",
                (int(mesh_msg_id),),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """Full-text search via FTS5. Returns matching messages newest-first.

        Supports the standard FTS5 query syntax — prefix-matches via `word*`,
        booleans `AND`/`OR`/`NOT`, phrase `"two words"`. Falls back to a LIKE
        scan if FTS5 isn't available on this SQLite build.
        """
        q = (query or "").strip()
        if not q:
            return []
        with self._lock:
            c = self._connect()
            try:
                rows = c.execute(
                    """
                    SELECT m.* FROM messages m
                    JOIN messages_fts f ON f.rowid = m.id
                    WHERE messages_fts MATCH ?
                    ORDER BY m.id DESC
                    LIMIT ?
                    """,
                    (q, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                # FTS5 absent — fall back to plain LIKE
                like = f"%{q}%"
                rows = c.execute(
                    "SELECT * FROM messages WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                    (like, int(limit)),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        """Fetch multiple messages by their internal IDs. Used for status polling."""
        if not ids:
            return []
        with self._lock:
            c = self._connect()
            placeholders = ",".join("?" for _ in ids)
            rows = c.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders})",
                tuple(int(i) for i in ids),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_since(self, since_id: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            c = self._connect()
            rows = c.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(since_id or 0), int(limit)),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            c = self._connect()
            rows = c.execute(
                "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (int(limit),),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def last_id(self) -> int:
        with self._lock:
            c = self._connect()
            row = c.execute("SELECT MAX(id) FROM messages").fetchone()
            return int(row[0] or 0)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Numbers for the main-page dashboard."""
        with self._lock:
            c = self._connect()
            now = int(time.time())
            day_ago = now - 86400

            def _scalar(sql: str, *args) -> Any:
                row = c.execute(sql, args).fetchone()
                return None if row is None else row[0]

            sent_24h = _scalar("SELECT COUNT(*) FROM messages WHERE incoming=0 AND is_reaction=0 AND time>=?", day_ago) or 0
            recv_24h = _scalar("SELECT COUNT(*) FROM messages WHERE incoming=1 AND is_reaction=0 AND time>=?", day_ago) or 0
            avg_rssi = _scalar(
                "SELECT AVG(rx_rssi) FROM messages WHERE incoming=1 AND rx_rssi IS NOT NULL AND time>=?",
                day_ago,
            )
            avg_hops = _scalar(
                "SELECT AVG(hops_taken) FROM messages WHERE incoming=1 AND hops_taken IS NOT NULL AND time>=?",
                day_ago,
            )
            unique_senders = _scalar(
                "SELECT COUNT(DISTINCT from_id) FROM messages WHERE incoming=1 AND time>=?",
                day_ago,
            ) or 0
            last_out_ts = _scalar(
                "SELECT time FROM messages WHERE incoming=0 AND is_reaction=0 ORDER BY id DESC LIMIT 1"
            )
            last_in_ts = _scalar(
                "SELECT time FROM messages WHERE incoming=1 AND is_reaction=0 ORDER BY id DESC LIMIT 1"
            )
            total = _scalar("SELECT COUNT(*) FROM messages") or 0

            return {
                "sent_24h": int(sent_24h),
                "received_24h": int(recv_24h),
                "unique_senders_24h": int(unique_senders),
                "avg_rssi_24h": round(float(avg_rssi), 1) if avg_rssi is not None else None,
                "avg_hops_24h": round(float(avg_hops), 1) if avg_hops is not None else None,
                "last_outgoing_ts": int(last_out_ts) if last_out_ts else None,
                "last_incoming_ts": int(last_in_ts) if last_in_ts else None,
                "total_messages": int(total),
            }

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def cleanup_old(self) -> int:
        """Delete messages older than retention. Returns rows deleted."""
        with self._lock:
            c = self._connect()
            cutoff = int(time.time()) - self._retention * 86400
            cur = c.execute("DELETE FROM messages WHERE time < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


__all__ = ["ChatDb"]
