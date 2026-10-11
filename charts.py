"""Графики по метрикам Prometheus: дневной отчёт и /charts по кнопкам.

Данные берутся через query_range, рисуется matplotlib (Agg, без дисплея),
наружу отдаётся PNG в байтах — отправкой занимается main.py / notifier.py.
"""

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from config import ALERT_THRESHOLDS, GPU_TEMP_WARN, METRICS_CONFIG, TZ  # noqa: E402
from prom import PromClient  # noqa: E402

logger = logging.getLogger(__name__)

_MAX_POINTS = 240
_COLORS = ("#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#B279A2")


def _node(extra: str = "") -> str:
    """node_exporter label matchers, with the optional host selector."""
    sel = ",".join(x for x in (extra, METRICS_CONFIG.node_selector) if x)
    return f"{{{sel}}}" if sel else ""


@dataclass
class Panel:
    key: str
    title: str
    button: str
    expr: str
    unit: str = ""
    y_max: Optional[float] = None
    threshold: Optional[float] = None
    label_key: Optional[str] = None  # label that names a series (e.g. "gpu")
    label_prefix: str = ""
    # Number format used in captions.
    fmt: str = "{:.0f}"


def _panels() -> dict[str, Panel]:
    idle = _node('mode="idle"')
    return {
        p.key: p
        for p in (
            Panel(
                "cpu", "CPU, %", "🖥 CPU",
                f"100 - (avg(rate(node_cpu_seconds_total{idle}[5m])) * 100)",
                unit="%", y_max=100, threshold=ALERT_THRESHOLDS.cpu_percent,
            ),
            Panel(
                "ram", "ОЗУ (RAM), %", "🧠 ОЗУ",
                f"100 * (1 - avg(node_memory_MemAvailable_bytes{_node()}) "
                f"/ avg(node_memory_MemTotal_bytes{_node()}))",
                unit="%", y_max=100, threshold=ALERT_THRESHOLDS.memory_percent,
            ),
            Panel(
                "gpu", "GPU: загрузка, %", "🎮 GPU util",
                "DCGM_FI_DEV_GPU_UTIL",
                unit="%", y_max=100, label_key="gpu", label_prefix="GPU",
            ),
            Panel(
                "vram", "VRAM (видеопамять), ГБ", "📦 VRAM",
                "DCGM_FI_DEV_FB_USED / 1024",
                unit=" ГБ", label_key="gpu", label_prefix="GPU", fmt="{:.1f}",
            ),
            Panel(
                "temp", "GPU: температура, °C", "🌡 Темп.",
                "DCGM_FI_DEV_GPU_TEMP",
                unit="°C", threshold=GPU_TEMP_WARN, label_key="gpu", label_prefix="GPU",
            ),
            Panel(
                "frames", "Обработано кадров, шт/сек", "🎞 Кадры",
                "sum(rate(ml_frames_processed_total[5m]))",
                unit="/с", fmt="{:.1f}",
            ),
        )
    }


PANEL_ORDER = ("cpu", "ram", "gpu", "vram", "temp", "frames")


def panel_buttons() -> list[tuple[str, str]]:
    panels = _panels()
    return [(panels[k].button, k) for k in PANEL_ORDER]


@dataclass
class Series:
    name: str
    points: list[tuple[datetime, float]]


async def _fetch(prom: PromClient, panel: Panel, hours: float) -> list[Series]:
    end = datetime.now(TZ).timestamp()
    start = end - hours * 3600
    step = max(60, int(hours * 3600 / _MAX_POINTS))
    raw = await prom.query_range(panel.expr, start, end, step)
    result: list[Series] = []
    for labels, values in raw:
        if panel.label_key:
            name = f"{panel.label_prefix}{labels.get(panel.label_key, '?')}"
        else:
            name = panel.title
        pts = [(datetime.fromtimestamp(ts, TZ), v) for ts, v in values]
        if pts:
            result.append(Series(name, pts))
    result.sort(key=lambda s: s.name)
    return result


