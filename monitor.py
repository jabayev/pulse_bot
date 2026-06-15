import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass
class ServiceStatus:
    name: str
    url: str
    is_up: Optional[bool] = None
    last_checked: Optional[str] = None
    last_error: Optional[str] = None
    response_time_ms: Optional[float] = None


# url → ServiceStatus; persists across check cycles
service_states: dict[str, ServiceStatus] = {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _check_one(session: aiohttp.ClientSession, svc: dict) -> tuple[bool, Optional[float], Optional[str]]:
    """Return (is_up, response_time_ms, error_message)."""
    url = svc["url"]
    start = time.monotonic()
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, ssl=False) as resp:
            elapsed = (time.monotonic() - start) * 1000
            if 200 <= resp.status < 300 or resp.status == 468:
                return True, round(elapsed, 1), None
            return False, round(elapsed, 1), f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        return False, None, "Timeout (>10s)"
    except aiohttp.ClientConnectionError as e:
        return False, None, f"Connection error: {e}"
    except Exception as e:
        return False, None, f"Unexpected error: {e}"


async def check_services(services: list[dict]) -> list[dict]:
    """
    Check all services concurrently.

    Returns a list of change events:
        {"service": ServiceStatus, "event": "down" | "up"}
    Only emitted after the first known state changes (up→down or down→up).
    """
    events: list[dict] = []

    async with aiohttp.ClientSession() as session:
        tasks = [_check_one(session, svc) for svc in services]
        results = await asyncio.gather(*tasks)

    for svc, (is_up, response_time_ms, error) in zip(services, results):
        url = svc["url"]
        previous = service_states.get(url)
        prev_is_up = previous.is_up if previous else None

        state = ServiceStatus(
            name=svc["name"],
            url=url,
            is_up=is_up,
            last_checked=_now_utc(),
            last_error=error,
            response_time_ms=response_time_ms,
        )
        service_states[url] = state

        status_label = "UP" if is_up else "DOWN"
        logger.info("[%s] %s — %s%s",
                    status_label,
                    svc["name"],
                    url,
                    f" ({error})" if error else f" {response_time_ms}ms")

        # First observation only seeds state; notifications are for real transitions.
        if prev_is_up is None:
            continue
        if is_up != prev_is_up:
            events.append({"service": state, "event": "up" if is_up else "down"})

    return events
