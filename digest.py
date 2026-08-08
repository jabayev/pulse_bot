"""Periodic health digest (not an alert): system summary over a window."""

import logging
from html import escape
from typing import Optional

from config import EXPECTED_GPU_COUNT
from containers import DockerClient
from prom import PromClient, sample_name

logger = logging.getLogger(__name__)


def _fmt(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


async def _scalar(prom: PromClient, expr: str) -> Optional[float]:
    try:
        return await prom.scalar(expr)
    except Exception as e:
        logger.debug("digest query failed: %s — %s", expr, e)
        return None


async def build_digest(
    prom: PromClient,
    docker: Optional[DockerClient],
    window: str = "12h",
) -> str:
    lines = [f"<b>📋 Дайджест за {escape(window)}</b>\n"]

    # Containers.
    if docker is not None:
        try:
            containers = await docker.list_containers()
            running = sum(1 for c in containers if c.is_running)
            bad = [c for c in containers if not c.is_running]
            line = f"🐳 Контейнеры: {running}/{len(containers)} запущено"
            if bad:
                line += " · ⚠️ " + ", ".join(escape(c.name) for c in bad[:5])
            lines.append(line)
        except Exception as e:
            lines.append(f"🐳 Контейнеры: недоступно ({escape(str(e))})")

    # Frames / cameras.
    frames = await _scalar(prom, f"sum(increase(ml_frames_processed_total[{window}]))")
    if frames is not None:
        lines.append(f"🎞 Кадров обработано: <b>{_fmt(frames)}</b>")
    cams_online = await _scalar(prom, "sum(ml_stream_status)")
    cams_total = await _scalar(prom, "count(ml_stream_status)")
    if cams_online is not None and cams_total:
        icon = "🟢" if cams_online == cams_total else "🟡"
        lines.append(f"{icon} Камеры online: {cams_online:.0f}/{cams_total:.0f}")
    reconnects = await _scalar(
        prom, f"sum(increase(ml_stream_reconnects_total[{window}]))"
    )
    if reconnects is not None and reconnects > 0:
        lines.append(f"🔁 Reconnects: {_fmt(reconnects)}")

    # Algorithm alerts by type.
    try:
        algo = await prom.query(
            f"topk(5, sum by (algorithm, type) (increase(ml_algorithm_alerts_total[{window}])))"
        )
        nonzero = [s for s in algo if s.value >= 1]
        if nonzero:
            parts = ", ".join(
                f"{escape(sample_name(s.labels, ('algorithm', 'type', 'name')))}: {_fmt(s.value)}"
                for s in sorted(nonzero, key=lambda s: -s.value)
            )
            lines.append(f"🧠 Алерты алгоритмов: {parts}")
    except Exception:
        pass

    # Face pipeline.
    confirmed = await _scalar(
        prom, f'sum(increase(ml_face_results_total{{outcome="confirmed"}}[{window}]))'
    )
    unknown = await _scalar(
        prom, f'sum(increase(ml_face_results_total{{outcome="unknown"}}[{window}]))'
    )
    if confirmed is not None or unknown is not None:
        lines.append(
            f"🙂 Face: confirmed {_fmt(confirmed or 0)} · unknown {_fmt(unknown or 0)}"
        )

    # VLM.
    vlm = await _scalar(
        prom, f'sum(increase(ml_model_inferences_total{{model=~"vlm_.*"}}[{window}]))'
    )
    if vlm is not None:
        lines.append(f"🤖 VLM-инференсов: {_fmt(vlm)}")

    # Webhook delivery.
    try:
        delivered = await prom.query(
            f"sum by (status) (increase(ml_events_delivered_total[{window}]))"
        )
        total = sum(s.value for s in delivered)
        failed = sum(
            s.value for s in delivered
            if "fail" in str(s.labels.get("status", "")).lower()
        )
        if total:
            icon = "🟢" if failed / total < 0.05 else "🔴"
            lines.append(
                f"{icon} Webhooks: {_fmt(total)} · неудачных {100 * failed / total:.1f}%"
            )
    except Exception:
        pass

    # GPU over the window.
    try:
        max_temp = await prom.query(f"max_over_time(DCGM_FI_DEV_GPU_TEMP[{window}])")
        avg_util = await prom.query(f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{window}])")
        max_vram = await prom.query(f"max_over_time(DCGM_FI_DEV_FB_USED[{window}])")
        if max_temp:
            util_by_gpu = {s.labels.get("gpu"): s.value for s in avg_util}
            vram_by_gpu = {s.labels.get("gpu"): s.value for s in max_vram}
            for s in sorted(max_temp, key=lambda s: str(s.labels.get("gpu"))):
                gpu = s.labels.get("gpu", "?")
                util = util_by_gpu.get(gpu)
                vram = vram_by_gpu.get(gpu)
                extra = ""
                if util is not None:
                    extra += f" · avg util {util:.0f}%"
                if vram is not None:
                    extra += f" · max VRAM {vram / 1024:.1f} GB"
                lines.append(f"🎮 GPU{escape(str(gpu))}: max {s.value:.0f}°C{extra}")
            if len(max_temp) < EXPECTED_GPU_COUNT:
                lines.append(
                    f"🚨 Видно {len(max_temp)} из {EXPECTED_GPU_COUNT} GPU!"
                )
    except Exception:
        pass

    # Host headroom.
    disk_free = await _scalar(
        prom,
        'min(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} '
        '/ node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) * 100',
    )
    mem_avail = await _scalar(
        prom, "avg(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
    )
    headroom = []
    if disk_free is not None:
        headroom.append(f"диск свободно {disk_free:.0f}%")
    if mem_avail is not None:
        headroom.append(f"RAM свободно {mem_avail:.0f}%")
    if headroom:
        lines.append("💽 " + " · ".join(headroom))

    # Top-5 problem cameras (by reconnects).
    try:
        top = await prom.query(
            f"topk(5, increase(ml_stream_reconnects_total[{window}]) > 0)"
        )
        if top:
            lines.append("\n<b>Top проблемные камеры:</b>")
            for s in sorted(top, key=lambda s: -s.value):
                lines.append(
                    f"    • {escape(sample_name(s.labels))}: {_fmt(s.value)} reconnects"
                )
    except Exception:
        pass

    if len(lines) == 1:
        lines.append("❔ Данных нет — проверьте PROMETHEUS_URL.")
    return "\n".join(lines)
