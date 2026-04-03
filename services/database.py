"""
Revory - Database Service (Schema v9)
UUID users + auth_methods + calendar_connections + events mirror + color mappings
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


async def update_user_email(user_id: UUID, email: str):
    """Заполняет email в users если ещё не установлен."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET email = $1 WHERE id = $2 AND email IS NULL",
        email, user_id,
    )
    logger.info(f"Set user email {email} for {user_id}")


# ─── Task destination preference ─────────────────────────

async def get_task_destination(user_id: UUID) -> Optional[str]:
    """Возвращает предпочтение пользователя: 'calendar', 'list' или None (не задано)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT task_destination FROM users WHERE id = $1",
        user_id,
    )
    return row["task_destination"] if row else None


async def set_task_destination(user_id: UUID, destination: str):
    """Сохраняет предпочтение пользователя: 'calendar' или 'list'."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET task_destination = $1 WHERE id = $2",
        destination, user_id,
    )
    logger.info(f"Set task_destination={destination} for user {user_id}")


# ─── Grammar form ─────────────────────────────────────────

async def get_grammar_form(user_id: UUID) -> str:
    """Возвращает грамматический род: 'm', 'f' или 'n' (нейтральный)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT grammar_form FROM users WHERE id = $1",
        user_id,
    )
    if row and row["grammar_form"]:
        return row["grammar_form"]
    return "n"


async def set_grammar_form(user_id: UUID, form: str):
    """Сохраняет грамматический род: 'm', 'f', 'n'."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET grammar_form = $1 WHERE id = $2",
        form, user_id,
    )
    logger.info(f"Set grammar_form={form} for user {user_id}")


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


# ─── Messages (контекст диалога) ──────────────────────────

async def save_message(user_id: UUID, role: str, content: str, parsed: dict = None):
    """Сохраняет сообщение в историю."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO messages (user_id, role, content, parsed)
        VALUES ($1, $2, $3, $4)
        """,
        user_id, role, content,
        json.dumps(parsed) if parsed else None,
    )


async def get_recent_messages(user_id: UUID, limit: int = 10) -> list[dict]:
    """Возвращает последние N сообщений для контекста AI."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT role, content FROM messages
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


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
    token_json = json.dumps(token_data)
    refresh_token = token_data.get("refresh_token")

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT id FROM calendar_connections
                WHERE user_id = $1 AND provider = $2
                  AND COALESCE(provider_email, '') = COALESCE($3, '')
                """,
                user_id, provider, provider_email,
            )

            if existing:
                row = await conn.fetchrow(
                    """
                    UPDATE calendar_connections
                    SET access_token_encrypted = $1,
                        refresh_token_encrypted = $2,
                        provider_email = COALESCE($3, provider_email),
                        status = 'active',
                        sync_token = NULL,
                        updated_at = now()
                    WHERE id = $4
                    RETURNING id
                    """,
                    token_json, refresh_token, provider_email, existing["id"],
                )
                is_primary = False
            else:
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
                   refresh_token_encrypted, calendar_id, is_primary, status,
                   sync_token
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
                   refresh_token_encrypted, calendar_id, is_primary, status,
                   sync_token
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
        "sync_token": row["sync_token"],
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


async def update_sync_token(connection_id: int, sync_token: Optional[str]):
    """Обновляет syncToken для инкрементальной синхронизации."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE calendar_connections SET sync_token = $1, updated_at = now() WHERE id = $2",
        sync_token, connection_id,
    )


async def switch_primary_calendar(user_id: UUID, connection_id: int) -> bool:
    """Переключает primary календарь."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE calendar_connections SET is_primary = FALSE WHERE user_id = $1",
                user_id,
            )
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


# ─── Events (зеркало) ────────────────────────────────────

