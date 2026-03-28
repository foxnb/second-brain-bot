"""
Revory - Database Service (Schema v9)
UUID users + auth_methods + calendar_connections
Supabase (PostgreSQL) через asyncpg
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

_pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL")
        _pool = await asyncpg.create_pool(database_url)
    return _pool


# ─── Users + Auth ─────────────────────────────────────────

async def ensure_user(
    telegram_id: int,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
) -> UUID:
    """
    Находит или создаёт пользователя по Telegram ID.
    Возвращает внутренний UUID user_id.
    """
    pool = await get_pool()

    # 1. Ищем существующий auth_method
    row = await pool.fetchrow(
        """
        SELECT user_id FROM auth_methods
        WHERE provider = 'telegram' AND provider_user_id = $1
        """,
        str(telegram_id),
    )

    if row:
        return row["user_id"]

    # 2. Создаём нового пользователя + auth_method в транзакции
    async with pool.acquire() as conn:
        async with conn.transaction():
            name = display_name or username or f"User {telegram_id}"
            user_row = await conn.fetchrow(
                """
                INSERT INTO users (display_name)
                VALUES ($1)
                RETURNING id
                """,
                name,
            )
            user_id = user_row["id"]

            metadata = {}
            if username:
                metadata["username"] = f"@{username}"

            await conn.execute(
                """
                INSERT INTO auth_methods (user_id, provider, provider_user_id, metadata)
                VALUES ($1, 'telegram', $2, $3)
                """,
                user_id,
                str(telegram_id),
                json.dumps(metadata) if metadata else None,
            )

    logger.info(f"Created user {user_id} for telegram {telegram_id}")
    return user_id


async def get_internal_user_id(telegram_id: int) -> Optional[UUID]:
    """Получает UUID пользователя по Telegram ID. None если не найден."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT user_id FROM auth_methods
        WHERE provider = 'telegram' AND provider_user_id = $1
        """,
        str(telegram_id),
    )
    return row["user_id"] if row else None


# ─── Timezone ─────────────────────────────────────────────

async def save_timezone(user_id: UUID, timezone: str):
    """Сохраняет часовой пояс пользователя."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET timezone = $1 WHERE id = $2",
        timezone,
        user_id,
    )
    logger.info(f"Saved timezone {timezone} for user {user_id}")


async def load_timezone(user_id: UUID) -> Optional[str]:
    """Загружает часовой пояс пользователя."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT timezone FROM users WHERE id = $1",
        user_id,
    )
    return row["timezone"] if row and row["timezone"] else None


async def load_timezone_by_telegram(telegram_id: int) -> Optional[str]:
    """Загружает timezone по Telegram ID (для удобства в хэндлерах)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.timezone FROM users u
        JOIN auth_methods am ON u.id = am.user_id
        WHERE am.provider = 'telegram' AND am.provider_user_id = $1
        """,
        str(telegram_id),
    )
    return row["timezone"] if row and row["timezone"] else None


# ─── Calendar Connections ─────────────────────────────────

async def save_calendar_connection(
    user_id: UUID,
    provider: str,
    token_data: dict,
    provider_email: Optional[str] = None,
) -> int:
    """
    Сохраняет или обновляет подключение календаря.
    Если первое подключение — автоматически is_primary=TRUE.
    Возвращает connection_id.
    """
    pool = await get_pool()

    # TODO: шифровать токены через Fernet (ENCRYPTION_KEY)
    # Пока храним как есть — добавим шифрование отдельным шагом
    token_json = json.dumps(token_data)
    refresh_token = token_data.get("refresh_token")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Ищем существующее подключение для этого провайдера
            # (NULL-safe: COALESCE обрабатывает NULL email)
            existing = await conn.fetchrow(
                """
                SELECT id FROM calendar_connections
                WHERE user_id = $1 AND provider = $2
                  AND COALESCE(provider_email, '') = COALESCE($3, '')
                """,
                user_id, provider, provider_email,
            )

            if existing:
                # Обновляем токены существующего подключения
                row = await conn.fetchrow(
                    """
                    UPDATE calendar_connections
                    SET access_token_encrypted = $1,
                        refresh_token_encrypted = $2,
                        provider_email = COALESCE($3, provider_email),
                        status = 'active',
                        updated_at = now()
                    WHERE id = $4
                    RETURNING id
                    """,
                    token_json, refresh_token, provider_email, existing["id"],
                )
            else:
                # Новое подключение — проверяем нужен ли primary
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM calendar_connections WHERE user_id = $1",
                    user_id,
                )
                is_primary = count == 0

                row = await conn.fetchrow(
                    """
                    INSERT INTO calendar_connections
                        (user_id, provider, provider_email, access_token_encrypted,
                         refresh_token_encrypted, is_primary, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'active')
                    RETURNING id
                    """,
                    user_id, provider, provider_email,
                    token_json, refresh_token, is_primary,
                )

    connection_id = row["id"]
    logger.info(f"Saved {provider} calendar for user {user_id} (conn={connection_id}, primary={is_primary})")
    return connection_id


async def load_calendar_connection(
    user_id: UUID,
    provider: Optional[str] = None,
) -> Optional[dict]:
    """
    Загружает подключение календаря.
    Если provider не указан — возвращает primary.
    """
    pool = await get_pool()

    if provider:
        row = await pool.fetchrow(
            """
            SELECT id, provider, provider_email, access_token_encrypted,
                   refresh_token_encrypted, calendar_id, is_primary, status
            FROM calendar_connections
            WHERE user_id = $1 AND provider = $2 AND status = 'active'
            ORDER BY is_primary DESC
            LIMIT 1
            """,
            user_id,
            provider,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT id, provider, provider_email, access_token_encrypted,
                   refresh_token_encrypted, calendar_id, is_primary, status
            FROM calendar_connections
            WHERE user_id = $1 AND status = 'active'
            ORDER BY is_primary DESC
            LIMIT 1
            """,
            user_id,
        )

    if not row:
        return None

    return {
        "id": row["id"],
        "provider": row["provider"],
        "provider_email": row["provider_email"],
        "token_data": json.loads(row["access_token_encrypted"]),
        "refresh_token": row["refresh_token_encrypted"],
        "calendar_id": row["calendar_id"],
        "is_primary": row["is_primary"],
    }


