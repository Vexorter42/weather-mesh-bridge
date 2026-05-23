"""Telegram → Mesh bridge (experimental).

Two modes (selectable in config / UI):

  1. **"web"** — no API key needed, no login, no `telethon` install. Polls the
     public preview page `https://t.me/s/<channel>` every N seconds and parses
     the embedded HTML. Works only for **public** channels (those that show
     a "View in Telegram" preview), but most UAV / civil-defence channels are
     intentionally public. This is the default and works from any IP.

  2. **"telethon"** — uses the official MTProto user-API via the
     `telethon` library. Requires `api_id` / `api_hash` from my.telegram.org
     and a one-time interactive auth (run `python telegram_setup.py` on the
     Pi). Works for private/secret channels too, but Telegram blocks the
     "create application" page on many residential and datacenter IPs.

Either way, when a new message matches one of the configured keywords, the
bridge forwards a short summary into the mesh via the supplied callback.
"""
from __future__ import annotations

import asyncio
import collections
import html as html_lib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

# Telethon is imported lazily — the web mode doesn't need it.
try:
    from telethon import TelegramClient, events  # type: ignore
    from telethon.errors import (  # type: ignore
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
    )
    TELETHON_AVAILABLE = True
except Exception:
    TelegramClient = None  # type: ignore
    events = None  # type: ignore
    TELETHON_AVAILABLE = False


DEFAULTS = {
    "enabled": False,
    "mode": "web",                # "web" (no API) or "telethon" (with API)
    "api_id": None,               # telethon mode only
    "api_hash": "",               # telethon mode only
    "channels": [],
    "keywords": ["БПЛА", "беспилотник", "угроза", "тревога", "атака", "ракет"],
    # Geo filter — extra keywords for narrowing alerts to your region. If the
    # list is non-empty, a message must mention at least one of these words
    # in addition to one of `keywords` to be forwarded. Empty list = no geo
    # filter (forward every keyword match).
    "geo_filter": [],
    "broadcast_to": "broadcast",
    "channel_index": 0,
    "min_interval_seconds": 60,
    "poll_interval_seconds": 60,  # web mode polling cadence
    "forward_prefix": "🚨 TG",
    # SOCKS5 / HTTP proxy for accessing t.me when ISP blocks it.
    # Examples:
    #   "socks5h://127.0.0.1:1080"           — local SOCKS5 (DNS through proxy)
    #   "socks5://user:pass@1.2.3.4:1080"    — remote with auth
    #   "http://1.2.3.4:8080"                — plain HTTP proxy
    # Empty string = direct connection.
    "proxy": "",
}


def _parse_proxy(url: str):
    """Parse a proxy URL into (requests_proxies, telethon_proxy_tuple).

    Returns (None, None) if URL is empty or malformed.
    """
    if not url:
        return None, None
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return None, None
    scheme = (p.scheme or "").lower()
    host = p.hostname
    port = p.port
    user = p.username
    password = p.password
    if not host or not port or not scheme:
        return None, None

    # For `requests` — the proxies dict accepts the raw URL for both http+https
    # destinations. Use socks5h to force DNS lookup through the proxy too —
    # important when the user's DNS also fails for t.me.
    if scheme == "socks5":
        request_url = url.replace("socks5://", "socks5h://", 1)
    else:
        request_url = url
    requests_proxies = {"http": request_url, "https": request_url}

    # For telethon — build a tuple (proxy_type, host, port, rdns, user, pass)
    telethon_proxy = None
    try:
        import socks  # type: ignore  # from PySocks
        if scheme in ("socks5", "socks5h"):
            telethon_proxy = (socks.SOCKS5, host, port, True, user, password)
        elif scheme == "socks4":
            telethon_proxy = (socks.SOCKS4, host, port, True, user, password)
        elif scheme in ("http", "https"):
            telethon_proxy = (socks.HTTP, host, port, True, user, password)
    except ImportError:
        # Without PySocks, telethon mode can't use the proxy. Web mode still
        # works through `requests` for http proxies, but SOCKS won't work
        # there either. Status-endpoint surfaces this if relevant.
        pass

    return requests_proxies, telethon_proxy


