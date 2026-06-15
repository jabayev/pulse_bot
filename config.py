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


def _parse_cameras(raw: str) -> list[dict]:
    try:
        cameras = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"CAMERAS must be valid JSON: {e}") from e

    normalized: list[dict] = []
    for i, cam in enumerate(cameras, 1):
        url = cam.get("url")
        if not url:
            raise ValueError(f"Camera #{i}: 'url' is required: {cam}")
        normalized.append(
            {
                "name": cam.get("name", f"Камера {i}"),
                "url": url,
            }
        )
    return normalized


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

SERVICES: list[dict] = _parse_services(_require("SERVICES"))

CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))

CAMERA_CHECK_TIMEOUT: float = float(os.getenv("CAMERA_CHECK_TIMEOUT", "5"))

# Each camera is one RTSP address checked as-is (one camera per address).
_cameras_raw = os.getenv("CAMERAS", "").strip()
CAMERAS: list[dict] = _parse_cameras(_cameras_raw) if _cameras_raw else []

CAMERA_MONITOR_ENABLED: bool = _parse_bool(
    os.getenv("CAMERA_MONITOR_ENABLED", "true" if CAMERAS else "false")
)
if CAMERA_MONITOR_ENABLED and not CAMERAS:
    raise ValueError("CAMERAS is required when CAMERA_MONITOR_ENABLED=true")
