import asyncio
import logging
from html import escape

import aiohttp
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import alerts
import am_webhook
import ml_reports
import charts
from access import restricted
from am_client import AlertmanagerClient, format_duration, parse_duration
from camera_monitor import check_all_cameras, check_random_camera
from camera_source import CameraRegistry
from config import (
    ALERT_THRESHOLDS,
    ALERTMANAGER_URL,
    CAMERA_CHECK_TIMEOUT,
    CAMERA_MONITOR_ENABLED,
    CAMERA_SOURCE,
    CAMERAS,
    CAMERAS_NOTIFY_CHANGES,
    CAMERAS_REFRESH_INTERVAL,
    CHARTS_DAILY_HOURS,
    CHARTS_ENABLED,
    CHARTS_TIMES,
    CHECK_INTERVAL,
    COMPOSE_PROJECT,
    DIGEST_ENABLED,
    DIGEST_TIMES,
    DOCKER_CHECK_INTERVAL,
    DOCKER_CRITICAL_CONTAINERS,
    DOCKER_DOWN_SECONDS,
    DOCKER_ENABLED,
    DOCKER_RESTART_THRESHOLD,
    DOCKER_RESTART_WINDOW,
    DOCKER_SOCKET,
    EXPECTED_GPU_COUNT,
    GPU_ENABLED,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_URL,
    METRICS_CHECK_INTERVAL,
    METRICS_CONFIG,
    METRICS_ENABLED,
    OLLAMA_ENABLED,
    SERVICES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_STATS_THREAD_ID,
    WEBHOOK_ENABLED,
)
from containers import ContainerWatcher, DockerClient, render_containers
from digest import build_digest
from gpu import collect_gpus, render_gpus, summary_lines
from metrics import (
    ServerMetrics,
    collect_metrics,
    format_bytes,
    format_rate,
    format_uptime,
)
from monitor import check_services, service_states
from notifier import (
    init_bot,
    notify_alert,
    notify_camera_down,
    notify_camera_up,
    notify_cameras_changed,
    notify_down,
    notify_error,
    notify_up,
    now_local,
    now_local_str,
    send_photo,
    send_text,
)
from prom import PromClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Seeded from CAMERAS; replaced by CAMERAS_URL contents when that is configured.
camera_registry = CameraRegistry(CAMERAS)

prom = PromClient(METRICS_CONFIG.prometheus_url, METRICS_CONFIG.prometheus_timeout)
am = AlertmanagerClient(ALERTMANAGER_URL)
docker = (
    DockerClient(DOCKER_SOCKET, COMPOSE_PROJECT) if DOCKER_ENABLED else None
)
container_watcher = ContainerWatcher(
    DOCKER_CRITICAL_CONTAINERS,
    DOCKER_DOWN_SECONDS,
    DOCKER_RESTART_THRESHOLD,
    DOCKER_RESTART_WINDOW,
)
webhook_server = am_webhook.WebhookServer() if WEBHOOK_ENABLED else None

# Digest scheduler: (date, "HH:MM") already fired.
_digest_sent: set[tuple[str, str]] = set()
_charts_sent: set[tuple[str, str]] = set()


# --- Jobs ------------------------------------------------------------------

async def _monitoring_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        events = await check_services(SERVICES)
        for ev in events:
            svc = ev["service"]
            if ev["event"] == "down":
                await notify_down(svc.name, svc.url, svc.last_error or "unknown")
            else:
                await notify_up(svc.name, svc.url, svc.response_time_ms)

        if not CAMERA_MONITOR_ENABLED:
            return

        camera_events = await check_random_camera(
            camera_registry.cameras,
            CAMERA_CHECK_TIMEOUT,
        )
        for ev in camera_events:
            camera = ev["camera"]
            if ev["event"] == "down":
                await notify_camera_down(
                    camera.name,
                    camera.display_url,
                    camera.last_error or "unknown",
                )
            else:
                await notify_camera_up(
                    camera.name,
                    camera.display_url,
                    camera.response_time_ms,
                )
    except Exception as e:
        logger.exception("Monitoring job failed, next cycle will continue")
        await notify_error("Проверка сервисов", f"{type(e).__name__}: {e}")


