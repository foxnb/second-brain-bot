"""
Revory - Google Calendar Service (Schema v9)
OAuth через публичный callback URL + CRUD
Работает с calendar_connections вместо google_token в users
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
    load_timezone,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_TZ = "Europe/Moscow"

# telegram_id -> Flow (живёт в памяти пока идёт OAuth)
# Ключ — telegram_id (не UUID), т.к. state в OAuth = telegram_id
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
        return None

    token_data = conn_data["token_data"]
    connection_id = conn_data["id"]

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

        # Пытаемся получить email из Google
        provider_email = None
        try:
            from googleapiclient.discovery import build as build_svc
            svc = build_svc("oauth2", "v2", credentials=creds)
            user_info = svc.userinfo().get().execute()
            provider_email = user_info.get("email")
        except Exception as e:
            logger.warning(f"Could not fetch Google email: {e}")

        await save_calendar_connection(
            user_id=user_id,
            provider="google",
            token_data=token_data,
            provider_email=provider_email,
        )

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
        logger.info(f"Created event: {event.get('id')} for user {user_id}")
        return {
            "id": event["id"],
            "title": event["summary"],
            "start": event["start"]["dateTime"],
            "end": event["end"]["dateTime"],
            "link": event.get("htmlLink", ""),
        }
    except Exception as e:
        logger.error(f"Create event error: {e}")
        return None


async def get_events(
    user_id: UUID,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    max_results: int = 10,
) -> Optional[list]:
    service = await _get_service(user_id)
    if not service:
        return None

    tz = await _get_user_tz(user_id)

    now = datetime.now()
    if not time_min:
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not time_max:
        time_max = time_min + timedelta(days=1)

    time_min_str = _to_rfc3339(time_min, tz)
    time_max_str = _to_rfc3339(time_max, tz)

    logger.info(f"get_events: timeMin={time_min_str}, timeMax={time_max_str}, tz={tz}")

    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min_str,
                timeMax=time_max_str,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = result.get("items", [])
        return [
            {
                "id": e["id"],
                "title": e.get("summary", "Без названия"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
            }
            for e in events
        ]
    except Exception as e:
        logger.error(f"Get events error: {e}")
        return None


async def delete_event(user_id: UUID, event_id: str) -> bool:
    service = await _get_service(user_id)
    if not service:
        return False

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        logger.info(f"Deleted event {event_id} for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Delete event error: {e}")
        return False