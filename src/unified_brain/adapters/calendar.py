"""Calendar channel adapter — polls calendar events via MS Graph API.

Ingests upcoming meetings and calendar changes as brain events.
Uses shared GraphClient for MS Graph access.

Config:
    token_path: str — path to msgraph tokens.json (local dev)
    tenant_id/client_id/client_secret: str — OAuth2 credentials (container)
    lookahead_hours: int — how far ahead to look for events (default 24)
    events_per_poll: int — max events per poll (default 50)
    enabled: bool — enable this adapter
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .base import BoundedSet, ChannelAdapter, parse_timestamp
from .graph_client import GraphClient

logger = logging.getLogger(__name__)


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
            self._client = GraphClient(
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