async def upsert_event(
    user_id: UUID,
    calendar_connection_id: int,
    external_event_id: str,
    title: str,
    start_time: datetime,
    end_time: datetime,
    timezone: str,
    description: str = "",
    status_id: Optional[int] = None,
    color_id: Optional[int] = None,
) -> int:
    """
    Создаёт или обновляет событие по external_event_id.
    Возвращает internal event id.
    """
    pool = await get_pool()

    if status_id is None:
        status_id = await pool.fetchval(
            """
            SELECT s.id FROM statuses s
            JOIN status_models sm ON s.model_id = sm.id
            WHERE sm.owner_user_id = $1 AND sm.is_default = TRUE
            AND s.position = 1
            LIMIT 1
            """,
            user_id,
        )
        if status_id is None:
            status_id = await pool.fetchval(
                "SELECT id FROM statuses WHERE is_system = TRUE AND position = 1 LIMIT 1"
            )

    row = await pool.fetchrow(
        """
        INSERT INTO events
            (user_id, calendar_connection_id, external_event_id,
             title, description, start_time, end_time, timezone, status_id, color_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (calendar_connection_id, external_event_id)
            WHERE external_event_id IS NOT NULL
        DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            timezone = EXCLUDED.timezone,
            color_id = EXCLUDED.color_id,
            is_deleted = FALSE,
            deleted_at = NULL,
            updated_at = now()
        RETURNING id
        """,
        user_id, calendar_connection_id, external_event_id,
        title, description, start_time, end_time, timezone, status_id, color_id,
    )
    return row["id"]


async def soft_delete_event_by_external_id(
    calendar_connection_id: int,
    external_event_id: str,
):
    """Мягкое удаление по external_event_id (при sync — событие удалено в Google)."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE events
        SET is_deleted = TRUE, deleted_at = now(), updated_at = now()
        WHERE calendar_connection_id = $1 AND external_event_id = $2
          AND is_deleted = FALSE
        """,
        calendar_connection_id, external_event_id,
    )


async def soft_delete_event(event_id: int):
    """Мягкое удаление по internal id (при удалении через бота)."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE events
        SET is_deleted = TRUE, deleted_at = now(), updated_at = now()
        WHERE id = $1
        """,
        event_id,
    )


async def get_events_from_db(
    user_id: UUID,
    time_min: datetime,
    time_max: datetime,
    limit: int = 50,
) -> list[dict]:
    """Читает события из БД за период (с color_id)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, external_event_id, title, description,
               start_time, end_time, timezone, color_id
        FROM events
        WHERE user_id = $1
          AND start_time >= $2
          AND start_time < $3
          AND is_deleted = FALSE
        ORDER BY start_time
        LIMIT $4
        """,
        user_id, time_min, time_max, limit,
    )
    return [dict(r) for r in rows]


async def find_event_by_title(
    user_id: UUID,
    title_query: str,
    time_min: datetime,
    time_max: datetime,
    limit: int = 20,
) -> list[dict]:
    """Ищет события по подстроке в названии."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, external_event_id, calendar_connection_id,
               title, start_time, end_time, color_id
        FROM events
        WHERE user_id = $1
          AND LOWER(title) LIKE '%' || LOWER($2) || '%'
          AND start_time >= $3
          AND start_time < $4
          AND is_deleted = FALSE
        ORDER BY start_time
        LIMIT $5
        """,
        user_id, title_query, time_min, time_max, limit,
    )
    return [dict(r) for r in rows]


async def find_duplicate_event(
    user_id: UUID,
    title: str,
    start_time: datetime,
    window_minutes: int = 5,
) -> Optional[dict]:
    """
    Ищет событие с тем же названием (точное, case-insensitive) в окне ±window_minutes минут.
    Используется перед созданием для дедупликации.
    """
    pool = await get_pool()
    from datetime import timedelta
    t_min = start_time - timedelta(minutes=window_minutes)
    t_max = start_time + timedelta(minutes=window_minutes)
    row = await pool.fetchrow(
        """
        SELECT id, external_event_id, title, start_time
        FROM events
        WHERE user_id = $1
          AND LOWER(title) = LOWER($2)
          AND start_time >= $3
          AND start_time <= $4
          AND is_deleted = FALSE
        LIMIT 1
        """,
        user_id, title, t_min, t_max,
    )
    return dict(row) if row else None


