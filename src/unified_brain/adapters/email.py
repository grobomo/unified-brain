"""Email channel adapter — polls inbox via MS Graph API.

Ingests emails as brain events. Uses the same _GraphClient as the Teams
adapter (token_path for local, client_credentials for containers).

Config:
    token_path: str — path to msgraph tokens.json (local dev)
    tenant_id/client_id/client_secret: str — OAuth2 credentials (container)
    folder: str — mail folder to poll (default "inbox")
    messages_per_poll: int — max messages per poll (default 20)
    filter: str — OData $filter for messages (optional)
    enabled: bool — enable this adapter
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import BoundedSet, ChannelAdapter, parse_timestamp

logger = logging.getLogger(__name__)


class _GraphClient:
    """Minimal MS Graph client — shared pattern with Teams adapter."""

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

    def get(self, path: str, params: dict = None) -> dict:
        token = self._get_token()
        url = f"{self.GRAPH_URL}{path}"
        if params:
            url += "?" + urlencode(params)

        req = Request(url)
        req.add_header("Authorization", f"Bearer {token}")

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            logger.error(f"Graph API error {e.code}: {path}")
            return {}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Graph API request failed: {e}")
            return {}


def _extract_text(body: dict) -> str:
    """Extract plain text from email body (HTML or text)."""
    content = body.get("content", "")
    content_type = body.get("contentType", "text")

    if content_type == "html":
        # Strip HTML tags
        text = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:5000]

    return content[:5000]


def _normalize_email(msg: dict, folder: str) -> dict:
    """Normalize a Graph API mail message into a brain event."""
    msg_id = msg.get("id", "")
    subject = msg.get("subject", "(no subject)")
    sender = msg.get("from", {}).get("emailAddress", {})
    sender_name = sender.get("name", "unknown")
    sender_email = sender.get("address", "")

    body = _extract_text(msg.get("body", {}))
    received = msg.get("receivedDateTime", "")
    is_read = msg.get("isRead", False)
    importance = msg.get("importance", "normal")
    has_attachments = msg.get("hasAttachments", False)

    # Build recipients list
    to_list = [r.get("emailAddress", {}).get("address", "")
               for r in msg.get("toRecipients", [])]
    cc_list = [r.get("emailAddress", {}).get("address", "")
               for r in msg.get("ccRecipients", [])]

    return {
        "id": f"email:{msg_id[:40]}",
        "source": "email",
        "channel": folder,
        "event_type": "email",
        "author": sender_name,
        "title": subject,
        "body": body,
        "created_at": parse_timestamp(received) if received else time.time(),
        "metadata": {
            "message_id": msg_id,
            "sender_email": sender_email,
            "sender_name": sender_name,
            "to": to_list[:10],
            "cc": cc_list[:10],
            "is_read": is_read,
            "importance": importance,
            "has_attachments": has_attachments,
            "folder": folder,
        },
    }


class EmailAdapter(ChannelAdapter):
    """Polls email inbox via MS Graph API and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("email", config)
        self.folder = self.config.get("folder", "inbox")
        self.poll_count = self.config.get("messages_per_poll", 20)
        self.filter = self.config.get("filter", "")
        self._client = None
        self._seen_ids = BoundedSet()

    @property
    def source(self) -> str:
        return "email"

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
            logger.error("Email adapter needs token_path or tenant_id/client_id/client_secret")
            return

        try:
            self._client = _GraphClient(
                tenant_id=tenant_id, client_id=client_id,
                client_secret=client_secret, token_path=token_path,
            )
            self._client._get_token()
            logger.info(f"Email adapter started ({auth_mode}), folder={self.folder}")
        except Exception as e:
            logger.error(f"Email adapter auth failed: {e}")
            self._client = None

    async def poll(self) -> list[dict]:
        if not self._client:
            return []

        events = []
        try:
            params = {
                "$top": str(self.poll_count),
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,toRecipients,ccRecipients,body,"
                           "receivedDateTime,isRead,importance,hasAttachments",
            }
            if self.filter:
                params["$filter"] = self.filter

            data = self._client.get(f"/me/mailFolders/{self.folder}/messages", params)
            messages = data.get("value", [])

            for msg in messages:
                mid = msg.get("id", "")
                if not mid or mid in self._seen_ids:
                    continue

                self._seen_ids.add(mid)
                event = _normalize_email(msg, self.folder)
                events.append(event)

        except Exception as e:
            logger.error(f"Email poll error: {e}")

        return events

    async def stop(self):
        logger.info("Email adapter stopped")
