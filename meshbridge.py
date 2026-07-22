"""Bridge to a Heltec Mesh Node V4 running Meshtastic firmware.

Supports two transports:
    - serial (USB)              — meshtastic.serial_interface.SerialInterface
    - tcp    (WiFi)             — meshtastic.tcp_interface.TCPInterface

Also keeps a rolling buffer of recent text messages (incoming + outgoing) for
the chat tab. Subscribes to the meshtastic pypubsub topic exactly once.

Docs:
    https://meshtastic.org/docs/software/python/cli/
    https://python.meshtastic.org/
"""
from __future__ import annotations

import collections
import glob
import logging
import random
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 4403

# Meshtastic firmware caps a single text packet at ~237 bytes of Data payload;
# the text itself can use ~228 of that. We pack chunks right up to this limit
# to minimise the number of LoRa transmissions. If you ever see a
# "Data payload too big" error from the firmware, lower MAX_TEXT_BYTES a bit.
MAX_TEXT_BYTES = 228
CHUNK_PREFIX_BUDGET = 9           # reserve for the "(i/N) " prefix — "(99/99) " == 8 chars
CHUNK_DELAY_SECONDS = 10.0        # default pause between chunks; can be overridden per-call


def _autodetect_port() -> Optional[str]:
    """Look for a likely USB serial device on Linux."""
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def split_for_mesh(text: str, max_bytes: int = MAX_TEXT_BYTES) -> list[str]:
    """Split text into chunks that each fit into max_bytes UTF-8 bytes.

    Packing strategy (greedy, word-aware):
      1. Each chunk is filled to the byte limit as tightly as possible.
      2. Breaks happen on **word boundaries** (spaces) and line boundaries —
         never in the middle of a word.
      3. Only if a *single word* is itself longer than the whole budget do we
         fall back to a character-level cut for that one word.

    So "привет меня зовут" never becomes "привет ме-ня зовут"; it stays
    "привет меня" / "зовут" if it has to split at all.
    """
    if not text:
        return [text] if text == "" else []
    if _utf8_len(text) <= max_bytes:
        return [text]

    chunks: list[str] = []
    current = ""           # chunk being built
    current_bytes = 0

    def flush():
        nonlocal current, current_bytes
        if current:
            chunks.append(current)
            current = ""
            current_bytes = 0

    def add(token: str, sep: str) -> None:
        """Append `token` to the current chunk, prefixed by `sep` (" " or "\\n"),
        starting a new chunk if it wouldn't fit. `sep` is omitted at chunk start."""
        nonlocal current, current_bytes
        tb = _utf8_len(token)

        # A single token bigger than the whole budget — char-split it (rare:
        # only some 200-char URL with no spaces would trigger this).
        if tb > max_bytes:
            flush()
            buf, buf_bytes = "", 0
            for ch in token:
                cb = _utf8_len(ch)
                if buf_bytes + cb > max_bytes:
                    chunks.append(buf)
                    buf, buf_bytes = ch, cb
                else:
                    buf += ch
                    buf_bytes += cb
            if buf:
                current, current_bytes = buf, buf_bytes
            return

        sb = _utf8_len(sep) if current else 0
        if current_bytes + sb + tb <= max_bytes:
            current += (sep if current else "") + token
            current_bytes += sb + tb
        else:
            flush()
            current = token
            current_bytes = tb

    lines = text.split("\n")
    for li, line in enumerate(lines):
        if line == "":
            # Preserve a blank line (paragraph break) if there's room in the
            # current chunk; otherwise just let the flush handle spacing.
            if current and current_bytes + 1 <= max_bytes:
                current += "\n"
                current_bytes += 1
            continue
        words = [w for w in line.split(" ") if w != ""]
        for wi, word in enumerate(words):
            if not current and not chunks:
                sep = ""          # very first token of the message
            elif wi == 0:
                sep = "\n"        # first word of a new line
            else:
                sep = " "         # subsequent word on the same line
            add(word, sep)

    flush()
    return chunks


