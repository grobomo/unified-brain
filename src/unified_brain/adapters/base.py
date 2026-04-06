"""Base channel adapter interface.

Each adapter polls one source and yields normalized events suitable for EventStore.
Adapters are intentionally thin — all intelligence lives in the brain.
"""

import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime, timezone


# Max dedup entries per adapter — prevents unbounded memory growth
_DEFAULT_SEEN_MAX = 10_000


def parse_timestamp(ts) -> float:
    """Parse ISO 8601 string or passthrough numeric timestamp to unix float."""
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return time.time()


class BoundedSet:
    """Set with a max size — evicts oldest entries when full."""

    def __init__(self, maxsize: int = _DEFAULT_SEEN_MAX):
        self._data: OrderedDict = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, item):
        return item in self._data

    def add(self, item):
        if item in self._data:
            self._data.move_to_end(item)
            return
        self._data[item] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)


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
