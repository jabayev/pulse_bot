import logging
from datetime import datetime, timezone
from html import escape
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def init_bot(bot: Bot) -> None:
    """Called once from main.py to share the Application's bot instance."""
    global _bot
    _bot = bot


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _send(text: str) -> bool:
    if _bot is None:
        logger.error("Bot not initialized — call init_bot() before sending notifications")
        return False
    try:
        await _bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramError as e:
        logger.error("Failed to send Telegram notification: %s", e)
        return False
    except Exception:
        logger.exception("Unexpected error while sending Telegram notification")
        return False


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