async def get_connection_id_for_event(event_id: int) -> Optional[int]:
    """Получает calendar_connection_id по event id."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT calendar_connection_id FROM events WHERE id = $1",
        event_id,
    )


async def get_distinct_colors_for_user(user_id: UUID) -> list[int]:
    """Уникальные colorId из событий пользователя (без NULL)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT color_id FROM events
        WHERE user_id = $1 AND color_id IS NOT NULL AND is_deleted = FALSE
        ORDER BY color_id
        """,
        user_id,
    )
    return [r["color_id"] for r in rows]


async def get_events_by_color(
    user_id: UUID,
    color_id: Optional[int],
    time_min: datetime,
    time_max: datetime,
    limit: int = 100,
) -> list[dict]:
    """События по цвету и периоду. Если color_id=None — все события за период."""
    pool = await get_pool()
    if color_id is not None:
        rows = await pool.fetch(
            """
            SELECT id, external_event_id, calendar_connection_id,
                   title, start_time, end_time, timezone, color_id
            FROM events
            WHERE user_id = $1
              AND color_id = $2
              AND start_time >= $3
              AND start_time < $4
              AND is_deleted = FALSE
            ORDER BY start_time
            LIMIT $5
            """,
            user_id, color_id, time_min, time_max, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, external_event_id, calendar_connection_id,
                   title, start_time, end_time, timezone, color_id
            FROM events
            WHERE user_id = $1
              AND start_time >= $2
              AND start_time < $3
              AND is_deleted = FALSE
            ORDER BY start_time
            LIMIT $4
            """,
            user_id, time_min, time_max, limit,
        )
    return [dict(r) for r in rows]


async def update_event_color(
    external_event_id: str,
    connection_id: int,
    color_id: int,
) -> None:
    """Обновляет color_id события по external_event_id."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE events SET color_id = $1, updated_at = now()
        WHERE external_event_id = $2 AND calendar_connection_id = $3
          AND is_deleted = FALSE
        """,
        color_id, external_event_id, connection_id,
    )


async def update_event_times(
    external_event_id: str,
    connection_id: int,
    new_start: datetime,
    new_end: datetime,
) -> bool:
    """Обновляет start_time и end_time события по external_event_id."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE events
        SET start_time = $1, end_time = $2, updated_at = now()
        WHERE external_event_id = $3 AND calendar_connection_id = $4
          AND is_deleted = FALSE
        """,
        new_start, new_end, external_event_id, connection_id,
    )
    return "UPDATE 1" in result


async def update_event_title(external_event_id: str, new_title: str) -> bool:
    """Обновляет название события по external_event_id."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE events SET title = $1, updated_at = now()
        WHERE external_event_id = $2 AND is_deleted = FALSE
        """,
        new_title, external_event_id,
    )
    return "UPDATE 1" in result


async def cleanup_deleted_events(days: int = 30) -> int:
    """Физически удаляет события, удалённые более N дней назад."""
    pool = await get_pool()
    result = await pool.execute(
        """
        DELETE FROM events
        WHERE is_deleted = TRUE
          AND deleted_at < now() - make_interval(days => $1)
        """,
        days,
    )
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        logger.info(f"Cleaned up {count} deleted events older than {days} days")
    return count


# ─── Color Mappings ───────────────────────────────────────

async def get_color_mappings(user_id: UUID) -> list[dict]:
    """Все маппинги цветов пользователя."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, google_color_id, label, emoji, category_id
        FROM color_mappings
        WHERE user_id = $1
        ORDER BY google_color_id
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def save_color_mapping(
    user_id: UUID,
    google_color_id: int,
    label: str,
    emoji: Optional[str] = None,
) -> int:
    """Сохраняет или обновляет маппинг цвета. Возвращает id."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO color_mappings (user_id, google_color_id, label, emoji)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, google_color_id)
        DO UPDATE SET label = EXCLUDED.label, emoji = COALESCE(EXCLUDED.emoji, color_mappings.emoji)
        RETURNING id
        """,
        user_id, google_color_id, label, emoji,
    )
    return row["id"]


async def delete_color_mappings(user_id: UUID):
    """Удаляет все маппинги цветов пользователя (для пересоздания)."""
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM color_mappings WHERE user_id = $1",
        user_id,
    )


async def get_colors_asked(user_id: UUID) -> bool:
    """Спрашивали ли пользователя про цвета."""
    pool = await get_pool()
    val = await pool.fetchval(
        "SELECT colors_asked FROM users WHERE id = $1",
        user_id,
    )
    return val or False


async def set_colors_asked(user_id: UUID, asked: bool):
    """Устанавливает флаг colors_asked."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET colors_asked = $1 WHERE id = $2",
        asked, user_id,
    )


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


