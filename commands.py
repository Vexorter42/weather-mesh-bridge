"""Mesh chat command handler.

Commands are messages starting with `!` or `/`. The bot recognises a small
set of useful ones (weather, ping, etc.) and replies into the mesh. Adding
a new command is a one-decorator job.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

import weather

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


@command("ping", help_text="/ping — проверка связи (отвечает pong)")
def cmd_ping(args, msg, bridge, cfg):
    return f"pong · {datetime.now().strftime('%H:%M:%S')}"


@command("время", "time", help_text="/время — текущее время на боте")
def cmd_time(args, msg, bridge, cfg):
    return f"Время на боте: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"


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


@command("nodes", "узлы", help_text="/nodes — список слышимых узлов mesh-сети")
def cmd_nodes(args, msg, bridge, cfg):
    status = bridge.status() if bridge else {}
    if not status.get("connected"):
        return "Бот не подключён к Heltec — список узлов недоступен."
    iface = getattr(bridge, "_iface", None)
    nodes = getattr(iface, "nodes", None) or {}
    if not nodes:
        return "Узлы пока не известны — нода только что подключилась."
    rows: list[tuple[str, Optional[int]]] = []
    for k, n in nodes.items():
        if not isinstance(n, dict):
            continue
        user = n.get("user") or {}
        name = user.get("longName") or user.get("shortName") or str(k)
        last_heard = n.get("lastHeard")
        try:
            last_heard = int(last_heard) if last_heard else None
        except (TypeError, ValueError):
            last_heard = None
        rows.append((name, last_heard))
    rows.sort(key=lambda r: (r[1] or 0), reverse=True)
    rows = rows[:8]  # ограничим, чтоб уместилось в один пакет
    now = int(time.time())
    out_lines = [f"Узлы ({len(rows)} из {len(nodes)}):"]
    for name, last in rows:
        if last:
            age = now - last
            if age < 60: age_str = f"{age}с"
            elif age < 3600: age_str = f"{age // 60}м"
            elif age < 86400: age_str = f"{age // 3600}ч"
            else: age_str = f"{age // 86400}д"
            out_lines.append(f"· {name} (был {age_str} назад)")
        else:
            out_lines.append(f"· {name}")
    return "\n".join(out_lines)


__all__ = ["handle", "command"]
