"""Calendar channel adapter — polls calendar events via MS Graph API.

Ingests upcoming meetings and calendar changes as brain events.
Uses the same _GraphClient pattern as Teams and Email adapters.

Config:
    token_path: str — path to msgraph tokens.json (local dev)
    tenant_id/client_id/client_secret: str — OAuth2 credentials (container)
    lookahead_hours: int — how far ahead to look for events (default 24)
    events_per_poll: int — max events per poll (default 50)
    enabled: bool — enable this adapter
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import BoundedSet, ChannelAdapter, parse_timestamp

logger = logging.getLogger(__name__)


class _GraphClient:
    """Minimal MS Graph client — shared pattern with Teams/Email adapters."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    GRAPH_URL = "https://graph.microsoft.com/v1.0"

    def __init__(self, tenant_id: str = "", client_id: str = "", client_secret: str = "",
                 token_path: str = ""):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self._token = None
        self._token_expires = 0

    def _get_token(self) -> str:
        if self.token_path:
            return self._get_token_from_file()

        if self._token and time.time() < self._token_expires - 60:
            return self._token

        url = self.TOKEN_URL.format(tenant_id=self.tenant_id)
        data = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()

        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
                self._token = body["access_token"]
                self._token_expires = time.time() + body.get("expires_in", 3600)
                return self._token
        except (URLError, HTTPError, KeyError) as e:
            logger.error(f"Token acquisition failed: {e}")
            raise

    def _get_token_from_file(self) -> str:
        import os
        path = os.path.expanduser(self.token_path)
        try:
            with open(path) as f:
                data = json.load(f)
            return data.get("access_token", "")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to read token from {path}: {e}")
            return ""

    def get(self, path: str, params: dict = None, headers: dict = None) -> dict:
        token = self._get_token()
        url = f"{self.GRAPH_URL}{path}"
        if params:
            url += "?" + urlencode(params)

        req = Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            logger.error(f"Graph API error {e.code}: {path}")
            return {}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Graph API request failed: {e}")
            return {}


def _normalize_calendar_event(event: dict) -> dict:
    """Normalize a Graph API calendar event into a brain event."""
    event_id = event.get("id", "")
    subject = event.get("subject", "(no subject)")
    organizer = event.get("organizer", {}).get("emailAddress", {})
    organizer_name = organizer.get("name", "unknown")

    start = event.get("start", {})
    end = event.get("end", {})
    start_time = start.get("dateTime", "")
    end_time = end.get("dateTime", "")
    start_tz = start.get("timeZone", "UTC")

    location = event.get("location", {}).get("displayName", "")
    is_online = event.get("isOnlineMeeting", False)
    online_url = event.get("onlineMeeting", {}).get("joinUrl", "") if is_online else ""
    is_cancelled = event.get("isCancelled", False)
    is_all_day = event.get("isAllDay", False)
    response_status = event.get("responseStatus", {}).get("response", "none")

    # Attendees
    attendees = []
    for att in event.get("attendees", []):
        addr = att.get("emailAddress", {})
        attendees.append({
            "name": addr.get("name", ""),
            "email": addr.get("address", ""),
            "status": att.get("status", {}).get("response", "none"),
        })

    # Build body
    body_parts = [f"Meeting: {subject}"]
    if start_time:
        body_parts.append(f"Start: {start_time} ({start_tz})")
    if end_time:
        body_parts.append(f"End: {end_time}")
    if location:
        body_parts.append(f"Location: {location}")
    if online_url:
        body_parts.append(f"Join: {online_url}")
    if attendees:
        names = [a["name"] or a["email"] for a in attendees[:10]]
        body_parts.append(f"Attendees: {', '.join(names)}")
    if is_cancelled:
        body_parts.append("STATUS: CANCELLED")

    # Determine event type
    if is_cancelled:
        event_type = "meeting_cancelled"
    elif is_all_day:
        event_type = "all_day_event"
    else:
        event_type = "meeting"

    return {
        "id": f"cal:{event_id[:40]}",
        "source": "calendar",
        "channel": "calendar",
        "event_type": event_type,
        "author": organizer_name,
        "title": subject,
        "body": "\n".join(body_parts),
        "created_at": parse_timestamp(start_time) if start_time else time.time(),
        "metadata": {
            "event_id": event_id,
            "organizer_name": organizer_name,
            "organizer_email": organizer.get("address", ""),
            "start_time": start_time,
            "end_time": end_time,
            "timezone": start_tz,
            "location": location,
            "is_online": is_online,
            "online_url": online_url,
            "is_cancelled": is_cancelled,
            "is_all_day": is_all_day,
            "response_status": response_status,
            "attendee_count": len(attendees),
            "attendees": attendees[:20],
        },
    }


class CalendarAdapter(ChannelAdapter):
    """Polls calendar events via MS Graph API and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("calendar", config)
        self.lookahead_hours = self.config.get("lookahead_hours", 24)
        self.events_per_poll = self.config.get("events_per_poll", 50)
        self._client = None
        self._seen_ids = BoundedSet()

    @property
    def source(self) -> str:
        return "calendar"

    async def start(self):
        token_path = self.config.get("token_path", "")
        tenant_id = self.config.get("tenant_id", "")
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")

        if token_path:
            auth_mode = "token_path"
        elif all([tenant_id, client_id, client_secret]):
            auth_mode = "client_credentials"
        else:
            logger.error("Calendar adapter needs token_path or tenant_id/client_id/client_secret")
            return

        try:
            self._client = _GraphClient(
                tenant_id=tenant_id, client_id=client_id,
                client_secret=client_secret, token_path=token_path,
            )
            self._client._get_token()
            logger.info(f"Calendar adapter started ({auth_mode}), "
                        f"lookahead={self.lookahead_hours}h")
        except Exception as e:
            logger.error(f"Calendar adapter auth failed: {e}")
            self._client = None

    async def poll(self) -> list[dict]:
        if not self._client:
            return []

        events = []
        try:
            now = datetime.now(timezone.utc)
            end = now + timedelta(hours=self.lookahead_hours)

            data = self._client.get(
                "/me/calendarView",
                params={
                    "startDateTime": now.isoformat(),
                    "endDateTime": end.isoformat(),
                    "$top": str(self.events_per_poll),
                    "$orderby": "start/dateTime",
                    "$select": "id,subject,organizer,start,end,location,"
                               "isOnlineMeeting,onlineMeeting,isCancelled,"
                               "isAllDay,attendees,responseStatus",
                },
                headers={"Prefer": 'outlook.timezone="UTC"'},
            )
            cal_events = data.get("value", [])

            for cal_event in cal_events:
                eid = cal_event.get("id", "")
                if not eid or eid in self._seen_ids:
                    continue

                self._seen_ids.add(eid)
                event = _normalize_calendar_event(cal_event)
                events.append(event)

        except Exception as e:
            logger.error(f"Calendar poll error: {e}")

        return events

    async def stop(self):
        logger.info("Calendar adapter stopped")
