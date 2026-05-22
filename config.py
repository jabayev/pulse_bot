import json
import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required env var: {key}")
    return value


def _parse_services(raw: str) -> list[dict]:
    try:
        services = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"SERVICES must be valid JSON: {e}") from e

    for svc in services:
        if "name" not in svc or "url" not in svc:
            raise ValueError(f"Each service must have 'name' and 'url': {svc}")

    return services


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

SERVICES: list[dict] = _parse_services(_require("SERVICES"))

CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))
