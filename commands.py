"""Mesh chat command handler.

Commands are messages starting with `!` or `/`. The bot recognises a small
set of useful ones (weather, ping, etc.) and replies into the mesh. Adding
a new command is a one-decorator job.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any, Callable, Optional

import weather
import llm

# Set when app.py starts up — used by /uptime.
BOT_START_TS: float = time.time()

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Callable[..., Optional[str]]] = {}
_HELP_LINES: list[str] = []


def command(*aliases: str, help_text: str = ""):
    """Register a function as a chat command under one or more aliases."""
    def decorator(fn: Callable[..., Optional[str]]):
        for a in aliases:
            _REGISTRY[a.lower()] = fn
        if help_text:
            _HELP_LINES.append(help_text)
        return fn
    return decorator


# Map Latin letters that visually look like Cyrillic to their Cyrillic equivalents.
# Helps people who accidentally typed "!cегодня" with a Latin "c" — common when
# switching keyboards on phones.
_LATIN_TO_CYRILLIC = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к",
    "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})


def _normalize_cmd(cmd: str) -> str:
    """Try to recover from common keyboard-layout typos.

    If the token contains any Cyrillic char, replace Latin lookalikes with their
    Cyrillic counterparts. If it's pure Latin, leave it alone (English commands
    like 'ping', 'help' keep working).
    """
    has_cyrillic = any("Ѐ" <= c <= "ӿ" for c in cmd)
    if has_cyrillic:
        return cmd.translate(_LATIN_TO_CYRILLIC)
    return cmd


def _parse(text: str) -> tuple[str, list[str]]:
    text = text.strip().lstrip("!/").strip()
    if not text:
        return "", []
    parts = text.split()
    return parts[0].lower(), parts[1:]


def handle(msg: dict[str, Any], *, bridge: Any, cfg: dict[str, Any]) -> Optional[str]:
    """Top-level dispatcher: figure out which command was called and run it.

    Returns the text the bot should send back, or None if no command was
    recognised. Errors are caught and returned as a human-readable string.
    """
    text = (msg.get("text") or "").strip()
    if not (text.startswith("!") or text.startswith("/")):
        return None
    cmd, args = _parse(text)
    if not cmd:
        return None
    handler = _REGISTRY.get(cmd)
    if not handler:
        # Try once more after substituting Latin lookalikes — helps when a phone
        # keyboard slipped to Latin and typed e.g. "!cегодня" (Latin "c").
        fixed = _normalize_cmd(cmd)
        if fixed != cmd:
            handler = _REGISTRY.get(fixed)
            if handler:
                log.info("Command typo corrected: %r -> %r", cmd, fixed)
                cmd = fixed
    if not handler:
        log.warning(
            "Unknown command: %r (codepoints %s)",
            cmd, [hex(ord(c)) for c in cmd],
        )
        return f"Неизвестная команда «/{cmd}». Попробуй /help"
    try:
        return handler(args=args, msg=msg, bridge=bridge, cfg=cfg)
    except Exception as exc:
        log.exception("Command /%s crashed", cmd)
        return f"Ошибка при выполнении /{cmd}: {exc}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@command("help", "помощь", "?", "h", help_text="/help — список команд")
def cmd_help(args, msg, bridge, cfg):
    lines = ["Команды бота:"] + _HELP_LINES
    return "\n".join(lines)


def _hops_phrase(n: Optional[int]) -> str:
    """Human-readable Russian plural for hop count. Mirrors the UI strip."""
    if n is None:
        return ""
    if n == 0:
        return "↯ напрямую"
    last = n % 10
    last2 = n % 100
    if last == 1 and last2 != 11:
        word = "прыжок"
    elif last in (2, 3, 4) and not (12 <= last2 <= 14):
        word = "прыжка"
    else:
        word = "прыжков"
    return f"↯ {n} {word}"


@command("ping", help_text="/ping — проверка связи (отвечает pong + кол-во hops)")
def cmd_ping(args, msg, bridge, cfg):
    time_str = datetime.now().strftime("%H:%M")
    parts = [f"pong · {time_str}"]
    hp = _hops_phrase(msg.get("hops_taken"))
    if hp:
        parts.append(hp)
    # Note if the request reached us via an MQTT gateway (internet) rather
    # than purely over LoRa RF.
    if msg.get("via_mqtt"):
        parts.append("🌐 MQTT")
    return " · ".join(parts)


@command("время", "time", help_text="/время — текущее время на боте")
def cmd_time(args, msg, bridge, cfg):
    return f"Время на боте: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"


def _format_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}с"
    if seconds < 3600:
        return f"{seconds // 60}м {seconds % 60}с"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}ч {m}м"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}д {h}ч"


@command("uptime", "аптайм", help_text="/uptime — сколько работает бот + статистика")
def cmd_uptime(args, msg, bridge, cfg):
    uptime = int(time.time() - BOT_START_TS)
    # Pull aggregate stats from chat_db if bridge has a reference to it
    db = getattr(bridge, "_db", None)
    sent_24h = recv_24h = total = 0
    if db is not None:
        try:
            stats = db.stats()
            sent_24h = stats.get("sent_24h", 0)
            recv_24h = stats.get("received_24h", 0)
            total = stats.get("total_messages", 0)
        except Exception:
            log.exception("/uptime: chat_db.stats() failed")
    return (
        f"⏱ Работаю {_format_uptime(uptime)}\n"
        f"за 24ч: ↑{sent_24h} ↓{recv_24h}\n"
        f"всего в БД: {total}"
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (azimuth) from point 1 to point 2, in degrees 0-360."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    y = math.sin(dλ) * math.cos(φ2)
    x = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _compass_8(deg: float) -> str:
    """0..360 → С/СВ/В/ЮВ/Ю/ЮЗ/З/СЗ."""
    dirs = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    return dirs[int((deg + 22.5) // 45) % 8]


def _find_node(bridge, query: str) -> Optional[dict]:
    """Match a node by node_id (!hex), short_name or part of long_name."""
    if not query or not bridge:
        return None
    q = query.strip().lower().lstrip("!")
    try:
        nodes = bridge.get_known_nodes()
    except Exception:
        return None
    # 1) Exact node_id match (handles "!a1b2c3d4")
    for n in nodes:
        nid = (n.get("node_id") or "").lstrip("!").lower()
        if nid == q:
            return n
    # 2) Exact short_name match
    for n in nodes:
        if (n.get("short_name") or "").lower() == q:
            return n
    # 3) Substring match on long_name
    for n in nodes:
        if q in (n.get("long_name") or "").lower():
            return n
    return None


@command("где", "where", help_text="/где <узел> — расстояние и азимут от бота до узла")
def cmd_where(args, msg, bridge, cfg):
    if not args:
        return "Использование: /где <имя_узла или !id>"
    query = " ".join(args)
    target = _find_node(bridge, query)
    if not target:
        return f"Узел «{query}» не найден среди слышимых нод. Попробуй полный ID или short_name."

    tlat, tlon = target.get("latitude"), target.get("longitude")
    if tlat is None or tlon is None:
        return f"У узла «{target.get('long_name') or query}» нет координат — он ещё не слал POSITION_APP."

    # Find OUR position too — use bot's own node entry
    status = bridge.status() if bridge else {}
    my_num = status.get("my_node_num")
    my_lat = my_lon = None
    if my_num is not None:
        for n in bridge.get_known_nodes():
            if n.get("num") == my_num:
                my_lat, my_lon = n.get("latitude"), n.get("longitude")
                break
    # Fallback: configured city location
    if my_lat is None or my_lon is None:
        loc = (cfg or {}).get("location") or {}
        my_lat, my_lon = loc.get("latitude"), loc.get("longitude")
    if my_lat is None or my_lon is None:
        return "У бота нет координат — не могу посчитать расстояние."

    dist_km = _haversine_km(my_lat, my_lon, float(tlat), float(tlon))
    az = _bearing_deg(my_lat, my_lon, float(tlat), float(tlon))
    name = target.get("long_name") or target.get("short_name") or target.get("node_id") or "?"
    dist_str = f"{dist_km*1000:.0f} м" if dist_km < 1 else f"{dist_km:.1f} км"
    return f"до {name}: {dist_str} · {az:.0f}° ({_compass_8(az)})"


def _resolve_location(args, cfg):
    """If args contains a city name, look it up; otherwise use configured one.

    Returns (lat, lon, name, tz) or raises a friendly RuntimeError.
    """
    if args:
        query = " ".join(args)
        candidates = weather.search_city(query, count=1)
        if not candidates:
            raise RuntimeError(f"Не нашёл город «{query}»")
        c = candidates[0]
        return c["latitude"], c["longitude"], c["name"], c.get("timezone") or "auto"

    loc = cfg.get("location") or {}
    if loc.get("latitude") is None:
        raise RuntimeError("Город не настроен. Укажи город в команде: /погода Москва")
    return loc["latitude"], loc["longitude"], loc.get("name", ""), loc.get("timezone") or "auto"


@command(
    "погода", "weather", "w",
    help_text="/погода [город] — текущая погода",
)
def cmd_weather(args, msg, bridge, cfg):
    lat, lon, name, tz = _resolve_location(args, cfg)
    data = weather.fetch_weather(lat, lon, tz)
    use_emojis = bool((cfg.get("message") or {}).get("use_emojis"))
    return weather.format_message(
        data,
        fields=["temp", "feels", "humidity", "wind"],
        location_name=name,
        include_header=True,
        use_emojis=use_emojis,
    )


# Full report = everything ALL_FIELDS knows, minus the "tomorrow" block.
_FULL_FIELDS = [
    "temp", "feels", "vs_yesterday", "water_temp",
    "humidity", "pressure", "wind", "precipitation",
    "air_quality", "uv_index", "forecast",
]


@command(
    "сводка", "полная", "all", "full",
    help_text="/сводка [город] — полная сводка (всё, кроме завтра)",
)
def cmd_full(args, msg, bridge, cfg):
    lat, lon, name, tz = _resolve_location(args, cfg)
    data = weather.fetch_weather(lat, lon, tz)
    # The extended fields need data from separate endpoints — fetch them
    # best-effort and inject into the same dict format_message expects.
    try:
        data["_air_quality"] = weather.fetch_air_quality(lat, lon, tz)
    except Exception:
        log.exception("/сводка: air quality fetch failed")
    try:
        data["_water_temp"] = weather.fetch_water_temperature(lat, lon, tz)
    except Exception:
        log.exception("/сводка: water temp fetch failed")
    try:
        data["_yesterday"] = weather.fetch_yesterday(lat, lon, tz)
    except Exception:
        log.exception("/сводка: yesterday fetch failed")
    use_emojis = bool((cfg.get("message") or {}).get("use_emojis"))
    return weather.format_message(
        data,
        fields=_FULL_FIELDS,
        location_name=name,
        include_header=True,
        use_emojis=use_emojis,
    )


# Short per-node conversational memory for /ai (so follow-up questions keep
# context). Kept in RAM only; entries expire after TTL.
_AI_MEMORY: dict[str, list[dict[str, Any]]] = {}
_AI_MEMORY_TTL = 1800      # 30 min
_AI_MEMORY_TURNS = 4       # keep the last N exchanges (2 messages each)


def _ai_history(node_id: Optional[str]) -> list[dict[str, str]]:
    if not node_id:
        return []
    now = time.time()
    items = [m for m in _AI_MEMORY.get(node_id, []) if now - m["ts"] < _AI_MEMORY_TTL]
    _AI_MEMORY[node_id] = items
    return [{"role": m["role"], "content": m["content"]} for m in items]


def _ai_remember(node_id: Optional[str], role: str, content: str) -> None:
    if not node_id:
        return
    lst = _AI_MEMORY.setdefault(node_id, [])
    lst.append({"role": role, "content": content, "ts": time.time()})
    keep = _AI_MEMORY_TURNS * 2
    if len(lst) > keep:
        del lst[: len(lst) - keep]


_SIT_DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _situation_context(cfg, bridge) -> str:
    """Compact snapshot of the real 'now' — date/time, weather, mesh state — so
    /ai answers about the current situation instead of stale training memory."""
    parts: list[str] = []
    loc = cfg.get("location") or {}
    city = (loc.get("name") or "").strip()
    now = datetime.now()
    line = f"Дата и время: {now.strftime('%d.%m.%Y %H:%M')} ({_SIT_DOW[now.weekday()]})"
    if city:
        line += f", {city}"
    parts.append(line + ".")
    # Current weather — cached in weather.py, so usually instant; best-effort.
    try:
        if loc.get("latitude") is not None:
            data = weather.fetch_weather(loc["latitude"], loc["longitude"],
                                         loc.get("timezone") or "auto")
            w = weather.format_message(
                data, fields=["temp", "feels", "humidity", "wind", "precipitation", "forecast"],
                location_name=city, include_header=False, use_emojis=False)
            w = " ".join((w or "").split())
            if w:
                parts.append("Погода сейчас: " + w)
    except Exception:
        log.debug("/ai situation: weather fetch failed", exc_info=True)
    # Mesh state — cheap, from the bridge.
    try:
        st = bridge.status() if bridge else {}
        online = st.get("nodes_online_2h")
        if online is not None:
            parts.append(f"Mesh-сеть: онлайн {online} узлов (за 2ч), "
                         f"шлюз {'на связи' if st.get('connected') else 'офлайн'}.")
    except Exception:
        pass
    return "\n".join(parts)


@command(
    "ai", "ии", "gpt", "спроси", "ask",
    help_text="/ai <вопрос> — спросить ИИ (ответ кратко, в эфир)",
)
def cmd_ai(args, msg, bridge, cfg):
    if not args:
        return "Использование: /ai <вопрос>. Например: /ai что взять в поход осенью?"
    if not llm.is_enabled(cfg):
        return "ИИ выключен или не настроен. Включи и впиши ключ в «Настройки → ИИ-ассистент»."
    question = " ".join(args).strip()
    if len(question) > 600:
        question = question[:600]
    node_id = msg.get("from_id")
    use_ctx = bool((cfg.get("llm") or {}).get("context_memory", True))
    history = _ai_history(node_id) if use_ctx else None
    # Ground the answer in the real current situation (time/weather/mesh) instead
    # of the model's frozen training memory.
    base = (cfg.get("llm") or {}).get("system_prompt") or llm.DEFAULTS["system_prompt"]
    sit = _situation_context(cfg, bridge)
    sys_prompt = base
    if sit:
        sys_prompt += ("\n\nАктуальные данные на сейчас (опирайся на них и не выдумывай; "
                       "если вопрос не про них — отвечай как обычно):\n" + sit)
    try:
        answer = llm.ask(question, cfg, system_override=sys_prompt, history=history)
    except Exception as exc:
        log.warning("/ai failed: %s", exc)
        return f"ИИ недоступен: {exc}"
    if use_ctx:
        _ai_remember(node_id, "user", question)
        _ai_remember(node_id, "assistant", answer)
    return answer


@command(
    "погодаии", "совет", "одеть", "wai",
    help_text="/совет <вопрос> — умный совет по погоде от ИИ (учитывает прогноз)",
)
def cmd_smart_weather(args, msg, bridge, cfg):
    """Answer a free-form question with the real forecast injected as context,
    e.g. «/совет что надеть завтра?» → LLM replies using today+tomorrow data."""
    if not llm.is_enabled(cfg):
        return "ИИ выключен или не настроен. Включи в «Настройки → ИИ-ассистент»."
    question = " ".join(args).strip() or "Что надеть и взять с собой сегодня?"

    # Build a compact weather context block to feed the model.
    try:
        loc = cfg.get("location") or {}
        if loc.get("latitude") is None:
            return "Город не настроен — укажи его в настройках."
        data = weather.fetch_weather(loc["latitude"], loc["longitude"], loc.get("timezone") or "auto")
        ctx = weather.format_message(
            data,
            fields=["temp", "feels", "humidity", "wind", "precipitation",
                    "forecast", "tomorrow_morning_evening"],
            location_name=loc.get("name", ""),
            include_header=True,
            use_emojis=False,
        )
    except Exception as exc:
        log.warning("/совет: weather fetch failed: %s", exc)
        return f"Не смог получить прогноз: {exc}"

    sys_prompt = (
        "Ты — помощник по погоде в LoRa-рации. Тебе дают актуальный прогноз и "
        "вопрос пользователя. Ответь по-русски кратко и практично: 1-3 коротких "
        "предложения, конкретные советы (одежда, обувь, зонт и т.п.), без воды, "
        "без markdown, без эмодзи."
    )
    prompt = f"Прогноз:\n{ctx}\n\nВопрос: {question}"
    try:
        return llm.ask(prompt, cfg, system_override=sys_prompt)
    except Exception as exc:
        log.warning("/совет failed: %s", exc)
        return f"ИИ недоступен: {exc}"


@command(
    "завтра", "tomorrow",
    help_text="/завтра [город] — прогноз на завтра",
)
def cmd_tomorrow(args, msg, bridge, cfg):
    lat, lon, name, tz = _resolve_location(args, cfg)
    data = weather.fetch_weather(lat, lon, tz)
    use_emojis = bool((cfg.get("message") or {}).get("use_emojis"))
    return weather.format_message(
        data,
        fields=["tomorrow_morning_evening"],
        location_name=name,
        include_header=True,
        use_emojis=use_emojis,
    )


@command(
    "сегодня", "today",
    help_text="/сегодня [город] — прогноз на сегодня (мин/макс/осадки)",
)
def cmd_today(args, msg, bridge, cfg):
    lat, lon, name, tz = _resolve_location(args, cfg)
    data = weather.fetch_weather(lat, lon, tz)
    use_emojis = bool((cfg.get("message") or {}).get("use_emojis"))
    return weather.format_message(
        data,
        fields=["temp", "forecast"],
        location_name=name,
        include_header=True,
        use_emojis=use_emojis,
    )


# Окно «онлайн» — то же, что в дашборде (см. meshbridge.MeshBridge.status).
NODES_ONLINE_WINDOW_SEC = 7200   # 2 часа


@command("nodes", "узлы", help_text="/nodes — узлы онлайн (за последние 2ч)")
def cmd_nodes(args, msg, bridge, cfg):
    status = bridge.status() if bridge else {}
    if not status.get("connected"):
        return "Бот не подключён к Heltec — список узлов недоступен."
    iface = getattr(bridge, "_iface", None)
    nodes = getattr(iface, "nodes", None) or {}
    if not nodes:
        return "Узлы пока не известны — нода только что подключилась."

    my_num = status.get("my_node_num")
    now = int(time.time())

    online: list[tuple[str, int]] = []  # (name, last_heard_ts)
    total_known = 0
    for k, n in nodes.items():
        if not isinstance(n, dict):
            continue
        total_known += 1
        # Сам бот в списке не нужен — он по определению «онлайн».
        if my_num is not None and n.get("num") == my_num:
            continue
        try:
            last_heard = int(n.get("lastHeard") or 0)
        except (TypeError, ValueError):
            last_heard = 0
        if not last_heard or (now - last_heard) > NODES_ONLINE_WINDOW_SEC:
            continue
        user = n.get("user") or {}
        name = user.get("longName") or user.get("shortName") or str(k)
        online.append((name, last_heard))

    if not online:
        return (
            f"Сейчас никто не онлайн. "
            f"Известно {total_known} узлов, но никого не слышали последние 2ч."
        )

    online.sort(key=lambda r: r[1], reverse=True)
    # LoRa пакет ~228 байт — ограничим выдачу, остальное упомянем хвостом.
    shown = online[:8]

    out_lines = [f"Онлайн: {len(online)} (из {total_known} известных)"]
    for name, last in shown:
        age = now - last
        if   age < 60:   age_str = f"{age}с"
        elif age < 3600: age_str = f"{age // 60}м"
        else:            age_str = f"{age // 3600}ч"
        out_lines.append(f"· {name} ({age_str} назад)")

    if len(online) > len(shown):
        out_lines.append(f"+ ещё {len(online) - len(shown)}")

    return "\n".join(out_lines)


__all__ = ["handle", "command"]
