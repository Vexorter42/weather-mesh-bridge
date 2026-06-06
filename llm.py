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

import logging
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

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


def ask(question: str, cfg: dict[str, Any], system_override: Optional[str] = None) -> str:
    """Send a single-turn question to the LLM and return the answer text.

    Raises RuntimeError with a human-readable message on failure.
    """
    c = _cfg(cfg)
    if not c.get("api_key"):
        raise RuntimeError("LLM не настроен: не задан api_key")
    if not c.get("model"):
        raise RuntimeError("LLM не настроен: не задана модель")
    base = (c.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("LLM не настроен: не задан base_url")

    url = f"{base}/chat/completions"
    payload = {
        "model": c["model"],
        "messages": [
            {"role": "system", "content": system_override or c["system_prompt"]},
            {"role": "user", "content": question},
        ],
        "max_tokens": int(c.get("max_tokens") or 200),
        "temperature": float(c.get("temperature") or 0.6),
        "stream": False,
    }
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
        log.exception("LLM request failed")
        raise RuntimeError(f"Ошибка сети при запросе к LLM: {exc}") from exc

    if r.status_code == 401:
        raise RuntimeError("LLM: неверный api_key (401)")
    if r.status_code == 404:
        raise RuntimeError("LLM: не найден endpoint/модель (404) — проверь base_url и model")
    if r.status_code == 429:
        raise RuntimeError("LLM: превышен лимит запросов (429), попробуй позже")
    if not r.ok:
        snippet = (r.text or "")[:200]
        raise RuntimeError(f"LLM вернул {r.status_code}: {snippet}")

    try:
        data = r.json()
        message = data["choices"][0]["message"]
        text = message.get("content")
    except Exception as exc:
        raise RuntimeError(f"LLM: не смог разобрать ответ: {exc}") from exc

    text = (text or "").strip()
    if not text:
        # Reasoning/"thinking" models (e.g. kimi-k2.6) can burn the whole token
        # budget on internal reasoning and return empty content. Detect that and
        # give actionable advice instead of a cryptic "empty answer".
        if message.get("reasoning") or message.get("reasoning_content"):
            raise RuntimeError(
                "Модель потратила лимит на «размышления» и не выдала ответ. "
                "Подними max_tokens (≥1500) или выбери модель без reasoning, "
                "например moonshotai/kimi-k2."
            )
        raise RuntimeError("LLM вернул пустой ответ")

    cap = int(c.get("max_reply_chars") or 600)
    if len(text) > cap:
        text = text[:cap - 1].rstrip() + "…"
    return text


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


__all__ = ["ask", "test_connection", "is_enabled", "DEFAULTS"]
