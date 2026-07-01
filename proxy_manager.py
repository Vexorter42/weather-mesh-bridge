"""Manage a local Xray (VLESS) tunnel from the web UI.

Parses a subscription URL into a list of exits, builds an Xray config for the
chosen one (SOCKS5 inbound on 127.0.0.1:10808), writes it and restarts the
`xray` service. Lets the user switch exit country from the «Прокси» tab instead
of editing configs over SSH.

No secrets live here — the subscription URL / UUIDs stay in config.json and the
git-ignored subscription cache on the Pi.
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

log = logging.getLogger(__name__)

XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
SOCKS_PORT = 10808
PROXY_URL = f"socks5://127.0.0.1:{SOCKS_PORT}"


def _b64d(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def fetch_subscription(url: str, timeout: int = 25) -> str:
    """Fetch + base64-decode a subscription body into newline-joined URIs."""
    raw = urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8", "replace").strip()
    try:
        dec = _b64d(raw).decode("utf-8", "replace")
        return dec if "://" in dec else raw
    except Exception:
        return raw


def parse_exits(decoded: str) -> list[dict[str, Any]]:
    """All exits from a decoded subscription. Each dict keeps the full `uri`
    (with UUID) for internal use — strip it before sending to the browser."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate([l.strip() for l in decoded.splitlines() if "://" in l]):
        try:
            if line.startswith("vmess://"):
                j = json.loads(_b64d(line[8:]).decode("utf-8", "replace"))
                proto, host, port, name = "vmess", j.get("add", "?"), j.get("port", "?"), j.get("ps", "")
            else:
                scheme, rest = line.split("://", 1)
                frag = ""
                if "#" in rest:
                    rest, frag = rest.split("#", 1)
                name = urllib.parse.unquote(frag)
                after_at = rest.split("@", 1)[1] if "@" in rest else rest
                hostport = after_at.split("?", 1)[0]
                host, _, port = hostport.partition(":")
                proto = scheme
            out.append({"index": i, "proto": proto, "name": name,
                        "host": host, "port": port, "uri": line})
        except Exception as exc:
            out.append({"index": i, "proto": "?", "name": f"(ошибка разбора: {exc})",
                        "host": "", "port": "", "uri": line})
    return out


def public_exits(exits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the UUID-bearing `uri` and skip placeholder/header rows."""
    pub = []
    for e in exits:
        if e.get("host") in ("", "0.0.0.0") or not e.get("uri", "").startswith(("vless://", "trojan://", "vmess://")):
            continue
        pub.append({k: e[k] for k in ("index", "proto", "name", "host", "port")})
    return pub


def build_xray_config(uri: str) -> dict[str, Any]:
    """VLESS URI → Xray config (SOCKS5 inbound + the VLESS outbound)."""
    if not uri.startswith("vless://"):
        raise ValueError("Поддерживается только vless:// (этот выход другого типа)")
    rest = uri.split("://", 1)[1]
    if "#" in rest:
        rest = rest.split("#", 1)[0]
    userinfo, hostpart = rest.split("@", 1)
    uuid_ = userinfo
    hostport, _, query = hostpart.partition("?")
    host, _, port = hostport.partition(":")
    port = int(port or 443)
    q = dict(urllib.parse.parse_qsl(query))

    net = q.get("type", "tcp")
    sec = q.get("security", "none")
    flow = q.get("flow", "")
    sni = q.get("sni") or q.get("peer") or host
    fp = q.get("fp", "chrome")

    stream: dict[str, Any] = {"network": net, "security": sec}
    if sec == "reality":
        stream["realitySettings"] = {
            "serverName": sni, "fingerprint": fp,
            "publicKey": q.get("pbk", ""), "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", ""),
        }
    elif sec == "tls":
        tls: dict[str, Any] = {"serverName": sni, "fingerprint": fp,
                               "allowInsecure": q.get("allowInsecure") in ("1", "true")}
        if q.get("alpn"):
            tls["alpn"] = urllib.parse.unquote(q["alpn"]).split(",")
        stream["tlsSettings"] = tls

    if net == "ws":
        stream["wsSettings"] = {"path": urllib.parse.unquote(q.get("path", "/")),
                                "headers": {"Host": q.get("host", sni)}}
        flow = ""
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": urllib.parse.unquote(q.get("serviceName", ""))}
        flow = ""
    elif net in ("h2", "http"):
        stream["httpSettings"] = {"path": urllib.parse.unquote(q.get("path", "/")),
                                  "host": [q.get("host", sni)]}
        flow = ""

    user: dict[str, Any] = {"id": uuid_, "encryption": "none"}
    if flow:
        user["flow"] = flow

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "socks-in", "listen": "127.0.0.1", "port": SOCKS_PORT, "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        }],
        "outbounds": [
            {"protocol": "vless", "tag": "proxy",
             "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
             "streamSettings": stream},
            {"protocol": "freedom", "tag": "direct"},
        ],
    }


def apply_exit(uri: str) -> None:
    """Write the Xray config for `uri` and restart the xray service.
    Needs passwordless sudo (cp into /usr/local/etc + systemctl restart)."""
    config = build_xray_config(uri)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(config, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    cp = subprocess.run(["sudo", "cp", tmp, XRAY_CONFIG_PATH],
                        capture_output=True, text=True, timeout=15)
    if cp.returncode != 0:
        raise RuntimeError(f"Не удалось записать конфиг Xray: {cp.stderr.strip() or cp.stdout.strip()}")
    rs = subprocess.run(["sudo", "systemctl", "restart", "xray"],
                        capture_output=True, text=True, timeout=20)
    if rs.returncode != 0:
        raise RuntimeError(f"Не удалось перезапустить xray: {rs.stderr.strip() or rs.stdout.strip()}")
    time.sleep(2)   # give the new tunnel a moment to establish


def current_exit_ip(timeout: int = 8, retries: int = 3, delay: float = 1.5) -> Optional[str]:
    """Public IP as seen through the local SOCKS proxy (None if it's down).
    Retries a few times — right after an xray restart the tunnel needs a moment."""
    proxies = {"http": f"socks5h://127.0.0.1:{SOCKS_PORT}",
               "https": f"socks5h://127.0.0.1:{SOCKS_PORT}"}
    import requests
    for attempt in range(max(1, retries)):
        try:
            r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=timeout)
            r.raise_for_status()
            return (r.json() or {}).get("ip")
        except Exception:
            if attempt + 1 < retries:
                time.sleep(delay)
    return None


def _tcp_ping(host: str, port: int, timeout: float = 3.0) -> Optional[int]:
    """TCP-connect latency to host:port in ms (None if unreachable)."""
    try:
        start = time.time()
        with socket.create_connection((host, int(port)), timeout=timeout):
            return int((time.time() - start) * 1000)
    except Exception:
        return None


def ping_exits(exits: list[dict[str, Any]], timeout: float = 3.0) -> list[dict[str, Any]]:
    """Measure TCP latency to every exit concurrently. exits = public_exits()."""
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = [(e, pool.submit(_tcp_ping, e.get("host"), e.get("port"), timeout))
                for e in exits if e.get("host") and e.get("port")]
        return [{"index": e["index"], "ms": fut.result()} for e, fut in futs]


__all__ = ["fetch_subscription", "parse_exits", "public_exits", "build_xray_config",
           "apply_exit", "current_exit_ip", "ping_exits", "PROXY_URL",
           "XRAY_CONFIG_PATH", "SOCKS_PORT"]
