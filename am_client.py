"""Alertmanager API v2 client: active alerts and silences."""

import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw]?)$", re.IGNORECASE)
_DURATION_UNITS = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(raw: str):
    """'30m' / '2h' / '1d' -> seconds. A bare number means minutes."""
    match = _DURATION_RE.match(raw.strip())
    if not match:
        return None
    return int(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


def format_duration(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{max(minutes, 1)}м"


class AlertmanagerClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def active_alerts(self) -> list[dict]:
        url = f"{self.base_url}/api/v2/alerts"
        params = {"active": "true", "silenced": "false", "inhibited": "false"}
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Alertmanager HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def create_silence(
        self,
        alertname: str,
        duration_seconds: int,
        created_by: str,
        comment: str = "silenced from Telegram bot",
    ) -> str:
        now = datetime.now(timezone.utc)
        body = {
            "matchers": [
                {
                    "name": "alertname",
                    "value": alertname,
                    "isRegex": False,
                    "isEqual": True,
                }
            ],
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(seconds=duration_seconds)).isoformat(),
            "createdBy": created_by,
            "comment": comment,
        }
        url = f"{self.base_url}/api/v2/silences"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, json=body) as resp:
                payload = await resp.json(content_type=None)
                if resp.status not in (200, 201):
                    raise RuntimeError(f"Alertmanager HTTP {resp.status}: {payload}")
                return payload.get("silenceID") or payload.get("id") or "?"
