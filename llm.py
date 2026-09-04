"""Tiny OpenAI-compatible LLM client.

Works with any chat-completions endpoint that speaks the OpenAI format —
NVIDIA build (https://integrate.api.nvidia.com/v1), OpenAI, OpenRouter,
local Ollama/LM Studio, etc. No SDK dependency: just `requests`.

Used by the `/ai` chat command so people on the (internet-less) LoRa mesh can
ask an LLM a question and get a short answer relayed back over radio.

Config lives in config.json under "llm" (the api_key stays out of git):
    {
      "enabled": false,
      "base_url": "https://integrate.api.nvidia.com/v1",
      "api_key": "nvapi-...",
      "model": "moonshotai/kimi-k2-instruct",
      "system_prompt": "...",
      "max_tokens": 200,
      "temperature": 0.6,
      "proxy": "",              # optional SOCKS5/HTTP proxy (e.g. VLESS local)
      "max_reply_chars": 600    # hard cap on the answer before mesh chunking
    }
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# Web-search tool exposed to the model when the caller passes allow_web=True and
# web_search is enabled in config. The model decides whether to call it.
MAX_TOOL_ITERS = 2
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Поиск в интернете для свежей или фактической информации: новости, "
            "события, курсы/цены, факты после обучения модели. Возвращает "
            "заголовки, сниппеты и ссылки."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос на языке вопроса",
                },
            },
            "required": ["query"],
        },
    },
}
WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Открыть веб-страницу по URL и получить её текст. Вызывай после "
            "web_search, когда в сниппетах нет точного факта/числа и его надо "
            "достать прямо со страницы."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL страницы (обычно из результатов web_search)",
                },
            },
            "required": ["url"],
        },
    },
}
_WEB_HINT = (
    "\n\nУ тебя есть инструменты web_search (поиск) и web_fetch (открыть страницу). "
    "Если для точного ответа нужны свежие данные из интернета (новости, события, "
    "цены, факты после обучения) — ищи через web_search, а если в сниппетах нет "
    "конкретного факта — открой нужную ссылку через web_fetch. Опирайся на "
    "результаты, не выдумывай. Финальный ответ всё равно держи коротким."
)

DEFAULTS = {
    "enabled": False,
    # OpenRouter is a convenient OpenAI-compatible aggregator. Swap base_url +
    # model for NVIDIA build / OpenAI / local Ollama as you like.
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    # Prefer a NON-reasoning model so it returns the answer directly within a
    # small token budget (reasoning models like kimi-k2.6 spend the budget
    # "thinking" and need max_tokens >= ~1500).
    "model": "moonshotai/kimi-k2",
    # Tried in order if the primary model errors/times out/returns empty. Either
    # a list or a comma-separated string of model ids on the same base_url/key.
    "fallback_models": [],
    "system_prompt": (
        "Ты — ассистент в автономной LoRa mesh-сети. Отвечай по-русски, "
        "максимально кратко и по делу: 1–3 коротких предложения, без markdown, "
        "без списков, без эмодзи. Твой ответ передаётся по радио с жёстким "
        "лимитом длины, поэтому будь лаконичен."
    ),
    "max_tokens": 200,
    "temperature": 0.6,
    "proxy": "",
    "max_reply_chars": 600,
    "timeout_seconds": 40,
    # Remember the last few /ai turns per node for follow-up questions.
    "context_memory": True,
}


def _proxies(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    url = proxy_url.strip()
    if url.startswith("socks5://"):
        # socks5h:// routes DNS through the proxy too
        url = url.replace("socks5://", "socks5h://", 1)
    return {"http": url, "https": url}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg.get("llm") or {})
    return out


def is_enabled(cfg: dict[str, Any]) -> bool:
    c = _cfg(cfg)
    return bool(c.get("enabled") and c.get("api_key") and c.get("model"))


def _model_candidates(c: dict[str, Any]) -> list[str]:
    """Primary model first, then fallbacks (list or comma-string), deduped."""
    models = [c.get("model")]
    fb = c.get("fallback_models") or []
    if isinstance(fb, str):
        fb = fb.split(",")
    models.extend(fb)
    out: list[str] = []
    seen: set[str] = set()
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _post_chat(c: dict[str, Any], model: str, messages: list[dict[str, Any]],
               tools: Optional[list] = None) -> dict[str, Any]:
    """One chat-completion call against `model`; returns the raw assistant
    `message` (which may carry `tool_calls`). Raises RuntimeError on failure."""
    url = f"{(c.get('base_url') or '').rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(c.get("max_tokens") or 200),
        "temperature": float(c.get("temperature") or 0.6),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(
            url, json=payload, headers=headers,
            proxies=_proxies(c.get("proxy") or ""),
            timeout=int(c.get("timeout_seconds") or 40),
        )
    except Exception as exc:
        raise RuntimeError(f"Ошибка сети при запросе к LLM: {exc}") from exc

    if r.status_code == 401:
        raise RuntimeError("LLM: неверный api_key (401)")
    if r.status_code == 404:
        raise RuntimeError(f"LLM: модель/endpoint не найдены (404) для «{model}»")
    if r.status_code == 429:
        raise RuntimeError("LLM: превышен лимит запросов (429)")
    if not r.ok:
        snippet = (r.text or "")[:200]
        raise RuntimeError(f"LLM вернул {r.status_code}: {snippet}")

    try:
        data = r.json()
        return data["choices"][0]["message"]
    except Exception as exc:
        raise RuntimeError(f"LLM: не смог разобрать ответ: {exc}") from exc


def _cap(c: dict[str, Any], text: str) -> str:
    cap = int(c.get("max_reply_chars") or 600)
    if len(text) > cap:
        text = text[:cap - 1].rstrip() + "…"
    return text


def _message_text(c: dict[str, Any], message: dict[str, Any]) -> str:
    """Extract non-empty capped text from an assistant message, else raise."""
    text = (message.get("content") or "").strip()
    if not text:
        # Reasoning/"thinking" models can burn the whole token budget on internal
        # reasoning and return empty content.
        if message.get("reasoning") or message.get("reasoning_content"):
            raise RuntimeError(
                "Модель потратила лимит на «размышления» и не выдала ответ. "
                "Подними max_tokens (≥1500) или выбери модель без reasoning."
            )
        raise RuntimeError("LLM вернул пустой ответ")
    return _cap(c, text)


def _complete(c: dict[str, Any], model: str, messages: list[dict[str, Any]]) -> str:
    """One plain chat-completion returning the answer text (no tools)."""
    return _message_text(c, _post_chat(c, model, messages))


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def _run_tool_loop(c: dict[str, Any], model: str,
                   messages: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    """Chat loop with the web_search tool: model may search up to MAX_TOOL_ITERS
    times; the last round is forced tool-free to produce a final answer."""
    import web_search

    msgs: list[dict[str, Any]] = list(messages)
    for step in range(MAX_TOOL_ITERS + 1):
        tools = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL] if step < MAX_TOOL_ITERS else None
        message = _post_chat(c, model, msgs, tools=tools)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return _message_text(c, message)
        for i, tc in enumerate(tool_calls):
            tc.setdefault("id", f"call_{step}_{i}")
            tc.setdefault("type", "function")
        msgs.append({"role": "assistant",
                     "content": message.get("content") or "",
                     "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            if name == "web_search":
                query = (_parse_tool_args(fn.get("arguments")).get("query") or "").strip()
                try:
                    results = web_search.search(query, cfg)
                    content = web_search.format_results(results)
                    log.info("/ai web_search «%s» → %d результатов", query, len(results))
                except Exception as exc:
                    content = f"Ошибка поиска: {exc}"
                    log.warning("/ai web_search «%s» failed: %s", query, exc)
            elif name == "web_fetch":
                url = (_parse_tool_args(fn.get("arguments")).get("url") or "").strip()
                try:
                    content = web_search.fetch_url(url, cfg)
                    log.info("/ai web_fetch «%s» → %d симв.", url, len(content))
                except Exception as exc:
                    content = f"Ошибка загрузки страницы: {exc}"
                    log.warning("/ai web_fetch «%s» failed: %s", url, exc)
            else:
                content = f"Неизвестный инструмент: {name}"
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": content})
    # Tool budget exhausted without a plain answer — one final tool-free call.
    return _complete(c, model, msgs)


def ask(question: str, cfg: dict[str, Any], system_override: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
        allow_web: bool = False) -> str:
    """Ask the LLM and return the answer text. Tries the primary model, then any
    `fallback_models` on failure. `history` is an optional list of prior
    {role, content} turns (for short conversational memory).

    When `allow_web` is True and web_search is enabled in config, the model is
    offered a `web_search` tool it may call for fresh/factual questions.

    Raises RuntimeError with a human-readable message if every model fails.
    """
    c = _cfg(cfg)
    if not c.get("api_key"):
        raise RuntimeError("LLM не настроен: не задан api_key")
    if not c.get("model"):
        raise RuntimeError("LLM не настроен: не задана модель")
    if not (c.get("base_url") or "").strip():
        raise RuntimeError("LLM не настроен: не задан base_url")

    web = False
    if allow_web:
        try:
            import web_search
            web = web_search.is_enabled(cfg)
        except Exception:
            web = False

    sys_content = system_override or c["system_prompt"]
    if web:
        sys_content += _WEB_HINT
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_content}]
    for h in (history or [])[-8:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    models = _model_candidates(c)
    last_err: Optional[Exception] = None
    for i, model in enumerate(models):
        try:
            if web:
                return _run_tool_loop(c, model, messages, cfg)
            return _complete(c, model, messages)
        except RuntimeError as exc:
            last_err = exc
            if i + 1 < len(models):
                log.warning("LLM model %s failed (%s) — trying fallback %s",
                            model, exc, models[i + 1])
    raise last_err or RuntimeError("LLM: нет доступных моделей")


def _stream_chat(c: dict[str, Any], model: str, messages: list[dict[str, Any]],
                 tools: Optional[list], on_content) -> tuple[str, list, Optional[str]]:
    """Stream one chat completion. Calls on_content(full_text_so_far) as content
    grows. Returns (content, tool_calls, finish_reason)."""
    url = f"{(c.get('base_url') or '').rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model, "messages": messages,
        "max_tokens": int(c.get("max_tokens") or 200),
        "temperature": float(c.get("temperature") or 0.6),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers,
                          proxies=_proxies(c.get("proxy") or ""),
                          timeout=(15, int(c.get("timeout_seconds") or 120)), stream=True)
    except Exception as exc:
        raise RuntimeError(f"Ошибка сети при запросе к LLM: {exc}") from exc
    if not r.ok:
        raise RuntimeError(f"LLM вернул {r.status_code}: {(r.text or '')[:200]}")

    content = ""
    tcs: dict[int, dict] = {}
    finish = None
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except Exception:
            continue
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content += delta["content"]
            try:
                on_content(content)
            except Exception:
                pass
        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            slot = tcs.setdefault(idx, {"id": tc.get("id"), "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    tool_calls = [{"id": v["id"] or f"call_{i}", "type": "function",
                   "function": {"name": v["name"], "arguments": v["arguments"]}}
                  for i, v in sorted(tcs.items())]
    return content, tool_calls, finish


def ask_stream(question: str, cfg: dict[str, Any], on_delta, system_override: Optional[str] = None,
               history: Optional[list[dict[str, str]]] = None, allow_web: bool = False) -> str:
    """Like ask() but streams: on_delta(text_so_far) is called as the answer grows
    (the caller throttles/edits a message). Keeps the web_search/web_fetch loop.
    Uses the primary model only. Returns the final capped text."""
    c = _cfg(cfg)
    if not (c.get("api_key") and c.get("model") and (c.get("base_url") or "").strip()):
        raise RuntimeError("LLM не настроен")
    web = False
    if allow_web:
        try:
            import web_search
            web = web_search.is_enabled(cfg)
        except Exception:
            web = False
    sys_content = system_override or c["system_prompt"]
    if web:
        sys_content += _WEB_HINT
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_content}]
    for h in (history or [])[-8:]:
        role = h.get("role")
        cont = (h.get("content") or "").strip()
        if role in ("user", "assistant") and cont:
            messages.append({"role": role, "content": cont})
    messages.append({"role": "user", "content": question})

    model = _model_candidates(c)[0]
    last = ""
    for step in range(MAX_TOOL_ITERS + 1):
        tools = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL] if (web and step < MAX_TOOL_ITERS) else None
        content, tool_calls, _finish = _stream_chat(c, model, messages, tools, on_delta)
        last = content
        if not tool_calls:
            text = (content or "").strip()
            if not text:
                raise RuntimeError("LLM вернул пустой ответ")
            return _cap(c, text)
        try:
            on_delta("🔎 Ищу в интернете…")
        except Exception:
            pass
        import web_search
        messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = _parse_tool_args(fn.get("arguments"))
            try:
                if name == "web_search":
                    res = web_search.search((args.get("query") or "").strip(), cfg)
                    out = web_search.format_results(res)
                elif name == "web_fetch":
                    out = web_search.fetch_url((args.get("url") or "").strip(), cfg)
                else:
                    out = f"Неизвестный инструмент: {name}"
            except Exception as exc:
                out = f"Ошибка инструмента: {exc}"
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
    return _cap(c, (last or "").strip()) or "(пустой ответ)"


def test_connection(cfg: dict[str, Any]) -> dict[str, Any]:
    """Quick connectivity/auth check — asks the model to reply with 'OK'."""
    import time
    t0 = time.time()
    try:
        answer = ask("Ответь одним словом: ok", cfg,
                     system_override="Ты — тестовый эхо. Ответь ровно: ok")
        return {"ok": True, "elapsed_seconds": round(time.time() - t0, 2), "answer": answer}
    except Exception as exc:
        return {"ok": False, "elapsed_seconds": round(time.time() - t0, 2), "error": str(exc)}


__all__ = ["ask", "ask_stream", "test_connection", "is_enabled", "DEFAULTS"]
