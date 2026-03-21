"""
Revory - Main
Telegram bot с webhook режимом для продакшена.
/auth/callback — endpoint для Google OAuth редиректа.
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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route
import uvicorn

from handlers.text import handle_text
from services.calendar import start_auth, finish_auth_callback
from services.database import ensure_user, get_pool

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Глобально — заполняется в main()
_telegram_app = None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await ensure_user(user.id, user.username)
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
    user = update.message.from_user
    await ensure_user(user.id, user.username)
    auth_url = start_auth(user.id)

    await update.message.reply_text(
        "🔑 Подключаем Google Calendar!\n\n"
        "1️⃣ Перейди по ссылке и разреши доступ:\n"
        f"{auth_url}\n\n"
        "После разрешения доступа Google автоматически завершит подключение — "
        "ничего копировать не нужно!"
    )


async def auth_callback(request: Request):
    """Google OAuth callback — GET /auth/callback?code=...&state=user_id"""
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return HTMLResponse("<h2>Ошибка: отсутствует code или state.</h2>", status_code=400)

    try:
        user_id = int(state)
    except ValueError:
        return HTMLResponse("<h2>Ошибка: некорректный state.</h2>", status_code=400)

    success = await finish_auth_callback(user_id, code)

    if success:
        # Отправляем сообщение пользователю в Telegram
        if _telegram_app:
            try:
                await _telegram_app.bot.send_message(
                    chat_id=user_id,
                    text="✅ Google Calendar успешно подключён! Теперь просто пиши что нужно сделать.",
                )
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")

        return HTMLResponse(
            "<h2>✅ Готово! Google Calendar подключён.</h2>"
            "<p>Можешь закрыть эту страницу и вернуться в Telegram.</p>"
        )
    else:
        return HTMLResponse(
            "<h2>❌ Что-то пошло не так.</h2>"
            "<p>Попробуй ещё раз — отправь /auth в боте.</p>",
            status_code=500,
        )


async def health(request: Request):
    return HTMLResponse("ok")


def build_starlette_app(telegram_app: Application) -> Starlette:
    """Собирает Starlette app с webhook + OAuth callback."""
    webhook_path = "/webhook"

    async def telegram_webhook(request: Request):
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return HTMLResponse("ok")

    routes = [
        Route(webhook_path, telegram_webhook, methods=["POST"]),
        Route("/auth/callback", auth_callback, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ]

    return Starlette(routes=routes)


async def on_startup(telegram_app: Application):
    await telegram_app.initialize()
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await telegram_app.bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook set: {webhook_url}/webhook")
    await get_pool()
    logger.info("DB pool initialized")
    
    # Keep-alive: пингуем себя каждые 5 минут
    if webhook_url:
        import asyncio
        import httpx
        async def keep_alive():
            while True:
                await asyncio.sleep(5 * 60)
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get(f"{webhook_url}/health", timeout=10)
                    logger.info("Keep-alive ping sent")
                except Exception as e:
                    logger.warning(f"Keep-alive failed: {e}")
        asyncio.create_task(keep_alive())
        logger.info("Keep-alive started")


async def on_shutdown(telegram_app: Application):
    await telegram_app.shutdown()


def main():
    global _telegram_app

    token = os.getenv("BOT_TOKEN")
    port = int(os.getenv("PORT", "8000"))
    webhook_url = os.getenv("WEBHOOK_URL")

    _telegram_app = Application.builder().token(token).build()

    # Команды
    _telegram_app.add_handler(CommandHandler("start", cmd_start))
    _telegram_app.add_handler(CommandHandler("auth", cmd_auth))
    _telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    if webhook_url:
        # Продакшен: Starlette + uvicorn
        starlette_app = build_starlette_app(_telegram_app)

        import asyncio

        async def run():
            await on_startup(_telegram_app)
            config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
            server = uvicorn.Server(config)
            try:
                await server.serve()
            finally:
                await on_shutdown(_telegram_app)

        asyncio.run(run())
    else:
        # Локально: polling
        logger.info("Starting polling (local mode)")
        _telegram_app.run_polling()


if __name__ == "__main__":
    main()