async def _metrics_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        metrics = await collect_metrics(METRICS_CONFIG)
        for event in alerts.evaluate(metrics, ALERT_THRESHOLDS):
            await notify_alert(event)
    except Exception as e:
        logger.exception("Metrics job failed, next cycle will continue")
        await notify_error("Сбор метрик", f"{type(e).__name__}: {e}")


async def _camera_refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if CAMERA_SOURCE is None:
        return
    try:
        diff = await camera_registry.refresh(CAMERA_SOURCE)
        if diff and diff.changed and CAMERAS_NOTIFY_CHANGES:
            await notify_cameras_changed(
                diff.added, diff.removed, len(camera_registry)
            )
    except Exception as e:
        logger.exception("Camera refresh failed, next cycle will continue")
        await notify_error("Обновление списка камер", f"{type(e).__name__}: {e}")


async def _containers_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Critical containers: down / restart-loop / recovery notifications."""
    if docker is None:
        return
    try:
        containers = await docker.list_containers()
    except Exception as e:
        logger.warning("Docker API unreachable: %s", e)
        await notify_error("Docker API", f"{type(e).__name__}: {e}")
        return

    for event in container_watcher.evaluate(containers):
        name = escape(event.name)
        detail = escape(event.detail)
        if event.kind in ("down", "missing"):
            what = "не найден" if event.kind == "missing" else "упал"
            await send_text(
                f"🚨 CRITICAL | <b>ContainerDown</b>\n"
                f"container: {name}\n"
                f"состояние: {what} ({detail})\n"
                f"время: {now_local_str()}",
                mention_admins=True,
            )
        elif event.kind == "restart_loop":
            await send_text(
                f"🚨 CRITICAL | <b>ContainerRestartLoop</b>\n"
                f"container: {name}\n"
                f"{detail}\n"
                f"время: {now_local_str()}",
                mention_admins=True,
            )
        elif event.kind == "up":
            await send_text(
                f"✅ RESOLVED | <b>ContainerDown</b>\n"
                f"container: {name} снова запущен ({detail})"
            )


async def _digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires the digest at the configured local times (checked every 30s)."""
    if not DIGEST_ENABLED:
        return
    now = now_local()
    current = now.strftime("%H:%M")
    if current not in DIGEST_TIMES:
        return
    key = (now.strftime("%Y-%m-%d"), current)
    if key in _digest_sent:
        return
    _digest_sent.add(key)
    if len(_digest_sent) > 100:
        _digest_sent.clear()
        _digest_sent.add(key)
    try:
        text = await build_digest(prom, docker)
        await send_text(text, thread_id=TELEGRAM_STATS_THREAD_ID)
    except Exception as e:
        logger.exception("Digest failed")
        await notify_error("Дайджест", f"{type(e).__name__}: {e}")


