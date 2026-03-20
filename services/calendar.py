"""
Revory - Google Calendar Service
OAuth Desktop App + CRUD
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"
TOKENS_DIR = "tokens"


def _get_token_path(user_id: int) -> str:
    os.makedirs(TOKENS_DIR, exist_ok=True)
    return os.path.join(TOKENS_DIR, f"token_{user_id}.json")


def get_credentials(user_id: int) -> Optional[Credentials]:
    token_path = _get_token_path(user_id)
    if not os.path.exists(token_path):
        return None

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.error(f"Token refresh error for {user_id}: {e}")
            return None

    return creds


def start_auth(user_id: int) -> str:
    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob"
    )
    auth_url, _ = flow.authorization_url(prompt="consent")

    flow_path = os.path.join(TOKENS_DIR, f"flow_{user_id}.json")
    os.makedirs(TOKENS_DIR, exist_ok=True)
    with open(flow_path, "w") as f:
        json.dump({"client_config": flow.client_config}, f)

    return auth_url


def finish_auth(user_id: int, auth_code: str) -> bool:
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE,
            scopes=SCOPES,
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        flow.fetch_token(code=auth_code)
        creds = flow.credentials

        token_path = _get_token_path(user_id)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

        flow_path = os.path.join(TOKENS_DIR, f"flow_{user_id}.json")
        if os.path.exists(flow_path):
            os.remove(flow_path)

        logger.info(f"Auth done for user {user_id}")
        return True

    except Exception as e:
        logger.error(f"Auth error for {user_id}: {e}")
        return False


def _get_service(user_id: int):
    creds = get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


async def create_event(
    user_id: int,
    title: str,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    description: str = ""
) -> Optional[dict]:
    service = _get_service(user_id)
    if not service:
        return None

    if not end_time:
        end_time = start_time + timedelta(hours=1)

    event_body = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "Europe/Moscow",
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "Europe/Moscow",
        },
    }

    try:
        event = service.events().insert(
            calendarId="primary",
            body=event_body
        ).execute()

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
    max_results: int = 10
) -> Optional[list]:
    service = _get_service(user_id)
    if not service:
        return None

    now = datetime.now()
    if not time_min:
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not time_max:
        time_max = time_min + timedelta(days=1)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat() + "+03:00",
            timeMax=time_max.isoformat() + "+03:00",
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        return [
            {
                "id": e["id"],
                "title": e.get("summary", "Bez nazvaniya"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
            }
            for e in events
        ]

    except Exception as e:
        logger.error(f"Get events error: {e}")
        return None


async def delete_event(user_id: int, event_id: str) -> bool:
    service = _get_service(user_id)
    if not service:
        return False

    try:
        service.events().delete(
            calendarId="primary",
            eventId=event_id
        ).execute()
        logger.info(f"Deleted event {event_id} for user {user_id}")
        return True

    except Exception as e:
        logger.error(f"Delete event error: {e}")
        return False