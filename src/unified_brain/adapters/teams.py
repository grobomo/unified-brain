"""Teams channel adapter — wraps teams-agent's poller into a ChannelAdapter.

Polls Teams chats via MS Graph API and yields normalized events for EventStore.

Depends on: teams-agent at _tmemu/teams-agent/ (poller, msgraph, registry, store)
Also requires: msgraph-lib at ~/Documents/ProjectsCL1/msgraph-lib/ (shared token management)
"""

import logging
import os
import sys
import time
from typing import Optional

from .base import ChannelAdapter, BoundedSet, parse_timestamp

logger = logging.getLogger(__name__)

_TEAMS_AGENT_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "ProjectsCL1", "_tmemu", "teams-agent"
)
_MSGRAPH_LIB_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "ProjectsCL1", "_tmemu", "msgraph-lib"
)


def _ensure_teams_deps():
    """Add teams-agent and msgraph-lib to sys.path."""
    for d in [_TEAMS_AGENT_DIR, _MSGRAPH_LIB_DIR]:
        if d not in sys.path:
            sys.path.insert(0, d)


class TeamsAdapter(ChannelAdapter):
    """Polls Teams chats and yields normalized events for EventStore."""

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
        _ensure_teams_deps()
        try:
            from lib.msgraph.auth import TokenManager

            self._client = TokenManager().get_client()
            logger.info(f"Teams adapter started, watching {len(self.chat_ids)} chats")
        except ImportError as e:
            logger.error(f"Teams adapter dependencies not available: {e}")
        except Exception as e:
            logger.error(f"Teams adapter auth failed: {e}")

    async def poll(self) -> list[dict]:
        if not self._client:
            return []

        _ensure_teams_deps()
        from lib.msgraph.teams import get_chat_messages, parse_message

        events = []
        for chat_id in self.chat_ids:
            try:
                messages = get_chat_messages(
                    self._client, chat_id,
                    top=self.poll_count,
                    order_by="lastModifiedDateTime desc",
                )
                for msg in messages:
                    parsed = parse_message(msg)
                    mid = parsed.get("message_id", "")
                    if not mid or mid in self._seen_ids:
                        continue
                    if not parsed.get("text", "").strip():
                        continue

                    self._seen_ids.add(mid)
                    events.append(self._to_event(parsed, chat_id))

            except Exception as e:
                logger.error(f"Teams poll error for chat {chat_id[:20]}...: {e}")

        return events

    def _to_event(self, parsed: dict, chat_id: str) -> dict:
        """Convert teams-agent parsed message to EventStore format."""
        return {
            "id": f"teams:{chat_id[:20]}:{parsed.get('message_id', '')}",
            "source": "teams",
            "channel": chat_id,
            "event_type": "message",
            "author": parsed.get("sender_name", "unknown"),
            "title": parsed.get("text", "")[:100],
            "body": parsed.get("text", ""),
            "created_at": parse_timestamp(parsed.get("timestamp", "")),
            "metadata": {
                "sender_id": parsed.get("sender_id", ""),
                "message_id": parsed.get("message_id", ""),
            },
        }
