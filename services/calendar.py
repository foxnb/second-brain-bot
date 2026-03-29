"""
Revory - Google Calendar Service (Schema v9)
OAuth через публичный callback URL + CRUD
Работает с calendar_connections вместо google_token в users
create_event / delete_event пишут в БД (зеркало)
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from services.database import (
    load_calendar_connection,
    save_calendar_connection,
    update_calendar_tokens,
    update_user_email,
    load_timezone,
    upsert_event,
    soft_delete_event_by_external_id,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
DEFAULT_TZ = "Europe/Moscow"

# telegram_id -> Flow (живёт в памяти пока идёт OAuth)
_pending_flows: dict[int, Flow] = {}


def _get_credentials_file() -> str:
    """Читает credentials из env или файла."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if creds_json:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(creds_json)
        tmp.close()
        return tmp.name
    return "credentials.json"


def _get_redirect_uri() -> str:
    base = os.getenv("WEBHOOK_URL", "http://localhost:8000")
    return f"{base}/auth/callback"


async def _get_user_tz(user_id: UUID) -> str:
    """Возвращает IANA timezone пользователя или дефолт."""
    tz = await load_timezone(user_id)
    return tz or DEFAULT_TZ


def _to_rfc3339(dt_naive: datetime, tz_name: str) -> str:
    """
    Принимает naive datetime + IANA timezone name,
    возвращает RFC3339 строку с offset.
    """
    tz = ZoneInfo(tz_name)
    dt_aware = dt_naive.replace(tzinfo=tz)
    return dt_aware.isoformat()


async def get_credentials(user_id: UUID) -> Optional[Credentials]:
    """Загружает Google credentials из calendar_connections."""
    conn_data = await load_calendar_connection(user_id, provider="google")
    if not conn_data:
        logger.warning(f"No calendar_connection found for user {user_id}, provider=google")
        return None

    token_data = conn_data.get("token_data")
    if not token_data:
        logger.warning(f"Calendar connection {conn_data['id']} has no token_data")
        return None

    connection_id = conn_data["id"]
    logger.info(f"Found calendar connection {connection_id} for user {user_id}")

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            await update_calendar_tokens(connection_id, json.loads(creds.to_json()))
        except Exception as e:
            logger.error(f"Token refresh error for user {user_id}: {e}")
            return None

    return creds


def start_auth(telegram_id: int) -> str:
    """
    Создаёт OAuth flow и возвращает URL для авторизации.
    state = telegram_id (для callback).
    """
    flow = Flow.from_client_secrets_file(
        _get_credentials_file(),
        scopes=SCOPES,
        redirect_uri=_get_redirect_uri(),
    )
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=str(telegram_id),
    )
    _pending_flows[telegram_id] = flow
    return auth_url


async def finish_auth_callback(
    user_id: UUID,
    telegram_id: int,
    code: str,
) -> bool:
    """
    Завершает OAuth flow после редиректа.
    Сохраняет токены в calendar_connections.
    """
    flow = _pending_flows.get(telegram_id)
    if not flow:
        logger.error(f"No pending flow for telegram {telegram_id}")
        return False

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_data = json.loads(creds.to_json())

        # Получаем email из id_token
        provider_email = None
        if hasattr(creds, "id_token") and creds.id_token:
            try:
                import base64
                payload = creds.id_token.split(".")[1]
                payload += "=" * (4 - len(payload) % 4)
                id_info = json.loads(base64.urlsafe_b64decode(payload))
                provider_email = id_info.get("email")
                logger.info(f"Got Google email from id_token: {provider_email}")
            except Exception as e:
                logger.warning(f"Could not parse id_token: {e}")

        await save_calendar_connection(
            user_id=user_id,
            provider="google",
            token_data=token_data,
            provider_email=provider_email,
        )

        # Заполняем email в users если ещё не установлен
        if provider_email:
            await update_user_email(user_id, provider_email)

        del _pending_flows[telegram_id]
        logger.info(f"Google auth done for user {user_id} (tg={telegram_id})")
        return True
    except Exception as e:
        logger.error(f"Auth error for telegram {telegram_id}: {e}")
        return False


async def _get_service(user_id: UUID):
    creds = await get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


async def _get_connection_id(user_id: UUID) -> Optional[int]:
    """Получает connection_id для записи в events."""
    conn_data = await load_calendar_connection(user_id, provider="google")
    return conn_data["id"] if conn_data else None


async def create_event(
    user_id: UUID,
    title: str,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    description: str = "",
) -> Optional[dict]:
    service = await _get_service(user_id)
    if not service:
        return None

    if not end_time:
        end_time = start_time + timedelta(hours=1)

    tz = await _get_user_tz(user_id)

    event_body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": _to_rfc3339(start_time, tz), "timeZone": tz},
        "end": {"dateTime": _to_rfc3339(end_time, tz), "timeZone": tz},
    }

    try:
        event = service.events().insert(calendarId="primary", body=event_body).execute()
        google_event_id = event["id"]
        logger.info(f"Created Google event: {google_event_id} for user {user_id}")

        # Сохраняем зеркало в БД
        connection_id = await _get_connection_id(user_id)
        if connection_id:
            tz_obj = ZoneInfo(tz)
            start_aware = start_time.replace(tzinfo=tz_obj)
            end_aware = end_time.replace(tzinfo=tz_obj)

            await upsert_event(
                user_id=user_id,
                calendar_connection_id=connection_id,
                external_event_id=google_event_id,
                title=title,
                start_time=start_aware,
                end_time=end_aware,
                timezone=tz,
                description=description,
            )
            logger.info(f"Saved event mirror in DB for {google_event_id}")

        return {
            "id": google_event_id,
            "title": event["summary"],
            "start": event["start"]["dateTime"],
            "end": event["end"]["dateTime"],
            "link": event.get("htmlLink", ""),
        }
    except Exception as e:
        logger.error(f"Create event error: {e}")
        return None


async def delete_event(user_id: UUID, event_id: str) -> bool:
    """
    Удаляет событие из Google Calendar + мягкое удаление в БД.
    event_id здесь — это external_event_id (Google Calendar ID).
    """
    service = await _get_service(user_id)
    if not service:
        return False

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        logger.info(f"Deleted Google event {event_id} for user {user_id}")

        # Мягкое удаление в БД
        connection_id = await _get_connection_id(user_id)
        if connection_id:
            await soft_delete_event_by_external_id(connection_id, event_id)
            logger.info(f"Soft-deleted event mirror {event_id} in DB")

        return True
    except Exception as e:
        logger.error(f"Delete event error: {e}")
        return False


async def revoke_google_token(access_token: str) -> bool:
    """Отзывает Google OAuth токен (GDPR compliance)."""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": access_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200:
                logger.info("Google token revoked successfully")
                return True
            else:
                logger.warning(f"Google token revoke failed: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Google token revoke error: {e}")
        return False