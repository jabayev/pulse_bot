"""Dynamic camera list pulled from an HTTP endpoint returning JSON.

The endpoint is expected to return the currently active cameras, either as a
bare array or wrapped in an object:

    [{"name": "Вход", "url": "rtsp://user:pass@host:554/Streaming/Channels/101"}]
    {"cameras": [...]}

Field names are matched loosely so the endpoint does not have to be built
around this bot exactly.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import aiohttp

from camera_monitor import camera_states, redact_url

logger = logging.getLogger(__name__)

_LIST_KEYS = ("cameras", "data", "items", "results", "list")
_URL_KEYS = ("url", "rtsp_url", "rtsp", "stream_url", "stream", "link", "address")
_NAME_KEYS = ("name", "title", "label", "camera_name", "id")
_ACTIVE_KEYS = ("active", "enabled", "is_active", "online", "is_enabled")


@dataclass
class CameraSourceConfig:
    url: str
    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CameraDiff:
    added: list[str]
    removed: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _extract_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(
        "Ответ должен быть массивом камер или объектом с полем "
        f"{'/'.join(_LIST_KEYS)}"
    )


def _first_key(item: dict, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _is_active(item: dict) -> bool:
    """Only an explicit false disables a camera; absent flags mean active."""
    for key in _ACTIVE_KEYS:
        if key in item and item[key] is False:
            return False
    return True


def normalize(payload) -> list[dict]:
    """Turn a raw endpoint response into [{"name", "url"}] with unique names."""
    raw_items = _extract_list(payload)
    cameras: list[dict] = []
    seen_names: set[str] = set()

    for i, item in enumerate(raw_items, 1):
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            logger.warning("Camera #%d skipped: unsupported entry type %s", i, type(item).__name__)
            continue
        if not _is_active(item):
            continue

        url = _first_key(item, _URL_KEYS)
        if not url:
            logger.warning("Camera #%d skipped: no RTSP url in %s", i, sorted(item.keys()))
            continue

        name = _first_key(item, _NAME_KEYS) or urlsplit(url).hostname or f"Камера {i}"
        # camera_states is keyed by name, so names have to stay unique.
        if name in seen_names:
            host = urlsplit(url).hostname
            candidate = f"{name} ({host})" if host else f"{name} #{i}"
            while candidate in seen_names:
                candidate = f"{candidate}'"
            name = candidate
        seen_names.add(name)

        cameras.append({"name": name, "url": url})

    return cameras


async def fetch_cameras(cfg: CameraSourceConfig) -> list[dict]:
    """Fetch and normalize the camera list. Raises on transport/format errors."""
    timeout = aiohttp.ClientTimeout(total=cfg.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(cfg.url, headers=cfg.headers, ssl=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            payload = await resp.json(content_type=None)

    return normalize(payload)


class CameraRegistry:
    """Holds the camera list currently being monitored."""

    def __init__(self, cameras: Optional[list[dict]] = None) -> None:
        self._cameras: list[dict] = list(cameras or [])
        self.last_updated: Optional[str] = None
        self.last_error: Optional[str] = None
        self.is_dynamic: bool = False

    @property
    def cameras(self) -> list[dict]:
        return self._cameras

    def __len__(self) -> int:
        return len(self._cameras)

    def replace(self, cameras: list[dict]) -> CameraDiff:
        old_names = {cam["name"] for cam in self._cameras}
        new_names = {cam["name"] for cam in cameras}

        # Drop cached status of cameras that no longer exist, otherwise they
        # would linger in /status_camera forever.
        for name in old_names - new_names:
            camera_states.pop(name, None)

        self._cameras = cameras
        self.last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.last_error = None
        return CameraDiff(
            added=sorted(new_names - old_names),
            removed=sorted(old_names - new_names),
        )

    async def refresh(self, cfg: CameraSourceConfig) -> Optional[CameraDiff]:
        """Pull a fresh list. On failure keeps the previous one and returns None."""
        try:
            cameras = await fetch_cameras(cfg)
        except Exception as e:
            self.last_error = str(e)
            logger.warning("Camera source refresh failed (%s): %s", cfg.url, e)
            return None

        if not cameras:
            self.last_error = "Источник вернул пустой список камер"
            logger.warning("Camera source returned no cameras — keeping previous list")
            return None

        diff = self.replace(cameras)
        self.is_dynamic = True
        if diff.changed:
            logger.info(
                "Camera list updated: +%d / -%d (total %d)",
                len(diff.added),
                len(diff.removed),
                len(cameras),
            )
            for cam in cameras:
                logger.debug("camera %s -> %s", cam["name"], redact_url(cam["url"]))
        return diff
