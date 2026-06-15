import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


@dataclass
class CameraStatus:
    name: str
    url: str
    display_url: str
    is_up: Optional[bool] = None
    last_checked: Optional[str] = None
    last_error: Optional[str] = None
    response_time_ms: Optional[float] = None


# camera name -> CameraStatus; persists across check cycles
camera_states: dict[str, CameraStatus] = {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def redact_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.username:
        return url

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        username = quote(parsed.username, safe="")
        netloc = f"{username}:***@{netloc}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _rtsp_options_packet(parsed_url) -> bytes:
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    request_uri = urlunsplit((parsed_url.scheme, parsed_url.netloc, path, "", ""))
    return (
        f"OPTIONS {request_uri} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: bahandi-pulse-bot\r\n"
        "\r\n"
    ).encode("ascii")


async def _check_one(url: str, timeout: float) -> tuple[bool, Optional[float], Optional[str]]:
    start = time.monotonic()
    writer = None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "rtsp":
            return False, None, f"Unsupported scheme: {parsed.scheme}"
        if not parsed.hostname:
            return False, None, "Missing RTSP host"

        port = parsed.port or 554
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port),
            timeout=timeout,
        )
        writer.write(_rtsp_options_packet(parsed))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        elapsed = (time.monotonic() - start) * 1000
        if data:
            return True, round(elapsed, 1), None
        return False, round(elapsed, 1), "No response from RTSP server"
    except asyncio.TimeoutError:
        return False, None, f"Timeout (>{timeout:g}s)"
    except OSError as e:
        return False, None, f"Connection error: {e}"
    except Exception as e:
        return False, None, f"Unexpected error: {e}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def check_camera(name: str, url: str, timeout: float) -> CameraStatus:
    is_up, response_time_ms, error = await _check_one(url, timeout)

    state = CameraStatus(
        name=name,
        url=url,
        display_url=redact_url(url),
        is_up=is_up,
        last_checked=_now_utc(),
        last_error=error,
        response_time_ms=response_time_ms,
    )
    camera_states[name] = state

    status_label = "UP" if is_up else "DOWN"
    logger.info(
        "[%s] camera %s — %s%s",
        status_label,
        name,
        state.display_url,
        f" ({error})" if error else f" {response_time_ms}ms",
    )
    return state


async def check_random_camera(cameras: list[dict], timeout: float) -> list[dict]:
    """
    Check one random camera (one RTSP address).

    Returns a list of change events:
        {"camera": CameraStatus, "event": "down" | "up"}
    """
    if not cameras:
        return []

    cam = random.choice(cameras)
    previous = camera_states.get(cam["name"])
    prev_is_up = previous.is_up if previous else None
    state = await check_camera(cam["name"], cam["url"], timeout)

    if prev_is_up is None or state.is_up == prev_is_up:
        return []
    if not state.is_up:
        return [{"camera": state, "event": "down"}]
    return [{"camera": state, "event": "up"}]


async def check_all_cameras(cameras: list[dict], timeout: float) -> list[CameraStatus]:
    tasks = [check_camera(cam["name"], cam["url"], timeout) for cam in cameras]
    return await asyncio.gather(*tasks)
