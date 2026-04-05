"""
Revory — Voice Handler
Telegram voice message → Groq Whisper transcription → handle_text flow.
Для групп: голосовое кешируется (TTL 90s), обрабатывается при упоминании бота.
"""

import io
import logging
import os
import time

from groq import AsyncGroq
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_groq_client: AsyncGroq | None = None

# ─── Voice cache для групп ────────────────────────────────
# {telegram_user_id: {"text": str, "ts": float}}
_voice_cache: dict[int, dict] = {}
VOICE_CACHE_TTL = 90  # секунд


def get_cached_voice(telegram_id: int) -> str | None:
    entry = _voice_cache.get(telegram_id)
    if not entry:
        return None
    if time.time() - entry["ts"] > VOICE_CACHE_TTL:
        _voice_cache.pop(telegram_id, None)
        return None
    return entry["text"]


def cache_voice(telegram_id: int, text: str):
    _voice_cache[telegram_id] = {"text": text, "ts": time.time()}


def clear_voice_cache(telegram_id: int):
    _voice_cache.pop(telegram_id, None)


def _get_groq() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY env var is not set")
        _groq_client = AsyncGroq(api_key=api_key)
    return _groq_client


async def _transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Скачивает и транскрибирует голосовое сообщение. Возвращает текст или None."""
    voice = update.message.voice
    if not voice:
        return None

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
        return None

    if not text:
        await update.message.reply_text("🤔 Не удалось разобрать речь. Попробуй ещё раз.")
        return None

    logger.info(f"Voice transcribed: {text!r}")
    return text


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовое в личке — транскрибируем и сразу обрабатываем."""
    text = await _transcribe_voice(update, context)
    if not text:
        return
    from handlers.router import handle_text
    await handle_text(update, context, text=text)
