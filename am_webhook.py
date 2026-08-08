"""Alertmanager -> Telegram webhook receiver.

Runs an aiohttp server inside the bot process:
  POST {WEBHOOK_PATH}  — Alertmanager webhook_config target
  GET  /healthz        — container healthcheck

Delivery goes through a bounded queue with a background worker, so a
Telegram outage never blocks Alertmanager (it just gets a fast 200) and
excess messages are dropped with a log record.
"""

import asyncio
import hmac
import logging
import time
from datetime import datetime, timezone
from html import escape
from typing import Optional

from aiohttp import web

import notifier
from config import (
    ALERT_DEDUP_SECONDS,
    TELEGRAM_ALERTS_THREAD_ID,
    WARNING_BATCH_SECONDS,
    WEBHOOK_HOST,
    WEBHOOK_PATH,
    WEBHOOK_PORT,
    WEBHOOK_SECRET,
)

logger = logging.getLogger(__name__)

_SEVERITY_ICONS = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}
_HIDDEN_LABELS = {
    "alertname", "severity", "job", "instance", "__name__", "prometheus",
    "monitor", "env",
}

# Names acked via /ack — shown in the alert message.
acks: dict[str, tuple[str, str]] = {}


def _parse_ts(raw: str) -> Optional[datetime]:
    if not raw or raw.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(start: Optional[datetime]) -> str:
    if start is None:
        return "?"
    seconds = (datetime.now(timezone.utc) - start).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return f"{int(seconds // 60)}м"
    if seconds < 86400:
        return f"{int(seconds // 3600)}ч {int(seconds % 3600 // 60)}м"
    return f"{int(seconds // 86400)}д {int(seconds % 86400 // 3600)}ч"


def format_alert(alert: dict) -> tuple[str, str]:
    """-> (severity, text). Format follows the spec:

    🚨 CRITICAL | CameraStreamLikelyInactive
    cam: store-12-entrance
    for: 6m
    desc: stream status=0
    """
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = labels.get("alertname", "UnknownAlert")
    severity = str(labels.get("severity", "warning")).lower()
    status = alert.get("status", "firing")
    started = _parse_ts(alert.get("startsAt", ""))

    if status == "resolved":
        ended = _parse_ts(alert.get("endsAt", ""))
        duration = ""
        if started and ended:
            duration = f"\nдлилось: {_age_between(started, ended)}"
        return severity, f"✅ RESOLVED | <b>{escape(name)}</b>{duration}"

    icon = _SEVERITY_ICONS.get(severity, "⚠️")
    lines = [f"{icon} {escape(severity.upper())} | <b>{escape(name)}</b>"]

    shown = 0
    for key in sorted(labels):
        if key in _HIDDEN_LABELS:
            continue
        lines.append(f"{escape(key)}: {escape(str(labels[key]))}")
        shown += 1
        if shown >= 5:
            break

    lines.append(f"for: {_age(started)}")

    desc = annotations.get("description") or annotations.get("summary") or ""
    if desc:
        lines.append(f"desc: {escape(desc[:300])}")

    ack = acks.get(name)
    if ack:
        lines.append(f"🛠 в работе: {escape(ack[0])} ({ack[1]})")

    return severity, "\n".join(lines)


def _age_between(start: datetime, end: datetime) -> str:
    seconds = max((end - start).total_seconds(), 0)
    if seconds < 3600:
        return f"{int(seconds // 60)}м"
    if seconds < 86400:
        return f"{int(seconds // 3600)}ч {int(seconds % 3600 // 60)}м"
    return f"{int(seconds // 86400)}д"


class AlertGate:
    """Same fingerprint+status is delivered at most once per window."""

    def __init__(self, window_seconds: int):
        self.window = window_seconds
        self._seen: dict[str, float] = {}

    def allow(self, fingerprint: str, status: str) -> bool:
        key = f"{fingerprint}:{status}"
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < self.window:
            return False
        self._seen[key] = now
        if len(self._seen) > 2000:
            cutoff = now - self.window
            for stale in [k for k, ts in self._seen.items() if ts < cutoff]:
                del self._seen[stale]
        return True


class WebhookServer:
    def __init__(self):
        self._gate = AlertGate(ALERT_DEDUP_SECONDS)
        # (text, mention_admins); bounded so a Telegram outage cannot
        # grow memory without limit.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._warning_buffer: list[str] = []
        self._warning_flush_at: Optional[float] = None
        self._runner: Optional[web.AppRunner] = None
        self._worker: Optional[asyncio.Task] = None

    # --- HTTP handlers ---

    def _authorized(self, request: web.Request) -> bool:
        if not WEBHOOK_SECRET:
            return True
        provided = (
            request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.headers.get("X-Webhook-Token", "").strip()
            or request.query.get("token", "").strip()
        )
        return hmac.compare_digest(provided, WEBHOOK_SECRET)

    async def _handle_alertmanager(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            logger.warning("Webhook: unauthorized request from %s", request.remote)
            return web.Response(status=401, text="unauthorized")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        alerts = payload.get("alerts") or []
        logger.info("Webhook: получено %d алертов", len(alerts))

        for alert in alerts:
            fingerprint = alert.get("fingerprint") or str(
                sorted((alert.get("labels") or {}).items())
            )
            status = alert.get("status", "firing")
            if not self._gate.allow(fingerprint, status):
                continue

            severity, text = format_alert(alert)
            critical = severity == "critical" and status == "firing"

            if (
                WARNING_BATCH_SECONDS > 0
                and not critical
                and status == "firing"
            ):
                self._buffer_warning(text)
            else:
                self._enqueue(text, mention=critical)

        return web.Response(text="ok")

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    # --- Queueing ---

    def _enqueue(self, text: str, mention: bool = False) -> None:
        try:
            self._queue.put_nowait((text, mention))
        except asyncio.QueueFull:
            logger.error("Alert queue full — message dropped: %.120s", text)

    def _buffer_warning(self, text: str) -> None:
        self._warning_buffer.append(text)
        if self._warning_flush_at is None:
            self._warning_flush_at = time.monotonic() + WARNING_BATCH_SECONDS

    async def _sender_loop(self) -> None:
        while True:
            try:
                # Flush the warning batch when its window expires.
                if (
                    self._warning_flush_at is not None
                    and time.monotonic() >= self._warning_flush_at
                ):
                    batch = self._warning_buffer
                    self._warning_buffer = []
                    self._warning_flush_at = None
                    if batch:
                        self._enqueue("\n\n".join(batch)[:4000])

                try:
                    text, mention = await asyncio.wait_for(
                        self._queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                await notifier.send_text(
                    text,
                    mention_admins=mention,
                    thread_id=TELEGRAM_ALERTS_THREAD_ID,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Alert sender loop error")
                await asyncio.sleep(2)

    # --- Lifecycle ---

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(WEBHOOK_PATH, self._handle_alertmanager)
        app.router.add_get("/healthz", self._handle_healthz)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        self._worker = asyncio.create_task(self._sender_loop())
        logger.info(
            "Alertmanager webhook listening on %s:%d%s (secret: %s)",
            WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH,
            "on" if WEBHOOK_SECRET else "OFF",
        )
        if not WEBHOOK_SECRET:
            logger.warning("WEBHOOK_SECRET не задан — приёмник без авторизации!")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._runner:
            await self._runner.cleanup()
