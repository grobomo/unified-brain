"""Slack channel adapter — polls Slack channels via Web API.

Self-contained: uses stdlib urllib only, no slack-sdk dependency.
Authenticates via Bot User OAuth Token (xoxb-...).

Config:
    bot_token: str — Slack Bot User OAuth Token
    channel_ids: list[str] — channel IDs to monitor (e.g. ["C0123456789"])
    messages_per_poll: int — messages per channel per poll (default 20)
"""

import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

from .base import ChannelAdapter, BoundedSet, parse_timestamp

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"


class _SlackClient:
    """Minimal Slack Web API client using only stdlib (urllib)."""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token

    def get(self, method: str, params: dict = None) -> dict:
        """Call a Slack Web API method (GET)."""
        url = f"{_SLACK_API}/{method}"
        if params:
            url += "?" + urlencode(params)

        req = Request(url)
        req.add_header("Authorization", f"Bearer {self.bot_token}")

        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if not data.get("ok"):
                    error = data.get("error", "unknown")
                    logger.error(f"Slack API error: {method} -> {error}")
                return data
        except HTTPError as e:
            logger.error(f"Slack API HTTP error {e.code}: {method}")
            return {"ok": False, "error": f"http_{e.code}"}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Slack API request failed: {e}")
            return {"ok": False, "error": str(e)}

    def post(self, method: str, payload: dict) -> dict:
        """Call a Slack Web API method (POST with JSON body)."""
        url = f"{_SLACK_API}/{method}"
        body = json.dumps(payload).encode()

        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.bot_token}")
        req.add_header("Content-Type", "application/json; charset=utf-8")

        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if not data.get("ok"):
                    error = data.get("error", "unknown")
                    logger.error(f"Slack API error: {method} -> {error}")
                return data
        except HTTPError as e:
            logger.error(f"Slack API HTTP error {e.code}: {method}")
            return {"ok": False, "error": f"http_{e.code}"}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Slack API request failed: {e}")
            return {"ok": False, "error": str(e)}


class SlackAdapter(ChannelAdapter):
    """Polls Slack channels via Web API and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("slack", config)
        self.channel_ids = self.config.get("channel_ids", [])
        self.poll_count = self.config.get("messages_per_poll", 20)
        self._client = None
        self._seen_ids = BoundedSet()
        # Track latest timestamp per channel for incremental polling
        self._channel_cursors: dict[str, str] = {}

    @property
    def source(self) -> str:
        return "slack"

    async def start(self):
        bot_token = self.config.get("bot_token", "")
        if not bot_token:
            logger.error("Slack adapter needs bot_token config")
            return

        self._client = _SlackClient(bot_token)

        # Verify token with auth.test
        result = self._client.get("auth.test")
        if result.get("ok"):
            bot_name = result.get("user", "unknown")
            team = result.get("team", "unknown")
            logger.info(f"Slack adapter started as @{bot_name} in {team}, "
                        f"watching {len(self.channel_ids)} channels")
        else:
            logger.error(f"Slack auth failed: {result.get('error', 'unknown')}")
            self._client = None

    async def poll(self) -> list[dict]:
        if not self._client:
            return []

        events = []
        for channel_id in self.channel_ids:
            try:
                params = {
                    "channel": channel_id,
                    "limit": str(self.poll_count),
                }
                # Use cursor-based incremental polling
                oldest = self._channel_cursors.get(channel_id)
                if oldest:
                    params["oldest"] = oldest

                data = self._client.get("conversations.history", params)
                if not data.get("ok"):
                    logger.error(f"Slack poll error for {channel_id}: {data.get('error')}")
                    continue

                messages = data.get("messages", [])
                newest_ts = oldest or "0"

                for msg in messages:
                    # Skip bot messages, system messages, and thread replies
                    if msg.get("subtype") in ("bot_message", "channel_join",
                                                "channel_leave", "channel_topic",
                                                "channel_purpose"):
                        continue

                    ts = msg.get("ts", "")
                    if not ts or ts in self._seen_ids:
                        continue

                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    self._seen_ids.add(ts)

                    # Track newest timestamp for next poll
                    if ts > newest_ts:
                        newest_ts = ts

                    user_id = msg.get("user", "unknown")
                    events.append({
                        "id": f"slack:{channel_id}:{ts}",
                        "source": "slack",
                        "channel": channel_id,
                        "event_type": "message",
                        "author": user_id,
                        "title": text[:100],
                        "body": text,
                        "created_at": parse_timestamp(ts),
                        "metadata": {
                            "user_id": user_id,
                            "ts": ts,
                            "thread_ts": msg.get("thread_ts", ""),
                            "channel_id": channel_id,
                        },
                    })

                # Update cursor to newest message timestamp
                if newest_ts != (oldest or "0"):
                    self._channel_cursors[channel_id] = newest_ts

            except Exception as e:
                logger.error(f"Slack poll error for {channel_id}: {e}")

        return events

    async def stop(self):
        self._client = None
        logger.info("Slack adapter stopped")
