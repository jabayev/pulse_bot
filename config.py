import json
import os
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alerts import Thresholds
from camera_source import CameraSourceConfig
from metrics import MetricsConfig

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
        method = str(svc.get("method", "GET")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError(f"Service {svc['name']}: unsupported method '{method}'")
        expect = svc.get("expect_status")
        if expect is not None and not (
            isinstance(expect, int)
            or (isinstance(expect, list) and all(isinstance(s, int) for s in expect))
        ):
            raise ValueError(
                f"Service {svc['name']}: 'expect_status' must be an int or list of ints"
            )
        headers = svc.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError(f"Service {svc['name']}: 'headers' must be a JSON object")

    return services


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_threshold(key: str, default: str) -> Optional[float]:
    """Threshold env var. Empty, 0 or 'off' disables the rule."""
    raw = os.getenv(key, default).strip().lower()
    if raw in {"", "0", "off", "false", "none", "-"}:
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{key} must be a number or 'off': {raw}") from e


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

# Optional: the bot can run purely on Alertmanager/Prometheus without
# its own HTTP service checks.
_services_raw = os.getenv("SERVICES", "").strip()
SERVICES: list[dict] = _parse_services(_services_raw) if _services_raw else []

CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))

CAMERA_CHECK_TIMEOUT: float = float(os.getenv("CAMERA_CHECK_TIMEOUT", "5"))

# Each camera is one RTSP address checked as-is (one camera per address).
# Used as the seed list, and as the whole list when CAMERAS_URL is not set.
_cameras_raw = os.getenv("CAMERAS", "").strip()
CAMERAS: list[dict] = _parse_cameras(_cameras_raw) if _cameras_raw else []

# --- Dynamic camera source -------------------------------------------------

CAMERAS_URL: str = os.getenv("CAMERAS_URL", "").strip()
CAMERAS_REFRESH_INTERVAL: int = int(os.getenv("CAMERAS_REFRESH_INTERVAL", "300"))
CAMERAS_NOTIFY_CHANGES: bool = _parse_bool(os.getenv("CAMERAS_NOTIFY_CHANGES", "true"))


def _camera_source_headers() -> dict[str, str]:
    raw = os.getenv("CAMERAS_HEADERS", "").strip()
    headers: dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"CAMERAS_HEADERS must be valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError("CAMERAS_HEADERS must be a JSON object")
        headers = {str(k): str(v) for k, v in parsed.items()}

    token = os.getenv("CAMERAS_API_TOKEN", "").strip()
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
    return headers


CAMERA_SOURCE: Optional[CameraSourceConfig] = (
    CameraSourceConfig(
        url=CAMERAS_URL,
        timeout=float(os.getenv("CAMERAS_SOURCE_TIMEOUT", "10")),
        headers=_camera_source_headers(),
    )
    if CAMERAS_URL
    else None
)

CAMERA_MONITOR_ENABLED: bool = _parse_bool(
    os.getenv("CAMERA_MONITOR_ENABLED", "true" if (CAMERAS or CAMERAS_URL) else "false")
)
if CAMERA_MONITOR_ENABLED and not CAMERAS and not CAMERAS_URL:
    raise ValueError(
        "CAMERAS or CAMERAS_URL is required when CAMERA_MONITOR_ENABLED=true"
    )

# --- Server metrics --------------------------------------------------------

METRICS_ENABLED: bool = _parse_bool(os.getenv("METRICS_ENABLED", "true"))
METRICS_CHECK_INTERVAL: int = int(os.getenv("METRICS_CHECK_INTERVAL", "60"))

METRICS_CONFIG = MetricsConfig(
    source=os.getenv("METRICS_SOURCE", "auto").strip().lower(),
    prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090").strip(),
    prometheus_timeout=float(os.getenv("PROMETHEUS_TIMEOUT", "10")),
    disk_mountpoint=os.getenv("DISK_MOUNTPOINT", "/").strip(),
    net_device_exclude=os.getenv(
        "NET_DEVICE_EXCLUDE", "lo|veth.*|docker.*|br-.*|virbr.*|tun.*"
    ).strip(),
    rate_window=os.getenv("PROMETHEUS_RATE_WINDOW", "5m").strip(),
)
if METRICS_CONFIG.source not in {"auto", "prometheus", "psutil"}:
    raise ValueError("METRICS_SOURCE must be one of: auto, prometheus, psutil")

ALERT_THRESHOLDS = Thresholds(
    cpu_percent=_parse_threshold("ALERT_CPU_PERCENT", "85"),
    memory_percent=_parse_threshold("ALERT_MEMORY_PERCENT", "90"),
    disk_percent=_parse_threshold("ALERT_DISK_PERCENT", "90"),
    swap_percent=_parse_threshold("ALERT_SWAP_PERCENT", "off"),
    load_per_core=_parse_threshold("ALERT_LOAD_PER_CORE", "2"),
    trigger_count=int(os.getenv("ALERT_TRIGGER_COUNT", "3")),
    recovery_count=int(os.getenv("ALERT_RECOVERY_COUNT", "2")),
    repeat_minutes=int(os.getenv("ALERT_REPEAT_MINUTES", "30")),
)