async def load_all_calendar_connections(user_id: UUID) -> list[dict]:
    """Все активные подключения пользователя."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, provider, provider_email, is_primary, status
        FROM calendar_connections
        WHERE user_id = $1 AND status = 'active'
        ORDER BY is_primary DESC, connected_at
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def update_calendar_tokens(connection_id: int, token_data: dict):
    """Обновляет токены для существующего подключения."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE calendar_connections
        SET access_token_encrypted = $1, updated_at = now()
        WHERE id = $2
        """,
        json.dumps(token_data),
        connection_id,
    )


async def switch_primary_calendar(user_id: UUID, connection_id: int) -> bool:
    """Переключает primary календарь."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Снимаем primary со всех
            await conn.execute(
                "UPDATE calendar_connections SET is_primary = FALSE WHERE user_id = $1",
                user_id,
            )
            # Ставим на нужный
            result = await conn.execute(
                """
                UPDATE calendar_connections
                SET is_primary = TRUE
                WHERE id = $1 AND user_id = $2 AND status = 'active'
                """,
                connection_id,
                user_id,
            )
    return "UPDATE 1" in result


# ─── Reminders ─────────────────────────────────────────────

async def save_reminder(
    user_id: UUID,
    title: str,
    remind_at: "datetime",
    event_id: Optional[int] = None,
) -> int:
    """Сохраняет напоминание в БД. Возвращает ID."""
    pool = await get_pool()
    reminder_id = await pool.fetchval(
        """
        INSERT INTO reminders (user_id, assigned_to, title, remind_at, event_id, status)
        VALUES ($1, $1, $2, $3, $4, 'pending')
        RETURNING id
        """,
        user_id, title, remind_at, event_id,
    )
    logger.info(f"Saved reminder {reminder_id} for user {user_id}: '{title}' at {remind_at}")
    return reminder_id


async def get_pending_reminders() -> list[dict]:
    """Возвращает все просроченные напоминания со статусом pending."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT r.id, r.user_id, r.title, r.remind_at,
               am.provider_user_id AS telegram_id
        FROM reminders r
        JOIN auth_methods am ON am.user_id = r.user_id AND am.provider = 'telegram'
        WHERE r.status = 'pending' AND r.remind_at <= now()
        ORDER BY r.remind_at
        LIMIT 50
        """
    )
    return [dict(r) for r in rows]


async def mark_reminder_sent(reminder_id: int):
    """Помечает напоминание как отправленное."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE reminders SET status = 'sent' WHERE id = $1",
        reminder_id,
    )


async def cancel_reminder(reminder_id: int, user_id: UUID) -> bool:
    """Отменяет напоминание (только своё)."""
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND user_id = $2 AND status = 'pending'",
        reminder_id, user_id,
    )
    return "UPDATE 1" in result