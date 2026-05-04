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

import glob
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 4403
MESSAGE_BUFFER_SIZE = 200

# Meshtastic firmware caps a single text packet around 228 bytes of UTF-8 payload.
# We keep some headroom for protocol overhead and for the "(N/M) " prefix when chunking.
MAX_TEXT_BYTES = 200
CHUNK_PREFIX_BUDGET = 12          # "(99/99) " upper bound
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
    """Split a multi-line text into chunks that each fit into max_bytes UTF-8 bytes.

    Splits at line boundaries first; falls back to char-level cuts only if a
    single line exceeds the limit.
    """
    if not text:
        return [text] if text == "" else []
    if _utf8_len(text) <= max_bytes:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0

    def flush():
        nonlocal current, current_bytes
        if current:
            chunks.append("\n".join(current))
            current = []
            current_bytes = 0

    for raw_line in text.split("\n"):
        line_bytes = _utf8_len(raw_line)
        sep = 1 if current else 0  # newline between accumulated lines
        if line_bytes <= max_bytes and current_bytes + sep + line_bytes <= max_bytes:
            current.append(raw_line)
            current_bytes += sep + line_bytes
            continue

        # Either the line itself is too big, or it can't fit alongside what we have.
        flush()

        if line_bytes <= max_bytes:
            current = [raw_line]
            current_bytes = line_bytes
            continue

        # Long single line — break by characters keeping UTF-8 boundaries intact.
        buf = ""
        buf_bytes = 0
        for ch in raw_line:
            cb = _utf8_len(ch)
            if buf_bytes + cb > max_bytes:
                chunks.append(buf)
                buf = ch
                buf_bytes = cb
            else:
                buf += ch
                buf_bytes += cb
        if buf:
            current = [buf]
            current_bytes = buf_bytes

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
    ):
        self._connection_type = connection_type or "serial"
        self._device_path = device_path or "auto"
        self._tcp_host = tcp_host or ""
        self._tcp_port = int(tcp_port or DEFAULT_TCP_PORT)
        self._iface = None
        self._lock = threading.Lock()

        # rolling chat buffer
        self._messages: deque[dict[str, Any]] = deque(maxlen=MESSAGE_BUFFER_SIZE)
        self._msg_lock = threading.Lock()
        self._msg_counter = 0
        self._pubsub_subscribed = False

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
            self._pubsub_subscribed = True
            log.info("Subscribed to meshtastic.receive.text")
        except Exception:
            log.exception("Failed to subscribe to meshtastic pubsub")

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
        msg_id: Any = None,
        reply_to: Any = None,
        is_reaction: bool = False,
        hops_taken: Optional[int] = None,
        rx_rssi: Optional[float] = None,
        rx_snr: Optional[float] = None,
    ) -> dict[str, Any]:
        with self._msg_lock:
            self._msg_counter += 1
            msg = {
                "id": self._msg_counter,
                "time": int(time.time()),
                "from_id": from_id,
                "from_name": from_name,
                "channel": int(channel or 0),
                "text": text,
                "incoming": incoming,
                "msg_id": msg_id if msg_id else None,
                "reply_to": reply_to if reply_to else None,
                "is_reaction": bool(is_reaction),
                "hops_taken": hops_taken,
                "rx_rssi": rx_rssi,
                "rx_snr": rx_snr,
            }
            self._messages.append(msg)
            return msg

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

            self._add_message(
                text=text,
                from_id=from_id,
                from_name=from_name,
                channel=channel,
                incoming=True,
                msg_id=msg_id,
                reply_to=reply_to,
                is_reaction=is_reaction,
                hops_taken=hops_taken,
                rx_rssi=rx_rssi,
                rx_snr=rx_snr,
            )
        except Exception:
            log.exception("Failed to handle incoming text packet")

    def get_messages(self, since_id: int = 0) -> list[dict[str, Any]]:
        with self._msg_lock:
            return [m for m in self._messages if m["id"] > since_id]

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
        self._add_message(
            text=text, from_id="me", from_name="Я",
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
                iface._sendPacket(packet, destinationId=dest)
                pkt_id = packet.id
            except Exception as exc:
                log.exception("Reaction send failed; closing interface")
                self._close_locked()
                raise RuntimeError(f"Ошибка отправки реакции: {exc}") from exc

        # Mirror our own reaction into the chat buffer so the UI shows it
        # as a chip immediately, without waiting for the mesh to echo it back.
        self._add_message(
            text=emoji_text,
            from_id="me",
            from_name="Я",
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
                iface._sendPacket(packet, destinationId=dest)
                pkt_id = packet.id
            except Exception as exc:
                log.exception("Reply send failed; closing interface")
                self._close_locked()
                raise RuntimeError(f"Ошибка отправки ответа: {exc}") from exc

        self._add_message(
            text=text,
            from_id="me",
            from_name="Я",
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
