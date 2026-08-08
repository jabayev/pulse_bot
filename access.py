"""Role / whitelist checks for bot commands.

Roles per the spec:
  Admin  — users from TELEGRAM_ADMIN_IDS: every command, silence/ack;
  Viewer — anyone in the whitelisted group chat: read-only commands;
  everyone else (foreign chats) is silently ignored.

Admins may also talk to the bot in private chat.
"""

import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from config import TELEGRAM_ADMIN_IDS, TELEGRAM_ALLOWED_CHAT_IDS

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    # An empty admin list means roles are not configured yet — then anyone
    # in the whitelisted chat is treated as admin (single-team setup).
    return not TELEGRAM_ADMIN_IDS or user_id in TELEGRAM_ADMIN_IDS


def restricted(admin_only: bool = True):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat = update.effective_chat
            user = update.effective_user
            message = update.effective_message
            if chat is None or user is None or message is None:
                return

            allowed_chat = chat.id in TELEGRAM_ALLOWED_CHAT_IDS
            admin_dm = chat.type == "private" and user.id in TELEGRAM_ADMIN_IDS
            if not allowed_chat and not admin_dm:
                logger.warning(
                    "Ignoring command %s from foreign chat %s (user %s)",
                    message.text, chat.id, user.id,
                )
                return

            if admin_only and not is_admin(user.id):
                await message.reply_text("⛔ Команда доступна только администраторам.")
                return

            return await func(update, context)

        return wrapper

    return decorator
