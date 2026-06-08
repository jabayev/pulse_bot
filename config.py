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


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

SERVICES: list[dict] = _parse_services(_require("SERVICES"))

CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))

CAMERA_RTSP_URL_TEMPLATE: str = os.getenv("CAMERA_RTSP_URL_TEMPLATE", "")
CAMERA_MONITOR_ENABLED: bool = _parse_bool(
    os.getenv("CAMERA_MONITOR_ENABLED", "true" if CAMERA_RTSP_URL_TEMPLATE else "false")
)
if CAMERA_MONITOR_ENABLED and not CAMERA_RTSP_URL_TEMPLATE:
    raise ValueError("CAMERA_RTSP_URL_TEMPLATE is required when CAMERA_MONITOR_ENABLED=true")
CAMERA_CHANNEL_START: int = int(os.getenv("CAMERA_CHANNEL_START", "101"))
CAMERA_CHANNEL_END: int = int(os.getenv("CAMERA_CHANNEL_END", "1501"))
CAMERA_CHANNEL_STEP: int = int(os.getenv("CAMERA_CHANNEL_STEP", "100"))
CAMERA_CHECK_TIMEOUT: float = float(os.getenv("CAMERA_CHECK_TIMEOUT", "5"))
CAMERA_CHANNELS: list[int] = list(
    range(CAMERA_CHANNEL_START, CAMERA_CHANNEL_END + 1, CAMERA_CHANNEL_STEP)
)