# ─── Disconnect / Logout / Delete ──────────────────────────

async def disconnect_calendar(user_id: UUID, provider: str = "google") -> bool:
    """Отключает календарь (удаляет токены, аккаунт остаётся)."""
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM calendar_connections WHERE user_id = $1 AND provider = $2",
        user_id, provider,
    )
    logger.info(f"Disconnected {provider} calendar for user {user_id}")
    return "DELETE" in result


async def logout_user(user_id: UUID) -> dict:
    """
    Полный выход: удаляет пользователя и все данные.
    CASCADE в FK удалит auth_methods, calendar_connections,
    messages, reminders, events, attachments.
    """
    pool = await get_pool()
    stats = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            stats["reminders"] = await conn.fetchval(
                "SELECT COUNT(*) FROM reminders WHERE user_id = $1", user_id
            )
            stats["messages"] = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE user_id = $1", user_id
            )
            stats["events"] = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE user_id = $1", user_id
            )
            stats["calendars"] = await conn.fetchval(
                "SELECT COUNT(*) FROM calendar_connections WHERE user_id = $1", user_id
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)

    logger.info(f"Deleted user {user_id}: {stats}")
    return stats


async def get_calendar_tokens_for_revoke(user_id: UUID) -> list[dict]:
    """Получает токены для отзыва у провайдеров перед удалением."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT provider, access_token_encrypted FROM calendar_connections WHERE user_id = $1",
        user_id,
    )
    result = []
    for r in rows:
        try:
            token_data = json.loads(r["access_token_encrypted"])
            result.append({"provider": r["provider"], "access_token": token_data.get("token")})
        except Exception:
            pass
    return result


# ─── Lists ─────────────────────────────────────────────────

async def create_list(
    user_id: UUID,
    name: str,
    list_type: str = "checklist",
    target_date=None,
    auto_archive_at=None,
    icon: str = None,
    settings: dict = None,
) -> int:
    """Создаёт список. Возвращает list_id."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO lists (user_id, name, list_type, target_date, auto_archive_at, icon, settings)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        user_id, name, list_type, target_date, auto_archive_at,
        icon, json.dumps(settings) if settings else None,
    )
    logger.info(f"Created list '{name}' (type={list_type}) for user {user_id}, id={row['id']}")
    return row["id"]


async def find_list_by_name(
    user_id: UUID,
    query: str,
    list_type: str = None,
    status: str = "active",
) -> list[dict]:
    """Ищет списки по подстроке в имени."""
    pool = await get_pool()
    if list_type:
        rows = await pool.fetch(
            """
            SELECT id, name, list_type, target_date, icon, status, auto_archive_at
            FROM lists
            WHERE user_id = $1 AND LOWER(name) LIKE '%' || LOWER($2) || '%'
              AND list_type = $3 AND status = $4
            ORDER BY updated_at DESC
            """,
            user_id, query, list_type, status,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, name, list_type, target_date, icon, status, auto_archive_at
            FROM lists
            WHERE user_id = $1 AND LOWER(name) LIKE '%' || LOWER($2) || '%'
              AND status = $3
            ORDER BY updated_at DESC
            """,
            user_id, query, status,
        )
    return [dict(r) for r in rows]


async def get_list_by_id(list_id: int) -> Optional[dict]:
    """Загружает список по ID."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, user_id, name, list_type, target_date, icon, status FROM lists WHERE id = $1",
        list_id,
    )
    return dict(row) if row else None


