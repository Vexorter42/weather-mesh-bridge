#!/usr/bin/env python3
"""One-time interactive Telegram authentication.

Run on the Pi via SSH:

    cd /home/pi/weather-mesh-bridge
    source .venv/bin/activate
    python telegram_setup.py

It will:
  1. Read api_id / api_hash from config.json (or prompt you for them)
  2. Ask for your phone number
  3. Send an SMS code and ask you to type it in
  4. (If you have 2FA) ask for your cloud password
  5. Save the session file (telegram.session) so the bot can auto-login later

After this runs successfully, toggle the bridge ON in the web UI ("Прочее ↦
Экспериментальное ↦ Telegram-мост") and it will start listening.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("Telethon не установлен. Запусти:")
    print("  source .venv/bin/activate")
    print("  pip install telethon")
    sys.exit(1)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_PATH = BASE_DIR / "telegram.session"


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def main() -> int:
    # Load existing config (if any) so we can reuse api_id / api_hash
    tg_cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            tg_cfg = cfg.get("telegram") or {}
        except Exception as exc:
            print(f"Не смог прочитать config.json: {exc}. Продолжаю без него.")

    print()
    print("=== Telegram → Mesh Bridge — первичная авторизация ===")
    print()
    print("Возьми api_id и api_hash на https://my.telegram.org → API development tools")
    print()

    api_id = _prompt("api_id", str(tg_cfg.get("api_id") or "")).strip()
    api_hash = _prompt("api_hash", str(tg_cfg.get("api_hash") or "")).strip()
    if not api_id or not api_hash:
        print("api_id и api_hash обязательны. Прерываю.")
        return 1
    try:
        api_id_int = int(api_id)
    except ValueError:
        print("api_id должен быть числом.")
        return 1

    phone = _prompt("Телефон (с +код страны, например +79161234567)").strip()
    if not phone:
        print("Телефон обязателен. Прерываю.")
        return 1

    client = TelegramClient(str(SESSION_PATH), api_id_int, api_hash)

    async def _do_auth():
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже авторизован как @{me.username or me.first_name}. Сессия в {SESSION_PATH.name}")
            return 0

        await client.send_code_request(phone)
        code = _prompt("SMS-код").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pw = getpass.getpass("Облачный пароль (2FA): ")
            await client.sign_in(password=pw)

        me = await client.get_me()
        print()
        print(f"✓ Авторизация прошла. Подключён как @{me.username or me.first_name}.")
        print(f"  Сессия сохранена в {SESSION_PATH}.")
        print()
        print("Теперь в config.json (или через UI) задай channels и keywords, и включи мост.")
        return 0

    import asyncio
    try:
        return asyncio.run(_do_auth())
    except Exception as exc:
        print(f"Ошибка авторизации: {exc}")
        return 1
    finally:
        try:
            asyncio.run(client.disconnect())
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
