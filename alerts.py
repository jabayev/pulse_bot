"""Threshold alerting on top of collected server metrics.

An alert only fires after the threshold has been exceeded `trigger_count`
cycles in a row, and only clears after `recovery_count` clean cycles — that
keeps a single spike from spamming the chat.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from metrics import ServerMetrics, format_bytes

logger = logging.getLogger(__name__)


@dataclass
class Thresholds:
    cpu_percent: Optional[float] = 95.0
    memory_percent: Optional[float] = 95.0
    disk_percent: Optional[float] = 95.0
    swap_percent: Optional[float] = None
    load_per_core: Optional[float] = 2.0
    trigger_count: int = 3
    recovery_count: int = 2
    repeat_minutes: int = 30


@dataclass
class AlertState:
    key: str
    firing: bool = False
    breaches: int = 0
    oks: int = 0
    since: Optional[str] = None
    last_notified: Optional[float] = None
    peak_value: Optional[float] = None


@dataclass
class AlertEvent:
    key: str
    label: str
    value: float
    threshold: float
    unit: str
    event: str  # "fire" | "repeat" | "recover"
    since: Optional[str] = None
    peak_value: Optional[float] = None
    detail: str = ""  # e.g. "12.3 GB / 31.2 GB" — absolute numbers behind the %

    def format_value(self, value: Optional[float] = None) -> str:
        target = self.value if value is None else value
        if self.unit == "%":
            return f"{target:.1f}%"
        return f"{target:.2f}"

    def format_threshold(self) -> str:
        # :g so 85.0 reads as "85" but a fractional threshold keeps its digits.
        return f"{self.threshold:g}{self.unit}"


# rule key -> AlertState; persists across cycles
alert_states: dict[str, AlertState] = {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _details(metrics: ServerMetrics) -> dict[str, str]:
    """Absolute figures to show next to a percentage, keyed by rule key."""
    details: dict[str, str] = {}
    if metrics.memory_used_bytes is not None and metrics.memory_total_bytes is not None:
        details["memory"] = (
            f"{format_bytes(metrics.memory_used_bytes)} / "
            f"{format_bytes(metrics.memory_total_bytes)}"
        )
    return details


def _rules(metrics: ServerMetrics, thresholds: Thresholds) -> list[tuple]:
    """(key, label, current value, threshold, unit) for every enabled rule."""
    return [
        ("cpu", "CPU", metrics.cpu_percent, thresholds.cpu_percent, "%"),
        ("memory", "Оперативная память (ОЗУ, не GPU)", metrics.memory_percent, thresholds.memory_percent, "%"),
        ("disk", "Диск", metrics.disk_percent, thresholds.disk_percent, "%"),
        ("swap", "Swap", metrics.swap_percent, thresholds.swap_percent, "%"),
        (
            "load",
            "Load average (на ядро)",
            metrics.load_per_core,
            thresholds.load_per_core,
            "",
        ),
    ]


def evaluate(metrics: ServerMetrics, thresholds: Thresholds) -> list[AlertEvent]:
    """Update alert state from a fresh collection and return events to notify."""
    events: list[AlertEvent] = []
    now = time.monotonic()
    details = _details(metrics)

    for key, label, value, threshold, unit in _rules(metrics, thresholds):
        # A threshold of None/0 disables the rule; a missing value means the
        # metric was not collected this cycle, so state is left untouched.
        if not threshold or value is None:
            continue

        state = alert_states.setdefault(key, AlertState(key=key))

        if value >= threshold:
            state.breaches += 1
            state.oks = 0
            state.peak_value = value if state.peak_value is None else max(state.peak_value, value)

            if not state.firing and state.breaches >= thresholds.trigger_count:
                state.firing = True
                state.since = _now_utc()
                state.last_notified = now
                events.append(
                    AlertEvent(
                        key, label, value, threshold, unit, "fire", state.since,
                        detail=details.get(key, ""),
                    )
                )
                logger.warning("ALERT %s: %s >= %s", key, value, threshold)
            elif (
                state.firing
                and thresholds.repeat_minutes
                and state.last_notified is not None
                and now - state.last_notified >= thresholds.repeat_minutes * 60
            ):
                state.last_notified = now
                events.append(
                    AlertEvent(
                        key, label, value, threshold, unit, "repeat", state.since,
                        state.peak_value, details.get(key, ""),
                    )
                )
        else:
            state.oks += 1
            state.breaches = 0

            if state.firing and state.oks >= thresholds.recovery_count:
                events.append(
                    AlertEvent(
                        key, label, value, threshold, unit, "recover", state.since,
                        state.peak_value, details.get(key, ""),
                    )
                )
                logger.info("RESOLVED %s: %s < %s", key, value, threshold)
                state.firing = False
                state.since = None
                state.last_notified = None
                state.peak_value = None

    return events


def active_alerts() -> list[str]:
    """Keys of the rules currently in a firing state."""
    return [key for key, state in alert_states.items() if state.firing]
