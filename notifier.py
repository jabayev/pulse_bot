import asyncio
import logging
import time
from datetime import datetime
from html import escape
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from config import TELEGRAM_ADMIN_MENTIONS, TELEGRAM_CHAT_ID, TZ, TZ_NAME

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def init_bot(bot: Bot) -> None:
    """Called once from main.py to share the Application's bot instance."""
    global _bot
    _bot = bot


def now_local() -> datetime:
    return datetime.now(TZ)


def now_local_str() -> str:
    # Message timezone per the spec: Asia/Almaty (+05).
    return now_local().strftime("%Y-%m-%d %H:%M:%S") + f" ({TZ_NAME.split('/')[-1]})"


# Kept as the internal alias so existing notify_* functions stay unchanged.
def _now_utc() -> str:
    return now_local_str()


async def send_text(
    text: str,
    mention_admins: bool = False,
    thread_id: Optional[int] = None,
    retries: int = 3,
) -> bool:
    """Send an HTML message to the main chat with limited retries.

    A Telegram outage must never break the monitoring loops, so after the
    retries are exhausted the message is dropped with a log record.
    """
    if _bot is None:
        logger.error("Bot not initialized — call init_bot() before sending notifications")
        return False

    if mention_admins and TELEGRAM_ADMIN_MENTIONS:
        text = " ".join(TELEGRAM_ADMIN_MENTIONS) + "\n" + text

    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            await _bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text[:4000],
                parse_mode=ParseMode.HTML,
                message_thread_id=thread_id,
            )
            return True
        except RetryAfter as e:
            wait = float(e.retry_after) + 1
            logger.warning("Telegram flood limit, waiting %.0fs", wait)
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.error(
                "Telegram send failed (attempt %d/%d): %s", attempt, retries, e
            )
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
        except Exception:
            logger.exception("Unexpected error while sending Telegram notification")
            return False

    logger.error("Message dropped after %d attempts: %.120s", retries, text)
    return False


async def _send(text: str) -> bool:
    return await send_text(text)


# --- Bot error reporting ----------------------------------------------------

# Same source+error is re-sent to the chat at most once per cooldown window,
# so a persistently failing job doesn't flood the group.
_ERROR_COOLDOWN_SEC = 600
_recent_errors: dict[str, float] = {}


async def notify_error(source: str, error: str) -> None:
    """Report an internal bot error to the Telegram chat."""
    logger.error("Bot error in %s: %s", source, error)

    key = f"{source}: {error}"
    now = time.monotonic()
    last = _recent_errors.get(key)
    if last is not None and now - last < _ERROR_COOLDOWN_SEC:
        return
    _recent_errors[key] = now
    if len(_recent_errors) > 200:
        cutoff = now - _ERROR_COOLDOWN_SEC
        for k in [k for k, ts in _recent_errors.items() if ts < cutoff]:
            del _recent_errors[k]

    text = (
        f"⚠️ <b>Ошибка бота</b>\n"
        f"\n"
        f"<b>Где:</b> {escape(source)}\n"
        f"<b>Ошибка:</b> {escape(error[:1000])}\n"
        f"<b>Время:</b> {_now_utc()}"
    )
    await _send(text)


async def notify_down(service_name: str, url: str, error: str) -> None:
    text = (
        f"🔴 <b>Сервис недоступен</b>\n"
        f"\n"
        f"<b>Сервис:</b> {escape(service_name)}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Ошибка:</b> {escape(error)}\n"
        f"<b>Время:</b> {_now_utc()}"
    )
    logger.warning("DOWN: %s (%s) — %s", service_name, url, error)
    await _send(text)


async def notify_up(service_name: str, url: str, response_time_ms: Optional[float]) -> None:
    rt = f"{response_time_ms} ms" if response_time_ms is not None else "—"
    text = (
        f"🟢 <b>Сервис восстановлен</b>\n"
        f"\n"
        f"<b>Сервис:</b> {escape(service_name)}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Время ответа:</b> {rt}\n"
        f"<b>Восстановлен:</b> {_now_utc()}"
    )
    logger.info("UP: %s (%s) — %s", service_name, url, rt)
    await _send(text)


async def notify_camera_down(name: str, url: str, error: str) -> None:
    text = (
        f"🔴 <b>RTSP камера недоступна</b>\n"
        f"\n"
        f"<b>Камера:</b> {escape(name)}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Ошибка:</b> {escape(error)}\n"
        f"<b>Время:</b> {_now_utc()}"
    )
    logger.warning("CAMERA DOWN: %s (%s) — %s", name, url, error)
    await _send(text)


async def notify_alert(event) -> None:
    """Send a threshold alert (alerts.AlertEvent)."""
    if event.event == "recover":
        peak = (
            f"<b>Пик:</b> {event.format_value(event.peak_value)}\n"
            if event.peak_value is not None
            else ""
        )
        text = (
            f"✅ <b>Нагрузка в норме</b>\n"
            f"\n"
            f"<b>Показатель:</b> {escape(event.label)}\n"
            f"<b>Значение:</b> {event.format_value()} (порог {event.format_threshold()})\n"
            f"{peak}"
            f"<b>Началось:</b> {event.since or '—'}\n"
            f"<b>Время:</b> {_now_utc()}"
        )
        logger.info("ALERT RESOLVED: %s = %s", event.label, event.format_value())
    else:
        title = (
            "🔥 <b>Высокая нагрузка</b>"
            if event.event == "fire"
            else "⏳ <b>Нагрузка всё ещё высокая</b>"
        )
        since = f"<b>Началось:</b> {event.since}\n" if event.since else ""
        text = (
            f"{title}\n"
            f"\n"
            f"<b>Показатель:</b> {escape(event.label)}\n"
            f"<b>Значение:</b> {event.format_value()} (порог {event.format_threshold()})\n"
            f"{since}"
            f"<b>Время:</b> {_now_utc()}"
        )
        logger.warning("ALERT: %s = %s", event.label, event.format_value())

    await _send(text)


async def notify_cameras_changed(added: list[str], removed: list[str], total: int) -> None:
    parts = ["📷 <b>Список камер обновлён</b>\n"]
    if added:
        parts.append(
            "<b>Добавлены:</b>\n" + "\n".join(f"  ➕ {escape(n)}" for n in added)
        )
    if removed:
        parts.append(
            "<b>Удалены:</b>\n" + "\n".join(f"  ➖ {escape(n)}" for n in removed)
        )
    parts.append(f"<b>Всего камер:</b> {total}\n<b>Время:</b> {_now_utc()}")

    logger.info("Camera list changed: +%d / -%d", len(added), len(removed))
    await _send("\n".join(parts))


async def notify_camera_up(name: str, url: str, response_time_ms: Optional[float]) -> None:
    rt = f"{response_time_ms} ms" if response_time_ms is not None else "—"
    text = (
        f"🟢 <b>RTSP камера восстановлена</b>\n"
        f"\n"
        f"<b>Камера:</b> {escape(name)}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Время ответа:</b> {rt}\n"
        f"<b>Восстановлена:</b> {_now_utc()}"
    )
    logger.info("CAMERA UP: %s (%s) — %s", name, url, rt)
    await _send(text)
