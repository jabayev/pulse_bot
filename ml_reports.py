"""Reports over the ML application metrics (PromQL).

Every report degrades gracefully: a failed query becomes a warning line
instead of breaking the whole command.
"""

import logging
import re
from html import escape

from prom import PromClient, Sample, sample_name

logger = logging.getLogger(__name__)

_WINDOW_RE = re.compile(r"^\d+[smhdw]$")
DEFAULT_WINDOW = "1h"


def parse_window(args: list[str], default: str = DEFAULT_WINDOW) -> str:
    """First arg like 1h/24h/30m, else the default."""
    if args and _WINDOW_RE.match(args[0].strip().lower()):
        return args[0].strip().lower()
    return default


def _fmt(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}K"
    if value == int(value):
        return f"{int(value)}"
    return f"{value:.1f}"


def _group(samples: list[Sample], candidates: tuple) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for sample in samples:
        key = sample_name(sample.labels, candidates)
        grouped[key] = grouped.get(key, 0.0) + sample.value
    return grouped


async def _try(prom: PromClient, expr: str) -> list[Sample]:
    try:
        return await prom.query(expr)
    except Exception as e:
        logger.warning("PromQL failed: %s — %s", expr, e)
        raise


# --- /cams -----------------------------------------------------------------

async def cams_report(prom: PromClient) -> str:
    try:
        status = await _try(prom, "ml_stream_status")
    except Exception as e:
        return f"⚠️ Prometheus недоступен или нет метрики ml_stream_status: {escape(str(e))}"

    if not status:
        return "❔ Метрика ml_stream_status пуста — ML-платформа не отдаёт стримы."

    online = [s for s in status if s.value >= 1]
    offline = [s for s in status if s.value < 1]

    lines = [
        "<b>📷 Камеры (ML-стримы)</b>\n",
        f"🟢 Online: <b>{len(online)}</b> / {len(status)}",
    ]
    if offline:
        lines.append(f"🔴 Offline: <b>{len(offline)}</b>")
        for sample in offline[:15]:
            lines.append(f"    • {escape(sample_name(sample.labels))}")
        if len(offline) > 15:
            lines.append(f"    … и ещё {len(offline) - 15}")

    try:
        reconnects = await _try(
            prom, "topk(5, increase(ml_stream_reconnects_total[1h]) > 0)"
        )
        if reconnects:
            lines.append("\n🔁 <b>Top reconnects за 1ч:</b>")
            for sample in sorted(reconnects, key=lambda s: -s.value):
                lines.append(
                    f"    • {escape(sample_name(sample.labels))}: {_fmt(sample.value)}"
                )
    except Exception:
        lines.append("\n⚠️ Не удалось получить ml_stream_reconnects_total")

    return "\n".join(lines)


# --- /frames ---------------------------------------------------------------

async def frames_report(prom: PromClient, window: str) -> str:
    try:
        total = await prom.scalar(f"sum(increase(ml_frames_processed_total[{window}]))")
    except Exception as e:
        return f"⚠️ Prometheus недоступен: {escape(str(e))}"

    if total is None:
        return f"❔ Нет данных ml_frames_processed_total за {escape(window)}."

    lines = [
        f"<b>🎞 Кадры за {escape(window)}</b>\n",
        f"Всего обработано: <b>{_fmt(total)}</b>",
    ]

    try:
        per_camera = await _try(
            prom, f"topk(10, increase(ml_frames_processed_total[{window}]))"
        )
        grouped = _group(per_camera, ("camera", "cam", "camera_name", "location", "name", "id"))
        if grouped:
            lines.append("\n<b>Top камеры:</b>")
            for name, value in sorted(grouped.items(), key=lambda kv: -kv[1])[:10]:
                lines.append(f"    • {escape(name)}: {_fmt(value)}")
    except Exception:
        lines.append("\n⚠️ Разбивка по камерам недоступна")

    try:
        persons = await prom.scalar(
            f"sum(increase(ml_frames_with_persons_total[{window}]))"
        )
        faces = await prom.scalar(f"sum(increase(ml_frames_with_faces_total[{window}]))")
        extras = []
        if persons is not None:
            extras.append(f"с людьми: {_fmt(persons)}")
        if faces is not None:
            extras.append(f"с лицами: {_fmt(faces)}")
        if extras:
            lines.append("\n" + " · ".join(extras))
    except Exception:
        pass

    return "\n".join(lines)


# --- /algo -----------------------------------------------------------------

_ALGO_LABELS = ("algorithm", "algo", "type", "alert_type", "stage", "name")


async def algo_report(prom: PromClient, name: str, window: str) -> str:
    try:
        alerts = await _try(prom, f"increase(ml_algorithm_alerts_total[{window}])")
    except Exception as e:
        return f"⚠️ Prometheus недоступен: {escape(str(e))}"

    if name:
        needle = re.sub(r"[^\w\-]", "", name).lower()
        alerts = [
            s for s in alerts
            if needle in sample_name(s.labels, _ALGO_LABELS).lower()
        ]

    title = f" · {escape(name)}" if name else ""
    lines = [f"<b>🧠 Алгоритмы{title} за {escape(window)}</b>\n"]

    grouped = _group(alerts, _ALGO_LABELS)
    if grouped:
        lines.append("<b>Алерты по типам:</b>")
        for algo, value in sorted(grouped.items(), key=lambda kv: -kv[1]):
            if value > 0:
                lines.append(f"    • {escape(algo)}: {_fmt(value)}")
        if all(v == 0 for v in grouped.values()):
            lines.append("    (нет срабатываний)")
    else:
        lines.append("❔ Нет данных ml_algorithm_alerts_total.")

    try:
        inferences = await _try(
            prom, f"increase(ml_model_inferences_total[{window}])"
        )
        grouped_inf = _group(inferences, ("model",))
        if grouped_inf:
            lines.append("\n<b>Инференсы по моделям:</b>")
            for model, value in sorted(grouped_inf.items(), key=lambda kv: -kv[1])[:10]:
                lines.append(f"    • {escape(model)}: {_fmt(value)}")
    except Exception:
        lines.append("\n⚠️ ml_model_inferences_total недоступна")

    return "\n".join(lines)


