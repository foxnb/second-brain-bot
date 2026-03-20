"""
Revory - Google Calendar Service
OAuth через публичный callback URL + CRUD
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from services.database import load_google_token, save_google_token

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"

# user_id -> Flow (живёт в памяти пока идёт OAuth)
_pending_flows: dict[int, Flow] = {}


def _get_redirect_uri() -> str:
    base = os.getenv("WEBHOOK_URL", "http://localhost:8000")
    return f"{base}/auth/callback"


async def get_credentials(user_id: int) -> Optional[Credentials]:
    token_data = await load_google_token(user_id)
    if not token_data:
        return None

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            await save_google_token(user_id, json.loads(creds.to_json()))
        except Exception as e:
            logger.error(f"Token refresh error for {user_id}: {e}")
            return None

    return creds


def start_auth(user_id: int) -> str:
    """Создаёт OAuth flow и возвращает URL для авторизации."""
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=_get_redirect_uri(),
    )
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=str(user_id),
    )
    _pending_flows[user_id] = flow
    return auth_url


async def finish_auth_callback(user_id: int, code: str) -> bool:
    """Завершает OAuth flow после редиректа. Вызывается из /auth/callback."""
    flow = _pending_flows.get(user_id)
    if not flow:
        logger.error(f"No pending flow for user {user_id}")
        return False

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        import json
        await save_google_token(user_id, json.loads(creds.to_json()))
        del _pending_flows[user_id]

        logger.info(f"Auth done for user {user_id}")
        return True

    except Exception as e:
        logger.error(f"Auth error for {user_id}: {e}")
        return False


async def _get_service(user_id: int):
    creds = await get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


async def create_event(
    user_id: int,
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

    event_body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_time.isoformat(), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": end_time.isoformat(), "timeZone": "Europe/Moscow"},
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
    user_id: int,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    max_results: int = 10,
) -> Optional[list]:
    service = await _get_service(user_id)
    if not service:
        return None

    now = datetime.now()
    if not time_min:
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not time_max:
        time_max = time_min + timedelta(days=1)

    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min.isoformat() + "+03:00",
                timeMax=time_max.isoformat() + "+03:00",
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


async def delete_event(user_id: int, event_id: str) -> bool:
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