# --- HTML parsing helpers for the "web" mode -------------------------------

# Each message block on t.me/s/<channel> has the shape
#   <div class="tgme_widget_message ..." data-post="channel/12345" ...>
#     ...
#     <div class="tgme_widget_message_text ...">… message text …</div>
#     ...
#   </div>
# We use a robust-ish regex (no DOM tree) — t.me preview HTML is stable.
_MSG_RE = re.compile(
    r'data-post="(?P<post>[^"]+)"[^>]*>.*?'
    r'class="tgme_widget_message_text[^"]*"[^>]*>(?P<text>.*?)</div>',
    re.S,
)
# Username/channel-title — found near the top of the page
_TITLE_RE = re.compile(
    r'<div class="tgme_channel_info_header_title"[^>]*><span[^>]*>(.*?)</span>',
    re.S,
)


def _strip_tags(s: str) -> str:
    """Convert <br>/<a>… to plain text. Used on message bodies only."""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>\s*<p[^>]*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html_lib.unescape(s).strip()


class TelegramBridge:
    def __init__(self, session_path: Path, mesh_send_callback: Callable[[str, int, str], None]):
        """
        :param session_path: Path to telethon session file (only used in telethon mode)
        :param mesh_send_callback: function(text, channel_index, destination)
        """
        self._session_path = Path(session_path)
        self._mesh_send = mesh_send_callback

        # --- common state ---
        self._cfg: dict[str, Any] = dict(DEFAULTS)
        self._cfg_lock = threading.Lock()
        self._last_fwd: dict[str, float] = {}   # per-channel dedup
        self._history: collections.deque[dict] = collections.deque(maxlen=50)
        self._stop_event = threading.Event()

        # --- telethon-mode state ---
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tele_thread: Optional[threading.Thread] = None
        self._client: Optional[Any] = None
        self._handler_disposers: list[Callable] = []

        # --- web-mode state ---
        self._web_thread: Optional[threading.Thread] = None
        self._web_last_seen: dict[str, int] = {}  # channel_username → highest seen msg_id

        self._status: dict[str, Any] = {
            "running": False,
            "mode": "web",
            "authorized": False,
            "username": None,
            "telethon_available": TELETHON_AVAILABLE,
            "last_error": None,
            "last_event_ts": None,
            "matched_count": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(self, cfg: dict[str, Any]) -> None:
        with self._cfg_lock:
            merged = dict(DEFAULTS)
            merged.update(cfg or {})
            self._cfg = merged
        if self._status["running"]:
            self.stop()
            if self._cfg.get("enabled"):
                self.start()

    def status(self) -> dict[str, Any]:
        with self._cfg_lock:
            cfg = dict(self._cfg)
        return {
            **self._status,
            "config": {
                "enabled": cfg["enabled"],
                "mode": cfg.get("mode", "web"),
                "api_id_set": bool(cfg.get("api_id")),
                "api_hash_set": bool(cfg.get("api_hash")),
                "channels": list(cfg.get("channels") or []),
                "keywords": list(cfg.get("keywords") or []),
                "geo_filter": list(cfg.get("geo_filter") or []),
                "broadcast_to": cfg.get("broadcast_to"),
                "channel_index": cfg.get("channel_index"),
                "min_interval_seconds": cfg.get("min_interval_seconds"),
                "poll_interval_seconds": cfg.get("poll_interval_seconds"),
                "forward_prefix": cfg.get("forward_prefix"),
                "proxy": cfg.get("proxy") or "",
            },
            "session_exists": self._session_path.exists(),
            "recent_matches": list(self._history),
        }

    def start(self) -> dict[str, Any]:
        if self._status["running"]:
            return {"ok": True, "already_running": True}

        with self._cfg_lock:
            cfg = dict(self._cfg)
        mode = (cfg.get("mode") or "web").lower()
        self._status["mode"] = mode

        if mode == "telethon":
            return self._start_telethon(cfg)
        else:
            return self._start_web(cfg)

    def stop(self) -> dict[str, Any]:
        if not self._status["running"]:
            return {"ok": True, "already_stopped": True}

        self._stop_event.set()

        # Web mode — just wait for the polling thread
        if self._web_thread and self._web_thread.is_alive():
            self._web_thread.join(timeout=5)
            self._web_thread = None

        # Telethon mode — schedule async shutdown on the loop
        loop = self._loop
        client = self._client
        if loop and loop.is_running():
            async def _shutdown():
                try:
                    for disp in list(self._handler_disposers):
                        try: disp()
                        except Exception: pass
                    self._handler_disposers.clear()
                    if client and client.is_connected():
                        await client.disconnect()
                except Exception:
                    log.exception("Telegram bridge shutdown failed")
                finally:
                    loop.stop()
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        if self._tele_thread and self._tele_thread.is_alive():
            self._tele_thread.join(timeout=5)
            self._tele_thread = None

        self._stop_event.clear()
        self._status["running"] = False
        return {"ok": True}

    # ------------------------------------------------------------------
    # Mode: WEB (no API key, public-channel scraping)
    # ------------------------------------------------------------------

    def _start_web(self, cfg: dict[str, Any]) -> dict[str, Any]:
        if not cfg.get("channels"):
            self._status["last_error"] = "Не задано ни одного канала. Впиши @имя_канала в список."
            return {"ok": False, "error": self._status["last_error"]}

        # Reset state
        self._stop_event.clear()
        # Don't reset _web_last_seen — we want to keep dedup state across restarts
        self._web_thread = threading.Thread(
            target=self._web_loop,
            daemon=True,
            name="tg-web-bridge",
        )
        self._web_thread.start()
        self._status["running"] = True
        self._status["last_error"] = None
        log.info("Telegram bridge (web mode) started on %d channels",
                 len(cfg.get("channels") or []))
        return {"ok": True, "mode": "web"}

    def _web_loop(self):
        """Polling loop — scrape every channel every N seconds."""
        first_pass = True
        while not self._stop_event.is_set():
            try:
                with self._cfg_lock:
                    cfg = dict(self._cfg)
                channels = cfg.get("channels") or []
                keywords = [k.lower() for k in (cfg.get("keywords") or []) if k]
                geo_filter = [g.lower() for g in (cfg.get("geo_filter") or []) if g]
                interval = max(15, int(cfg.get("poll_interval_seconds") or 60))
                min_iv = float(cfg.get("min_interval_seconds") or 60)
                prefix = cfg.get("forward_prefix") or "🚨 TG"
                broadcast_to = cfg.get("broadcast_to") or "broadcast"
                channel_idx = int(cfg.get("channel_index") or 0)

                for ch in channels:
                    if self._stop_event.is_set():
                        break
                    try:
                        self._poll_channel_web(
                            ch, keywords, geo_filter, prefix, broadcast_to,
                            channel_idx, min_iv, first_pass,
                        )
                    except Exception:
                        log.exception("Web-poll failed for %r", ch)
            except Exception:
                log.exception("Telegram web loop iteration crashed")

            first_pass = False
            # Sleep but wake up immediately if stop() is called
            self._stop_event.wait(interval)
        log.info("Telegram web loop stopped")

    def _poll_channel_web(
        self, channel_name: str, keywords: list[str], geo_filter: list[str],
        prefix: str, broadcast_to: str, channel_idx: int,
        min_iv: float, first_pass: bool,
    ) -> None:
        """Fetch one channel's preview page and process new messages."""
        ch = channel_name.lstrip("@").strip()
        if not ch:
            return
        if ch.startswith("-100") or ch.lstrip("-").isdigit():
            log.warning(
                "Web mode can't fetch numeric channel id %r — use @username instead. "
                "If the channel has no public username, you need 'telethon' mode.",
                ch,
            )
            return

        url = f"https://t.me/s/{ch}"
        with self._cfg_lock:
            proxy_url = self._cfg.get("proxy") or ""
        proxies, _ = _parse_proxy(proxy_url)
        try:
            r = requests.get(
                url, timeout=15, proxies=proxies,
                headers={"User-Agent": "Mozilla/5.0 (compatible; weather-mesh-bridge)"},
            )
            r.raise_for_status()
            html = r.text
        except Exception as exc:
            log.warning("Failed to fetch %s — %s", url, exc)
            return

        # Resolve a friendly channel title for the mesh message
        title_m = _TITLE_RE.search(html)
        src_name = (
            _strip_tags(title_m.group(1)) if title_m else f"@{ch}"
        ) or f"@{ch}"

        # Find all message blocks; iterate oldest → newest (t.me preview is
        # listed bottom-up in the DOM, but iterating in match order is fine
        # — we sort by parsed msg_id at the end).
        matches: list[tuple[int, str]] = []  # (msg_id, plain_text)
        for m in _MSG_RE.finditer(html):
            post = m.group("post")          # "channel/12345"
            try:
                msg_id = int(post.split("/")[-1])
            except (ValueError, IndexError):
                continue
            text = _strip_tags(m.group("text"))
            if not text:
                continue
            matches.append((msg_id, text))

        if not matches:
            return
        matches.sort(key=lambda r: r[0])
        latest_seen = self._web_last_seen.get(ch, 0)

        # On the very first pass for a channel, just record the latest id
        # without forwarding history — otherwise we'd spam the mesh with
        # 20 backlogged messages.
        if first_pass and latest_seen == 0:
            self._web_last_seen[ch] = matches[-1][0]
            log.info("Telegram[web] %s — primed with msg_id %d, no backlog forwarded",
                     ch, matches[-1][0])
            return

        for msg_id, text in matches:
            if msg_id <= latest_seen:
                continue
            self._web_last_seen[ch] = msg_id

            tl = text.lower()
            matched_kw = next((k for k in keywords if k in tl), None) if keywords else "*"
            if not matched_kw:
                continue
            # Geo filter: if defined, at least one geo term must also be in the text
            if geo_filter:
                matched_geo = next((g for g in geo_filter if g in tl), None)
                if not matched_geo:
                    log.debug("TG[web] %s — keyword %r matched but geo filter skipped",
                              channel_name, matched_kw)
                    continue
                # Annotate the keyword with the geo word for the history feed
                matched_kw = f"{matched_kw} · {matched_geo}"

            now = time.time()
            key = str(src_name)
            last = self._last_fwd.get(key, 0)
            throttled = (now - last) < min_iv
            if not throttled:
                self._last_fwd[key] = now

            snippet = text if len(text) <= 600 else text[:597] + "…"
            mesh_text = f"{prefix} · {src_name}\n{snippet}"
            entry = {
                "ts": int(now),
                "channel": src_name,
                "text": snippet,
                "keyword": matched_kw,
                "throttled": throttled,
            }
            self._history.appendleft(entry)
            self._status["matched_count"] += 1
            self._status["last_event_ts"] = int(now)
            if not throttled:
                try:
                    self._mesh_send(mesh_text, channel_idx, broadcast_to)
                    log.info("TG[web] → mesh: %s · %r", src_name, snippet[:60])
                except Exception:
                    log.exception("Failed to forward TG[web] message to mesh")

    # ------------------------------------------------------------------
    # Mode: TELETHON (MTProto, requires api_id / api_hash / session)
    # ------------------------------------------------------------------

    def _start_telethon(self, cfg: dict[str, Any]) -> dict[str, Any]:
        if not TELETHON_AVAILABLE:
            self._status["last_error"] = "telethon не установлен. Запусти `pip install telethon` в venv."
            return {"ok": False, "error": self._status["last_error"]}
        if not cfg.get("api_id") or not cfg.get("api_hash"):
            self._status["last_error"] = "Не заданы api_id / api_hash. Возьми их на https://my.telegram.org"
            return {"ok": False, "error": self._status["last_error"]}
        if not self._session_path.exists():
            self._status["last_error"] = (
                "Сессия Telegram не создана. SSH в Pi и запусти `python telegram_setup.py` "
                "один раз — это попросит телефон, SMS-код (и 2FA пароль если включён)."
            )
            return {"ok": False, "error": self._status["last_error"]}

        self._stop_event.clear()
        self._tele_thread = threading.Thread(
            target=self._tele_run_loop,
            args=(cfg,),
            daemon=True,
            name="tg-telethon-bridge",
        )
        self._tele_thread.start()
        for _ in range(50):
            if self._loop is not None:
                break
            time.sleep(0.02)
        return {"ok": True, "mode": "telethon"}

    def _tele_run_loop(self, cfg: dict[str, Any]):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._tele_async_setup(cfg))
            self._status["running"] = True
            log.info("Telegram bridge (telethon mode) started on %d channels",
                     len(cfg.get("channels") or []))
            loop.run_forever()
        except Exception as exc:
            log.exception("Telegram telethon loop crashed")
            self._status["last_error"] = str(exc)
        finally:
            self._status["running"] = False
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            self._client = None
            log.info("Telegram telethon loop stopped")

    async def _tele_async_setup(self, cfg: dict[str, Any]):
        api_id = int(cfg["api_id"])
        api_hash = str(cfg["api_hash"])
        _, telethon_proxy = _parse_proxy(cfg.get("proxy") or "")
        kw = {}
        if telethon_proxy:
            kw["proxy"] = telethon_proxy
        self._client = TelegramClient(str(self._session_path), api_id, api_hash, **kw)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            self._status["authorized"] = False
            raise RuntimeError(
                "Сессия найдена, но не авторизована. Запусти telegram_setup.py заново."
            )
        self._status["authorized"] = True
        try:
            me = await self._client.get_me()
            self._status["username"] = (
                f"@{me.username}" if getattr(me, "username", None)
                else getattr(me, "first_name", "?")
            )
        except Exception:
            self._status["username"] = "?"

        resolved: list[Any] = []
        for ch in cfg.get("channels") or []:
            try:
                resolved.append(await self._client.get_entity(ch))
                log.info("Telegram[telethon]: subscribed to %r", ch)
            except Exception as exc:
                log.warning("Telegram[telethon]: failed to resolve %r — %s", ch, exc)

        keywords = [k.lower() for k in (cfg.get("keywords") or []) if k]
        geo_filter = [g.lower() for g in (cfg.get("geo_filter") or []) if g]
        min_iv = float(cfg.get("min_interval_seconds") or 60)
        prefix = cfg.get("forward_prefix") or "🚨 TG"
        broadcast_to = cfg.get("broadcast_to") or "broadcast"
        channel_idx = int(cfg.get("channel_index") or 0)

        @self._client.on(events.NewMessage(chats=resolved))
        async def _handler(event):
            try:
                text = (event.raw_text or "").strip()
                if not text:
                    return
                tl = text.lower()
                matched = next((k for k in keywords if k in tl), None) if keywords else "*"
                if not matched:
                    return
                if geo_filter:
                    matched_geo = next((g for g in geo_filter if g in tl), None)
                    if not matched_geo:
                        return
                    matched = f"{matched} · {matched_geo}"
                try:
                    chat = await event.get_chat()
                    src_name = (
                        getattr(chat, "title", None)
                        or getattr(chat, "username", None)
                        or str(getattr(chat, "id", "?"))
                    )
                except Exception:
                    src_name = "?"
                key = str(src_name)
                now = time.time()
                last = self._last_fwd.get(key, 0)
                throttled = (now - last) < min_iv
                if not throttled:
                    self._last_fwd[key] = now
                snippet = text if len(text) <= 600 else text[:597] + "…"
                mesh_text = f"{prefix} · {src_name}\n{snippet}"
                entry = {
                    "ts": int(now),
                    "channel": src_name,
                    "text": snippet,
                    "keyword": matched,
                    "throttled": throttled,
                }
                self._history.appendleft(entry)
                self._status["matched_count"] += 1
                self._status["last_event_ts"] = int(now)
                if not throttled:
                    try:
                        self._mesh_send(mesh_text, channel_idx, broadcast_to)
                    except Exception:
                        log.exception("Failed to forward TG[telethon] message to mesh")
            except Exception:
                log.exception("Telegram telethon handler crashed")

        self._handler_disposers.append(lambda: self._client.remove_event_handler(_handler))


    # ------------------------------------------------------------------
    # Diagnostics — quick connectivity test (used by the UI "Проверить" button)
    # ------------------------------------------------------------------

    def test_send(self, text: Optional[str] = None) -> dict:
        """Send a TEST message into the mesh using the bridge's destination /
        channel / prefix settings. Lets the user verify the *full* forwarding
        pipeline without waiting for a real TG event.
        """
        with self._cfg_lock:
            cfg = dict(self._cfg)
        prefix = cfg.get("forward_prefix") or "🚨 TG"
        broadcast_to = cfg.get("broadcast_to") or "broadcast"
        channel_idx = int(cfg.get("channel_index") or 0)
        if not text:
            text = (
                f"{prefix} · TEST · {time.strftime('%H:%M:%S')}\n"
                f"Тестовое сообщение — Telegram-мост жив и видит mesh."
            )
        try:
            self._mesh_send(text, channel_idx, broadcast_to)
        except Exception as exc:
            log.exception("test_send failed")
            return {"ok": False, "error": str(exc), "text": text}
        # Also append to the history so the user sees it in the UI feed
        self._history.appendleft({
            "ts": int(time.time()),
            "channel": "[TEST]",
            "text": text,
            "keyword": "test",
            "throttled": False,
        })
        return {
            "ok": True,
            "text": text,
            "broadcast_to": broadcast_to,
            "channel_index": channel_idx,
        }

    def test_fetch(self, channel: str = "durov") -> dict:
        """Fetch one t.me preview page via the configured proxy and report
        diagnostic info. Doesn't touch persistent state."""
        with self._cfg_lock:
            cfg = dict(self._cfg)
        ch = (channel or "durov").strip().lstrip("@")
        url = f"https://t.me/s/{ch}"
        proxies, _ = _parse_proxy(cfg.get("proxy") or "")
        info: dict[str, Any] = {
            "url": url,
            "via_proxy": bool(proxies),
            "proxy": cfg.get("proxy") or None,
        }
        t0 = time.time()
        try:
            r = requests.get(
                url, timeout=15, proxies=proxies,
                headers={"User-Agent": "Mozilla/5.0 (compatible; weather-mesh-bridge)"},
            )
            info["elapsed_seconds"] = round(time.time() - t0, 2)
            info["status_code"] = r.status_code
            info["bytes"] = len(r.content)
            info["ok"] = r.ok and "tgme_widget_message" in r.text
            if not info["ok"] and r.ok:
                info["hint"] = "Страница загрузилась, но в ней нет сообщений — возможно, канал приватный или несуществующий."
            elif r.ok:
                # Count messages found so user can see it's parsing
                info["messages_seen"] = len(_MSG_RE.findall(r.text))
        except Exception as exc:
            info["elapsed_seconds"] = round(time.time() - t0, 2)
            info["ok"] = False
            info["error"] = str(exc)
        return info


__all__ = ["TelegramBridge", "DEFAULTS", "TELETHON_AVAILABLE"]