def _draw(ax, panel: Panel, series: list[Series]) -> None:
    ax.set_title(panel.title, fontsize=11, loc="left", fontweight="bold")
    ax.grid(True, alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    if not series:
        ax.text(0.5, 0.5, "нет данных", ha="center", va="center",
                transform=ax.transAxes, color="#888")
        ax.set_xticks([])
        ax.set_yticks([])
        return

    for i, s in enumerate(series):
        xs = [p[0] for p in s.points]
        ys = [p[1] for p in s.points]
        color = _COLORS[i % len(_COLORS)]
        ax.plot(xs, ys, label=s.name, color=color, linewidth=1.6)
        if len(series) == 1:
            ax.fill_between(xs, ys, alpha=0.12, color=color)
        peak = max(range(len(ys)), key=ys.__getitem__)
        ax.plot([xs[peak]], [ys[peak]], "o", color=color, markersize=4)

    if panel.threshold:
        ax.axhline(panel.threshold, color="#E45756", linestyle="--", linewidth=1,
                   alpha=0.8)
    top = panel.y_max
    if top is None:
        top = max(p[1] for s in series for p in s.points) * 1.15 or 1
        if panel.threshold:
            top = max(top, panel.threshold * 1.05)
    ax.set_ylim(0, top)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=TZ, maxticks=8))
    ax.tick_params(labelsize=8)
    if len(series) > 1:
        ax.legend(fontsize=8, loc="upper left", ncols=len(series), frameon=False)


def _render(items: list[tuple[Panel, list[Series]]], suptitle: str) -> bytes:
    n = len(items)
    cols = 1 if n == 1 else 2
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(
        rows, cols, figsize=(10 if n == 1 else 14, 4.2 if n == 1 else 3.6 * rows),
        squeeze=False,
    )
    flat = [ax for row in axes for ax in row]
    for ax, (panel, series) in zip(flat, items):
        _draw(ax, panel, series)
    for ax in flat[n:]:
        ax.axis("off")
    fig.suptitle(suptitle, fontsize=13, fontweight="bold", x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def _caption_line(panel: Panel, series: list[Series]) -> str:
    if not series:
        return f"{panel.title}: нет данных"
    parts = []
    for s in series:
        vals = [p[1] for p in s.points]
        avg = sum(vals) / len(vals)
        peak_i = max(range(len(vals)), key=vals.__getitem__)
        f = panel.fmt.format
        label = f"{s.name}: " if len(series) > 1 else ""
        parts.append(
            f"{label}ср {f(avg)}{panel.unit}, пик {f(vals[peak_i])}{panel.unit} "
            f"в {s.points[peak_i][0].strftime('%H:%M')}"
        )
    return f"<b>{panel.title}</b> — " + "; ".join(parts)


def _window_label(hours: float) -> str:
    return f"{hours:g} ч"


async def build_chart(
    prom: PromClient, keys: list[str], hours: float
) -> tuple[bytes, str]:
    """PNG + HTML caption for the given panel keys over the last `hours`."""
    panels = _panels()
    keys = [k for k in keys if k in panels]
    results = await asyncio.gather(
        *(_fetch(prom, panels[k], hours) for k in keys), return_exceptions=True
    )
    items: list[tuple[Panel, list[Series]]] = []
    for k, res in zip(keys, results):
        if isinstance(res, Exception):
            logger.warning("Chart query failed for %s: %s", k, res)
            res = []
        items.append((panels[k], res))

    now = datetime.now(TZ)
    title = f"Нагрузка за {_window_label(hours)} · до {now.strftime('%d.%m %H:%M')}"
    png = await asyncio.to_thread(_render, items, title)
    caption = f"<b>📊 {title}</b>\n\n" + "\n".join(
        _caption_line(p, s) for p, s in items
    )
    return png, caption[:1024]
