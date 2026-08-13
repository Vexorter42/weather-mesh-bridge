"""MeshCore companion-radio bridge (USB serial).

Mirrors mesh broadcasts into a **MeshCore** network, in parallel with the
Meshtastic node. MeshCore is a separate mesh protocol; its Python companion
library (`meshcore`, asyncio) talks to a Companion-role node over Serial / BLE
/ TCP. Here we use **USB serial** (the companion plugged into the Pi).

Because the app is threaded (Flask + APScheduler) and the meshcore library is
asyncio, we own a dedicated event loop in a background thread. A supervisor
coroutine keeps the connection alive (reconnects on drop / config change).
Public methods are synchronous and safe to call from any thread:

  - send_channel(text)  → mirror a broadcast (fire-and-forget by default)
  - status()            → dict for the UI card
  - list_ports()        → detected USB serial devices (for the UI picker)

Enable + port/baud/channel live in config.json under the `meshcore` key.

Message length: MeshCore packs the text as UTF-8 into a small LoRa frame, so
the practical limit is ~143 **bytes** (== 143 chars for Latin, ~67 for
Cyrillic). We reuse the Meshtastic word/newline-aware splitter with that
budget and prefix multi-part messages with "(i/N) ".
"""
from __future__ import annotations

import asyncio
import collections
import glob
import inspect
import logging
import threading
import time
from typing import Callable, Optional

from meshbridge import split_for_mesh, _utf8_len  # reuse the word-aware chunker

log = logging.getLogger(__name__)

try:
    from meshcore import MeshCore
    from meshcore.events import EventType
    MESHCORE_AVAILABLE = True
except Exception:                       # library not installed → feature dormant
    MeshCore = None                     # type: ignore
    EventType = None                    # type: ignore
    MESHCORE_AVAILABLE = False

# MeshCore text budget in UTF-8 bytes (firmware/LoRa frame limit ~143).
MAX_TEXT_BYTES = 143
CHUNK_PREFIX_BUDGET = 9                  # reserve for "(99/99) "
CHUNK_DELAY_S = 0.4                      # pause between chunks (LoRa airtime)

DEFAULTS = {
    "enabled": False,
    "port": "auto",         # "auto" → first /dev/ttyACM*|ttyUSB*; or explicit path
    "baud": 115200,
    "channel_name": "",     # channel to send to, by name (pulled from the node); "" = first
    "channel_index": 0,     # numeric fallback when the name can't be resolved
    "chunk_delay": 8.0,     # seconds between multi-part message chunks (LoRa airtime)
}

MAX_CHANNELS = 16           # how many channel slots to probe on the Companion


def _autodetect_port() -> Optional[str]:
    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


