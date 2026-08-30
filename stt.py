"""Speech-to-text via a Whisper ASR HTTP service (voice notes → text).

The Telegram command bot downloads a voice note and POSTs it to an
OpenAI-Whisper ASR web service (e.g. onerahmet/openai-whisper-asr-webservice)
running on the GPU box, then relays the transcript. Configured under
config.json "stt" (url is set locally so no LAN address ships in git).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "url": "",                  # e.g. http://host:9000 — set in config.json
    "language": "ru",
    "voice_to_mesh": False,     # admin voice notes also broadcast to mesh
    "timeout_seconds": 90,
}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.get("stt") or {})}


def is_enabled(cfg: dict[str, Any]) -> bool:
    c = _cfg(cfg)
    return bool(c.get("enabled") and c.get("url"))


def transcribe(audio_bytes: bytes, cfg: dict[str, Any], filename: str = "voice.oga") -> str:
    """Send audio to the ASR service and return the transcript text."""
    c = _cfg(cfg)
    base = (c.get("url") or "").rstrip("/")
    if not base:
        raise RuntimeError("STT: не задан url ASR-сервиса")
    params = {
        "task": "transcribe",
        "language": (c.get("language") or "ru"),
        "encode": "true",
        "output": "txt",
    }
    r = requests.post(
        base + "/asr", params=params,
        files={"audio_file": (filename, audio_bytes, "audio/ogg")},
        timeout=int(c.get("timeout_seconds") or 90),
    )
    r.raise_for_status()
    return (r.text or "").strip()


__all__ = ["transcribe", "is_enabled", "DEFAULTS"]
