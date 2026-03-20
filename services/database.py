"""
Revory - Database Service
Supabase (PostgreSQL) через asyncpg
"""

import json
import logging
import os
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL")
        _pool = await asyncpg.create_pool(database_url)
    return _pool


async def ensure_user(user_id: int, username: Optional[str] = None):
    """Создаёт пользователя если не существует."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO users (user_id, telegram_username)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
        username,
    )


async def save_google_token(user_id: int, token_data: dict):
    """Сохраняет Google OAuth токен в БД."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE users SET google_token = $1 WHERE user_id = $2
        """,
        json.dumps(token_data),
        user_id,
    )
    logger.info(f"Saved Google token for user {user_id}")


async def load_google_token(user_id: int) -> Optional[dict]:
    """Загружает Google OAuth токен из БД."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT google_token FROM users WHERE user_id = $1", user_id
    )
    if row and row["google_token"]:
        return json.loads(row["google_token"])
    return None