class MeshCoreBridge:
    """Async MeshCore companion connection (serial) driven from a background loop."""

    def __init__(self, config_load):
        self._cfg = config_load
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._mc = None                       # MeshCore instance (loop thread only)
        self._stop = threading.Event()
        self._connected = False
        self._resolved_port = ""
        self._channels: list[dict] = []       # [{index, name}] read from the Companion
        self._command_handler: Optional[Callable[..., Optional[str]]] = None
        self._rx_count = 0
        # Recent RF-log paths (recv_ts, hops[]) for text packets — used to show
        # the real route of an incoming command. Populated only when the
        # Companion has RF logging enabled (emits RX_LOG_DATA frames).
        self._rxlog_buf: "collections.deque" = collections.deque(maxlen=40)
        self._last_error: Optional[str] = None
        self._sent_count = 0
        self._last_sent_ts = 0.0
        self._cfg_port = ""
        self._cfg_baud = 0

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _mc_cfg(self) -> dict:
        return (self._cfg() or {}).get("meshcore") or {}

    def _enabled(self) -> bool:
        return bool(self._mc_cfg().get("enabled"))

    @staticmethod
    def list_ports() -> list[str]:
        return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))

    def set_command_handler(self, fn: Callable[..., Optional[str]]) -> None:
        """fn(text, channel_index, meta) -> reply text (or None). Called for
        incoming MeshCore channel messages that look like commands (start with
        ! or /). `meta` = {hops_taken, from_id}."""
        self._command_handler = fn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="meshcore-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if not MESHCORE_AVAILABLE:
            self._last_error = "библиотека meshcore не установлена"
            log.warning("MeshCore bridge: library not available — feature dormant")
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._supervisor())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _supervisor(self) -> None:
        """Keep a live connection while enabled; reconnect on drop/config change."""
        while not self._stop.is_set():
            if not self._enabled():
                await self._teardown()
                await asyncio.sleep(5)
                continue
            c = self._mc_cfg()
            port = (c.get("port") or "auto").strip()
            baud = int(c.get("baud") or 115200)
            if self._mc is None or port != self._cfg_port or baud != self._cfg_baud:
                self._cfg_port, self._cfg_baud = port, baud
                await self._teardown()
                await self._connect(port, baud)
            else:
                try:
                    self._connected = bool(self._mc.is_connected)
                except Exception:
                    self._connected = False
                if not self._connected:
                    await self._teardown()
                    await self._connect(port, baud)
            await asyncio.sleep(3)
        await self._teardown()

    async def _connect(self, port: str, baud: int) -> None:
        dev = _autodetect_port() if port == "auto" else port
        if not dev:
            self._connected = False
            self._resolved_port = ""
            self._last_error = "USB-устройство не найдено (/dev/ttyACM*, /dev/ttyUSB*)"
            return
        try:
            mc = await MeshCore.create_serial(dev, baud, auto_reconnect=True)
            if mc is None:                       # lib returns None when the node never answered
                self._connected = False
                self._last_error = f"нет ответа от {dev} (это точно MeshCore Companion?)"
                log.warning("MeshCore: no response from %s", dev)
                return
            self._mc = mc
            try:
                mc.auto_update_contacts = True    # keep the contact cache fresh on new adverts
            except Exception:
                pass
            self._resolved_port = dev
            self._connected = bool(getattr(mc, "is_connected", True))
            self._last_error = None
            log.info("MeshCore bridge connected on %s @ %d", dev, baud)
            await self._load_channels()
            await self._subscribe_incoming()
            await self._load_contacts()
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            log.warning("MeshCore connect failed (%s @ %d): %s", dev, baud, exc)

    async def _load_channels(self) -> None:
        """Read the Companion's channel table (index → name)."""
        if self._mc is None:
            return
        found: list[dict] = []
        empties = 0
        for idx in range(MAX_CHANNELS):
            try:
                ev = await self._mc.commands.get_channel(idx)
            except Exception:
                break
            if EventType is not None and getattr(ev, "type", None) == EventType.ERROR:
                break
            payload = getattr(ev, "payload", None) or {}
            name = (payload.get("channel_name") or "").strip()
            secret = payload.get("channel_secret") or b""
            has = bool(name) or any(secret)
            if has:
                found.append({"index": idx, "name": name or f"Канал {idx}"})
                empties = 0
            else:
                empties += 1
                if idx >= 1 and empties >= 2:   # two blank slots in a row → end of table
                    break
        if found:
            self._channels = found
            log.info("MeshCore channels: %s", ", ".join(f"{c['index']}:{c['name']}" for c in found))

    async def _subscribe_incoming(self) -> None:
        """Start auto-fetching queued messages and dispatch channel messages to
        the command handler."""
        if self._mc is None:
            return
        try:
            maybe = self._mc.start_auto_message_fetching()
            if inspect.isawaitable(maybe):
                await maybe
            self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
            try:
                self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log)
            except Exception:
                log.debug("MeshCore: RX_LOG subscribe failed", exc_info=True)
            log.info("MeshCore: listening for channel messages")
        except Exception:
            log.exception("MeshCore: failed to subscribe to incoming messages")

    def _on_rx_log(self, event) -> None:
        # Raw RX-log frames carry the full flood path — but only if the Companion
        # firmware has RF logging enabled (standard builds don't emit these).
        # We buffer paths of text packets and match them to incoming commands.
        try:
            p = getattr(event, "payload", None) or {}
            ptype = (p.get("payload_typename") or "")
            if "TXT" not in ptype.upper():
                return
            hops = self._hex_to_hops((p.get("path") or "").strip(),
                                     int(p.get("path_len") or 0))
            self._rxlog_buf.append((time.time(), hops))
        except Exception:
            pass

    def _rxlog_route(self, ts: float, window: float = 4.0) -> Optional[list[str]]:
        """Route (hop list) of the buffered text packet nearest to `ts`. Empty
        list = heard directly (0 hops); None = no RF-log data available."""
        best, best_dt = None, window
        for rt, hops in reversed(self._rxlog_buf):
            dt = abs(rt - ts)
            if dt <= best_dt:
                best, best_dt = hops, dt
        return best

    def _on_channel_msg(self, event) -> None:
        # subscribe callback runs in the loop thread — spawn a task so a slow
        # command (weather fetch) never blocks the reader.
        try:
            asyncio.create_task(self._handle_incoming(event))
        except Exception:
            log.exception("MeshCore: failed to schedule incoming handler")

    async def _handle_incoming(self, event) -> None:
        payload = getattr(event, "payload", None) or {}
        raw = (payload.get("text") or "").strip()
        chan = int(payload.get("channel_idx") or 0)
        self._rx_count += 1
        # MeshCore channel messages arrive as "SenderName: message" — the node
        # prepends the sender's name. Strip it before looking for a command.
        if ": " in raw:
            sender, body = raw.split(": ", 1)
            sender, body = sender.strip(), body.strip()
        else:
            sender, body = "", raw
        if not body or not (body.startswith("!") or body.startswith("/")):
            return
        # MeshCore-specific: !trace <node> actively discovers the route (path
        # discovery). Handled here (not via the shared registry) since it needs
        # the MeshCore node; also warms up out_path so !ping shows hops after.
        parts = body.lstrip("!/ ").split()
        if parts and parts[0].lower() in ("trace", "path", "путь", "трейс"):
            log.info("MeshCore trace %r on chan=%s", body[:40], chan)
            try:
                await self._handle_trace(parts[1:], chan, sender)
            except Exception:
                log.exception("MeshCore trace failed")
            return
        if not self._command_handler:
            return
        # Prefer the real per-packet path from the RF log (if RF logging is on):
        # give the paired RX_LOG frame a moment to arrive, then match by time.
        cmd_ts = time.time()
        await asyncio.sleep(0.7)
        rx_route = self._rxlog_route(cmd_ts)
        if rx_route is not None:
            hops = len(rx_route)                          # authoritative from RF log
            route_path = rx_route                         # [] = heard directly
        else:                                             # no RF-log data — fall back
            pl = payload.get("path_len")
            hops = 0 if pl in (None, 255) else int(pl)
            route_path = self._route_for(sender)
        meta = {"hops_taken": hops, "from_id": sender or None, "route_path": route_path}
        loop = asyncio.get_running_loop()
        try:
            reply = await loop.run_in_executor(None, self._command_handler, body, chan, meta)
        except Exception:
            log.exception("MeshCore command handler crashed")
            return
        if reply:
            await asyncio.sleep(1.5)          # small back-off so the channel clears
            mention = self._mention(sender)
            r = await self._send_coro(mention + reply, chan)
            log.info("MeshCore cmd %r hops=%s route=%s chan=%s -> sent=%s",
                     body[:40], hops, route_path, chan, r.get("ok"))
            # For ping, follow up with the route as a second message (like the
            # reference bot) — only when the packet took hops.
            if parts and parts[0].lower() == "ping" and route_path:
                await asyncio.sleep(3)        # short gap between the two messages
                chain = " → ".join(route_path)
                n = len(route_path)
                await self._send_coro(
                    f"Путь: {chain} ({n} {self._hop_word(n)})", chan)

    async def _load_contacts(self) -> None:
        """Populate the contact cache so we can look up a sender's stored route."""
        if self._mc is None:
            return
        try:
            maybe = self._mc.ensure_contacts()
            if inspect.isawaitable(maybe):
                await maybe
            log.info("MeshCore contacts loaded: %d", len(getattr(self._mc, "_contacts", {}) or {}))
        except Exception:
            log.debug("MeshCore: ensure_contacts failed", exc_info=True)

    def _route_for(self, sender: str) -> Optional[list[str]]:
        """The known route (list of hop-hash hex strings) to `sender`, read from
        the contact's stored out_path. None if the sender isn't a known contact
        or the node has no learned path (heard directly)."""
        if not sender or self._mc is None:
            return None
        try:
            c = self._mc.get_contact_by_name(sender)
        except Exception:
            log.debug("MeshCore route: lookup failed", exc_info=True)
            return None
        if not c:
            return None
        hexs = (c.get("out_path") or "").strip()
        n = int(c.get("out_path_len") or 0)
        if not hexs or n <= 0:                            # -1 = flood (no fixed path)
            return None
        return self._hex_to_hops(hexs, n) or None

    @staticmethod
    def _mention(sender: str) -> str:
        """MeshCore reply/mention prefix — replies look like '@[Name] text'."""
        return f"@[{sender}] " if sender else ""

    @staticmethod
    def _hop_word(n: int) -> str:
        d, dd = n % 10, n % 100
        if d == 1 and dd != 11:
            return "прыжок"
        if 2 <= d <= 4 and not (12 <= dd <= 14):
            return "прыжка"
        return "прыжков"

    @staticmethod
    def _hex_to_hops(hexs: str, n: int) -> list[str]:
        """Split a concatenated path-hash hex string into `n` hop tokens."""
        hexs = (hexs or "").strip()
        if not hexs or n <= 0:
            return []
        step = max(2, len(hexs) // n)
        hops = [hexs[i:i + step] for i in range(0, len(hexs), step) if hexs[i:i + step]]
        return hops[:n]

    def _find_contact(self, name: str):
        """Contact by exact adv_name, then a loose case-insensitive substring."""
        if self._mc is None or not name:
            return None
        try:
            c = self._mc.get_contact_by_name(name)
        except Exception:
            c = None
        if c:
            return c
        q = name.lower()
        for v in (getattr(self._mc, "_contacts", {}) or {}).values():
            if q in (v.get("adv_name") or "").lower():
                return v
        return None

    @staticmethod
    def _split_hops(hexs: str, n: int, hash_len: int) -> list[str]:
        if not hexs or n <= 0:
            return []
        step = max(2, int(hash_len) * 2)
        hops = [hexs[i:i + step] for i in range(0, len(hexs), step) if hexs[i:i + step]]
        return hops[:n]

    async def _handle_trace(self, args: list[str], chan: int, sender: str = "") -> None:
        """!trace <node> — run MeshCore path discovery and report the route."""
        m = self._mention(sender)
        name = " ".join(args).strip()
        if not name:
            await self._send_coro(f"{m}Использование: !trace <имя узла>", chan)
            return
        contact = self._find_contact(name)
        if not contact:
            await self._send_coro(f"{m}Узел «{name}» не найден в контактах ноды.", chan)
            return
        disp = contact.get("adv_name") or name
        await self._send_coro(f"{m}🛰 Ищу маршрут до {disp}…", chan)
        hops = None
        try:
            ev = await self._mc.commands.send_path_discovery_sync(contact, min_timeout=20)
            if ev and not (EventType is not None and getattr(ev, "type", None) == EventType.ERROR):
                p = getattr(ev, "payload", None) or {}
                hops = self._split_hops((p.get("out_path") or "").strip(),
                                        int(p.get("out_path_len") or 0),
                                        int(p.get("out_path_hash_len") or 1)) or None
        except Exception as exc:
            log.info("MeshCore trace error: %s", exc)
        # Fallback: the discovery may have updated the contact's stored route even
        # if no PATH_RESPONSE came back synchronously (auto_update_contacts).
        if not hops:
            hops = self._route_for(disp)
        if hops:
            await self._send_coro(
                f"{m}🛣 {disp}: {' → '.join(hops)} ({len(hops)} {self._hop_word(len(hops))})", chan)
        else:
            await self._send_coro(f"{m}🛣 {disp}: маршрут не найден (нода не ответила на trace)", chan)

    def _resolve_chan(self) -> int:
        """Configured channel_name → index (from the Companion table); fall back
        to a numeric name, then channel_index, then 0."""
        c = self._mc_cfg()
        name = (c.get("channel_name") or "").strip()
        if name:
            for ch in self._channels:
                if ch["name"].lower() == name.lower():
                    return ch["index"]
            if name.isdigit():
                return int(name)
        return int(c.get("channel_index") or 0)

    async def _teardown(self) -> None:
        if self._mc is not None:
            try:
                await self._mc.disconnect()
            except Exception:
                pass
        self._mc = None
        self._connected = False
        self._resolved_port = ""

    async def _send_coro(self, text: str, chan: int) -> dict:
        if self._mc is None:
            return {"ok": False, "error": "не подключено"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "пустой текст"}
        if _utf8_len(text) <= MAX_TEXT_BYTES:
            chunks, prefixed = [text], False
        else:
            chunks = split_for_mesh(text, max_bytes=MAX_TEXT_BYTES - CHUNK_PREFIX_BUDGET)
            prefixed = True
        n = len(chunks)
        try:
            delay = float(self._mc_cfg().get("chunk_delay", DEFAULTS["chunk_delay"]))
        except (TypeError, ValueError):
            delay = DEFAULTS["chunk_delay"]
        if n > 1:
            log.info("MeshCore: sending %d chunks with %.1fs gap", n, delay)
        for i, raw in enumerate(chunks):
            msg = f"({i + 1}/{n}) {raw}" if prefixed else raw
            try:
                res = await self._mc.commands.send_chan_msg(chan, msg)
            except Exception as exc:
                self._last_error = str(exc)
                return {"ok": False, "error": str(exc), "sent": i}
            if EventType is not None and getattr(res, "type", None) == EventType.ERROR:
                reason = ""
                try:
                    reason = (res.payload or {}).get("reason", "")
                except Exception:
                    pass
                self._last_error = f"нода отклонила: {reason or '?'}"
                return {"ok": False, "error": self._last_error, "sent": i}
            if i < n - 1 and delay > 0:
                await asyncio.sleep(delay)
        self._sent_count += 1
        self._last_sent_ts = time.time()
        self._last_error = None
        return {"ok": True, "chunks": n}

    # ------------------------------------------------------------------
    # Public sync API (callable from any thread)
    # ------------------------------------------------------------------
    def send_channel(self, text: str, channel_index: Optional[int] = None,
                     wait: bool = False) -> dict:
        """Broadcast `text` to the configured MeshCore channel.

        wait=False (default): fire-and-forget — schedule on the loop and return
        immediately, so mirroring never adds latency to the Meshtastic path.
        wait=True: block for the result (used by the UI test button).
        """
        if not self._enabled():
            return {"ok": False, "error": "disabled"}
        if not self._loop or not self._connected:
            return {"ok": False, "error": self._last_error or "не подключено"}
        chan = int(channel_index) if channel_index is not None else self._resolve_chan()
        try:
            fut = asyncio.run_coroutine_threadsafe(self._send_coro(text, chan), self._loop)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not wait:
            return {"ok": True, "queued": True}
        try:
            return fut.result(timeout=25)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def channels(self, refresh: bool = False) -> list[dict]:
        """Cached channel table [{index, name}]. refresh=True re-reads the node."""
        if refresh and self._loop and self._connected:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._load_channels(), self._loop)
                fut.result(timeout=15)
            except Exception:
                pass
        return list(self._channels)

    def status(self) -> dict:
        c = self._mc_cfg()
        return {
            "available": MESHCORE_AVAILABLE,
            "enabled": bool(c.get("enabled")),
            "connected": bool(self._connected),
            "port": c.get("port") or "auto",
            "resolved_port": self._resolved_port,
            "baud": int(c.get("baud") or 115200),
            "channel_name": c.get("channel_name") or "",
            "channel_index": self._resolve_chan(),
            "channels": list(self._channels),
            "detected_ports": self.list_ports(),
            "sent_count": self._sent_count,
            "rx_count": self._rx_count,
            "last_sent_ts": int(self._last_sent_ts) or None,
            "last_error": self._last_error,
        }


__all__ = ["MeshCoreBridge", "DEFAULTS", "MESHCORE_AVAILABLE", "MAX_TEXT_BYTES"]