async def _charts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily chart report at the configured local times (checked every 30s)."""
    now = now_local()
    current = now.strftime("%H:%M")
    if current not in CHARTS_TIMES:
        return
    key = (now.strftime("%Y-%m-%d"), current)
    if key in _charts_sent:
        return
    _charts_sent.add(key)
    if len(_charts_sent) > 100:
        _charts_sent.clear()
        _charts_sent.add(key)
    try:
        png, caption = await charts.build_chart(
            prom, list(charts.PANEL_ORDER), CHARTS_DAILY_HOURS
        )
        await send_photo(png, caption, thread_id=TELEGRAM_STATS_THREAD_ID)
    except Exception as e:
        logger.exception("Daily charts failed")
        await notify_error("Графики", f"{type(e).__name__}: {e}")


async def _heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HEARTBEAT_URL:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(HEARTBEAT_URL) as resp:
                await resp.read()
    except Exception as e:
        logger.warning("Heartbeat ping failed: %s", e)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error:
        logger.error("Telegram handler failed: %s", context.error, exc_info=context.error)
        await notify_error(
            "Обработчик команды",
            f"{type(context.error).__name__}: {context.error}",
        )
    else:
        logger.error("Telegram handler failed without exception details")
        await notify_error("Обработчик команды", "Ошибка без деталей")


# --- Rendering -------------------------------------------------------------

def _plural_cores(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "ядер"
    return {1: "ядро", 2: "ядра", 3: "ядра", 4: "ядра"}.get(count % 10, "ядер")


def _render_metrics(metrics: ServerMetrics) -> str:
    def pct(value):
        return f"{value:.1f}%" if value is not None else "—"

    cores = ""
    if metrics.cpu_cores:
        count = int(metrics.cpu_cores)
        cores = f" · {count} {_plural_cores(count)}"
    mem_detail = ""
    if metrics.memory_used_bytes is not None and metrics.memory_total_bytes is not None:
        mem_detail = (
            f" · {format_bytes(metrics.memory_used_bytes)}"
            f" / {format_bytes(metrics.memory_total_bytes)}"
        )
    disk_detail = ""
    if metrics.disk_used_bytes is not None and metrics.disk_total_bytes is not None:
        disk_detail = (
            f" · {format_bytes(metrics.disk_used_bytes)}"
            f" / {format_bytes(metrics.disk_total_bytes)}"
        )

    lines = [
        "<b>📊 Метрики сервера</b>\n",
        f"🖥 <b>CPU:</b> {pct(metrics.cpu_percent)}{cores}",
        f"🧠 <b>Память:</b> {pct(metrics.memory_percent)}{mem_detail}",
        f"💽 <b>Диск ({escape(METRICS_CONFIG.disk_mountpoint)}):</b> "
        f"{pct(metrics.disk_percent)}{disk_detail}",
    ]
    if metrics.swap_percent is not None:
        lines.append(f"♻️ <b>Swap:</b> {pct(metrics.swap_percent)}")

    if metrics.load1 is not None:
        per_core = metrics.load_per_core
        suffix = f" ({per_core:.2f} на ядро)" if per_core is not None else ""
        loads = " / ".join(
            f"{v:.2f}" if v is not None else "—"
            for v in (metrics.load1, metrics.load5, metrics.load15)
        )
        lines.append(f"⚖️ <b>Load average:</b> {loads}{suffix}")

    if metrics.net_rx_bytes_sec is not None or metrics.net_tx_bytes_sec is not None:
        lines.append(
            f"🌐 <b>Сеть:</b> ↓ {format_rate(metrics.net_rx_bytes_sec)}"
            f" · ↑ {format_rate(metrics.net_tx_bytes_sec)}"
        )
    if metrics.uptime_seconds is not None:
        lines.append(f"⏱ <b>Uptime:</b> {format_uptime(metrics.uptime_seconds)}")

    firing = alerts.active_alerts()
    if firing:
        labels = {
            "cpu": "CPU",
            "memory": "Память",
            "disk": "Диск",
            "swap": "Swap",
            "load": "Load average",
        }
        active = ", ".join(labels.get(key, key) for key in firing)
        lines.append(f"\n🔥 <b>Активные алерты:</b> {escape(active)}")

    for error in metrics.errors:
        lines.append(f"⚠️ {escape(error)}")

    lines.append(f"\n🕐 {metrics.collected_at} · источник: {escape(metrics.source)}")
    return "\n".join(lines)


async def _ollama_status() -> str:
    if not OLLAMA_ENABLED:
        return "⚪️ отключён (OLLAMA_ENABLED=0)"
    if docker is not None:
        try:
            for c in await docker.list_containers():
                if "ollama" in c.name.lower() or "ollama" in c.container_name.lower():
                    if c.is_running:
                        return f"🟢 up ({escape(c.status)})"
                    return f"🔴 {escape(c.status or c.state)}"
        except Exception:
            pass
    try:
        value = await prom.scalar('max(up{job=~".*ollama.*"})')
        if value is not None:
            return "🟢 up" if value >= 1 else "🔴 down"
    except Exception:
        pass
    return "❔ нет данных"


# --- Commands: сводка ------------------------------------------------------

@restricted(admin_only=False)
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"<b>🩺 Статус системы</b> · {now_local_str()}\n"]

    async def containers_part() -> list[str]:
        if docker is None:
            return ["🐳 Docker: мониторинг отключён"]
        try:
            containers = await docker.list_containers()
        except Exception as e:
            return [f"🐳 Docker: недоступен ({escape(str(e))})"]
        running = sum(1 for c in containers if c.is_running)
        icon = "🟢" if running == len(containers) and containers else "🟡"
        part = [f"{icon} Контейнеры: {running}/{len(containers)} запущено"]
        for c in containers:
            if not c.is_running:
                part.append(f"    🔴 {escape(c.name)} — {escape(c.status or c.state)}")
        return part

    async def gpu_part() -> list[str]:
        if not GPU_ENABLED:
            return []
        try:
            gpus = await collect_gpus(prom)
        except Exception:
            return ["🎮 GPU: нет данных DCGM"]
        if not gpus:
            return ["🎮 GPU: нет данных DCGM"]
        part = summary_lines(gpus)
        if len(gpus) < EXPECTED_GPU_COUNT:
            part.append(f"🚨 Видно {len(gpus)} из {EXPECTED_GPU_COUNT} GPU!")
        return part

    async def prom_part() -> list[str]:
        up = await prom.is_up()
        return [f"{'🟢' if up else '🔴'} Prometheus: {'up' if up else 'down'}"]

    async def am_part() -> list[str]:
        try:
            active = await am.active_alerts()
            if active:
                return [f"🔥 Активных алертов в AM: <b>{len(active)}</b> — /alerts"]
            return ["🟢 Alertmanager: активных алертов нет"]
        except Exception:
            return ["❔ Alertmanager: недоступен"]

    parts = await asyncio.gather(
        containers_part(), gpu_part(), prom_part(), am_part()
    )
    for part in parts:
        lines.extend(part)

    lines.append(f"🤖 Ollama: {await _ollama_status()}")

    if service_states:
        lines.append("")
        for state in service_states.values():
            icon = "🟢" if state.is_up else "🔴"
            rt = f" · {state.response_time_ms} ms" if state.response_time_ms else ""
            lines.append(f"{icon} {escape(state.name)}{rt}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted()
async def cmd_gpus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        gpus = await collect_gpus(prom)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Prometheus недоступен: {e}")
        return
    await update.message.reply_text(render_gpus(gpus), parse_mode=ParseMode.HTML)


@restricted()
async def cmd_containers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if docker is None:
        await update.message.reply_text(
            "Мониторинг Docker отключён — проверьте DOCKER_ENABLED и "
            "монтирование /var/run/docker.sock."
        )
        return
    try:
        containers = await docker.list_containers()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Docker API недоступен: {e}")
        return
    await update.message.reply_text(
        render_containers(containers, COMPOSE_PROJECT), parse_mode=ParseMode.HTML
    )


@restricted()
async def cmd_cams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await ml_reports.cams_report(prom)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted()
async def cmd_frames(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    window = ml_reports.parse_window(context.args, "1h")
    text = await ml_reports.frames_report(prom, window)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted()
async def cmd_algo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = list(context.args or [])
    name = ""
    if args and not ml_reports.parse_window([args[0]], "") :
        name = args.pop(0)
    window = ml_reports.parse_window(args, "24h")
    text = await ml_reports.algo_report(prom, name, window)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted()
async def cmd_face(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    window = ml_reports.parse_window(context.args, "24h")
    text = await ml_reports.face_report(prom, window)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted()
async def cmd_vlm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    window = ml_reports.parse_window(context.args, "24h")
    ollama = await _ollama_status()
    text = await ml_reports.vlm_report(prom, window, ollama)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted()
async def cmd_webhooks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    window = ml_reports.parse_window(context.args, "1h")
    text = await ml_reports.webhooks_report(prom, window)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- Commands: Alertmanager ------------------------------------------------

@restricted(admin_only=False)
async def cmd_am_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        active = await am.active_alerts()
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Alertmanager недоступен ({e}).\n"
            f"URL: {ALERTMANAGER_URL}"
        )
        return

    if not active:
        await update.message.reply_text("🟢 Активных алертов нет.")
        return

    blocks = []
    for alert in active[:15]:
        # Alertmanager v2 payload has the same labels/annotations shape.
        alert.setdefault("status", "firing")
        _, text = am_webhook.format_alert(alert)
        blocks.append(text)
    header = f"<b>🔥 Активные алерты: {len(active)}</b>\n"
    if len(active) > 15:
        blocks.append(f"… и ещё {len(active) - 15}")
    await update.message.reply_text(
        header + "\n\n".join(blocks), parse_mode=ParseMode.HTML
    )


@restricted()
async def cmd_silence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование: /silence <alertname> [длительность]\n"
            "Например: /silence GpuTempHigh 2h (по умолчанию 1h)"
        )
        return

    alertname = args[0]
    duration = parse_duration(args[1]) if len(args) > 1 else 3600
    if duration is None:
        await update.message.reply_text(
            f"Не понял длительность '{args[1]}'. Примеры: 30m, 2h, 1d."
        )
        return

    user = update.effective_user
    created_by = user.full_name or user.username or str(user.id)
    try:
        silence_id = await am.create_silence(alertname, duration, created_by)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось создать silence: {e}")
        return

    await update.message.reply_text(
        f"🔇 Silence на <b>{escape(alertname)}</b> на {format_duration(duration)}\n"
        f"Создал: {escape(created_by)} · ID: <code>{escape(silence_id)}</code>",
        parse_mode=ParseMode.HTML,
    )


@restricted()
async def cmd_ack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /ack <alertname>")
        return
    alertname = args[0]
    user = update.effective_user
    who = user.full_name or user.username or str(user.id)
    am_webhook.acks[alertname] = (who, now_local_str())
    await update.message.reply_text(
        f"🛠 <b>{escape(who)}</b> взял(а) в работу <b>{escape(alertname)}</b>",
        parse_mode=ParseMode.HTML,
    )


@restricted()
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    window = ml_reports.parse_window(context.args, "12h")
    await update.message.reply_text("Собираю дайджест...")
    text = await build_digest(prom, docker, window)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- Commands: графики ------------------------------------------------------

_CHART_WINDOWS = (6, 12, 24)


def _charts_keyboard(hours: int) -> InlineKeyboardMarkup:
    windows = [
        InlineKeyboardButton(
            f"{'✅ ' if h == hours else ''}{h} ч", callback_data=f"chw:{h}"
        )
        for h in _CHART_WINDOWS
    ]
    panels = [
        InlineKeyboardButton(label, callback_data=f"ch:{key}:{hours}")
        for label, key in charts.panel_buttons()
    ]
    rows = [
        [InlineKeyboardButton("📊 Все графики", callback_data=f"ch:all:{hours}")],
        panels[0:3],
        panels[3:6],
        windows,
    ]
    return InlineKeyboardMarkup(rows)


@restricted(admin_only=False)
async def cmd_charts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hours = 24
    if context.args and context.args[0].lower().rstrip("h").isdigit():
        hours = min(int(context.args[0].lower().rstrip("h")), 72)
    await update.message.reply_text(
        f"<b>📈 Графики</b> · окно {hours} ч\nВыбери график:",
        parse_mode=ParseMode.HTML,
        reply_markup=_charts_keyboard(hours),
    )


@restricted(admin_only=False)
async def on_charts_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    try:
        if parts[0] == "chw":
            hours = int(parts[1])
            await query.answer()
            await query.edit_message_text(
                f"<b>📈 Графики</b> · окно {hours} ч\nВыбери график:",
                parse_mode=ParseMode.HTML,
                reply_markup=_charts_keyboard(hours),
            )
            return
        key, hours = parts[1], int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    await query.answer("Рисую график...")
    keys = list(charts.PANEL_ORDER) if key == "all" else [key]
    try:
        png, caption = await charts.build_chart(prom, keys, hours)
    except Exception as e:
        logger.exception("Chart failed")
        await query.message.reply_text(f"⚠️ Не удалось построить график: {e}")
        return
    await query.message.reply_photo(
        photo=png, caption=caption, parse_mode=ParseMode.HTML
    )


# --- Commands: существующий функционал -------------------------------------

@restricted()
async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not METRICS_ENABLED:
        await update.message.reply_text("Сбор метрик сервера отключён.")
        return

    await update.message.reply_text("Собираю метрики...")
    metrics = await collect_metrics(METRICS_CONFIG)
    await update.message.reply_text(_render_metrics(metrics), parse_mode=ParseMode.HTML)


@restricted()
async def cmd_thresholds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = ALERT_THRESHOLDS

    def rule(label: str, value, unit: str = "%") -> str:
        if not value:
            return f"  • {label}: отключено"
        return f"  • {label}: <b>{value:g}{unit}</b>"

    lines = [
        "<b>⚙️ Пороги алертов бота</b>\n",
        rule("CPU", t.cpu_percent),
        rule("Память", t.memory_percent),
        rule("Диск", t.disk_percent),
        rule("Swap", t.swap_percent),
        rule("Load average на ядро", t.load_per_core, ""),
        "",
        f"Срабатывает после <b>{t.trigger_count}</b> превышений подряд,",
        f"снимается после <b>{t.recovery_count}</b> нормальных проверок.",
        f"Повтор напоминания: "
        + (f"каждые <b>{t.repeat_minutes} мин</b>" if t.repeat_minutes else "отключён"),
        f"\nИнтервал сбора метрик: <b>{METRICS_CHECK_INTERVAL} сек</b>",
        f"Источник: <b>{escape(METRICS_CONFIG.source)}</b>"
        + (
            f" ({escape(METRICS_CONFIG.prometheus_url)})"
            if METRICS_CONFIG.source != "psutil"
            else ""
        ),
    ]

    firing = alerts.active_alerts()
    lines.append(
        f"\n🔥 Сейчас активно: <b>{len(firing)}</b>" if firing else "\n🟢 Активных алертов нет"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted()
async def cmd_status_camera(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CAMERA_MONITOR_ENABLED:
        await update.message.reply_text("Проверка RTSP-камер отключена.")
        return
    if not camera_registry.cameras:
        await update.message.reply_text(
            "Список камер пуст."
            + (f"\n⚠️ {camera_registry.last_error}" if camera_registry.last_error else "")
        )
        return

    await update.message.reply_text("Проверяю RTSP-камеры...")
    states = await check_all_cameras(
        camera_registry.cameras,
        CAMERA_CHECK_TIMEOUT,
    )

    lines = ["<b>Статус RTSP-камер</b>\n"]
    for state in states:
        icon = "🟢" if state.is_up else "🔴"
        rt = f" · {state.response_time_ms} ms" if state.response_time_ms else ""
        err = f"\n    ⚠️ {escape(state.last_error)}" if state.last_error else ""
        lines.append(
            f"{icon} <b>{escape(state.name)}</b>{rt}\n"
            f"    {escape(state.display_url)}\n"
            f"    🕐 {state.last_checked or '—'}{err}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


@restricted()
async def cmd_refresh_cameras(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if CAMERA_SOURCE is None:
        await update.message.reply_text(
            "Динамический источник камер не настроен — задайте CAMERAS_URL."
        )
        return

    await update.message.reply_text("Обновляю список камер...")
    diff = await camera_registry.refresh(CAMERA_SOURCE)
    if diff is None:
        await update.message.reply_text(
            f"⚠️ Не удалось обновить список: {camera_registry.last_error}\n"
            f"Использую предыдущий список ({len(camera_registry)} камер)."
        )
        return

    if not diff.changed:
        await update.message.reply_text(
            f"✅ Список актуален — {len(camera_registry)} камер."
        )
        return

    await notify_cameras_changed(diff.added, diff.removed, len(camera_registry))


@restricted()
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["<b>Что отслеживается</b>\n"]

    if SERVICES:
        lines.append("<b>HTTP-сервисы:</b>")
        for i, svc in enumerate(SERVICES, 1):
            lines.append(f"{i}. <b>{escape(svc['name'])}</b>\n   {escape(svc['url'])}")
        lines.append(f"⏱ Интервал проверки: <b>{CHECK_INTERVAL} сек</b>")

    if CAMERA_MONITOR_ENABLED:
        camera_lines = "\n".join(
            f"   • {escape(cam['name'])}" for cam in camera_registry.cameras
        ) or "   (список пуст)"
        source = (
            f"   Источник: {escape(CAMERA_SOURCE.url)}\n"
            f"   Обновлён: {camera_registry.last_updated or '—'}\n"
            if CAMERA_SOURCE
            else "   Источник: .env (CAMERAS)\n"
        )
        error = (
            f"   ⚠️ {escape(camera_registry.last_error)}\n"
            if camera_registry.last_error
            else ""
        )
        lines.append(
            f"\n📷 RTSP-камеры: <b>{len(camera_registry)}</b>\n"
            f"{camera_lines}\n"
            f"{source}{error}"
            f"   Таймаут: <b>{CAMERA_CHECK_TIMEOUT:g} сек</b>"
        )
    if METRICS_ENABLED:
        lines.append(
            f"\n📊 Метрики сервера: каждые <b>{METRICS_CHECK_INTERVAL} сек</b>"
            f" · источник <b>{escape(METRICS_CONFIG.source)}</b>"
        )
    if WEBHOOK_ENABLED:
        lines.append("📡 Приёмник Alertmanager: включён")
    if docker is not None:
        lines.append(
            f"🐳 Docker ({escape(COMPOSE_PROJECT)}): каждые {DOCKER_CHECK_INTERVAL} сек · "
            f"критичные: {escape(', '.join(DOCKER_CRITICAL_CONTAINERS))}"
        )
    if DIGEST_ENABLED:
        lines.append(f"📋 Дайджест: {escape(', '.join(DIGEST_TIMES))}")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


@restricted(admin_only=False)
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "<b>BahandiML monitoring bot</b>\n",
        "<b>Сводки:</b>",
        "/status — контейнеры, GPU, Prometheus, Ollama",
        "/gpus — температура, util, VRAM, power по GPU",
        "/containers — контейнеры prod-проекта",
        "/digest [12h|24h] — сводный дайджест",
        "/charts [24h] — графики CPU/ОЗУ/GPU/VRAM/кадров по кнопкам",
        "",
        "<b>ML-пайплайн:</b>",
        "/cams — камеры online/offline, reconnects",
        "/frames [1h|24h] — обработанные кадры",
        "/algo [имя] [24h] — алерты и инференсы алгоритмов",
        "/face [1h|24h] — face-пайплайн по outcome",
        "/vlm — Ollama, VLM-инференсы, circuit breaker",
        "/webhooks [1h] — доставка событий",
        "",
        "<b>Алерты:</b>",
        "/alerts — активные алерты из Alertmanager",
        "/silence &lt;alert&gt; &lt;30m|2h&gt; — заглушить алерт",
        "/ack &lt;alert&gt; — взять в работу",
        "",
        "<b>Хост и камеры (RTSP):</b>",
        "/metrics — CPU, RAM, диск, сеть хоста",
        "/thresholds — пороги встроенных алертов",
        "/status_camera — проверить RTSP-камеры",
        "/refresh_cameras — перечитать список камер",
        "/list — что отслеживается",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --- Entry point -----------------------------------------------------------

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("status", "Сводка: контейнеры, GPU, Prometheus"),
            BotCommand("gpus", "GPU: температура, VRAM, util"),
            BotCommand("containers", "Контейнеры prod-проекта"),
            BotCommand("cams", "Камеры ML: online/offline"),
            BotCommand("frames", "Кадры за период"),
            BotCommand("algo", "Алгоритмы: алерты, инференсы"),
            BotCommand("face", "Face-пайплайн"),
            BotCommand("vlm", "VLM / Ollama"),
            BotCommand("webhooks", "Доставка событий"),
            BotCommand("alerts", "Активные алерты Alertmanager"),
            BotCommand("silence", "Заглушить алерт"),
            BotCommand("ack", "Взять алерт в работу"),
            BotCommand("digest", "Сводный дайджест"),
            BotCommand("charts", "Графики нагрузки"),
            BotCommand("metrics", "Метрики хоста"),
            BotCommand("status_camera", "Проверить RTSP-камеры"),
            BotCommand("help", "Список команд"),
        ]
    )
    if webhook_server is not None:
        await webhook_server.start()


async def _post_shutdown(app: Application) -> None:
    if webhook_server is not None:
        await webhook_server.stop()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    init_bot(app.bot)

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("gpus", cmd_gpus))
    app.add_handler(CommandHandler("containers", cmd_containers))
    app.add_handler(CommandHandler("cams", cmd_cams))
    app.add_handler(CommandHandler("frames", cmd_frames))
    app.add_handler(CommandHandler("algo", cmd_algo))
    app.add_handler(CommandHandler("face", cmd_face))
    app.add_handler(CommandHandler("vlm", cmd_vlm))
    app.add_handler(CommandHandler("webhooks", cmd_webhooks))
    app.add_handler(CommandHandler("alerts", cmd_am_alerts))
    app.add_handler(CommandHandler("silence", cmd_silence))
    app.add_handler(CommandHandler("ack", cmd_ack))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("charts", cmd_charts))
    app.add_handler(CallbackQueryHandler(on_charts_button, pattern=r"^ch[w]?:"))
    app.add_handler(CommandHandler("metrics", cmd_metrics))
    app.add_handler(CommandHandler("thresholds", cmd_thresholds))
    app.add_handler(CommandHandler("status_camera", cmd_status_camera))
    app.add_handler(CommandHandler("refresh_cameras", cmd_refresh_cameras))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_error_handler(_error_handler)

    if CAMERA_MONITOR_ENABLED and CAMERA_SOURCE is not None:
        # Runs before the first monitoring cycle so checks use a fresh list.
        app.job_queue.run_repeating(
            _camera_refresh_job,
            interval=CAMERAS_REFRESH_INTERVAL,
            first=1,
        )

    if SERVICES or CAMERA_MONITOR_ENABLED:
        app.job_queue.run_repeating(
            _monitoring_job,
            interval=CHECK_INTERVAL,
            first=5,  # первая проверка через 5 сек после старта
        )

    if METRICS_ENABLED:
        app.job_queue.run_repeating(
            _metrics_job,
            interval=METRICS_CHECK_INTERVAL,
            first=10,
        )

    if docker is not None:
        app.job_queue.run_repeating(
            _containers_job,
            interval=DOCKER_CHECK_INTERVAL,
            first=15,
        )

    if DIGEST_ENABLED:
        app.job_queue.run_repeating(_digest_job, interval=30, first=30)

    if CHARTS_ENABLED:
        app.job_queue.run_repeating(_charts_job, interval=30, first=35)

    if HEARTBEAT_URL:
        app.job_queue.run_repeating(
            _heartbeat_job, interval=HEARTBEAT_INTERVAL, first=20
        )

    logger.info(
        "pulse-bot started — %d service(s), %d camera(s), docker=%s, webhook=%s, "
        "digest=%s, metrics=%s",
        len(SERVICES),
        len(camera_registry) if CAMERA_MONITOR_ENABLED else 0,
        "on" if docker is not None else "off",
        "on" if WEBHOOK_ENABLED else "off",
        ",".join(DIGEST_TIMES) if DIGEST_ENABLED else "off",
        METRICS_CONFIG.source if METRICS_ENABLED else "off",
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
