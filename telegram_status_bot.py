"""Telegram status-bot — pins and live-edits a single message in a chat.

Unlike `telegram_bridge.py` (which *reads* public channels via t.me/s/ or
Telethon MTProto), this module uses the regular **HTTP Bot API** to *write*.
It maintains a single pinned message in a target chat, periodically rewriting
it with:

  - bot online indicator + timestamp of last update
  - current weather snippet (temp + humidity + condition)
  - mesh stats (known nodes / online count)

Setup (one-time, by the user):
  1. Talk to @BotFather → /newbot → copy the bot_token
  2. Add the bot to your group/chat
  3. Promote it to admin with permission "Pin messages"
  4. Find the chat_id — e.g. forward a message to @getmyid_bot
  5. Save bot_token + chat_id in the UI, click "▶ Запустить"

If the chat is in Russia / api.telegram.org is blocked at your ISP, set the
SOCKS5 proxy URL — it's reused from the `telegram_bridge` config by default
to avoid duplication.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",            # number, can be negative (-100… for supergroups)
    "update_seconds": 60,     # how often we re-edit the pinned message
    "auto_pin": True,         # try to pin the message on first send
    # Proxy URL — usually same as in telegram bridge config. Empty = direct.
    "proxy": "",
    # Persisted state (filled by the worker, don't edit by hand)
    "message_id": None,
    # Toggle parts of the rendered message
    "show_mesh_stats": True,
    "show_weather": True,
    "extra_text": "",         # optional footer line (e.g. instructions for users)
}


TG_API = "https://api.telegram.org"


def _api_call(token: str, method: str, params: dict, proxies: dict | None = None) -> dict:
    """Call a Bot API method. Raises on HTTP failure; returns the JSON dict."""
    url = f"{TG_API}/bot{token}/{method}"
    r = requests.get(url, params=params, proxies=proxies, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', '?')}")
    return data.get("result", {})


class TelegramStatusBot:
    """Manages the lifetime of the pinned status message."""

    def __init__(
        self,
        config_save: Callable[[dict], None],
        config_load: Callable[[], dict],
        stats_provider: Callable[[], dict],
        weather_provider: Callable[[], Optional[dict]],
    ):
        """
        :param config_save: function(partial_telegram_status_dict) — called when
            we get a new message_id from Telegram and need to persist it
        :param config_load: function() — returns the freshest CONFIG (so we
            can re-read settings at every tick without restarts)
        :param stats_provider: function() returning a dict with at least
            `mesh_nodes_known` and `mesh_nodes_online_2h`
        :param weather_provider: function() returning a current-weather dict
            (see app.py `/api/weather/current` shape) or None
        """
        self._config_save = config_save
        self._config_load = config_load
        self._stats_provider = stats_provider
        self._weather_provider = weather_provider

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_run_ts: float = 0
        self._last_error: Optional[str] = None
        self._last_success_ts: float = 0
        self._updates_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "last_run_ts": int(self._last_run_ts) if self._last_run_ts else None,
            "last_success_ts": int(self._last_success_ts) if self._last_success_ts else None,
            "last_error": self._last_error,
            "updates_count": self._updates_count,
        }

    def start(self) -> dict[str, Any]:
        if self.is_running():
            return {"ok": True, "already_running": True}
        cfg = self._read_cfg()
        if not cfg.get("bot_token") or not cfg.get("chat_id"):
            return {"ok": False, "error": "Заполни bot_token и chat_id"}
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tg-status-bot")
        self._thread.start()
        return {"ok": True}

    def stop(self) -> dict[str, Any]:
        if not self.is_running():
            return {"ok": True, "already_stopped": True}
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        return {"ok": True}

    def update_now(self) -> dict[str, Any]:
        """Force a single update outside the worker loop — used by the test button."""
        try:
            self._tick()
            return {"ok": True, "last_error": None}
        except Exception as exc:
            log.exception("tg-status update_now crashed")
            self._last_error = str(exc)
            return {"ok": False, "error": str(exc)}

    def mark_offline(self) -> dict[str, Any]:
        """Replace the pinned message with the 'offline' stub. Used by the
        watchdog script via /api/tg-status/mark-offline before the bot dies."""
        try:
            cfg = self._read_cfg()
            if not cfg.get("bot_token") or not cfg.get("chat_id") or not cfg.get("message_id"):
                return {"ok": False, "error": "Нечего обновлять — нет message_id"}
            proxies = self._build_proxies(cfg)
            _api_call(
                cfg["bot_token"], "editMessageText",
                {
                    "chat_id": cfg["chat_id"],
                    "message_id": cfg["message_id"],
                    "text": "🔴 OFFLINE",
                },
                proxies=proxies,
            )
            return {"ok": True}
        except Exception as exc:
            log.exception("mark_offline failed")
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Internal — worker
    # ------------------------------------------------------------------

    def _read_cfg(self) -> dict[str, Any]:
        full = self._config_load() or {}
        cfg = dict(DEFAULTS)
        cfg.update(full.get("telegram_status") or {})
        return cfg

    def _build_proxies(self, cfg: dict[str, Any]) -> dict | None:
        proxy = (cfg.get("proxy") or "").strip()
        if not proxy:
            # Fall back to the main telegram bridge's proxy
            tg = (self._config_load() or {}).get("telegram") or {}
            proxy = (tg.get("proxy") or "").strip()
        if not proxy:
            return None
        # SOCKS5 needs `socks5h://` so DNS is also routed through the proxy
        if proxy.startswith("socks5://"):
            proxy = proxy.replace("socks5://", "socks5h://", 1)
        return {"http": proxy, "https": proxy}

    def _loop(self):
        log.info("Telegram status-bot started")
        try:
            while not self._stop_event.is_set():
                start = time.time()
                try:
                    self._tick()
                except Exception:
                    log.exception("tg-status tick crashed")
                # Sleep until the next interval, but wake up immediately on stop
                interval = max(15, int(self._read_cfg().get("update_seconds") or 60))
                elapsed = time.time() - start
                wait = max(1.0, interval - elapsed)
                self._stop_event.wait(wait)
        finally:
            log.info("Telegram status-bot loop stopped")

    def _tick(self):
        """One iteration — render the message text and push it to Telegram."""
        self._last_run_ts = time.time()
        cfg = self._read_cfg()
        if not cfg.get("bot_token") or not cfg.get("chat_id"):
            self._last_error = "bot_token / chat_id не заданы"
            return

        text = self._render_message(cfg)
        proxies = self._build_proxies(cfg)

        # If we already have a message_id, edit it. Otherwise send a new
        # message and (try to) pin it, then persist the id.
        msg_id = cfg.get("message_id")
        try:
            if msg_id:
                _api_call(
                    cfg["bot_token"], "editMessageText",
                    {"chat_id": cfg["chat_id"], "message_id": msg_id, "text": text},
                    proxies=proxies,
                )
            else:
                result = _api_call(
                    cfg["bot_token"], "sendMessage",
                    {"chat_id": cfg["chat_id"], "text": text, "disable_notification": True},
                    proxies=proxies,
                )
                new_id = result.get("message_id")
                if new_id:
                    self._config_save({"telegram_status": {"message_id": new_id}})
                    if cfg.get("auto_pin"):
                        try:
                            _api_call(
                                cfg["bot_token"], "pinChatMessage",
                                {
                                    "chat_id": cfg["chat_id"],
                                    "message_id": new_id,
                                    "disable_notification": True,
                                },
                                proxies=proxies,
                            )
                        except Exception as exc:
                            log.warning("Failed to pin message: %s (нужно дать боту право Pin messages?)", exc)
            self._last_success_ts = time.time()
            self._updates_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            # If we hit "message to edit not found" — clear the stored id so
            # the next tick sends a fresh message.
            err = str(exc).lower()
            if "message to edit not found" in err or "message_id_invalid" in err:
                self._config_save({"telegram_status": {"message_id": None}})
            raise

    def _render_message(self, cfg: dict[str, Any]) -> str:
        """Build the multi-line text of the pinned message."""
        now = time.strftime("%H:%M")
        lines: list[str] = []
        lines.append(f"🟢 ONLINE · обновлено {now}")
        lines.append("")

        if cfg.get("show_weather", True):
            try:
                w = self._weather_provider()
            except Exception:
                w = None
            if w:
                t = w.get("temperature_c")
                feels = w.get("feels_like_c")
                hum = w.get("humidity")
                cond = w.get("condition_text")
                emoji = w.get("condition_emoji") or ""
                wind = w.get("wind_speed_ms")
                city = w.get("city") or ""

                if city:
                    lines.append(f"🌤 Погода — {city}")
                if t is not None:
                    feels_txt = f" (ощущается {feels:+.0f})" if feels is not None else ""
                    lines.append(f"🌡 {t:+.1f}°C{feels_txt}")
                if hum is not None:
                    lines.append(f"💧 Влажность {int(hum)}%")
                if cond:
                    lines.append(f"{emoji} {cond}")
                if wind is not None:
                    lines.append(f"💨 Ветер {wind:.1f} м/с")
                lines.append("")

        if cfg.get("show_mesh_stats", True):
            try:
                s = self._stats_provider()
            except Exception:
                s = {}
            if s:
                online = s.get("mesh_nodes_online_2h") or s.get("mesh_nodes_online_1h")
                connected = s.get("mesh_connected")
                conn_icon = "🟢" if connected else "🔴"
                lines.append(f"{conn_icon} онлайн {online or 0}")
                lines.append("")

        extra = (cfg.get("extra_text") or "").strip()
        if extra:
            lines.append(extra)

        # Trim trailing blank lines
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


__all__ = ["TelegramStatusBot", "DEFAULTS"]