async def get_user_lists(
    user_id: UUID,
    list_type: str = None,
    status: str = "active",
) -> list[dict]:
    """Все списки пользователя (опционально по типу)."""
    pool = await get_pool()
    if list_type:
        rows = await pool.fetch(
            """
            SELECT l.id, l.name, l.list_type, l.target_date, l.icon, l.status,
                   COUNT(li.id) FILTER (WHERE li.is_deleted = FALSE) AS item_count,
                   COUNT(li.id) FILTER (WHERE li.is_checked = TRUE AND li.is_deleted = FALSE) AS checked_count
            FROM lists l
            LEFT JOIN list_items li ON li.list_id = l.id
            WHERE l.user_id = $1 AND l.list_type = $2 AND l.status = $3
            GROUP BY l.id
            ORDER BY l.updated_at DESC
            """,
            user_id, list_type, status,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT l.id, l.name, l.list_type, l.target_date, l.icon, l.status,
                   COUNT(li.id) FILTER (WHERE li.is_deleted = FALSE) AS item_count,
                   COUNT(li.id) FILTER (WHERE li.is_checked = TRUE AND li.is_deleted = FALSE) AS checked_count
            FROM lists l
            LEFT JOIN list_items li ON li.list_id = l.id
            WHERE l.user_id = $1 AND l.status = $2
            GROUP BY l.id
            ORDER BY l.updated_at DESC
            """,
            user_id, status,
        )
    return [dict(r) for r in rows]


async def add_list_items(
    list_id: int,
    items: list[str],
    added_by: UUID = None,
) -> list[int]:
    """Добавляет элементы в список. Возвращает список ID."""
    pool = await get_pool()

    max_pos = await pool.fetchval(
        "SELECT COALESCE(MAX(position), 0) FROM list_items WHERE list_id = $1",
        list_id,
    )

    ids = []
    for i, content in enumerate(items):
        item_id = await pool.fetchval(
            """
            INSERT INTO list_items (list_id, added_by, content, position)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            list_id, added_by, content, max_pos + i + 1,
        )
        ids.append(item_id)

    await pool.execute("UPDATE lists SET updated_at = now() WHERE id = $1", list_id)

    logger.info(f"Added {len(ids)} items to list {list_id}")
    return ids


async def get_list_items(
    list_id: int,
    include_checked: bool = True,
) -> list[dict]:
    """Элементы списка."""
    pool = await get_pool()
    if include_checked:
        rows = await pool.fetch(
            """
            SELECT id, content, metadata, is_checked, checked_at, position,
                   COALESCE(status, CASE WHEN is_checked THEN 'done' ELSE 'todo' END) AS status
            FROM list_items
            WHERE list_id = $1 AND is_deleted = FALSE
            ORDER BY is_checked, position
            """,
            list_id,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, content, metadata, is_checked, checked_at, position,
                   COALESCE(status, 'todo') AS status
            FROM list_items
            WHERE list_id = $1 AND is_deleted = FALSE AND is_checked = FALSE
            ORDER BY position
            """,
            list_id,
        )
    return [dict(r) for r in rows]


async def check_list_items(
    list_id: int,
    item_queries: list[str],
    checked_by: UUID = None,
) -> list[str]:
    """
    Отмечает элементы как выполненные по подстроке.
    Возвращает список отмеченных названий.
    """
    pool = await get_pool()
    checked = []

    for query in item_queries:
        row = await pool.fetchrow(
            """
            UPDATE list_items
            SET is_checked = TRUE, checked_at = now(), checked_by = $3
            WHERE list_id = $1
              AND LOWER(content) LIKE '%' || LOWER($2) || '%'
              AND is_checked = FALSE AND is_deleted = FALSE
            RETURNING content
            """,
            list_id, query, checked_by,
        )
        if row:
            checked.append(row["content"])

    if checked:
        await pool.execute("UPDATE lists SET updated_at = now() WHERE id = $1", list_id)

    return checked


async def remove_list_items(
    list_id: int,
    item_queries: list[str],
) -> list[str]:
    """
    Мягко удаляет элементы по подстроке.
    Возвращает список удалённых названий.
    """
    pool = await get_pool()
    removed = []

    for query in item_queries:
        row = await pool.fetchrow(
            """
            UPDATE list_items
            SET is_deleted = TRUE
            WHERE list_id = $1
              AND LOWER(content) LIKE '%' || LOWER($2) || '%'
              AND is_deleted = FALSE
            RETURNING content
            """,
            list_id, query,
        )
        if row:
            removed.append(row["content"])

    if removed:
        await pool.execute("UPDATE lists SET updated_at = now() WHERE id = $1", list_id)

    return removed


async def set_list_item_status(list_id: int, item_query: str, status: str) -> str | None:
    """
    Устанавливает статус элемента по подстроке.
    Возвращает название элемента если нашёл, иначе None.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE list_items
        SET status = $1, updated_at = now()
        WHERE list_id = $2
          AND LOWER(content) LIKE '%' || LOWER($3) || '%'
          AND is_deleted = FALSE
        RETURNING content
        """,
        status, list_id, item_query,
    )
    if row:
        await pool.execute("UPDATE lists SET updated_at = now() WHERE id = $1", list_id)
        return row["content"]
    return None


async def set_list_item_status_across_lists(user_id, item_query: str, status: str) -> list[tuple[str, str]]:
    """
    Устанавливает статус по подстроке во всех активных списках пользователя.
    Возвращает [(list_name, item_content), ...]
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        UPDATE list_items li
        SET status = $1, updated_at = now()
        FROM lists l
        WHERE li.list_id = l.id
          AND l.user_id = $2
          AND l.status = 'active'
          AND LOWER(li.content) LIKE '%' || LOWER($3) || '%'
          AND li.is_deleted = FALSE
        RETURNING l.name AS list_name, li.content AS item_content
        """,
        status, user_id, item_query,
    )
    return [(r["list_name"], r["item_content"]) for r in rows]


async def get_list_statuses(list_id: int) -> list[str]:
    """Возвращает кастомные статусы списка из settings JSONB."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT settings FROM lists WHERE id = $1", list_id)
    if not row or not row["settings"]:
        return []
    import json
    settings = row["settings"] if isinstance(row["settings"], dict) else json.loads(row["settings"])
    return settings.get("statuses", [])


