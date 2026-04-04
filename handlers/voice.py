"""
Revory — Voice Handler
Telegram voice message → Groq Whisper transcription → handle_text flow.
"""

import io
import logging
import os

from groq import AsyncGroq
from telegram import Update
from telegram.ext import ContextTypes

from handlers.router import handle_text

logger = logging.getLogger(__name__)

_groq_client: AsyncGroq | None = None


def _get_groq() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY env var is not set")
        _groq_client = AsyncGroq(api_key=api_key)
    return _groq_client


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    if not voice:
        return

    # Скачиваем файл в память
    file = await context.bot.get_file(voice.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    buf.name = "voice.ogg"

    try:
        transcription = await _get_groq().audio.transcriptions.create(
            file=buf,
            model="whisper-large-v3",
            language="ru",
            prompt="Запись задач, встреч, напоминаний и списков дел. Названия событий, людей, мест.",
        )
        text = transcription.text.strip()
    except Exception as e:
        logger.error(f"Groq transcription failed: {e}")
        await update.message.reply_text("❌ Не удалось распознать голосовое сообщение. Попробуй ещё раз.")
        return

    if not text:
        await update.message.reply_text("🤔 Не удалось разобрать речь. Попробуй ещё раз.")
        return

    logger.info(f"Voice transcribed: {text!r}")

    await handle_text(update, context, text=text)