# --- /face -----------------------------------------------------------------

_FACE_ORDER = ("confirmed", "unknown", "no_face", "pending")
_FACE_ICONS = {"confirmed": "✅", "unknown": "❓", "no_face": "🚫", "pending": "⏳"}


async def face_report(prom: PromClient, window: str) -> str:
    try:
        samples = await _try(prom, f"increase(ml_face_results_total[{window}])")
    except Exception as e:
        return f"⚠️ Prometheus недоступен: {escape(str(e))}"

    if not samples:
        return f"❔ Нет данных ml_face_results_total за {escape(window)}."

    grouped = _group(samples, ("outcome", "result", "status"))
    total = sum(grouped.values())
    lines = [f"<b>🙂 Face-пайплайн за {escape(window)}</b>\n"]
    ordered = [k for k in _FACE_ORDER if k in grouped]
    ordered += [k for k in sorted(grouped) if k not in _FACE_ORDER]
    for outcome in ordered:
        value = grouped[outcome]
        share = f" ({100 * value / total:.0f}%)" if total else ""
        icon = _FACE_ICONS.get(outcome, "•")
        lines.append(f"{icon} {escape(outcome)}: <b>{_fmt(value)}</b>{share}")

    if total and grouped.get("unknown", 0) / total > 0.5:
        lines.append("\n⚠️ Доля unknown выше 50% — возможна деградация распознавания.")
    return "\n".join(lines)


# --- /vlm ------------------------------------------------------------------

async def vlm_report(prom: PromClient, window: str, ollama_status: str) -> str:
    lines = ["<b>🤖 VLM / Ollama</b>\n", f"Ollama: {ollama_status}"]

    try:
        inferences = await _try(
            prom, f'increase(ml_model_inferences_total{{model=~"vlm_.*"}}[{window}])'
        )
        grouped = _group(inferences, ("model",))
        if grouped:
            lines.append(f"\n<b>Инференсы за {escape(window)}:</b>")
            for model, value in sorted(grouped.items(), key=lambda kv: -kv[1]):
                lines.append(f"    • {escape(model)}: {_fmt(value)}")
        else:
            lines.append(f"\n❔ Нет VLM-инференсов за {escape(window)}.")
    except Exception as e:
        lines.append(f"\n⚠️ Prometheus: {escape(str(e))}")

    # Optional metrics from the recommendations section — show when present.
    try:
        circuit = await prom.scalar("ml_vlm_circuit_open")
        if circuit is not None:
            state = "🔴 OPEN (VLM отключён)" if circuit >= 1 else "🟢 closed"
            lines.append(f"\nCircuit breaker: {state}")
    except Exception:
        pass
    try:
        errors = await prom.scalar(
            f"sum(increase(ml_vlm_request_errors_total[{window}]))"
        )
        if errors is not None:
            lines.append(f"Ошибки запросов к Ollama: {_fmt(errors)}")
    except Exception:
        pass

    return "\n".join(lines)


# --- /webhooks -------------------------------------------------------------

async def webhooks_report(prom: PromClient, window: str) -> str:
    try:
        delivered = await _try(
            prom, f"increase(ml_events_delivered_total[{window}])"
        )
    except Exception as e:
        return f"⚠️ Prometheus недоступен: {escape(str(e))}"

    if not delivered:
        return f"❔ Нет данных ml_events_delivered_total за {escape(window)}."

    grouped = _group(delivered, ("status", "result", "outcome"))
    total = sum(grouped.values())
    failed = sum(v for k, v in grouped.items() if "fail" in k.lower() or "error" in k.lower())

    lines = [f"<b>📨 Доставка webhooks за {escape(window)}</b>\n"]
    for status, value in sorted(grouped.items(), key=lambda kv: -kv[1]):
        share = f" ({100 * value / total:.1f}%)" if total else ""
        icon = "🔴" if ("fail" in status.lower() or "error" in status.lower()) else (
            "🟡" if "retr" in status.lower() else "🟢"
        )
        lines.append(f"{icon} {escape(status)}: <b>{_fmt(value)}</b>{share}")

    if total:
        lines.append(f"\nДоля неудачных: <b>{100 * failed / total:.1f}%</b>")

    try:
        generated = await prom.scalar(
            f"sum(increase(ml_events_generated_total[{window}]))"
        )
        if generated is not None:
            lines.append(f"Сгенерировано событий: {_fmt(generated)}")
    except Exception:
        pass

    try:
        latency = await prom.scalar(
            f"sum(rate(ml_event_delivery_duration_seconds_sum[{window}]))"
            f" / sum(rate(ml_event_delivery_duration_seconds_count[{window}]))"
        )
        if latency is not None:
            lines.append(f"Средняя задержка доставки: {latency:.2f} с")
    except Exception:
        pass

    return "\n".join(lines)
