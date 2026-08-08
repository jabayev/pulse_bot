"""Thin Prometheus HTTP API client for the report/command modules.

metrics.py keeps its own query set for the host metrics summary; this client
is the generic one used by GPU / ML / digest reports.
"""

import logging
import math
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class Sample:
    labels: dict
    value: float


class PromClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def query(self, expr: str) -> list[Sample]:
        """Instant PromQL query. NaN/Inf samples are dropped."""
        url = f"{self.base_url}/api/v1/query"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url, params={"query": expr}) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Prometheus HTTP {resp.status}")
                payload = await resp.json(content_type=None)

        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error") or "Prometheus вернул ошибку")

        samples: list[Sample] = []
        for item in payload.get("data", {}).get("result") or []:
            raw = item.get("value", [None, None])[1]
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isnan(value) or math.isinf(value):
                continue
            samples.append(Sample(labels=item.get("metric", {}), value=value))
        return samples

    async def scalar(self, expr: str):
        """First value of an instant query, or None when there is no data."""
        samples = await self.query(expr)
        return samples[0].value if samples else None

    async def is_up(self) -> bool:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}/-/healthy") as resp:
                    return resp.status == 200
        except Exception:
            return False


# Application metrics may name the camera/entity label differently between
# services, so reports pick the first label that is present.
NAME_LABELS = (
    "camera", "cam", "camera_name", "camera_id", "stream", "location",
    "name", "title", "id",
)


def sample_name(labels: dict, candidates: tuple = NAME_LABELS) -> str:
    for key in candidates:
        value = labels.get(key)
        if value:
            return str(value)
    extra = {
        k: v for k, v in labels.items()
        if k not in {"__name__", "job", "instance"}
    }
    if extra:
        return ", ".join(f"{k}={v}" for k, v in sorted(extra.items()))
    return str(labels.get("instance", "—"))