async def save_list_statuses(list_id: int, statuses: list[str]) -> None:
    """Сохраняет кастомные статусы в settings JSONB списка."""
    pool = await get_pool()
    import json
    await pool.execute(
        """
        UPDATE lists
        SET settings = COALESCE(settings, '{}'::jsonb) || $1::jsonb, updated_at = now()
        WHERE id = $2
        """,
        json.dumps({"statuses": statuses}), list_id,
    )


async def rename_list_item(list_id: int, old_query: str, new_content: str) -> str | None:
    """
    Переименовывает элемент списка по подстроке.
    Возвращает старое название если нашёл, иначе None.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE list_items
        SET content = $1, updated_at = now()
        WHERE list_id = $2
          AND LOWER(content) LIKE '%' || LOWER($3) || '%'
          AND is_deleted = FALSE
        RETURNING content
        """,
        new_content, list_id, old_query,
    )
    if row:
        await pool.execute("UPDATE lists SET updated_at = now() WHERE id = $1", list_id)
        return new_content
    return None


async def archive_list(user_id: UUID, list_id: int) -> bool:
    """Архивирует (удаляет) список по ID. Только свой."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE lists SET status = 'archived', updated_at = now()
        WHERE id = $1 AND user_id = $2 AND status = 'active'
        """,
        list_id, user_id,
    )
    success = "UPDATE 1" in result
    if success:
        logger.info(f"Archived list {list_id} for user {user_id}")
    return success


async def archive_expired_lists() -> int:
    """Архивирует списки с истёкшим auto_archive_at."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE lists SET status = 'archived', updated_at = now()
        WHERE auto_archive_at <= now() AND status = 'active'
        """
    )
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        logger.info(f"Archived {count} expired lists")
    return count


async def cleanup_archived_lists(days: int = 30) -> int:
    """Физически удаляет списки, заархивированные более N дней назад."""
    pool = await get_pool()
    result = await pool.execute(
        """
        DELETE FROM lists
        WHERE status = 'archived'
          AND updated_at < now() - make_interval(days => $1)
        """,
        days,
    )
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        logger.info(f"Cleaned up {count} archived lists older than {days} days")
    return count