"""Server metrics collection.

Primary source is Prometheus (node_exporter metrics via the HTTP query API).
If Prometheus is unreachable or has no node_exporter data, falls back to
reading the local machine through psutil.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SOURCE_PROMETHEUS = "prometheus"
SOURCE_PSUTIL = "psutil"
SOURCE_AUTO = "auto"


@dataclass
class MetricsConfig:
    source: str = SOURCE_AUTO
    prometheus_url: str = "http://localhost:9090"
    prometheus_timeout: float = 10.0
    disk_mountpoint: str = "/"
    # Virtual/container interfaces are excluded from network throughput.
    net_device_exclude: str = "lo|veth.*|docker.*|br-.*|virbr.*|tun.*"
    # Window for rate() in the Prometheus queries.
    rate_window: str = "5m"


@dataclass
class ServerMetrics:
    source: str
    collected_at: str
    cpu_percent: Optional[float] = None
    cpu_cores: Optional[float] = None
    memory_percent: Optional[float] = None
    memory_used_bytes: Optional[float] = None
    memory_total_bytes: Optional[float] = None
    swap_percent: Optional[float] = None
    disk_percent: Optional[float] = None
    disk_used_bytes: Optional[float] = None
    disk_total_bytes: Optional[float] = None
    load1: Optional[float] = None
    load5: Optional[float] = None
    load15: Optional[float] = None
    net_rx_bytes_sec: Optional[float] = None
    net_tx_bytes_sec: Optional[float] = None
    uptime_seconds: Optional[float] = None
    errors: list[str] = field(default_factory=list)

    @property
    def load_per_core(self) -> Optional[float]:
        if self.load1 is None or not self.cpu_cores:
            return None
        return self.load1 / self.cpu_cores

    @property
    def is_empty(self) -> bool:
        """True when nothing at all could be collected."""
        return all(
            value is None
            for value in (
                self.cpu_percent,
                self.memory_percent,
                self.disk_percent,
                self.load1,
            )
        )


# Latest successful collection; served by /metrics between cycles.
last_metrics: Optional[ServerMetrics] = None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# --- Formatting helpers ----------------------------------------------------

def format_bytes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} PB"


def format_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{format_bytes(value)}/s"


def format_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д {hours}ч {minutes}м"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


# --- Prometheus ------------------------------------------------------------

def _build_queries(cfg: MetricsConfig) -> dict[str, str]:
    window = cfg.rate_window
    mount = cfg.disk_mountpoint
    fs = f'{{mountpoint="{mount}",fstype!~"tmpfs|overlay|squashfs"}}'
    net = f'{{device!~"{cfg.net_device_exclude}"}}'
    return {
        "cpu_percent": (
            f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle"}}[{window}])) * 100)'
        ),
        "cpu_cores": "count(count by (cpu) (node_cpu_seconds_total))",
        "memory_percent": (
            "100 * (1 - avg(node_memory_MemAvailable_bytes) "
            "/ avg(node_memory_MemTotal_bytes))"
        ),
        "memory_total_bytes": "avg(node_memory_MemTotal_bytes)",
        "memory_used_bytes": (
            "avg(node_memory_MemTotal_bytes) - avg(node_memory_MemAvailable_bytes)"
        ),
        "swap_percent": (
            "100 * (1 - avg(node_memory_SwapFree_bytes) "
            "/ avg(node_memory_SwapTotal_bytes))"
        ),
        "disk_percent": (
            f"100 * (1 - avg(node_filesystem_avail_bytes{fs}) "
            f"/ avg(node_filesystem_size_bytes{fs}))"
        ),
        "disk_total_bytes": f"avg(node_filesystem_size_bytes{fs})",
        "disk_used_bytes": (
            f"avg(node_filesystem_size_bytes{fs}) - avg(node_filesystem_avail_bytes{fs})"
        ),
        "load1": "avg(node_load1)",
        "load5": "avg(node_load5)",
        "load15": "avg(node_load15)",
        "net_rx_bytes_sec": f"sum(rate(node_network_receive_bytes_total{net}[{window}]))",
        "net_tx_bytes_sec": f"sum(rate(node_network_transmit_bytes_total{net}[{window}]))",
        "uptime_seconds": "time() - avg(node_boot_time_seconds)",
    }


async def _query_instant(
    session: aiohttp.ClientSession, base_url: str, query: str
) -> Optional[float]:
    """Run an instant PromQL query. Returns None when there is no data."""
    url = f"{base_url.rstrip('/')}/api/v1/query"
    async with session.get(url, params={"query": query}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        payload = await resp.json(content_type=None)

    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or "Prometheus returned an error")

    result = payload.get("data", {}).get("result") or []
    if not result:
        return None

    raw = result[0].get("value", [None, None])[1]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Prometheus reports division by zero (e.g. no swap) as NaN.
    if math.isnan(value) or math.isinf(value):
        return None
    return value


async def _collect_prometheus(cfg: MetricsConfig) -> ServerMetrics:
    metrics = ServerMetrics(source=SOURCE_PROMETHEUS, collected_at=_now_utc())
    queries = _build_queries(cfg)
    timeout = aiohttp.ClientTimeout(total=cfg.prometheus_timeout)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(
            *(_query_instant(session, cfg.prometheus_url, q) for q in queries.values()),
            return_exceptions=True,
        )

    transport_failures = 0
    for field_name, result in zip(queries.keys(), results):
        if isinstance(result, Exception):
            transport_failures += 1
            logger.debug("Prometheus query failed for %s: %s", field_name, result)
            continue
        setattr(metrics, field_name, result)

    if transport_failures:
        metrics.errors.append(
            f"Prometheus: {transport_failures} из {len(queries)} запросов не выполнено"
        )
    elif metrics.is_empty:
        metrics.errors.append(
            "Prometheus отвечает, но метрик node_exporter нет — проверьте scrape-конфиг"
        )

    return metrics


# --- psutil fallback -------------------------------------------------------

# (monotonic_ts, bytes_recv, bytes_sent) from the previous sample.
_last_net_sample: Optional[tuple[float, float, float]] = None


def _collect_psutil_blocking(cfg: MetricsConfig) -> ServerMetrics:
    global _last_net_sample

    import psutil

    metrics = ServerMetrics(source=SOURCE_PSUTIL, collected_at=_now_utc())

    try:
        metrics.cpu_percent = psutil.cpu_percent(interval=1.0)
        metrics.cpu_cores = psutil.cpu_count() or None
    except Exception as e:
        metrics.errors.append(f"CPU: {e}")

    try:
        mem = psutil.virtual_memory()
        metrics.memory_percent = mem.percent
        metrics.memory_total_bytes = mem.total
        metrics.memory_used_bytes = mem.total - mem.available
    except Exception as e:
        metrics.errors.append(f"RAM: {e}")

    try:
        swap = psutil.swap_memory()
        metrics.swap_percent = swap.percent if swap.total else None
    except Exception as e:
        metrics.errors.append(f"Swap: {e}")

    try:
        disk = psutil.disk_usage(cfg.disk_mountpoint)
        metrics.disk_percent = disk.percent
        metrics.disk_total_bytes = disk.total
        metrics.disk_used_bytes = disk.used
    except Exception as e:
        metrics.errors.append(f"Диск: {e}")

    try:
        metrics.load1, metrics.load5, metrics.load15 = psutil.getloadavg()
    except (AttributeError, OSError) as e:
        metrics.errors.append(f"Load average: {e}")

    try:
        net = psutil.net_io_counters()
        now = time.monotonic()
        previous = _last_net_sample
        _last_net_sample = (now, net.bytes_recv, net.bytes_sent)
        if previous:
            elapsed = now - previous[0]
            # Counters reset on reboot; a negative delta means we lost the baseline.
            if elapsed > 0 and net.bytes_recv >= previous[1] and net.bytes_sent >= previous[2]:
                metrics.net_rx_bytes_sec = (net.bytes_recv - previous[1]) / elapsed
                metrics.net_tx_bytes_sec = (net.bytes_sent - previous[2]) / elapsed
    except Exception as e:
        metrics.errors.append(f"Сеть: {e}")

    try:
        metrics.uptime_seconds = time.time() - psutil.boot_time()
    except Exception as e:
        metrics.errors.append(f"Uptime: {e}")

    return metrics


async def _collect_psutil(cfg: MetricsConfig) -> ServerMetrics:
    try:
        return await asyncio.to_thread(_collect_psutil_blocking, cfg)
    except ImportError:
        return ServerMetrics(
            source=SOURCE_PSUTIL,
            collected_at=_now_utc(),
            errors=["psutil не установлен — добавьте его в requirements.txt"],
        )


# --- Public API ------------------------------------------------------------

async def collect_metrics(cfg: MetricsConfig) -> ServerMetrics:
    """Collect server metrics according to the configured source."""
    global last_metrics

    if cfg.source == SOURCE_PSUTIL:
        metrics = await _collect_psutil(cfg)
    else:
        try:
            metrics = await _collect_prometheus(cfg)
        except Exception as e:
            logger.warning("Prometheus collection failed: %s", e)
            metrics = ServerMetrics(
                source=SOURCE_PROMETHEUS,
                collected_at=_now_utc(),
                errors=[f"Prometheus недоступен: {e}"],
            )

        if cfg.source == SOURCE_AUTO and metrics.is_empty:
            logger.info("Prometheus returned no usable metrics — falling back to psutil")
            fallback = await _collect_psutil(cfg)
            if not fallback.is_empty:
                fallback.errors = metrics.errors + fallback.errors
                metrics = fallback

    last_metrics = metrics
    logger.info(
        "metrics[%s] cpu=%s%% mem=%s%% disk=%s%% load1=%s",
        metrics.source,
        f"{metrics.cpu_percent:.1f}" if metrics.cpu_percent is not None else "—",
        f"{metrics.memory_percent:.1f}" if metrics.memory_percent is not None else "—",
        f"{metrics.disk_percent:.1f}" if metrics.disk_percent is not None else "—",
        f"{metrics.load1:.2f}" if metrics.load1 is not None else "—",
    )
    return metrics
