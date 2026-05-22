import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import CHECK_INTERVAL, SERVICES, TELEGRAM_BOT_TOKEN
from monitor import check_services, service_states
from notifier import init_bot, notify_down, notify_up

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- Job -------------------------------------------------------------------

async def _monitoring_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    events = await check_services(SERVICES)
    for ev in events:
        svc = ev["service"]
        if ev["event"] == "down":
            await notify_down(svc.name, svc.url, svc.last_error or "unknown")
        else:
            await notify_up(svc.name, svc.url, svc.response_time_ms)


# --- Commands --------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not service_states:
        await update.message.reply_text("Проверки ещё не запускались. Подождите первого цикла.")
        return

    lines = ["<b>Статус сервисов</b>\n"]
    for state in service_states.values():
        icon = "🟢" if state.is_up else "🔴"
        rt = f" · {state.response_time_ms} ms" if state.response_time_ms else ""
        err = f"\n    ⚠️ {state.last_error}" if state.last_error else ""
        lines.append(
            f"{icon} <b>{state.name}</b>{rt}\n"
            f"    {state.url}\n"
            f"    🕐 {state.last_checked or '—'}{err}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["<b>Отслеживаемые сервисы</b>\n"]
    for i, svc in enumerate(SERVICES, 1):
        lines.append(f"{i}. <b>{svc['name']}</b>\n   {svc['url']}")

    lines.append(f"\n⏱ Интервал проверки: <b>{CHECK_INTERVAL} сек</b>")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


# --- Entry point -----------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    init_bot(app.bot)

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))

    app.job_queue.run_repeating(
        _monitoring_job,
        interval=CHECK_INTERVAL,
        first=5,  # первая проверка через 5 сек после старта
    )

    logger.info(
        "pulse-bot started — %d service(s), interval=%ds",
        len(SERVICES),
        CHECK_INTERVAL,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