class MeshBridge:
    """Thread-safe wrapper around meshtastic Serial/TCP interface."""

    def __init__(
        self,
        connection_type: str = "serial",
        device_path: str = "auto",
        tcp_host: str = "",
        tcp_port: int = DEFAULT_TCP_PORT,
        chat_db: Any = None,                                   # ChatDb instance
        command_handler: Optional[Callable[[dict], Optional[str]]] = None,
    ):
        self._connection_type = connection_type or "serial"
        self._device_path = device_path or "auto"
        self._tcp_host = tcp_host or ""
        self._tcp_port = int(tcp_port or DEFAULT_TCP_PORT)
        self._iface = None
        self._lock = threading.Lock()

        # persistent chat history
        self._db = chat_db
        self._msg_lock = threading.Lock()
        self._pubsub_subscribed = False

        # Our own "last heard" per node num. The library's in-memory
        # iface.nodes[*].lastHeard can go stale between reconnects (it only
        # refreshes the full NodeDB at connect), which makes the online count
        # under-report. We bump this on every received packet and fold it into
        # the freshness check so the count stays live without a reconnect.
        self._last_seen: dict[int, int] = {}
        # Rolling traffic log for /traffic analytics: (ts, portnum, from_num).
        # Bounded so a busy mesh can't grow it without limit; pruned by time on read.
        self._traffic: "collections.deque" = collections.deque(maxlen=300000)
        # Optional callback(ts, portnum, from_num) — app.py persists to history_db.
        self._pkt_callback: Optional[Callable] = None

        # optional callback: gets the message dict, returns text to send back
        # (used for the !commands feature). Called in a background thread.
        self._command_handler = command_handler

        # Traceroute reply rendezvous — maps "node_id" → {event, result}.
        self._traceroute_waiters: dict[str, dict[str, Any]] = {}
        self._traceroute_lock = threading.Lock()

        # Random back-off before answering a command. Replying the instant a
        # request arrives often collides on the LoRa channel (the requester's
        # radio / the mesh may still be busy), so we wait a random few seconds
        # to let the air clear. Configurable via set_command_reply_delay().
        self._reply_delay_min = 5.0
        self._reply_delay_max = 10.0

    def set_packet_callback(self, fn) -> None:
        self._pkt_callback = fn

    def set_chat_db(self, db: Any) -> None:
        self._db = db

    def set_command_reply_delay(self, min_s: float, max_s: float) -> None:
        """Configure the random back-off (seconds) before a command reply."""
        try:
            lo = max(0.0, float(min_s))
            hi = max(lo, float(max_s))
        except (TypeError, ValueError):
            return
        self._reply_delay_min = lo
        self._reply_delay_max = hi

    def set_command_handler(self, fn: Optional[Callable[[dict], Optional[str]]]) -> None:
        self._command_handler = fn

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, mesh_cfg: dict[str, Any]) -> None:
        """Update connection settings; close current connection if anything changed."""
        with self._lock:
            new_type = (mesh_cfg.get("connection_type") or "serial").lower()
            new_path = mesh_cfg.get("device_path") or "auto"
            new_host = (mesh_cfg.get("tcp_host") or "").strip()
            new_port = int(mesh_cfg.get("tcp_port") or DEFAULT_TCP_PORT)

            if (
                new_type != self._connection_type
                or new_path != self._device_path
                or new_host != self._tcp_host
                or new_port != self._tcp_port
            ):
                self._connection_type = new_type
                self._device_path = new_path
                self._tcp_host = new_host
                self._tcp_port = new_port
                self._close_locked()

    @property
    def connection_type(self) -> str:
        return self._connection_type

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _resolve_path(self) -> Optional[str]:
        if not self._device_path or self._device_path == "auto":
            return _autodetect_port()
        return self._device_path

    def _ensure_locked(self):
        if self._iface is not None:
            return self._iface

        if self._connection_type == "tcp":
            from meshtastic.tcp_interface import TCPInterface  # type: ignore

            host = self._tcp_host
            if not host:
                raise RuntimeError(
                    "Не указан IP-адрес Heltec. Открой Meshtastic-приложение, подключи плату к WiFi и впиши её IP."
                )
            log.info("Opening Meshtastic TCP on %s:%d", host, self._tcp_port)
            self._iface = TCPInterface(hostname=host, portNumber=self._tcp_port)
        else:
            import meshtastic.serial_interface as msi  # type: ignore

            path = self._resolve_path()
            if path is None:
                raise RuntimeError(
                    "Не найдено USB-устройство Heltec. Проверь кабель или переключись на WiFi (TCP)."
                )
            log.info("Opening Meshtastic serial on %s", path)
            self._iface = msi.SerialInterface(devPath=path)

        self._ensure_pubsub_subscription()
        return self._iface

    def _ensure_pubsub_subscription(self) -> None:
        if self._pubsub_subscribed:
            return
        try:
            from pubsub import pub  # type: ignore

            pub.subscribe(self._on_text_received, "meshtastic.receive.text")
            # Generic receive — used to catch TRACEROUTE_APP responses. The
            # text-specific handler above runs alongside this one for text
            # packets; we just early-return for non-traceroute portnums here.
            pub.subscribe(self._on_any_packet, "meshtastic.receive")
            self._pubsub_subscribed = True
            log.info("Subscribed to meshtastic.receive.text and .receive")
        except Exception:
            log.exception("Failed to subscribe to meshtastic pubsub")

    def _mark_seen(self, packet) -> None:
        """Record that we just heard from a node — keeps the online count fresh
        even if the library's NodeDB lastHeard hasn't been refreshed."""
        try:
            num = (packet or {}).get("from")
            if isinstance(num, int):
                self._last_seen[num] = int(time.time())
        except Exception:
            pass

    def _on_any_packet(self, packet=None, interface=None):
        """Dispatch non-text packets we care about (traceroute, ACKs)."""
        try:
            self._mark_seen(packet)
            decoded = (packet or {}).get("decoded") or {}
            portnum = decoded.get("portnum")

            # Traffic accounting: every received packet, for /traffic analytics.
            try:
                _ev = (int(time.time()), str(portnum or "?"), (packet or {}).get("from"))
                self._traffic.append(_ev)
                if self._pkt_callback:
                    self._pkt_callback(*_ev)
            except Exception:
                pass

            # ROUTING_APP packets are ACK / NAK responses — match by request_id
            # to update delivery status of our outgoing messages.
            if portnum == "ROUTING_APP":
                self._handle_routing_ack(packet, decoded)
                return

            if portnum != "TRACEROUTE_APP":
                return

            # The library sometimes pre-decodes the RouteDiscovery into
            # decoded["routeDiscovery"]; if not, parse the raw payload.
            rd = decoded.get("routeDiscovery")
            route_nums: list[int] = []
            route_back_nums: list[int] = []
            snr_towards: list[float] = []
            snr_back: list[float] = []
            if isinstance(rd, dict):
                route_nums = list(rd.get("route") or [])
                route_back_nums = list(rd.get("routeBack") or rd.get("route_back") or [])
                snr_towards = [s / 4.0 for s in (rd.get("snrTowards") or rd.get("snr_towards") or [])]
                snr_back = [s / 4.0 for s in (rd.get("snrBack") or rd.get("snr_back") or [])]
            else:
                try:
                    from meshtastic import mesh_pb2  # type: ignore
                    payload = decoded.get("payload")
                    if payload:
                        proto = mesh_pb2.RouteDiscovery()
                        proto.ParseFromString(payload)
                        route_nums = list(proto.route)
                        route_back_nums = list(getattr(proto, "route_back", []) or [])
                        # snr values are stored as int8 scaled ×4
                        snr_towards = [s / 4.0 for s in getattr(proto, "snr_towards", [])]
                        snr_back    = [s / 4.0 for s in getattr(proto, "snr_back", [])]
                except Exception:
                    log.exception("Failed to decode RouteDiscovery payload")

            from_id = packet.get("fromId") or packet.get("from")

            # Resolve readable names for every hop on each path
            def _to_hop_list(nums: list[int]) -> list[dict]:
                out = []
                for num in nums:
                    name = self._resolve_name(num)
                    hop_id = f"!{int(num):08x}" if isinstance(num, int) else str(num)
                    out.append({"node_id": hop_id, "name": name, "num": int(num)})
                return out

            hops_forward = _to_hop_list(route_nums)
            hops_back    = _to_hop_list(route_back_nums)

            # Resolve "our" node — useful for the map so the UI knows where the
            # forward arrow starts / the return arrow ends.
            me = None
            try:
                my_info = getattr(self._iface, "myInfo", None)
                my_num = getattr(my_info, "my_node_num", None) if my_info else None
                if my_num is not None:
                    me = {
                        "node_id": f"!{int(my_num):08x}",
                        "name": self._resolve_name(my_num),
                        "num": int(my_num),
                    }
            except Exception:
                me = None

            result = {
                "from_id": str(from_id) if from_id else None,
                "from_name": self._resolve_name(from_id) if from_id else "?",
                # Backward-compatible alias (some older UI code reads `hops`).
                "hops": hops_forward,
                "hops_forward": hops_forward,
                "hops_back": hops_back,
                "snr_towards": snr_towards,
                "snr_back": snr_back,
                "rx_snr": packet.get("rxSnr"),
                "rx_rssi": packet.get("rxRssi"),
                "me": me,
            }
            log.info(
                "Traceroute reply from %s: forward=%s, back=%s",
                from_id,
                [h["name"] for h in hops_forward],
                [h["name"] for h in hops_back],
            )

            # Wake any waiter keyed by that node id (we accept either the
            # short hex or the numeric form).
            keys_to_try = []
            if from_id:
                keys_to_try.append(str(from_id))
                try:
                    keys_to_try.append(f"!{int(from_id):08x}")
                except (TypeError, ValueError):
                    pass

            with self._traceroute_lock:
                for k in keys_to_try:
                    waiter = self._traceroute_waiters.get(k)
                    if waiter is not None:
                        waiter["result"] = result
                        waiter["event"].set()
                        break
        except Exception:
            log.exception("on_any_packet crashed")

    def _handle_routing_ack(self, packet: dict, decoded: dict) -> None:
        """Update delivery_status of an outgoing message when a ROUTING_APP
        packet arrives that references it via request_id.
        """
        if self._db is None:
            return
        # request_id is the mesh packet ID of the message being ACKed.
        # meshtastic-python serialises proto fields to camelCase ("requestId")
        # in some versions and snake_case in others — handle both.
        req_id = (
            decoded.get("requestId")
            or decoded.get("request_id")
        )
        if req_id is None:
            routing = decoded.get("routing") or {}
            if isinstance(routing, dict):
                req_id = routing.get("requestId") or routing.get("request_id")
        try:
            req_id = int(req_id) if req_id is not None else None
        except (TypeError, ValueError):
            req_id = None
        log.info(
            "ROUTING_APP received: req_id=%s, from=%s, raw_decoded_keys=%s",
            req_id, packet.get("fromId"), list(decoded.keys()),
        )
        if not req_id:
            return

        # error_reason: 0 = NONE (success), anything else = failure
        err = 0
        routing = decoded.get("routing")
        if isinstance(routing, dict):
            err = routing.get("errorReason") or routing.get("error_reason") or 0
        else:
            err = decoded.get("errorReason") or decoded.get("error_reason") or 0
        try:
            err = int(err) if err else 0
        except (TypeError, ValueError):
            err = 0

        # hops taken by the ACK packet — proxy for hops to destination
        hops_taken = None
        try:
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            if hop_start is not None and hop_limit is not None:
                hops_taken = max(0, int(hop_start) - int(hop_limit))
        except (TypeError, ValueError):
            hops_taken = None

        status = "delivered" if err == 0 else "error"
        try:
            row = self._db.update_delivery_by_mesh_id(req_id, status, hops=hops_taken)
            if row:
                log.info(
                    "Delivery ACK: mesh_id=%s → %s (hops=%s, err=%s)",
                    req_id, status, hops_taken, err,
                )
        except Exception:
            log.exception("Failed to update delivery status")

    def traceroute(self, destination: str, hop_limit: int = 5,
                   channel_index: int = 0, timeout: float = 60.0) -> dict[str, Any]:
        """Send a traceroute request and block waiting for the reply.

        Returns a dict { hops: [{node_id, name, num}], snr_towards: [...],
        rx_snr, rx_rssi } or { error } on timeout / send failure.
        """
        if not destination or destination in ("broadcast", "^all"):
            return {"error": "Нужен конкретный узел, не broadcast"}

        start_ts = time.time()
        event = threading.Event()
        waiter = {"event": event, "result": None}
        with self._traceroute_lock:
            self._traceroute_waiters[str(destination)] = waiter

        with self._lock:
            try:
                iface = self._ensure_locked()
                send_fn = getattr(iface, "sendTraceRoute", None)
                if not callable(send_fn):
                    return {"error": "Эта версия meshtastic-библиотеки не умеет sendTraceRoute"}
                # The library version that ships in pypi has sendTraceRoute(dest, hopLimit, channelIndex)
                # — but it ALSO blocks on its own internal waiter and prints to stdout.
                # We use a non-blocking workaround: build the packet manually.
                try:
                    from meshtastic import BROADCAST_NUM, mesh_pb2, portnums_pb2  # type: ignore
                    rd = mesh_pb2.RouteDiscovery()
                    data = mesh_pb2.Data()
                    data.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
                    data.payload = rd.SerializeToString()
                    data.want_response = True

                    pkt = mesh_pb2.MeshPacket()
                    pkt.decoded.CopyFrom(data)
                    pkt.channel = int(channel_index)
                    pkt.hop_limit = int(hop_limit)
                    pkt.want_ack = True
                    pkt.id = iface._generatePacketId()
                    iface._sendPacket(pkt, destinationId=destination)
                    log.info("Traceroute → %s sent (id=%s, hop_limit=%d)",
                             destination, pkt.id, hop_limit)
                except Exception as exc:
                    log.exception("Manual traceroute send failed, will try library API")
                    # Fallback to library call (will block its own thread)
                    threading.Thread(
                        target=lambda: send_fn(destination, hop_limit, channel_index),
                        daemon=True,
                    ).start()
            except Exception as exc:
                with self._traceroute_lock:
                    self._traceroute_waiters.pop(str(destination), None)
                return {"error": f"Не удалось отправить traceroute: {exc}"}

        # Wait outside the bridge lock so other operations can proceed.
        got = event.wait(timeout)
        with self._traceroute_lock:
            self._traceroute_waiters.pop(str(destination), None)
        if not got or waiter["result"] is None:
            return {
                "error": f"Узел {destination} не ответил за {int(timeout)} сек",
                "elapsed_seconds": round(time.time() - start_ts, 2),
            }
        result = waiter["result"]
        result["elapsed_seconds"] = round(time.time() - start_ts, 2)
        return result

    def _close_locked(self):
        if self._iface is not None:
            try:
                self._iface.close()
            except Exception:
                pass
            self._iface = None

    def close(self):
        with self._lock:
            self._close_locked()

    def connect(self) -> dict:
        """Force-open the interface and return its status."""
        with self._lock:
            try:
                self._ensure_locked()
            except Exception as exc:
                self._close_locked()
                return {
                    "connected": False,
                    "connection_type": self._connection_type,
                    "configured_path": self._device_path,
                    "resolved_path": self._resolve_path(),
                    "tcp_host": self._tcp_host,
                    "tcp_port": self._tcp_port,
                    "error": str(exc),
                }
        return self.status()

    def status(self) -> dict:
        with self._lock:
            connected = self._iface is not None
            info: dict[str, Any] = {
                "connection_type": self._connection_type,
                "configured_path": self._device_path,
                "resolved_path": self._resolve_path() if self._connection_type == "serial" else None,
                "tcp_host": self._tcp_host,
                "tcp_port": self._tcp_port,
                "connected": connected,
            }
            if connected:
                try:
                    my_info = getattr(self._iface, "myInfo", None)
                    nodes = getattr(self._iface, "nodes", None) or {}
                    info["my_node_num"] = getattr(my_info, "my_node_num", None)
                    info["nodes_known"] = len(nodes)
                    # «Онлайн» — те, чей last_heard был не дальше двух часов назад.
                    # Берём максимум из NodeDB lastHeard и нашего _last_seen
                    # (обновляется на каждый принятый пакет), чтобы счётчик не
                    # занижался из-за протухшего NodeDB между реконнектами.
                    now = int(time.time())
                    online = 0
                    for n in nodes.values():
                        if not isinstance(n, dict):
                            continue
                        try:
                            lh = int(n.get("lastHeard") or 0)
                        except (TypeError, ValueError):
                            lh = 0
                        seen = self._last_seen.get(n.get("num"), 0)
                        fresh = max(lh, seen)
                        if fresh and (now - fresh) < 7200:
                            online += 1
                    info["nodes_online_2h"] = online
                    # Legacy key kept for backward compat with older UIs.
                    info["nodes_online_1h"] = online
                except Exception as exc:
                    info["info_error"] = str(exc)
            return info

    # ------------------------------------------------------------------
    # Send / Receive
    # ------------------------------------------------------------------

    def _resolve_name(self, node_id: Any) -> str:
        if not node_id:
            return "?"
        try:
            nodes = getattr(self._iface, "nodes", None) or {}
            for k, n in nodes.items():
                num = (n or {}).get("num")
                if num == node_id or k == node_id or str(num) == str(node_id):
                    user = (n or {}).get("user") or {}
                    return user.get("longName") or user.get("shortName") or str(node_id)
        except Exception:
            pass
        # fall back to formatted hex if it's a number
        try:
            n = int(node_id)
            return f"!{n:08x}"
        except Exception:
            return str(node_id)

    def _add_message(
        self,
        *,
        text: str,
        from_id: Any,
        from_name: str,
        channel: int,
        incoming: bool,
        to_id: Optional[str] = None,
        msg_id: Any = None,
        reply_to: Any = None,
        is_reaction: bool = False,
        hops_taken: Optional[int] = None,
        rx_rssi: Optional[float] = None,
        rx_snr: Optional[float] = None,
        delivery_status: Optional[str] = None,
        via_mqtt: bool = False,
    ) -> dict[str, Any]:
        # For outgoing messages we default to "enroute" — the packet is already
        # on the air (we wouldn't be here if _sendPacket had thrown). Status
        # then transitions to "delivered"/"error" when a ROUTING_APP ACK arrives.
        if not incoming and delivery_status is None and msg_id and not is_reaction:
            delivery_status = "enroute"
        msg = {
            "time": int(time.time()),
            "from_id": from_id,
            "from_name": from_name,
            "to_id": to_id,
            "channel": int(channel or 0),
            "text": text,
            "incoming": incoming,
            "msg_id": msg_id if msg_id else None,
            "reply_to": reply_to if reply_to else None,
            "is_reaction": bool(is_reaction),
            "hops_taken": hops_taken,
            "rx_rssi": rx_rssi,
            "rx_snr": rx_snr,
            "delivery_status": delivery_status,
            "via_mqtt": bool(via_mqtt),
        }
        if self._db is None:
            log.warning("ChatDb is not set on MeshBridge; message will be dropped")
            return msg
        with self._msg_lock:
            return self._db.add(msg)

    def _on_text_received(self, packet=None, interface=None):
        try:
            if not packet:
                return
            decoded = packet.get("decoded") or {}
            text = decoded.get("text")
            if not text:
                payload = decoded.get("payload")
                if isinstance(payload, (bytes, bytearray)):
                    try:
                        text = payload.decode("utf-8")
                    except Exception:
                        text = payload.decode("utf-8", errors="replace")
            if not text:
                return
            from_id = packet.get("fromId") or packet.get("from")
            to_id = packet.get("toId")  # "^all" for broadcast, "!hex" for DM
            channel = packet.get("channel", 0)
            from_name = self._resolve_name(from_id)

            # Reactions are TEXT_MESSAGE_APP packets with emoji=1 and replyId pointing
            # to the original packet. Plain replies have emoji=0 + replyId. Keep both flags
            # so the UI can render reactions as chips and replies as threaded messages.
            emoji_flag = decoded.get("emoji") or 0
            reply_to_raw = decoded.get("replyId") or 0
            try:
                reply_to = int(reply_to_raw) if reply_to_raw else None
            except (TypeError, ValueError):
                reply_to = None
            is_reaction = bool(emoji_flag) and reply_to is not None

            msg_id_raw = packet.get("id")
            try:
                msg_id = int(msg_id_raw) if msg_id_raw else None
            except (TypeError, ValueError):
                msg_id = None

            # RF metadata: number of hops the packet took, signal strength, SNR.
            # hopStart - hopLimit = how many relay hops the packet went through.
            hops_taken: Optional[int] = None
            try:
                hop_start = packet.get("hopStart")
                hop_limit = packet.get("hopLimit")
                if hop_start is not None and hop_limit is not None:
                    hops_taken = max(0, int(hop_start) - int(hop_limit))
            except (TypeError, ValueError):
                hops_taken = None

            def _to_float(v):
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            rx_rssi = _to_float(packet.get("rxRssi"))
            rx_snr = _to_float(packet.get("rxSnr"))

            # Packets relayed through an MQTT gateway carry the `viaMqtt` flag
            # (i.e. they reached us over the internet, not purely over LoRa RF).
            via_mqtt = bool(packet.get("viaMqtt") or packet.get("via_mqtt"))

            msg = self._add_message(
                text=text,
                from_id=from_id,
                from_name=from_name,
                to_id=to_id,
                channel=channel,
                incoming=True,
                msg_id=msg_id,
                reply_to=reply_to,
                is_reaction=is_reaction,
                hops_taken=hops_taken,
                rx_rssi=rx_rssi,
                rx_snr=rx_snr,
                via_mqtt=via_mqtt,
            )
            # Trigger command handler in a background thread — it might want
            # to send a reply, which we can't do from inside the pubsub callback.
            if (
                self._command_handler
                and msg.get("incoming")
                and not msg.get("is_reaction")
                and (text.startswith("!") or text.startswith("/"))
            ):
                threading.Thread(
                    target=self._dispatch_command,
                    args=(dict(msg),),
                    daemon=True,
                ).start()
        except Exception:
            log.exception("Failed to handle incoming text packet")

    def _dispatch_command(self, msg: dict[str, Any]) -> None:
        """Run the command handler and send its response back into the mesh.

        If the original request arrived as a direct message (to_id is a specific
        node, not "^all"), the response goes back as a DM to the sender. Public
        broadcasts get a threaded reply on the same channel.
        """
        try:
            response = self._command_handler(msg) if self._command_handler else None
        except Exception:
            log.exception("Command handler crashed")
            return
        if not response:
            return

        # LoRa back-off: wait a random few seconds before transmitting the reply
        # so it doesn't collide with the requester still occupying the channel.
        # Runs in this background thread, so the sleep blocks nothing important.
        if self._reply_delay_max > 0:
            delay = random.uniform(self._reply_delay_min, self._reply_delay_max)
            if delay > 0:
                log.info("Command reply back-off: waiting %.1fs before sending", delay)
                time.sleep(delay)

        to_id = msg.get("to_id")
        is_dm = bool(to_id) and to_id not in ("^all", "all")
        channel_index = int(msg.get("channel", 0))

        try:
            if is_dm:
                # Reply privately to the sender; no reply_id needed (it's already
                # a one-on-one conversation thread).
                from_id = msg.get("from_id")
                if from_id:
                    log.info("Command was DM from %s — replying privately", from_id)
                    self.send_text_chunked(
                        response,
                        channel_index=channel_index,
                        destination=str(from_id),
                    )
                else:
                    # No sender id — fall back to broadcast.
                    self.send_text_chunked(response, channel_index=channel_index)
            elif msg.get("msg_id"):
                self.send_reply(
                    response,
                    reply_to=int(msg["msg_id"]),
                    channel_index=channel_index,
                )
            else:
                self.send_text_chunked(response, channel_index=channel_index)
        except Exception:
            log.exception("Sending command response failed")

    # ------------------------------------------------------------------
    # Heltec device configuration (long/short name, region, role, ...)
    # ------------------------------------------------------------------

    # Region codes (LoRa frequency band) — mirrored from
    # meshtastic.config_pb2.Config.LoRaConfig.RegionCode enum so the UI can
    # display human-friendly labels without needing to import protobufs.
    REGION_CODES: list[tuple[int, str]] = [
        (0, "UNSET (не выбран)"),
        (1, "US — 902–928 МГц"),
        (2, "EU_433 — 433 МГц"),
        (3, "EU_868 — 868 МГц"),
        (4, "CN — 470–510 МГц"),
        (5, "JP — 920–923 МГц"),
        (6, "ANZ — 915–928 МГц"),
        (7, "KR — 920–923 МГц"),
        (8, "TW — 920–925 МГц"),
        (9, "RU — 868–870 МГц"),
        (10, "IN — 865–867 МГц"),
        (11, "NZ_865 — 864–868 МГц"),
        (12, "TH — 920–925 МГц"),
        (13, "LORA_24 — 2.4 ГГц"),
        (14, "UA_433 — 433 МГц"),
        (15, "UA_868 — 868 МГц"),
        (16, "MY_433 — 433 МГц"),
        (17, "MY_919 — 919–924 МГц"),
        (18, "SG_923 — 917–925 МГц"),
    ]

    # Device roles — mirrored from DeviceConfig.Role enum.
    ROLE_CODES: list[tuple[int, str]] = [
        (0, "CLIENT — обычный клиент"),
        (1, "CLIENT_MUTE — не ретранслирует"),
        (2, "ROUTER — постоянный ретранслятор"),
        (4, "REPEATER — выделенный репитер"),
        (5, "TRACKER — GPS-трекер"),
        (6, "SENSOR — сенсор телеметрии"),
        (7, "TAK"),
        (8, "CLIENT_HIDDEN — скрытый"),
        (9, "LOST_AND_FOUND"),
        (10, "TAK_TRACKER"),
        (11, "ROUTER_LATE — поздний ретранслятор"),
    ]

    # LoRa modem preset (speed vs range trade-off).
    MODEM_PRESETS: list[tuple[int, str]] = [
        (0, "LONG_FAST (по умолчанию)"),
        (1, "LONG_SLOW"),
        (3, "MEDIUM_SLOW"),
        (4, "MEDIUM_FAST"),
        (5, "SHORT_SLOW"),
        (6, "SHORT_FAST"),
        (7, "LONG_MODERATE"),
        (8, "SHORT_TURBO"),
    ]

    def get_device_info(self) -> dict[str, Any]:
        """Read current Heltec/Meshtastic local config.

        Returns a flat dict with the fields exposed in the UI. Empty dict if
        the link isn't connected yet.
        """
        info: dict[str, Any] = {
            "connected": False,
            "regions": [{"value": v, "label": l} for v, l in self.REGION_CODES],
            "roles": [{"value": v, "label": l} for v, l in self.ROLE_CODES],
            "modem_presets": [{"value": v, "label": l} for v, l in self.MODEM_PRESETS],
        }
        with self._lock:
            if self._iface is None:
                return info
            try:
                node = getattr(self._iface, "localNode", None)
                my_info = getattr(self._iface, "myInfo", None)
                meta = getattr(self._iface, "metadata", None)

                # Owner name comes from the User proto in nodes[<my_num>].user
                my_num = getattr(my_info, "my_node_num", None) if my_info else None
                nodes = getattr(self._iface, "nodes", None) or {}
                me_user = {}
                if my_num is not None:
                    for n in nodes.values():
                        if isinstance(n, dict) and n.get("num") == my_num:
                            me_user = n.get("user") or {}
                            break

                info["long_name"] = me_user.get("longName", "")
                info["short_name"] = me_user.get("shortName", "")
                info["hw_model"] = me_user.get("hwModel", "")
                info["my_node_num"] = my_num

                if meta is not None:
                    info["firmware_version"] = getattr(meta, "firmware_version", "") or ""

                local_cfg = getattr(node, "localConfig", None) if node else None
                if local_cfg is not None:
                    lora = getattr(local_cfg, "lora", None)
                    device = getattr(local_cfg, "device", None)
                    if lora is not None:
                        info["region"] = int(getattr(lora, "region", 0))
                        info["hop_limit"] = int(getattr(lora, "hop_limit", 3))
                        info["modem_preset"] = int(getattr(lora, "modem_preset", 0))
                        info["use_preset"] = bool(getattr(lora, "use_preset", True))
                        info["tx_enabled"] = bool(getattr(lora, "tx_enabled", True))
                        info["tx_power"] = int(getattr(lora, "tx_power", 0))
                    if device is not None:
                        info["role"] = int(getattr(device, "role", 0))

                info["connected"] = True
            except Exception as exc:
                log.exception("Failed to read device info")
                info["error"] = str(exc)
        return info

    def set_device_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update to Heltec settings.

        Accepts any of: long_name, short_name, region, role, hop_limit,
        modem_preset, tx_enabled, tx_power.

        Each change goes through a settings-transaction so the device commits
        them atomically and the radio reloads once.
        """
        with self._lock:
            iface = self._ensure_locked()
            node = getattr(iface, "localNode", None)
            if node is None:
                raise RuntimeError("Нода ещё не отдала localConfig — попробуй через пару секунд.")

            applied: list[str] = []

            # Owner name (long + short) — separate admin op, not part of localConfig.
            long_name = payload.get("long_name")
            short_name = payload.get("short_name")
            if long_name is not None or short_name is not None:
                try:
                    set_owner = getattr(node, "setOwner", None)
                    if not callable(set_owner):
                        raise RuntimeError("Эта версия meshtastic-библиотеки не умеет setOwner")
                    kw: dict[str, Any] = {}
                    if long_name is not None:
                        kw["long_name"] = str(long_name)[:39]
                    if short_name is not None:
                        kw["short_name"] = str(short_name)[:4]
                    set_owner(**kw)
                    applied.append("owner")
                except Exception as exc:
                    log.exception("setOwner failed")
                    raise RuntimeError(f"Не удалось сменить имя ноды: {exc}") from exc

            # LoRa / device settings — go through writeConfig
            lora_changed = False
            device_changed = False
            local_cfg = getattr(node, "localConfig", None)
            if local_cfg is None:
                if applied:
                    return {"applied": applied}
                raise RuntimeError("localConfig недоступен — нода не ответила")

            lora = getattr(local_cfg, "lora", None)
            device = getattr(local_cfg, "device", None)

            if "region" in payload and lora is not None:
                lora.region = int(payload["region"])
                lora_changed = True
            if "hop_limit" in payload and lora is not None:
                hl = max(1, min(7, int(payload["hop_limit"])))
                lora.hop_limit = hl
                lora_changed = True
            if "modem_preset" in payload and lora is not None:
                lora.modem_preset = int(payload["modem_preset"])
                lora.use_preset = True
                lora_changed = True
            if "tx_enabled" in payload and lora is not None:
                lora.tx_enabled = bool(payload["tx_enabled"])
                lora_changed = True
            if "tx_power" in payload and lora is not None:
                lora.tx_power = int(payload["tx_power"])
                lora_changed = True

            if "role" in payload and device is not None:
                device.role = int(payload["role"])
                device_changed = True

            if lora_changed or device_changed:
                try:
                    begin = getattr(node, "beginSettingsTransaction", None)
                    commit = getattr(node, "commitSettingsTransaction", None)
                    if callable(begin):
                        begin()
                    if lora_changed:
                        node.writeConfig("lora")
                        applied.append("lora")
                    if device_changed:
                        node.writeConfig("device")
                        applied.append("device")
                    if callable(commit):
                        commit()
                except Exception as exc:
                    log.exception("writeConfig failed")
                    raise RuntimeError(f"Не удалось применить настройки: {exc}") from exc

            return {"applied": applied}

    def reboot_device(self, delay_seconds: int = 5) -> dict[str, Any]:
        """Tell the Heltec to reboot after `delay_seconds`."""
        with self._lock:
            iface = self._ensure_locked()
            node = getattr(iface, "localNode", None)
            if node is None:
                raise RuntimeError("Нет соединения с Heltec")
            reboot = getattr(node, "reboot", None)
            if not callable(reboot):
                raise RuntimeError("Эта версия meshtastic-библиотеки не умеет reboot")
            try:
                reboot(int(delay_seconds))
            except TypeError:
                # старые версии принимают позиционный/без аргумента
                reboot()
        return {"ok": True, "delay": int(delay_seconds)}

    def get_known_channels(self) -> list[dict[str, Any]]:
        """Return the list of channels configured on the connected Heltec.

        Each entry: {"index", "name", "role"} where role is 'primary' or
        'secondary'. Disabled channels are skipped. Falls back to a single
        primary channel 0 if nothing is reported (e.g. node still warming up).
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            if self._iface is None:
                return out
            try:
                node = getattr(self._iface, "localNode", None)
                channels = getattr(node, "channels", None) if node else None
                if channels:
                    for ch in channels:
                        try:
                            role = int(getattr(ch, "role", 0))
                            if role == 0:  # DISABLED
                                continue
                            idx = int(getattr(ch, "index", 0))
                            settings = getattr(ch, "settings", None)
                            name = ""
                            if settings is not None:
                                name = getattr(settings, "name", "") or ""
                            out.append({
                                "index": idx,
                                "name": name or f"Канал {idx}",
                                "role": "primary" if role == 1 else "secondary",
                            })
                        except Exception:
                            log.exception("Failed to read channel info")
            except Exception:
                log.exception("Failed to enumerate channels")
        if not out:
            out.append({"index": 0, "name": "Канал 0", "role": "primary"})
        return out

    def traffic_stats(self, window_s: int = 86400, top: int = 10) -> dict[str, Any]:
        """Packet analytics over the last `window_s`: total, breakdown by portnum
        and the busiest source nodes. Built from the rolling traffic log."""
        cutoff = int(time.time()) - int(window_s)
        by_type: dict[str, int] = {}
        by_node: dict[Any, int] = {}
        total = 0
        for ts, port, frm in list(self._traffic):
            if ts < cutoff:
                continue
            total += 1
            by_type[port] = by_type.get(port, 0) + 1
            if frm is not None:
                by_node[frm] = by_node.get(frm, 0) + 1
        top_nodes = sorted(by_node.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return {
            "total": total,
            "window_s": window_s,
            "by_type": by_type,
            "top_nodes": [{"name": self._resolve_name(num), "count": c} for num, c in top_nodes],
        }

    def get_known_nodes(self) -> list[dict[str, Any]]:
        """Return a list of nodes the bot has heard from. Used for DM selector,
        node-map and node-profile.

        Includes telemetry (battery, voltage, channel utilization) when the
        node has shared it through TELEMETRY_APP.
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            if self._iface is None:
                return out
            try:
                nodes = getattr(self._iface, "nodes", None) or {}
                for k, n in nodes.items():
                    if not isinstance(n, dict):
                        continue
                    user = n.get("user") or {}
                    position = n.get("position") or {}
                    metrics = n.get("deviceMetrics") or {}
                    lh = int(n.get("lastHeard") or 0)
                    lh = max(lh, self._last_seen.get(n.get("num"), 0))
                    out.append({
                        "node_id": user.get("id") or str(k),
                        "num": n.get("num"),
                        "short_name": user.get("shortName") or "",
                        "long_name": user.get("longName") or "",
                        "hw_model": user.get("hwModel") or "",
                        "role": user.get("role") or "",
                        "last_heard": lh or None,
                        "snr": n.get("snr"),
                        "latitude": position.get("latitude"),
                        "longitude": position.get("longitude"),
                        "altitude": position.get("altitude"),
                        # Telemetry — may be missing if the node hasn't sent it
                        "battery_level": metrics.get("batteryLevel"),
                        "voltage": metrics.get("voltage"),
                        "channel_utilization": metrics.get("channelUtilization"),
                        "air_util_tx": metrics.get("airUtilTx"),
                        "uptime_seconds": metrics.get("uptimeSeconds"),
                    })
            except Exception:
                log.exception("Failed to enumerate nodes")
        out.sort(key=lambda r: (r.get("last_heard") or 0), reverse=True)
        return out

    def get_messages(self, since_id: int = 0) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        return self._db.get_since(int(since_id or 0))

    def send_text(
        self,
        text: str,
        channel_index: int = 0,
        destination: str = "broadcast",
    ) -> dict:
        """Send a text message via the mesh."""
        if not text:
            raise ValueError("Empty message")
        with self._lock:
            iface = self._ensure_locked()
            try:
                kwargs = {"channelIndex": int(channel_index)}
                if destination and destination != "broadcast":
                    kwargs["destinationId"] = destination
                    # Ask radio-level for ACK so the destination's firmware sends
                    # a Routing packet back — that's what we use to flip the
                    # delivery indicator from ☁ to ✓✓.
                    kwargs["wantAck"] = True
                packet = iface.sendText(text, **kwargs)
                pkt_id = None
                try:
                    pkt_id = getattr(packet, "id", None)
                    if pkt_id is None and hasattr(packet, "get"):
                        pkt_id = packet.get("id")
                except Exception:
                    pkt_id = None
                try:
                    pkt_id = int(pkt_id) if pkt_id else None
                except (TypeError, ValueError):
                    pkt_id = None
            except Exception as exc:
                log.exception("Mesh send failed; closing interface")
                self._close_locked()
                raise RuntimeError(f"Ошибка отправки в mesh: {exc}") from exc
        # log outgoing message into chat buffer (outside lock)
        is_broadcast = (not destination) or destination == "broadcast"
        self._add_message(
            text=text, from_id="me", from_name="Я",
            to_id="^all" if is_broadcast else str(destination),
            channel=int(channel_index), incoming=False,
            msg_id=pkt_id,
        )
        return {"ok": True, "packet_id": pkt_id, "chars": len(text)}

    def send_reaction(
        self,
        emoji_text: str,
        reply_to: int,
        channel_index: int = 0,
        destination: str = "broadcast",
    ) -> dict:
        """Send an emoji reaction to a previous mesh message.

        Reactions are TEXT_MESSAGE_APP packets with the `emoji=1` flag and
        `reply_id` pointing at the original message. The bundled `sendText`
        doesn't expose those fields, so we build the protobuf packet manually
        and send it via the interface's low-level helper.
        """
        if not emoji_text:
            raise ValueError("Empty emoji")
        if not reply_to:
            raise ValueError("reply_to is required")

        with self._lock:
            iface = self._ensure_locked()
            try:
                from meshtastic import (  # type: ignore
                    BROADCAST_ADDR,
                    mesh_pb2,
                    portnums_pb2,
                )

                data = mesh_pb2.Data()
                data.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
                data.payload = emoji_text.encode("utf-8")
                data.emoji = 1
                data.reply_id = int(reply_to)

                packet = mesh_pb2.MeshPacket()
                packet.decoded.CopyFrom(data)
                packet.channel = int(channel_index)
                packet.id = iface._generatePacketId()

                dest = BROADCAST_ADDR if destination == "broadcast" else destination
                if dest != BROADCAST_ADDR:
                    packet.want_ack = True  # DMs need ACK for delivery indicator
                iface._sendPacket(packet, destinationId=dest)
                pkt_id = packet.id
            except Exception as exc:
                log.exception("Reaction send failed; closing interface")
                self._close_locked()
                raise RuntimeError(f"Ошибка отправки реакции: {exc}") from exc

        # Mirror our own reaction into the chat buffer so the UI shows it
        # as a chip immediately, without waiting for the mesh to echo it back.
        is_broadcast = (not destination) or destination == "broadcast"
        self._add_message(
            text=emoji_text,
            from_id="me",
            from_name="Я",
            to_id="^all" if is_broadcast else str(destination),
            channel=int(channel_index),
            incoming=False,
            msg_id=pkt_id,
            reply_to=int(reply_to),
            is_reaction=True,
        )
        return {"ok": True, "packet_id": pkt_id}

    def _send_one_with_reply(
        self,
        text: str,
        reply_to: int,
        channel_index: int,
        destination: str,
    ) -> dict:
        """Send a single text packet with reply_id set (no emoji flag)."""
        if not text:
            raise ValueError("Empty text")
        with self._lock:
            iface = self._ensure_locked()
            try:
                from meshtastic import (  # type: ignore
                    BROADCAST_ADDR,
                    mesh_pb2,
                    portnums_pb2,
                )

                data = mesh_pb2.Data()
                data.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
                data.payload = text.encode("utf-8")
                data.reply_id = int(reply_to)

                packet = mesh_pb2.MeshPacket()
                packet.decoded.CopyFrom(data)
                packet.channel = int(channel_index)
                packet.id = iface._generatePacketId()

                dest = BROADCAST_ADDR if destination == "broadcast" else destination
                if dest != BROADCAST_ADDR:
                    packet.want_ack = True  # DMs need ACK for delivery indicator
                iface._sendPacket(packet, destinationId=dest)
                pkt_id = packet.id
            except Exception as exc:
                log.exception("Reply send failed; closing interface")
                self._close_locked()
                raise RuntimeError(f"Ошибка отправки ответа: {exc}") from exc

        is_broadcast = (not destination) or destination == "broadcast"
        self._add_message(
            text=text,
            from_id="me",
            from_name="Я",
            to_id="^all" if is_broadcast else str(destination),
            channel=int(channel_index),
            incoming=False,
            msg_id=pkt_id,
            reply_to=int(reply_to),
        )
        return {"ok": True, "packet_id": pkt_id, "chars": len(text)}

    def send_reply(
        self,
        text: str,
        reply_to: int,
        channel_index: int = 0,
        destination: str = "broadcast",
        chunk_delay: float = CHUNK_DELAY_SECONDS,
    ) -> dict:
        """Send a text reply to a previous mesh message.

        If the text fits, sent as one packet. Otherwise auto-split into chunks;
        only the first chunk carries reply_id (so receiver renders one threaded
        quote, not three).
        """
        if not text:
            raise ValueError("Empty text")
        if not reply_to:
            raise ValueError("reply_to is required")

        if _utf8_len(text) <= MAX_TEXT_BYTES:
            r = self._send_one_with_reply(text, reply_to, channel_index, destination)
            r["chunks"] = 1
            return r

        budget = MAX_TEXT_BYTES - CHUNK_PREFIX_BUDGET
        raw_chunks = split_for_mesh(text, max_bytes=budget)
        n = len(raw_chunks)
        last_pkt_id = None
        total_chars = 0
        for i, raw in enumerate(raw_chunks):
            chunk = f"({i + 1}/{n}) {raw}"
            if i > 0 and chunk_delay > 0:
                time.sleep(chunk_delay)
            if i == 0:
                r = self._send_one_with_reply(chunk, reply_to, channel_index, destination)
            else:
                r = self.send_text(chunk, channel_index=channel_index, destination=destination)
            last_pkt_id = r.get("packet_id") or last_pkt_id
            total_chars += int(r.get("chars") or 0)
        return {"ok": True, "packet_id": last_pkt_id, "chars": total_chars, "chunks": n}

    def send_text_chunked(
        self,
        text: str,
        channel_index: int = 0,
        destination: str = "broadcast",
        chunk_delay: float = CHUNK_DELAY_SECONDS,
    ) -> dict:
        """Send text, splitting into multiple sendText calls if it exceeds MAX_TEXT_BYTES.

        Each chunk after splitting gets a "(i/N) " prefix so the receiver can
        reassemble visually.
        """
        if _utf8_len(text) <= MAX_TEXT_BYTES:
            r = self.send_text(text, channel_index=channel_index, destination=destination)
            r["chunks"] = 1
            return r

        # Reserve bytes for the "(i/N) " prefix so the prefixed chunk still fits.
        budget = MAX_TEXT_BYTES - CHUNK_PREFIX_BUDGET
        raw_chunks = split_for_mesh(text, max_bytes=budget)
        n = len(raw_chunks)
        last_pkt_id = None
        total_chars = 0
        for i, raw in enumerate(raw_chunks):
            chunk = f"({i + 1}/{n}) {raw}"
            if i > 0 and chunk_delay > 0:
                time.sleep(chunk_delay)
            r = self.send_text(chunk, channel_index=channel_index, destination=destination)
            last_pkt_id = r.get("packet_id") or last_pkt_id
            total_chars += int(r.get("chars") or 0)
        return {"ok": True, "packet_id": last_pkt_id, "chars": total_chars, "chunks": n}


__all__ = ["MeshBridge", "split_for_mesh", "MAX_TEXT_BYTES", "CHUNK_DELAY_SECONDS"]
