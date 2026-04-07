"""Teams channel adapter — polls Teams chats via MS Graph API.

Self-contained: uses shared GraphClient for HTTP calls, no external dependencies.
Authenticates via client_credentials OAuth2 flow with tenant_id/client_id/client_secret.
"""

import logging
import re

from .base import ChannelAdapter, BoundedSet, parse_timestamp
from .graph_client import GraphClient

logger = logging.getLogger(__name__)


class TeamsAdapter(ChannelAdapter):
    """Polls Teams chats via MS Graph API and yields normalized events."""

    def __init__(self, config: dict = None, persona_registry=None):
        super().__init__("teams", config)
        self.chat_ids = self.config.get("chat_ids", [])
        self.poll_count = self.config.get("messages_per_poll", 20)
        self._client = None
        self._seen_ids = BoundedSet()
        self._persona_registry = persona_registry

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
            self._client = GraphClient(
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
                    text = re.sub(r'<[^>]+>', '', body_content).strip()
                    if not text:
                        continue

                    # Skip brain's own messages (identified by persona emoji prefix)
                    if self._persona_registry and self._persona_registry.is_own_message(text):
                        self._seen_ids.add(mid)
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
