"""Base channel adapter interface.

Each adapter polls one source and yields normalized events suitable for EventStore.
Adapters are intentionally thin — all intelligence lives in the brain.
"""

from abc import ABC, abstractmethod


class ChannelAdapter(ABC):
    """Abstract base for channel adapters."""

    def __init__(self, name: str, config: dict = None):
        self.name = name
        self.config = config or {}

    @property
    @abstractmethod
    def source(self) -> str:
        """Source identifier (e.g., 'github', 'teams')."""
        ...

    @abstractmethod
    async def poll(self) -> list[dict]:
        """Poll for new events. Returns normalized event dicts.

        Each event dict must have:
            id: str           — unique event ID
            source: str       — same as self.source
            channel: str      — repo name, chat ID, etc.
            event_type: str   — 'issue', 'comment', 'message', 'pr', etc.
            author: str       — who created the event
            title: str        — short summary
            body: str         — full content
            created_at: float — unix timestamp
            metadata: dict    — source-specific data (optional)
        """
        ...

    async def start(self):
        """Optional setup (e.g., auth, websocket connect)."""
        pass

    async def stop(self):
        """Optional cleanup."""
        pass
