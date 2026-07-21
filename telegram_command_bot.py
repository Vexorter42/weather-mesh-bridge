"""Interactive Telegram command bot.

Long-polls getUpdates on the SAME bot token as telegram_status_bot (which only
sends/edits a pinned message and never polls, so there's no getUpdates
conflict). Answers mesh/weather commands in DMs or groups:

  /mesh   — network summary (online / idle / offline buckets)
  /nodes  — freshest nodes with SNR / battery
  /seen <name>  — node card
  /route <name> — traceroute path
  /weather — current weather
  /air    — LoRa airtime
  /map    — link to the web UI
  /help /start — command list

Token + proxy are read from config["telegram_status"]; enable with
telegram_status.commands_enabled = true. Phase 2 (DM subscriptions, /daily) and
Phase 3 (/traffic) build on this.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"


def _proxies_from(cfg_load: Callable[[], dict]) -> Optional[dict]:
    full = cfg_load() or {}
    tgs = full.get("telegram_status") or {}
    proxy = (tgs.get("proxy") or "").strip()
    if not proxy:
        proxy = ((full.get("telegram") or {}).get("proxy") or "").strip()
    if not proxy:
        return None
    if proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://", 1)
    return {"http": proxy, "https": proxy}


def _rel(ts: int) -> str:
    if not ts:
        return "—"
    s = max(0, int(time.time()) - int(ts))
    if s < 60:
        return f"{s} с назад"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


class TelegramCommandBot:
    def __init__(self, config_load: Callable[[], dict], providers: dict[str, Callable]):
        self._cfg = config_load
        self._p = providers                 # stats, nodes, weather, traceroute, airtime, web_url
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._offset = 0
        self._last_error: Optional[str] = None
        self._handled = 0

    # ------------------------------------------------------------------

    def _tgs(self) -> dict:
        return (self._cfg() or {}).get("telegram_status") or {}

    def _enabled(self) -> bool:
        c = self._tgs()
        return bool(c.get("commands_enabled") and c.get("bot_token"))

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": self._enabled(),
            "handled": self._handled,
            "last_error": self._last_error,
        }

    def start_worker(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="tg-command-bot")
        t.start()
        self._thread = t
        return t

    # ------------------------------------------------------------------

    def _call(self, method: str, params: dict, timeout: int = 20) -> Optional[dict]:
        token = self._tgs().get("bot_token")
        if not token:
            return None
        try:
            r = requests.get(f"{TG_API}/bot{token}/{method}", params=params,
                             proxies=_proxies_from(self._cfg), timeout=timeout)
            data = r.json()
            if not data.get("ok"):
                self._last_error = data.get("description", "?")
                return None
            return data.get("result")
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def _send(self, chat_id, text: str):
        # Telegram hard-limits a message to 4096 chars.
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                                   "disable_web_page_preview": True})

    def _drain_backlog(self):
        """Skip messages that arrived before the bot came up."""
        res = self._call("getUpdates", {"timeout": 0, "offset": -1}, timeout=10)
        if res:
            self._offset = res[-1]["update_id"] + 1

    def _loop(self):
        time.sleep(15)
        drained = False
        while not self._stop.is_set():
            if not self._enabled():
                drained = False
                self._stop.wait(10)
                continue
            if not drained:
                self._drain_backlog()
                drained = True
                log.info("Telegram command bot polling (offset=%s)", self._offset)
            res = self._call("getUpdates",
                             {"timeout": 25, "offset": self._offset, "allowed_updates": '["message"]'},
                             timeout=32)
            if res is None:
                self._stop.wait(3)
                continue
            for upd in res:
                self._offset = upd["update_id"] + 1
                try:
                    self._handle(upd.get("message") or {})
                except Exception:
                    log.exception("command handler crashed")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _handle(self, msg: dict):
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not text or not text.startswith("/") or chat is None:
            return
        head, _, rest = text.partition(" ")
        cmd = head.split("@", 1)[0].lower()          # strip @botname in groups
        arg = rest.strip()
        fn = {
            "/start": self._cmd_help, "/help": self._cmd_help,
            "/mesh": self._cmd_mesh, "/nodes": self._cmd_nodes,
            "/seen": self._cmd_seen, "/route": self._cmd_route,
            "/weather": self._cmd_weather, "/air": self._cmd_air,
            "/map": self._cmd_map,
        }.get(cmd)
        if not fn:
            return
        try:
            reply = fn(arg)
        except Exception as exc:
            log.exception("cmd %s failed", cmd)
            reply = f"⚠️ Ошибка: {exc}"
        if reply:
            self._send(chat, reply)
            self._handled += 1

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _cmd_help(self, _arg):
        return (
            "📡 Meshtastic-бот\n"
            "Мониторинг твоей mesh-сети.\n\n"
            "🌐 Сеть\n"
            "/mesh — сводка сети\n"
            "/nodes — свежие узлы (SNR, батарея)\n"
            "/air — радиоэфир (LoRa)\n"
            "/map — карта сети\n\n"
            "🔍 Узлы\n"
            "/seen <имя> — карточка узла\n"
            "/route <имя> — маршрут до узла\n\n"
            "🌦 Сервисы\n"
            "/weather — погода\n\n"
            "💡 Поиск узла — по любым 3+ символам имени."
        )

    def _nodes(self) -> list[dict]:
        try:
            return self._p["nodes"]() or []
        except Exception:
            return []

    def _cmd_mesh(self, _arg):
        try:
            st = self._p["stats"]() or {}
        except Exception:
            st = {}
        nodes = self._nodes()
        now = int(time.time())
        online = idle = offline = 0
        for n in nodes:
            age = now - int(n.get("last_heard") or 0) if n.get("last_heard") else 10 ** 9
            if age < 900:
                online += 1
            elif age < 3600:
                idle += 1
            else:
                offline += 1
        total = len(nodes)
        conn = "🟢 на связи" if st.get("mesh_connected") else "🔴 нет связи"
        return (
            "🌐 Mesh-сеть\n\n"
            f"📡 Нода-шлюз: {conn}\n"
            f"📊 Узлов известно: {total}\n\n"
            f"🟢 Online (<15 мин): {online}\n"
            f"🟡 Idle (15–60 мин): {idle}\n"
            f"🔴 Offline (>60 мин): {offline}\n\n"
            f"🕒 {time.strftime('%H:%M')}"
        )

    def _cmd_nodes(self, _arg):
        nodes = sorted(self._nodes(), key=lambda n: n.get("last_heard") or 0, reverse=True)
        if not nodes:
            return "Список узлов пуст — нода никого не слышала."
        lines = ["🛰 Свежие узлы\n"]
        for i, n in enumerate(nodes[:10], 1):
            name = n.get("long_name") or n.get("short_name") or n.get("node_id") or "?"
            snr = f" · SNR {float(n['snr']):.1f}" if n.get("snr") is not None else ""
            batt = f" · 🔋{n['battery_level']}%" if n.get("battery_level") is not None else ""
            lines.append(f"{i}. {name}{snr}{batt} · {_rel(n.get('last_heard'))}")
        return "\n".join(lines)

    def _find_node(self, arg: str) -> Optional[dict]:
        q = arg.strip().lower()
        if len(q) < 3:
            return None
        best = None
        for n in self._nodes():
            hay = f"{n.get('long_name','')} {n.get('short_name','')} {n.get('node_id','')}".lower()
            if q in hay:
                if best is None or (n.get("last_heard") or 0) > (best.get("last_heard") or 0):
                    best = n
        return best

    def _cmd_seen(self, arg):
        if len(arg.strip()) < 3:
            return "Использование: /seen <имя> (3+ символа). Пример: /seen KULT"
        n = self._find_node(arg)
        if not n:
            return f"Узел «{arg}» не найден."
        name = n.get("long_name") or n.get("short_name") or n.get("node_id")
        parts = [f"🔍 {name}", f"🆔 {n.get('node_id','?')}"]
        if n.get("short_name"):
            parts.append(f"🏷 {n['short_name']}")
        if n.get("snr") is not None:
            parts.append(f"📶 SNR {float(n['snr']):.1f} dB")
        if n.get("battery_level") is not None:
            parts.append(f"🔋 {n['battery_level']}%")
        if n.get("voltage") is not None:
            parts.append(f"⚡ {float(n['voltage']):.2f} В")
        if n.get("latitude") is not None and n.get("longitude") is not None:
            parts.append(f"📍 {n['latitude']:.4f}, {n['longitude']:.4f}")
        parts.append(f"🕒 {_rel(n.get('last_heard'))}")
        return "\n".join(parts)

    def _cmd_route(self, arg):
        if len(arg.strip()) < 3:
            return "Использование: /route <имя> (3+ символа)."
        n = self._find_node(arg)
        if not n:
            return f"Узел «{arg}» не найден."
        dest = n.get("node_id")
        name = n.get("long_name") or n.get("short_name") or dest
        try:
            r = self._p["traceroute"](dest) or {}
        except Exception as exc:
            return f"Трассировка не удалась: {exc}"
        if r.get("error"):
            return f"🛣 {name}: {r['error']}"
        fwd = r.get("hops_forward") or r.get("hops") or []
        if not fwd:
            return f"🛣 {name}\n🎯 Прямая видимость (без ретрансляторов)"
        chain = " → ".join(h.get("name") or h.get("node_id") for h in fwd)
        return f"🛣 Маршрут до {name}\n{chain}\n({len(fwd)} hops)"

    def _cmd_weather(self, _arg):
        try:
            w = self._p["weather"]()
        except Exception:
            w = None
        if not w:
            return "Погода недоступна — задай город в «Настройках» бота."
        city = w.get("city") or ""
        lines = [f"🌤 Погода — {city}" if city else "🌤 Погода"]
        if w.get("temperature_c") is not None:
            feels = w.get("feels_like_c")
            ft = f" (ощущается {feels:+.0f})" if feels is not None else ""
            lines.append(f"🌡 {w['temperature_c']:+.1f}°C{ft}")
        if w.get("humidity") is not None:
            lines.append(f"💧 Влажность {int(w['humidity'])}%")
        if w.get("condition_text"):
            lines.append(f"{w.get('condition_emoji','')} {w['condition_text']}")
        if w.get("wind_speed_ms") is not None:
            lines.append(f"💨 Ветер {w['wind_speed_ms']:.1f} м/с")
        return "\n".join(lines)

    def _cmd_air(self, _arg):
        try:
            a = self._p["airtime"]() or {}
        except Exception:
            a = {}
        cu = a.get("channel_utilization")
        tx = a.get("air_util_tx")
        lines = ["📶 Радиоэфир (LoRa)\n"]
        lines.append(f"📡 Занятость канала: {cu:.1f}%" if cu is not None else "📡 Занятость канала: —")
        lines.append(f"📤 Наша передача: {tx:.1f}%" if tx is not None else "📤 Наша передача: —")
        lines.append(f"📦 Пакетов ↑/↓ за час: {a.get('sent_1h', 0)} / {a.get('received_1h', 0)}")
        lines.append(f"📦 За сутки: {a.get('sent_24h', 0)} / {a.get('received_24h', 0)}")
        return "\n".join(lines)

    def _cmd_map(self, _arg):
        url = ""
        try:
            url = self._p["web_url"]() or ""
        except Exception:
            pass
        return f"🗺 Карта сети:\n{url}" if url else "🗺 Карта — во вкладке «Сеть» веб-интерфейса бота."


__all__ = ["TelegramCommandBot"]
