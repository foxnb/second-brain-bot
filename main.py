"""
Revory - Main
Telegram bot с webhook режимом для продакшена.
"""

import logging
import os
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

from handlers.text import handle_text
from services.calendar import start_auth

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я Revory — твой личный календарный ассистент 🗓️\n\n"
        "Могу:\n"
        "• Создавать встречи — «встреча завтра в 15:00 с клиентом»\n"
        "• Показывать расписание — «что у меня сегодня?»\n"
        "• Удалять события — «удали встречу с клиентом»\n"
        "• Ставить напоминания — «напомни в 10 утра купить продукты»\n\n"
        "Для начала подключи календарь: /auth\n"
        "Потом просто пиши как думаешь!"
    )


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    auth_url = start_auth(user_id)

    await update.message.reply_text(
        "🔑 Подключаем Google Calendar!\n\n"
        "1️⃣ Перейди по ссылке:\n"
        f"{auth_url}\n\n"
        "2️⃣ Разреши доступ\n"
        "3️⃣ Скопируй код и отправь мне сюда"
    )
    context.user_data["awaiting_auth_code"] = True


def main():
    token = os.getenv("BOT_TOKEN")
    app = Application.builder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("auth", cmd_auth))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Режим запуска: webhook на сервере, polling локально
    webhook_url = os.getenv("WEBHOOK_URL")
    port = int(os.getenv("PORT", "8000"))

    if webhook_url:
        logger.info(f"Starting webhook on port {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=f"{webhook_url}/webhook",
        )
    else:
        logger.info("Starting polling (local mode)")
        app.run_polling()


if __name__ == "__main__":
    main()