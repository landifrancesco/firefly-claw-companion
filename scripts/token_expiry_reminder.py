#!/usr/bin/env python3
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import time

import requests


STATE_PATH = Path(os.getenv("FIREFLY_TOKEN_REMINDER_STATE_PATH", Path.home() / ".openclaw" / "token-reminder-state.json"))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_thresholds(raw: str) -> list[int]:
    thresholds: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        thresholds.append(int(part))
    return sorted(set(thresholds), reverse=True)


def load_state() -> dict[str, list[int]]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, list[int]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, list):
            normalized[key] = [int(item) for item in value]
    return normalized


def save_state(state: dict[str, list[int]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")


def reminder_text(days_left: int, expiry_date: date) -> str:
    return (
        "Firefly III personal access token expiry reminder.\n"
        f"Expiry date: {expiry_date.isoformat()}\n"
        f"Days left: {days_left}\n\n"
        "Create a new token soon and update the companion with the host secret file instead of sending it in Telegram.\n"
        "Recommended update path:\n"
        "1. Replace secrets/firefly_access_token.txt on the host\n"
        "2. Restart the companion container\n"
        "3. Delete any old token messages from your Telegram chat manually if you ever sent one there"
    )


def main() -> int:
    telegram_enabled = env_bool("TELEGRAM_ENABLED", True)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_TARGET_ID", os.getenv("TELEGRAM_OWNER_ID", "")).strip()
    expires_raw = os.getenv("FIREFLY_ACCESS_TOKEN_EXPIRES_ON", "").strip()
    interval_seconds = int(os.getenv("FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS", "21600"))
    thresholds = parse_thresholds(os.getenv("FIREFLY_TOKEN_REMINDER_DAYS", "60,30,14,7,3,1"))

    if not telegram_enabled or not bot_token or not chat_id or not expires_raw:
        return 0

    expiry_date = date.fromisoformat(expires_raw)
    state = load_state()
    state_key = expiry_date.isoformat()
    sent_thresholds = set(state.get(state_key, []))

    while True:
        today = datetime.now(timezone.utc).date()
        days_left = (expiry_date - today).days

        for threshold in thresholds:
            if days_left <= threshold and threshold not in sent_thresholds:
                send_telegram_message(bot_token, chat_id, reminder_text(days_left, expiry_date))
                sent_thresholds.add(threshold)
                state[state_key] = sorted(sent_thresholds, reverse=True)
                save_state(state)

        time.sleep(max(interval_seconds, 3600))


if __name__ == "__main__":
    raise SystemExit(main())
