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

import llm
import commands

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
    def __init__(self, config_load: Callable[[], dict], providers: dict[str, Callable],
                 subs_path=None):
        self._cfg = config_load
        self._p = providers                 # stats, nodes, weather, traceroute, airtime, recent_alerts
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._offset = 0
        self._last_error: Optional[str] = None
        self._handled = 0
        # DM subscriptions: {str(chat_id): {"alerts": bool, "daily": bool}}
        self._subs_path = subs_path
        self._subs: dict[str, dict] = self._load_subs()
        self._subs_lock = threading.Lock()
        self._last_alert_ts = int(time.time())   # don't replay history on boot
        self._last_daily = ""                    # YYYY-MM-DD of last daily send

    # ---- subscriptions store ----
    def _load_subs(self) -> dict:
        import json
        try:
            if self._subs_path and self._subs_path.exists():
                return json.loads(self._subs_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to read subscribers")
        return {}

    def _save_subs(self):
        import json
        if not self._subs_path:
            return
        try:
            tmp = self._subs_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._subs, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._subs_path)
        except Exception:
            log.exception("failed to save subscribers")

    def _sub(self, chat_id, key: str, value: bool):
        cid = str(chat_id)
        with self._subs_lock:
            rec = self._subs.setdefault(cid, {"alerts": False, "daily": False})
            rec[key] = value
            if not any(rec.get(k) for k in ("alerts", "daily", "admin")):
                self._subs.pop(cid, None)
            self._save_subs()

    def _subscribers(self, key: str) -> list[str]:
        with self._subs_lock:
            return [cid for cid, r in self._subs.items() if r.get(key)]

    # ---- admin access ----
    def _is_admin(self, chat) -> bool:
        cid = str(chat)
        ids = [str(x) for x in (self._tgs().get("admin_ids") or [])]
        if cid in ids:
            return True
        return bool((self._subs.get(cid) or {}).get("admin"))

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
        threading.Thread(target=self._delivery_loop, daemon=True, name="tg-cmd-delivery").start()
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

    def _send_long(self, chat_id, text: str):
        """Send a long reply split into Telegram-sized chunks (no truncation)."""
        text = (text or "").strip()
        if not text:
            self._send(chat_id, "(пустой ответ)")
            return
        LIMIT = 3900
        while text:
            if len(text) <= LIMIT:
                chunk, text = text, ""
            else:
                cut = text.rfind("\n", 0, LIMIT)
                if cut < LIMIT // 2:
                    cut = text.rfind(" ", 0, LIMIT)
                if cut < LIMIT // 2:
                    cut = LIMIT
                chunk, text = text[:cut], text[cut:].lstrip("\n")
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                       "disable_web_page_preview": True})

    def _send_get_id(self, chat_id, text: str):
        r = self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                                       "disable_web_page_preview": True})
        return (r or {}).get("message_id")

    def _edit(self, chat_id, message_id, text: str):
        if message_id is None:
            self._send(chat_id, text)
            return
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                       "text": text[:4000], "disable_web_page_preview": True})

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
            "/ai": self._cmd_ai, "/ии": self._cmd_ai, "/gpt": self._cmd_ai, "/спроси": self._cmd_ai,
            "/traffic": self._cmd_traffic, "/activity": self._cmd_activity,
            "/subscribe": self._cmd_subscribe, "/unsubscribe": self._cmd_unsubscribe,
            "/daily": self._cmd_daily, "/settings": self._cmd_settings,
            "/admin": self._cmd_admin, "/say": self._cmd_say, "/broadcast": self._cmd_say,
            "/status": self._cmd_status, "/restart": self._cmd_restart,
            "/sendreport": self._cmd_sendreport, "/announce": self._cmd_announce,
        }.get(cmd)
        if not fn:
            return
        # Count command usage for the daily report's "top users" section.
        try:
            frm = msg.get("from") or {}
            user = frm.get("username") or frm.get("first_name") or str(frm.get("id") or "?")
            self._p.get("record_command", lambda *_: None)(user)
        except Exception:
            pass
        try:
            reply = fn(arg, chat)
        except Exception as exc:
            log.exception("cmd %s failed", cmd)
            reply = f"⚠️ Ошибка: {exc}"
        if reply:
            self._send(chat, reply)
            self._handled += 1

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _cmd_help(self, _arg, chat=None):
        base = (
            "📡 Meshtastic-бот\n"
            "Мониторинг твоей mesh-сети.\n\n"
            "🌐 Сеть\n"
            "/mesh — сводка сети\n"
            "/nodes — свежие узлы (SNR, батарея)\n"
            "/air — радиоэфир (LoRa)\n"
            "/traffic — аналитика трафика (24 ч)\n"
            "/activity — активность за 7 дней\n\n"
            "🔍 Узлы\n"
            "/seen <имя> — карточка узла\n"
            "/route <имя> — маршрут до узла\n\n"
            "🌦 Сервисы\n"
            "/weather — погода\n"
            "/ai <вопрос> — спросить ИИ (полный ответ, с поиском в сети)\n\n"
            "🔔 Подписки (в этот чат)\n"
            "/subscribe — алерты (гроза, дождь…)\n"
            "/daily — ежедневная сводка\n"
            "/settings — мои подписки\n"
            "/unsubscribe — отписаться от всего\n\n"
            "💡 Поиск узла — по любым 3+ символам имени."
        )
        if self._is_admin(chat):
            base += (
                "\n\n🔐 Админ\n"
                "/admin — админ-панель\n"
                "/say — отправить в mesh\n"
                "/announce — объявление подписчикам\n"
                "/status — состояние бота\n"
                "/sendreport — разослать отчёт\n"
                "/restart — перезапуск"
            )
        return base

    def _cmd_ai(self, arg, chat=None):
        """Ask the LLM from Telegram — full answer, no mesh length cap.
        Runs in a worker thread so a slow answer doesn't block the poll loop."""
        q = (arg or "").strip()
        if not q:
            return "Использование: /ai <вопрос> — спросить ИИ (ответ без ограничения длины)."
        if not llm.is_enabled(self._cfg()):
            return "ИИ выключен или не настроен (в веб-морде: Интеграции → ИИ-ассистент)."
        self._send(chat, "🤖 Думаю…")
        threading.Thread(target=self._ai_worker, args=(q, chat),
                         daemon=True, name="tg-ai").start()
        return None

    def _ai_worker(self, q: str, chat):
        cfg = self._cfg()
        # Telegram: full-length answer — drop the mesh char cap, more tokens, longer timeout.
        tcfg = dict(cfg)
        tl = dict(tcfg.get("llm") or {})
        tl["max_reply_chars"] = 100000
        tl["max_tokens"] = max(int(tl.get("max_tokens") or 200), 1500)
        tl["timeout_seconds"] = max(int(tl.get("timeout_seconds") or 40), 150)
        tcfg["llm"] = tl
        sys_prompt = ("Ты — полезный ИИ-ассистент в Telegram. Отвечай по-русски, "
                      "развёрнуто и по делу; ограничения на длину ответа нет. "
                      "Без лишней воды и без выдумок.")
        try:
            sit = commands._situation_context(cfg, None)
            if sit:
                sys_prompt += "\n\nАктуальные данные на сейчас (опирайся на них, не выдумывай):\n" + sit
        except Exception:
            pass

        msg_id = self._send_get_id(chat, "🤖 Думаю…")
        # Live "typing": edit the placeholder as the answer streams in (throttled
        # so we don't hit Telegram's edit rate limit).
        state = {"last": 0.0, "shown": ""}

        def on_delta(text):
            t = (text or "")[:4000]
            if not t or t == state["shown"]:
                return
            now = time.time()
            if now - state["last"] < 1.5:   # ≤1 edit / 1.5s — Telegram edit rate limit
                return
            state["last"] = now
            state["shown"] = t
            self._edit(chat, msg_id, t)

        try:
            answer = llm.ask_stream(q, tcfg, on_delta, system_override=sys_prompt, allow_web=True)
        except Exception as exc:
            log.warning("/ai (telegram) failed: %s", exc)
            self._edit(chat, msg_id, f"ИИ недоступен: {exc}")
            return
        answer = (answer or "").strip()
        if len(answer) <= 4000:
            if answer and answer != state["shown"]:
                self._edit(chat, msg_id, answer)
        else:
            cut = answer.rfind("\n", 0, 4000)
            if cut < 2000:
                cut = answer.rfind(" ", 0, 4000)
            if cut < 2000:
                cut = 4000
            self._edit(chat, msg_id, answer[:cut])
            self._send_long(chat, answer[cut:].lstrip("\n"))
        self._handled += 1

    def _nodes(self) -> list[dict]:
        try:
            return self._p["nodes"]() or []
        except Exception:
            return []

    def _cmd_mesh(self, _arg, chat=None):
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

    def _cmd_nodes(self, _arg, chat=None):
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

    def _cmd_seen(self, arg, chat=None):
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

    def _cmd_route(self, arg, chat=None):
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

    def _cmd_weather(self, _arg, chat=None):
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

    def _cmd_air(self, _arg, chat=None):
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

    # portnum → (emoji, label). Anything else is lumped into «Прочее».
    _PORT_LABELS = {
        "ROUTING_APP": ("🛣", "Маршрутизация"),
        "POSITION_APP": ("📍", "Позиция"),
        "TELEMETRY_APP": ("🔋", "Телеметрия"),
        "TEXT_MESSAGE_APP": ("💬", "Сообщения"),
        "NODEINFO_APP": ("🪪", "Инфо узлов"),
        "TRACEROUTE_APP": ("🧭", "Трассировка"),
        "NEIGHBORINFO_APP": ("🔗", "Соседи"),
    }

    def _cmd_traffic(self, _arg, chat=None):
        try:
            t = self._p["traffic"]() or {}
        except Exception as exc:
            return f"Аналитика недоступна: {exc}"
        total = int(t.get("total") or 0)
        if not total:
            return ("📦 Аналитика трафика (24 ч)\n\n"
                    "Пока нет данных — счётчик копится с момента запуска бота. "
                    "Загляни попозже.")
        pct = lambda n: f"{100 * n / total:.0f}%"
        lines = ["📦 Аналитика трафика (24 ч)\n",
                 f"📦 Всего пакетов: {total:,}".replace(",", " "),
                 f"🛰 Активных узлов: {len(t.get('top_nodes') or [])}+\n"]

        top = t.get("top_nodes") or []
        if top:
            medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
            lines.append("🏆 Самые активные узлы")
            for i, n in enumerate(top):
                lines.append(f"{medals[i]} {n['name']} — {n['count']} ({pct(n['count'])})")
            lines.append("")

        by_type = t.get("by_type") or {}
        grouped: dict[str, int] = {}
        for port, cnt in by_type.items():
            emoji, label = self._PORT_LABELS.get(port, ("📦", "Прочее"))
            key = f"{emoji} {label}"
            grouped[key] = grouped.get(key, 0) + cnt
        lines.append("📊 Типы пакетов")
        for key, cnt in sorted(grouped.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"{key}: {cnt} ({pct(cnt)})")
        return "\n".join(lines)

    def _cmd_activity(self, _arg, chat=None):
        try:
            return self._p["activity_report"]()
        except Exception as exc:
            return f"Активность недоступна: {exc}"

    # ------------------------------------------------------------------
    # Subscriptions (Phase 2)
    # ------------------------------------------------------------------

    def _cmd_subscribe(self, _arg, chat=None):
        self._sub(chat, "alerts", True)
        return ("🔔 Подписка на алерты включена.\n"
                "Буду присылать сюда предупреждения: гроза, приближающийся дождь, "
                "сильный ветер, заморозки и т.п.\n\n"
                "Ещё: /daily — ежедневная сводка · /settings — статус · /unsubscribe — отписаться.")

    def _cmd_unsubscribe(self, _arg, chat=None):
        cid = str(chat)
        with self._subs_lock:
            self._subs.pop(cid, None)
            self._save_subs()
        return "🔕 Отписал от всех рассылок."

    def _cmd_daily(self, arg, chat=None):
        want = (arg or "").strip().lower()
        cur = str(chat) in self._subscribers("daily")
        new = True if want in ("on", "вкл", "") and not cur else (want in ("on", "вкл"))
        if want in ("off", "выкл"):
            new = False
        elif not want:
            new = not cur                       # bare /daily toggles
        self._sub(chat, "daily", new)
        tm = (self._tgs().get("daily_time") or "09:00")
        return (f"📅 Ежедневная сводка: {'✅ включена' if new else '❌ выключена'}"
                + (f" (в {tm})" if new else ""))

    def _cmd_settings(self, _arg, chat=None):
        rec = self._subs.get(str(chat)) or {}
        tm = (self._tgs().get("daily_time") or "09:00")
        out = ("⚙️ Твои подписки\n\n"
               f"🔔 Алерты: {'✅ вкл' if rec.get('alerts') else '❌ выкл'}  (/subscribe · /unsubscribe)\n"
               f"📅 Ежедневная сводка: {'✅ вкл' if rec.get('daily') else '❌ выкл'} в {tm}  (/daily)")
        if self._is_admin(chat):
            out += "\n\n🔐 Ты админ — /admin для панели."
        return out

    # ------------------------------------------------------------------
    # Admin panel (owner-only)
    # ------------------------------------------------------------------

    def _admin_only(self, chat) -> Optional[str]:
        return None if self._is_admin(chat) else "⛔ Эта команда только для админа бота."

    def _cmd_admin(self, arg, chat=None):
        secret = (self._tgs().get("admin_secret") or "").strip()
        arg = (arg or "").strip()
        if arg and secret and arg == secret:
            self._sub(chat, "admin", True)
            return "✅ Готово — теперь ты админ бота. Набери /admin для панели."
        if not self._is_admin(chat):
            return ("⛔ Нет доступа.\n"
                    "Если ты владелец — задай «Секрет админа» в настройках бота и пришли "
                    "/admin <секрет>.")
        n_alerts = len(self._subscribers("alerts"))
        n_daily = len(self._subscribers("daily"))
        return (
            "🔐 Админ-панель\n\n"
            "📡 /say <текст> — отправить в mesh (broadcast)\n"
            "📢 /announce <текст> — объявление всем подписчикам\n"
            "📊 /status — состояние бота\n"
            "📤 /sendreport — разослать ежедневный отчёт сейчас\n"
            "♻️ /restart — перезапустить бота\n\n"
            f"👥 Подписчиков: 🔔 {n_alerts} · 📅 {n_daily}"
        )

    def _cmd_say(self, arg, chat=None):
        deny = self._admin_only(chat)
        if deny:
            return deny
        text = (arg or "").strip()
        if not text:
            return "Использование: /say <текст> — уйдёт в mesh (broadcast)."
        if len(text) > 200:
            text = text[:200]
        try:
            self._p["send_mesh"](text)
        except Exception as exc:
            return f"⚠️ Не удалось отправить: {exc}"
        return f"📡 Отправлено в mesh:\n{text}"

    def _cmd_announce(self, arg, chat=None):
        deny = self._admin_only(chat)
        if deny:
            return deny
        text = (arg or "").strip()
        if not text:
            return "Использование: /announce <текст> — уйдёт всем подписчикам в ЛС."
        with self._subs_lock:
            targets = list(self._subs.keys())
        msg = f"📢 {text}"
        for cid in targets:
            self._send(cid, msg)
        return f"📢 Объявление разослано ({len(targets)})."

    def _cmd_status(self, _arg, chat=None):
        deny = self._admin_only(chat)
        if deny:
            return deny
        try:
            s = self._p["bot_status"]() or {}
        except Exception as exc:
            return f"Статус недоступен: {exc}"
        up = s.get("uptime_s") or 0
        up_str = f"{up // 3600} ч {up % 3600 // 60} мин" if up >= 3600 else f"{up // 60} мин"
        proxy = (f"через прокси · {s.get('proxy_ip')}" if s.get("proxy_via") else "напрямую")
        return (
            "📊 Состояние бота\n\n"
            f"🏷 Версия: {s.get('version', '?')}\n"
            f"⏱ Аптайм: {up_str}\n"
            f"📡 Нода: {'🟢 на связи' if s.get('mesh_connected') else '🔴 нет связи'}"
            f" · онлайн {s.get('nodes_online') or 0} / известно {s.get('nodes_known') or 0}\n"
            f"🌐 Сеть: {proxy}\n"
            f"👥 Подписчиков: 🔔 {len(self._subscribers('alerts'))} · 📅 {len(self._subscribers('daily'))}\n"
            f"🤖 Обработано команд: {self._handled}"
        )

    def _cmd_restart(self, _arg, chat=None):
        deny = self._admin_only(chat)
        if deny:
            return deny
        try:
            self._p["restart"]()
        except Exception as exc:
            return f"⚠️ Не удалось перезапустить: {exc}"
        return "♻️ Перезапускаю бота… (вернусь через ~10 сек)"

    def _cmd_sendreport(self, _arg, chat=None):
        deny = self._admin_only(chat)
        if deny:
            return deny
        report = self._daily_report()
        subs = self._subscribers("daily")
        for cid in subs:
            self._send(cid, report)
        # always show the admin a preview
        self._send(chat, "👁 Превью:\n\n" + report)
        return f"📤 Отчёт разослан подписчикам /daily ({len(subs)})."

    # ------------------------------------------------------------------
    # Delivery loop: alerts → subscribers, daily report
    # ------------------------------------------------------------------

    def _daily_report(self) -> str:
        try:
            report = self._p.get("daily_report", lambda: None)()
            if report:
                return report
        except Exception:
            log.exception("daily_report provider failed")
        # Fallback to a minimal digest if the analytics report isn't available.
        return "📊 Ежедневная сводка\n\n" + self._cmd_mesh(None) + "\n\n" + self._cmd_weather(None)

    def _delivery_loop(self):
        time.sleep(40)
        while not self._stop.is_set():
            try:
                if self._enabled():
                    self._deliver_alerts()
                    self._deliver_daily()
            except Exception:
                log.exception("delivery loop error")
            self._stop.wait(45)

    def _deliver_alerts(self):
        subs = self._subscribers("alerts")
        if not subs:
            return
        try:
            fresh = [a for a in (self._p["recent_alerts"]() or [])
                     if int(a.get("ts") or 0) > self._last_alert_ts]
        except Exception:
            fresh = []
        if not fresh:
            return
        self._last_alert_ts = max(int(a.get("ts") or 0) for a in fresh)
        for a in fresh:
            text = "🔔 " + (a.get("text") or "")
            for cid in subs:
                self._send(cid, text)

    def _deliver_daily(self):
        subs = self._subscribers("daily")
        if not subs:
            return
        tm = (self._tgs().get("daily_time") or "09:00").strip()
        today = time.strftime("%Y-%m-%d")
        if self._last_daily == today:
            return
        if time.strftime("%H:%M") != tm:
            return
        self._last_daily = today
        report = self._daily_report()
        for cid in subs:
            self._send(cid, report)


__all__ = ["TelegramCommandBot"]
