"""Web search for the /ai command — pluggable provider.

Gives the LLM tool-call loop (see llm.py) a way to look things up online for
fresh/factual questions (news, rates, events, anything past the model's training
cutoff). Keyless **DuckDuckGo** works out of the box; self-hosted **SearXNG** and
**Tavily** (API key) are selectable via config — no code change to switch.

config.json → "web_search":
    {
      "enabled": false,
      "provider": "duckduckgo",         # duckduckgo | searxng | tavily
      "max_results": 5,
      "timeout_seconds": 12,
      "proxy": "",                       # optional SOCKS5/HTTP, like llm.proxy
      "region": "ru-ru",
      "searxng_url": "http://host:8080", # required for provider=searxng
      "tavily_api_key": ""               # required for provider=tavily
    }

DuckDuckGo needs the small `ddgs` package (pip install ddgs). SearXNG/Tavily use
plain `requests`, no extra dependency.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "provider": "duckduckgo",
    "max_results": 5,
    "timeout_seconds": 12,
    "proxy": "",
    "region": "ru-ru",
    "searxng_url": "",
    "tavily_api_key": "",
}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg.get("web_search") or {})
    return out


def _proxies(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    url = proxy_url.strip()
    if url.startswith("socks5://"):
        url = url.replace("socks5://", "socks5h://", 1)
    return {"http": url, "https": url}


def is_enabled(cfg: dict[str, Any]) -> bool:
    c = _cfg(cfg)
    if not c.get("enabled"):
        return False
    p = (c.get("provider") or "duckduckgo").lower()
    if p == "searxng":
        return bool(c.get("searxng_url"))
    if p == "tavily":
        return bool(c.get("tavily_api_key"))
    return True  # duckduckgo is keyless


def provider_name(cfg: dict[str, Any]) -> str:
    return (_cfg(cfg).get("provider") or "duckduckgo").lower()


def search(query: str, cfg: dict[str, Any],
           max_results: Optional[int] = None) -> list[dict[str, str]]:
    """Return [{title, url, snippet}, ...]. Raises on hard failure."""
    c = _cfg(cfg)
    query = (query or "").strip()
    if not query:
        return []
    n = int(max_results or c.get("max_results") or 5)
    p = (c.get("provider") or "duckduckgo").lower()
    if p == "searxng":
        return _searxng(query, c, n)
    if p == "tavily":
        return _tavily(query, c, n)
    return _duckduckgo(query, c, n)


def _duckduckgo(query: str, c: dict[str, Any], n: int) -> list[dict[str, str]]:
    try:
        try:
            from ddgs import DDGS  # new package name
        except ImportError:
            from duckduckgo_search import DDGS  # legacy name
    except ImportError as exc:
        raise RuntimeError(
            "web_search: не установлен пакет для DuckDuckGo (pip install ddgs)"
        ) from exc
    out: list[dict[str, str]] = []
    with DDGS(proxy=(c.get("proxy") or None),
              timeout=int(c.get("timeout_seconds") or 12)) as ddgs:
        for r in ddgs.text(query, region=(c.get("region") or "ru-ru"),
                           max_results=n):
            out.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })
    return out


def _searxng(query: str, c: dict[str, Any], n: int) -> list[dict[str, str]]:
    base = (c.get("searxng_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("web_search: не задан searxng_url")
    lang = (c.get("region") or "ru").split("-")[0]
    r = requests.get(
        base + "/search",
        params={"q": query, "format": "json", "language": lang},
        headers={"User-Agent": "weather-mesh-bridge"},
        proxies=_proxies(c.get("proxy") or ""),
        timeout=int(c.get("timeout_seconds") or 12),
    )
    r.raise_for_status()
    out: list[dict[str, str]] = []
    for it in (r.json().get("results") or [])[:n]:
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": it.get("content", ""),
        })
    return out


def _tavily(query: str, c: dict[str, Any], n: int) -> list[dict[str, str]]:
    r = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": c.get("tavily_api_key"), "query": query,
              "max_results": n, "search_depth": "basic"},
        proxies=_proxies(c.get("proxy") or ""),
        timeout=int(c.get("timeout_seconds") or 12),
    )
    r.raise_for_status()
    out: list[dict[str, str]] = []
    for it in (r.json().get("results") or [])[:n]:
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": it.get("content", ""),
        })
    return out


def _html_to_text(html_text: str) -> str:
    """Very light HTML → readable text (no bs4 dependency)."""
    html_text = re.sub(r"(?is)<(script|style|head|nav|footer|svg|noscript)[^>]*>.*?</\1>", " ", html_text)
    html_text = re.sub(r"(?is)<!--.*?-->", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", html_text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_url(url: str, cfg: dict[str, Any], max_chars: int = 2500) -> str:
    """Fetch a page and return its readable text (for the web_fetch tool)."""
    c = _cfg(cfg)
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "web_fetch: некорректный URL."
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (weather-mesh-bridge)"},
        proxies=_proxies(c.get("proxy") or ""),
        timeout=int(c.get("timeout_seconds") or 12),
    )
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if not any(t in ctype for t in ("html", "text", "json", "xml")):
        return f"web_fetch: не текстовая страница ({ctype or '?'})."
    return _html_to_text(r.text)[:max_chars]


def format_results(results: list[dict[str, str]], max_chars: int = 1500) -> str:
    """Compact text block fed back to the model as the tool result."""
    if not results:
        return "Поиск ничего не вернул."
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        sn = " ".join((r.get("snippet") or "").split())
        if len(sn) > 300:
            sn = sn[:300].rstrip() + "…"
        title = " ".join((r.get("title") or "").split())
        lines.append(f"[{i}] {title}\n{sn}\n{r.get('url', '')}")
    return "\n\n".join(lines)[:max_chars]


__all__ = ["search", "fetch_url", "format_results", "is_enabled", "provider_name", "DEFAULTS"]