# --- BahandiML prod monitoring ---------------------------------------------

TZ_NAME: str = os.getenv("TZ_NAME", "Asia/Almaty").strip()
TZ = ZoneInfo(TZ_NAME)


def _parse_id_set(key: str) -> set[int]:
    raw = os.getenv(key, "").replace(";", ",")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as e:
            raise ValueError(f"{key}: '{part}' не является числовым ID") from e
    return ids


def _parse_str_list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default).replace(";", ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_optional_int(key: str) -> Optional[int]:
    raw = os.getenv(key, "").strip()
    return int(raw) if raw else None


# Roles / whitelist. Admins get every command; the group chat is read-only.
TELEGRAM_ADMIN_IDS: set[int] = _parse_id_set("TELEGRAM_ADMIN_IDS")
TELEGRAM_ALLOWED_CHAT_IDS: set[int] = {int(TELEGRAM_CHAT_ID)} | _parse_id_set(
    "TELEGRAM_ALLOWED_CHAT_IDS"
)
# "@user1,@user2" — mentioned in critical alerts.
TELEGRAM_ADMIN_MENTIONS: list[str] = _parse_str_list("TELEGRAM_ADMIN_MENTIONS")
# Optional forum-topic threads inside the group.
TELEGRAM_ALERTS_THREAD_ID: Optional[int] = _parse_optional_int("TELEGRAM_ALERTS_THREAD_ID")
TELEGRAM_STATS_THREAD_ID: Optional[int] = _parse_optional_int("TELEGRAM_STATS_THREAD_ID")

ALERTMANAGER_URL: str = os.getenv("ALERTMANAGER_URL", "http://localhost:9093").strip()

# Alertmanager -> bot webhook receiver.
WEBHOOK_ENABLED: bool = _parse_bool(os.getenv("WEBHOOK_ENABLED", "true"))
WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0").strip()
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/alertmanager").strip()
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "").strip()
# The same fingerprint+status is not re-sent within this window (anti-spam).
ALERT_DEDUP_SECONDS: int = int(os.getenv("ALERT_DEDUP_SECONDS", "300"))
# Warnings can be batched into a short digest during alert storms;
# 0 — send immediately. Criticals are always immediate.
WARNING_BATCH_SECONDS: int = int(os.getenv("WARNING_BATCH_SECONDS", "0"))

# GPU (DCGM exporter via Prometheus).
GPU_ENABLED: bool = _parse_bool(os.getenv("GPU_ENABLED", "true"))
GPU_TEMP_WARN: float = float(os.getenv("GPU_TEMP_WARN", "80"))
GPU_TEMP_CRIT: float = float(os.getenv("GPU_TEMP_CRIT", "88"))
EXPECTED_GPU_COUNT: int = int(os.getenv("EXPECTED_GPU_COUNT", "2"))

OLLAMA_ENABLED: bool = _parse_bool(os.getenv("OLLAMA_ENABLED", "true"))

# Docker containers of the prod compose project.
DOCKER_SOCKET: str = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock").strip()
DOCKER_ENABLED: bool = _parse_bool(
    os.getenv("DOCKER_ENABLED", "true" if os.path.exists(DOCKER_SOCKET) else "false")
)
# NB: deliberately not COMPOSE_PROJECT_NAME — that name is read by docker
# compose itself and would hijack the bot's own compose project.
COMPOSE_PROJECT: str = os.getenv(
    "WATCH_COMPOSE_PROJECT", os.getenv("COMPOSE_PROJECT_NAME", "mlbahandi-prod")
).strip()
DOCKER_CRITICAL_CONTAINERS: list[str] = _parse_str_list(
    "DOCKER_CRITICAL_CONTAINERS",
    "ml-platform,analytics-worker,frame-ingest,redis,webapp-backend,webapp-db,go2rtc",
)
DOCKER_CHECK_INTERVAL: int = int(os.getenv("DOCKER_CHECK_INTERVAL", "30"))
# Critical container not running longer than this -> critical alert.
DOCKER_DOWN_SECONDS: int = int(os.getenv("DOCKER_DOWN_SECONDS", "90"))
# >= N restarts/exits within the window -> restart-loop alert.
DOCKER_RESTART_THRESHOLD: int = int(os.getenv("DOCKER_RESTART_THRESHOLD", "3"))
DOCKER_RESTART_WINDOW: int = int(os.getenv("DOCKER_RESTART_WINDOW", "600"))

# Digests: local times (TZ_NAME) separated by comma.
DIGEST_ENABLED: bool = _parse_bool(os.getenv("DIGEST_ENABLED", "true"))
DIGEST_TIMES: list[str] = _parse_str_list("DIGEST_TIMES", "09:00,21:00")
for _t in DIGEST_TIMES:
    parts = _t.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"DIGEST_TIMES: '{_t}' должно быть в формате HH:MM")

# Optional external heartbeat (healthchecks.io etc.); empty — disabled.
HEARTBEAT_URL: str = os.getenv("HEARTBEAT_URL", "").strip()
HEARTBEAT_INTERVAL: int = int(os.getenv("HEARTBEAT_INTERVAL", "60"))
