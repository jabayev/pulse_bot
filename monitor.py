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


async def _body_snippet(resp: aiohttp.ClientResponse, limit: int = 300) -> str:
    """First bytes of the response body — backend error details for the alert."""
    try:
        text = (await resp.text(errors="replace")).strip()
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _status_ok(status: int, svc: dict) -> bool:
    expect = svc.get("expect_status")
    if expect is not None:
        allowed = {expect} if isinstance(expect, int) else set(expect)
        return status in allowed
    return 200 <= status < 300 or status == 468


async def _check_one(session: aiohttp.ClientSession, svc: dict) -> tuple[bool, Optional[float], Optional[str]]:
    """Return (is_up, response_time_ms, error_message)."""
    url = svc["url"]
    method = str(svc.get("method", "GET")).upper()
    body = svc.get("body")
    start = time.monotonic()
    try:
        async with session.request(
            method,
            url,
            json=body if isinstance(body, (dict, list)) else None,
            data=body if isinstance(body, str) else None,
            headers=svc.get("headers"),
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            ssl=False,
        ) as resp:
            elapsed = (time.monotonic() - start) * 1000
            if not _status_ok(resp.status, svc):
                detail = await _body_snippet(resp)
                error = f"HTTP {resp.status}" + (f" — {detail}" if detail else "")
                return False, round(elapsed, 1), error
            expect_contains = svc.get("expect_contains")
            if expect_contains:
                text = await resp.text(errors="replace")
                if expect_contains not in text:
                    return False, round(elapsed, 1), (
                        f"HTTP {resp.status}, но в ответе нет «{expect_contains}»"
                    )
            return True, round(elapsed, 1), None
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
