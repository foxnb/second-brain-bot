"""
Revory — Calendar Sync Service
Ленивая синхронизация Google Calendar → events (БД)
Поддерживает syncToken для инкрементальных обновлений.
"""

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Optional
from uuid import UUID

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from services.database import (
    load_calendar_connection,
    update_sync_token,
    upsert_event,
    soft_delete_event_by_external_id,
)
from services.calendar import get_credentials

logger = logging.getLogger(__name__)


async def _build_service(user_id: UUID):
    """Создаёт Google Calendar API service."""
    from googleapiclient.discovery import build
    creds = await get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


async def sync_calendar(user_id: UUID, provider: str = "google") -> bool:
    """
    Синхронизирует события из Google Calendar в БД.
    
    - Если есть syncToken → инкрементальная sync (только изменения)
    - Если нет syncToken (первый раз или сброс) → полная sync
    - При 410 Gone → сбрасывает токен и делает полную sync
    
    Возвращает True если синхронизация успешна.
    """
    conn_data = await load_calendar_connection(user_id, provider=provider)
    if not conn_data:
        logger.warning(f"No calendar connection for user {user_id}")
        return False

    connection_id = conn_data["id"]
    sync_token = conn_data.get("sync_token")
    calendar_id = conn_data.get("calendar_id") or "primary"

    service = await _build_service(user_id)
    if not service:
        return False

    try:
        if sync_token:
            # Инкрементальная sync
            return await _incremental_sync(
                service, user_id, connection_id, calendar_id, sync_token
            )
        else:
            # Полная sync (первый раз)
            return await _full_sync(
                service, user_id, connection_id, calendar_id
            )

    except HttpError as e:
        if e.resp.status == 410:
            # syncToken истёк — сбрасываем и делаем полную sync
            logger.info(f"syncToken expired for user {user_id}, doing full sync")
            await update_sync_token(connection_id, None)
            return await _full_sync(
                service, user_id, connection_id, calendar_id
            )
        logger.error(f"Google API error during sync for user {user_id}: {e}")
        return False
    except RefreshError as e:
        logger.error(f"Token refresh failed for user {user_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Sync error for user {user_id}: {e}")
        return False


async def _full_sync(
    service,
    user_id: UUID,
    connection_id: int,
    calendar_id: str,
) -> bool:
    """
    Полная синхронизация: загружает все события за последние 30 дней
    и на 90 дней вперёд. Сохраняет syncToken для будущих инкрементальных sync.
    """
    from datetime import timedelta

    now = datetime.now(dt_timezone.utc)
    time_min = (now - timedelta(days=30)).isoformat()
    time_max = (now + timedelta(days=90)).isoformat()

    logger.info(f"Full sync for user {user_id}, connection {connection_id}")

    all_events = []
    page_token = None

    while True:
        request = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
        )
        result = request.execute()

        items = result.get("items", [])
        all_events.extend(items)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    # Сохраняем события в БД
    count = 0
    for event in all_events:
        saved = await _save_google_event(user_id, connection_id, event)
        if saved:
            count += 1

    # Сохраняем syncToken
    next_sync_token = result.get("nextSyncToken")
    if next_sync_token:
        await update_sync_token(connection_id, next_sync_token)

    logger.info(f"Full sync done for user {user_id}: {count} events saved, syncToken={'yes' if next_sync_token else 'no'}")
    return True


async def _incremental_sync(
    service,
    user_id: UUID,
    connection_id: int,
    calendar_id: str,
    sync_token: str,
) -> bool:
    """
    Инкрементальная sync: запрашивает только изменения с прошлого syncToken.
    Обрабатывает добавления, обновления и удаления.
    """
    logger.info(f"Incremental sync for user {user_id}")

    all_events = []
    page_token = None
    next_sync_token = None

    while True:
        request_kwargs = {
            "calendarId": calendar_id,
            "syncToken": sync_token,
            "maxResults": 250,
        }
        if page_token:
            request_kwargs["pageToken"] = page_token
            # При пагинации syncToken не передаём
            del request_kwargs["syncToken"]

        result = service.events().list(**request_kwargs).execute()

        items = result.get("items", [])
        all_events.extend(items)

        page_token = result.get("nextPageToken")
        if not page_token:
            next_sync_token = result.get("nextSyncToken")
            break

    # Обрабатываем изменения
    created = 0
    deleted = 0

    for event in all_events:
        event_id = event.get("id")
        status = event.get("status")

        if status == "cancelled":
            # Событие удалено в Google → мягкое удаление в БД
            await soft_delete_event_by_external_id(connection_id, event_id)
            deleted += 1
        else:
            # Новое или обновлённое событие
            saved = await _save_google_event(user_id, connection_id, event)
            if saved:
                created += 1

    # Обновляем syncToken
    if next_sync_token:
        await update_sync_token(connection_id, next_sync_token)

    logger.info(
        f"Incremental sync done for user {user_id}: "
        f"{created} upserted, {deleted} deleted, "
        f"syncToken={'updated' if next_sync_token else 'unchanged'}"
    )
    return True


async def _save_google_event(
    user_id: UUID,
    connection_id: int,
    event: dict,
) -> bool:
    """
    Парсит событие Google Calendar и сохраняет в БД через upsert.
    Возвращает True если успешно.
    """
    event_id = event.get("id")
    if not event_id:
        return False

    # Пропускаем отменённые
    if event.get("status") == "cancelled":
        return False

    title = event.get("summary", "Без названия")
    description = event.get("description", "")

    # Парсим время
    start_data = event.get("start", {})
    end_data = event.get("end", {})

    start_str = start_data.get("dateTime") or start_data.get("date")
    end_str = end_data.get("dateTime") or end_data.get("date")

    if not start_str:
        logger.warning(f"Event {event_id} has no start time, skipping")
        return False

    # Timezone из события или дефолт
    tz_name = start_data.get("timeZone") or event.get("timeZone") or "UTC"

    try:
        start_time = _parse_google_datetime(start_str)
        end_time = _parse_google_datetime(end_str) if end_str else start_time
    except Exception as e:
        logger.warning(f"Cannot parse time for event {event_id}: {e}")
        return False

    try:
        await upsert_event(
            user_id=user_id,
            calendar_connection_id=connection_id,
            external_event_id=event_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
            timezone=tz_name,
            description=description,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to upsert event {event_id}: {e}")
        return False


def _parse_google_datetime(dt_str: str) -> datetime:
    """
    Парсит datetime из Google Calendar API.
    Форматы: '2026-03-29T15:00:00+03:00' или '2026-03-29' (all-day)
    """
    if "T" in dt_str:
        # dateTime формат с offset
        return datetime.fromisoformat(dt_str)
    else:
        # date формат (all-day event)
        return datetime.fromisoformat(dt_str + "T00:00:00+00:00")