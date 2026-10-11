"""GPU state via DCGM exporter metrics in Prometheus."""

import asyncio
import logging
from dataclasses import dataclass
from html import escape
from typing import Optional

from config import EXPECTED_GPU_COUNT, GPU_TEMP_CRIT, GPU_TEMP_WARN
from prom import PromClient

logger = logging.getLogger(__name__)


@dataclass
class GpuStat:
    index: str
    name: str = ""
    temp: Optional[float] = None
    util: Optional[float] = None
    fb_used_mib: Optional[float] = None
    fb_total_mib: Optional[float] = None
    power_w: Optional[float] = None
    pcie_gen: Optional[float] = None
    pcie_width: Optional[float] = None

    @property
    def vram_percent(self) -> Optional[float]:
        if self.fb_used_mib is None or not self.fb_total_mib:
            return None
        return 100.0 * self.fb_used_mib / self.fb_total_mib


async def _by_gpu(prom: PromClient, expr: str) -> dict[str, tuple[float, dict]]:
    result: dict[str, tuple[float, dict]] = {}
    for sample in await prom.query(expr):
        index = str(sample.labels.get("gpu", sample.labels.get("GPU", "?")))
        result[index] = (sample.value, sample.labels)
    return result


async def collect_gpus(prom: PromClient) -> list[GpuStat]:
    (
        temp, util, fb_used, fb_free, power, pcie_gen, pcie_width, fb_reserved,
    ) = await asyncio.gather(
        _by_gpu(prom, "DCGM_FI_DEV_GPU_TEMP"),
        _by_gpu(prom, "DCGM_FI_DEV_GPU_UTIL"),
        _by_gpu(prom, "DCGM_FI_DEV_FB_USED"),
        _by_gpu(prom, "DCGM_FI_DEV_FB_FREE"),
        _by_gpu(prom, "DCGM_FI_DEV_POWER_USAGE"),
        _by_gpu(prom, "DCGM_FI_DEV_PCIE_LINK_GEN"),
        _by_gpu(prom, "DCGM_FI_DEV_PCIE_LINK_WIDTH"),
        _by_gpu(prom, "DCGM_FI_DEV_FB_RESERVED"),
        return_exceptions=True,
    )

    def safe(data) -> dict:
        return data if isinstance(data, dict) else {}

    temp, util, fb_used, fb_free, power = (
        safe(temp), safe(util), safe(fb_used), safe(fb_free), safe(power),
    )
    pcie_gen, pcie_width = safe(pcie_gen), safe(pcie_width)
    fb_reserved = safe(fb_reserved)

    indices = sorted(
        set(temp) | set(util) | set(fb_used) | set(power),
        key=lambda i: (not i.isdigit(), int(i) if i.isdigit() else i),
    )

    gpus: list[GpuStat] = []
    for index in indices:
        stat = GpuStat(index=index)
        for source, attr in (
            (temp, "temp"), (util, "util"), (fb_used, "fb_used_mib"),
            (power, "power_w"), (pcie_gen, "pcie_gen"), (pcie_width, "pcie_width"),
        ):
            if index in source:
                setattr(stat, attr, source[index][0])
        if index in fb_used and index in fb_free:
            # nvidia-smi's total also includes the driver-reserved slice; without
            # it the % is overstated vs what people see in nvidia-smi.
            reserved = fb_reserved.get(index, (0.0, {}))[0]
            stat.fb_total_mib = fb_used[index][0] + fb_free[index][0] + reserved
        labels = (temp.get(index) or util.get(index) or (None, {}))[1]
        stat.name = str(labels.get("modelName", labels.get("model_name", "")))
        gpus.append(stat)
    return gpus


def _temp_icon(temp: Optional[float]) -> str:
    if temp is None:
        return "❔"
    if temp >= GPU_TEMP_CRIT:
        return "🚨"
    if temp >= GPU_TEMP_WARN:
        return "🔥"
    return "🟢"


def _mib(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value / 1024:.1f} GB"


def summary_lines(gpus: list[GpuStat]) -> list[str]:
    """Short one-liner per GPU for /status and digests."""
    lines = []
    for gpu in gpus:
        temp = f"{gpu.temp:.0f}°C" if gpu.temp is not None else "—"
        util = f"{gpu.util:.0f}%" if gpu.util is not None else "—"
        vram = (
            f"{_mib(gpu.fb_used_mib)}/{_mib(gpu.fb_total_mib)}"
            if gpu.fb_used_mib is not None
            else "—"
        )
        lines.append(
            f"{_temp_icon(gpu.temp)} GPU{escape(gpu.index)}: {temp} · util {util} · VRAM {vram}"
        )
    return lines


def render_gpus(gpus: list[GpuStat]) -> str:
    if not gpus:
        return (
            "❔ Нет данных DCGM в Prometheus.\n"
            "Проверьте dcgm-exporter и scrape-конфиг (job nvidia-gpu)."
        )

    lines = ["<b>🎮 GPU</b>\n"]
    for gpu in gpus:
        name = f" · {escape(gpu.name)}" if gpu.name else ""
        temp = f"{gpu.temp:.0f}°C" if gpu.temp is not None else "—"
        util = f"{gpu.util:.0f}%" if gpu.util is not None else "—"
        power = f"{gpu.power_w:.0f} Вт" if gpu.power_w is not None else "—"
        vram_pct = f" ({gpu.vram_percent:.0f}%)" if gpu.vram_percent is not None else ""
        pcie = ""
        if gpu.pcie_gen is not None and gpu.pcie_width is not None:
            pcie = f"\n    PCIe: Gen{gpu.pcie_gen:.0f} x{gpu.pcie_width:.0f}"
        lines.append(
            f"{_temp_icon(gpu.temp)} <b>GPU{escape(gpu.index)}</b>{name}\n"
            f"    🌡 {temp} (warn {GPU_TEMP_WARN:g}° / crit {GPU_TEMP_CRIT:g}°)\n"
            f"    ⚙️ Util: {util} · ⚡ {power}\n"
            f"    🧠 VRAM: {_mib(gpu.fb_used_mib)} / {_mib(gpu.fb_total_mib)}{vram_pct}"
            f"{pcie}"
        )

    if len(gpus) < EXPECTED_GPU_COUNT:
        lines.append(
            f"\n🚨 <b>Видно {len(gpus)} из {EXPECTED_GPU_COUNT} GPU</b> — "
            f"возможен отвал карты!"
        )
    return "\n\n".join(lines)
