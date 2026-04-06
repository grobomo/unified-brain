"""Teams channel adapter — polls Teams chats via MS Graph API.

Self-contained: uses `requests` for HTTP calls, no external dependencies.
Authenticates via client_credentials OAuth2 flow with tenant_id/client_id/client_secret.
"""

import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

from .base import ChannelAdapter, BoundedSet, parse_timestamp

logger = logging.getLogger(__name__)


class _GraphClient:
    """Minimal MS Graph client using only stdlib (urllib).

    Supports two auth modes:
    - client_credentials: tenant_id/client_id/client_secret (for containers/EC2)
    - token_path: path to a JSON file with access_token (for local, uses msgraph-lib)
    """

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
        """Acquire token — from file or client_credentials flow."""
        # Mode 1: Read from token file (local dev, managed by msgraph-lib)
        if self.token_path:
            return self._get_token_from_file()

        # Mode 2: Client credentials flow (containers/EC2)
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
        """Read access_token from a JSON file (e.g. ~/.msgraph/tokens.json)."""
        import os
        path = os.path.expanduser(self.token_path)
        try:
            with open(path) as f:
                data = json.load(f)
            token = data.get("access_token", "")
            if not token:
                logger.error(f"No access_token in {path}")
            return token
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to read token from {path}: {e}")
            return ""

    def get(self, path: str, params: dict = None) -> dict:
        """GET from MS Graph API."""
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


class TeamsAdapter(ChannelAdapter):
    """Polls Teams chats via MS Graph API and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("teams", config)
        self.chat_ids = self.config.get("chat_ids", [])
        self.poll_count = self.config.get("messages_per_poll", 20)
        self._client = None
        self._seen_ids = BoundedSet()

    @property
    def source(self) -> str:
        return "teams"

    async def start(self):
        token_path = self.config.get("token_path", "")
        tenant_id = self.config.get("tenant_id", "")
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")

        if token_path:
            # Local mode: read token from file (managed by msgraph-lib)
            auth_mode = "token_path"
        elif all([tenant_id, client_id, client_secret]):
            # Container/EC2 mode: client credentials
            auth_mode = "client_credentials"
        else:
            logger.error("Teams adapter needs either token_path or tenant_id/client_id/client_secret")
            return

        try:
            self._client = _GraphClient(
                tenant_id=tenant_id, client_id=client_id,
                client_secret=client_secret, token_path=token_path,
            )
            self._client._get_token()
            logger.info(f"Teams adapter started ({auth_mode}), watching {len(self.chat_ids)} chats")
        except Exception as e:
            logger.error(f"Teams adapter auth failed: {e}")
            self._client = None

    async def poll(self) -> list[dict]:
        if not self._client:
            return []

        events = []
        for chat_id in self.chat_ids:
            try:
                data = self._client.get(
                    f"/chats/{chat_id}/messages",
                    params={
                        "$top": str(self.poll_count),
                        "$orderby": "lastModifiedDateTime desc",
                    },
                )
                messages = data.get("value", [])
                for msg in messages:
                    mid = msg.get("id", "")
                    if not mid or mid in self._seen_ids:
                        continue

                    # Extract text from message body
                    body_content = (msg.get("body") or {}).get("content", "")
                    # Strip HTML tags (simple approach)
                    import re
                    text = re.sub(r'<[^>]+>', '', body_content).strip()
                    if not text:
                        continue

                    self._seen_ids.add(mid)
                    sender = (msg.get("from") or {}).get("user") or {}
                    events.append({
                        "id": f"teams:{chat_id[:20]}:{mid}",
                        "source": "teams",
                        "channel": chat_id,
                        "event_type": "message",
                        "author": sender.get("displayName", "unknown"),
                        "title": text[:100],
                        "body": text,
                        "created_at": parse_timestamp(msg.get("createdDateTime", "")),
                        "metadata": {
                            "sender_id": sender.get("id", ""),
                            "message_id": mid,
                        },
                    })
            except Exception as e:
                logger.error(f"Teams poll error for chat {chat_id[:20]}...: {e}")

        return events
