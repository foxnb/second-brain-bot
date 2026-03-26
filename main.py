"""
Revory - Main (Schema v9)
Telegram bot с webhook режимом для продакшена.
/auth/callback — endpoint для Google OAuth редиректа.
"""

import logging
import os
import re
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
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
from services.database import (
    ensure_user,
    get_user_id_by_telegram,
    save_timezone,
    load_timezone,
    load_timezone_by_telegram,
    get_pool,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Глобально — заполняется в main()
_telegram_app = None


# ─── Утилита: смещение → IANA timezone ────────────────────

_OFFSET_TO_IANA = {
    -12: "Etc/GMT+12",
    -11: "Pacific/Pago_Pago",
    -10: "Pacific/Honolulu",
    -9: "America/Anchorage",
    -8: "America/Los_Angeles",
    -7: "America/Denver",
    -6: "America/Chicago",
    -5: "America/New_York",
    -4: "America/Halifax",
    -3: "America/Sao_Paulo",
    -2: "Atlantic/South_Georgia",
    -1: "Atlantic/Azores",
    0: "Etc/GMT",
    1: "Europe/London",
    2: "Europe/Berlin",
    3: "Europe/Moscow",
    4: "Asia/Dubai",
    5: "Asia/Karachi",
    6: "Asia/Almaty",
    7: "Asia/Bangkok",
    8: "Asia/Shanghai",
    9: "Asia/Tokyo",
    10: "Australia/Sydney",
    11: "Pacific/Noumea",
    12: "Pacific/Auckland",
    13: "Pacific/Apia",
    14: "Pacific/Kiritimati",
}

_FRACTIONAL_TO_IANA = {
    "+3:30": "Asia/Tehran",
    "+4:30": "Asia/Kabul",
    "+5:30": "Asia/Kolkata",
    "+5:45": "Asia/Kathmandu",
    "+6:30": "Asia/Yangon",
    "+9:30": "Australia/Darwin",
    "-3:30": "America/St_Johns",
    "-9:30": "Pacific/Marquesas",
}


def offset_to_iana(offset_str: str) -> str | None:
    offset_str = offset_str.strip().replace("UTC", "").replace("utc", "")

    if offset_str in _FRACTIONAL_TO_IANA:
        return _FRACTIONAL_TO_IANA[offset_str]

    match = re.match(r'^([+-]?)(\d{1,2})$', offset_str)
    if not match:
        return None

    sign = match.group(1) or "+"
    hours = int(match.group(2))
    offset_int = hours if sign == "+" else -hours

    return _OFFSET_TO_IANA.get(offset_int)


def iana_to_display(tz: str) -> str:
    for offset, zone in _OFFSET_TO_IANA.items():
        if zone == tz:
            if offset == 0:
                return "UTC±0"
            sign = "+" if offset > 0 else ""
            return f"UTC{sign}{offset}"

    for offset_str, zone in _FRACTIONAL_TO_IANA.items():
        if zone == tz:
            return f"UTC{offset_str}"

    return tz


# ─── Хелпер: получить UUID из context или БД ─────────────

async def _get_user_id(telegram_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Получает UUID user_id. Кэширует в context.user_data."""
    cached = context.user_data.get("user_id")
    if cached:
        return cached

    user_id = await get_user_id_by_telegram(telegram_id)
    if user_id:
        context.user_data["user_id"] = user_id
    return user_id


# ─── /start ───────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = await ensure_user(user.id, user.username, user.first_name)
    context.user_data["user_id"] = user_id

    tz = await load_timezone(user_id)
    if tz:
        await _send_welcome(update)
    else:
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_set:Europe/Moscow")],
            [InlineKeyboardButton("🌍 Другой", callback_data="tz_ask_custom")],
        ]
        await update.message.reply_text(
            "Привет! Я Revory — твой личный календарный ассистент 🗓️\n\n"
            "Для начала — какой у тебя часовой пояс?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def _send_welcome(update_or_query):
    text = (
        "🗓️ Я Revory — твой личный календарный ассистент!\n\n"
        "Могу:\n"
        "• Создавать встречи — «встреча завтра в 15:00 с клиентом»\n"
        "• Показывать расписание — «что у меня сегодня?»\n"
        "• Удалять события — «удали встречу с клиентом»\n"
        "• Ставить напоминания — «напомни в 10 утра купить продукты»\n\n"
        "Подключи календарь: /auth\n"
        "Сменить часовой пояс: /timezone\n\n"
        "Просто пиши как думаешь!"
    )
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text)
    elif hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text)


# ─── Callback для кнопок timezone ─────────────────────────

async def tz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    telegram_id = query.from_user.id
    user_id = await _get_user_id(telegram_id, context)
    if not user_id:
        await query.edit_message_text("❌ Ошибка. Нажми /start")
        return

    if data.startswith("tz_set:"):
        tz = data.split(":", 1)[1]
        await save_timezone(user_id, tz)
        display = iana_to_display(tz)
        await query.edit_message_text(
            f"✅ Часовой пояс установлен: {display}\n\n"
            "Теперь подключи календарь: /auth"
        )

    elif data == "tz_ask_custom":
        context.user_data["awaiting_timezone"] = True
        await query.edit_message_text(
            "🌍 Введи своё смещение от UTC.\n\n"
            "Примеры: +3, -5, +5:30, 0\n\n"
            "Не знаешь своё смещение? Погугли «мой часовой пояс UTC»."
        )


# ─── Обработка ввода timezone ─────────────────────────────

async def handle_timezone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_timezone"):
        return False

    text = update.message.text.strip()
    tz = offset_to_iana(text)

    if not tz:
        await update.message.reply_text(
            "❌ Не понял формат. Введи смещение как: +3, -5, +5:30, 0"
        )
        return True

    telegram_id = update.message.from_user.id
    user_id = await _get_user_id(telegram_id, context)
    if not user_id:
        await update.message.reply_text("❌ Ошибка. Нажми /start")
        return True

    await save_timezone(user_id, tz)
    context.user_data["awaiting_timezone"] = False

    display = iana_to_display(tz)
    await update.message.reply_text(
        f"✅ Часовой пояс установлен: {display}\n\n"
        "Теперь подключи календарь: /auth"
    )
    return True


# ─── /timezone (смена timezone) ───────────────────────────

async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    user_id = await _get_user_id(telegram_id, context)

    msg = ""
    if user_id:
        current_tz = await load_timezone(user_id)
        if current_tz:
            display = iana_to_display(current_tz)
            msg = f"Текущий часовой пояс: {display}\n\n"

    keyboard = [
        [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_set:Europe/Moscow")],
        [InlineKeyboardButton("🌍 Другой", callback_data="tz_ask_custom")],
    ]
    await update.message.reply_text(
        msg + "Выбери часовой пояс:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── /auth ────────────────────────────────────────────────

async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = await ensure_user(user.id, user.username, user.first_name)
    context.user_data["user_id"] = user_id

    auth_url = start_auth(user.id)

    await update.message.reply_text(
        "🔑 Подключаем Google Calendar!\n\n"
        "1️⃣ Перейди по ссылке и разреши доступ:\n"
        f"{auth_url}\n\n"
        "После разрешения доступа Google автоматически завершит подключение — "
        "ничего копировать не нужно!"
    )


# ─── OAuth callback ───────────────────────────────────────

async def auth_callback(request: Request):
    """Google OAuth callback — GET /auth/callback?code=...&state=telegram_id"""
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return HTMLResponse("<h2>Ошибка: отсутствует code или state.</h2>", status_code=400)

    try:
        telegram_id = int(state)
    except ValueError:
        return HTMLResponse("<h2>Ошибка: некорректный state.</h2>", status_code=400)

    user_id = await get_user_id_by_telegram(telegram_id)
    if not user_id:
        return HTMLResponse("<h2>Ошибка: пользователь не найден. Нажми /start в боте.</h2>", status_code=400)

    success = await finish_auth_callback(user_id, telegram_id, code)

    if success:
        if _telegram_app:
            try:
                await _telegram_app.bot.send_message(
                    chat_id=telegram_id,
                    text="✅ Google Calendar успешно подключён! Теперь просто пиши что нужно сделать.",
                )
            except Exception as e:
                logger.error(f"Failed to notify user {telegram_id}: {e}")

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


# ─── Обёртка для text handler с проверкой timezone ────────

async def handle_text_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handled = await handle_timezone_input(update, context)
    if handled:
        return

    telegram_id = update.message.from_user.id
    tz = await load_timezone_by_telegram(telegram_id)
    if not tz:
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_set:Europe/Moscow")],
            [InlineKeyboardButton("🌍 Другой", callback_data="tz_ask_custom")],
        ]
        await update.message.reply_text(
            "⏰ Сначала установи часовой пояс:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    await handle_text(update, context)


# ─── Starlette app ────────────────────────────────────────

def build_starlette_app(telegram_app: Application) -> Starlette:
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

    await get_pool()
    logger.info("DB pool initialized")

    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await telegram_app.bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook set: {webhook_url}/webhook")

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

    _telegram_app.add_handler(CommandHandler("start", cmd_start))
    _telegram_app.add_handler(CommandHandler("auth", cmd_auth))
    _telegram_app.add_handler(CommandHandler("timezone", cmd_timezone))
    _telegram_app.add_handler(CallbackQueryHandler(tz_callback, pattern=r"^tz_"))
    _telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_wrapper)
    )

    if webhook_url:
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
        logger.info("Starting polling (local mode)")
        _telegram_app.run_polling()


if __name__ == "__main__":
    main()