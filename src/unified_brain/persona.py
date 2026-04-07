"""Persona system — per-user identity for brain responses.

Each user can assign the brain a custom name + emoji. When the brain
responds in a channel, it prefixes messages with that user's persona
emoji. Adapters use the persona registry to filter out the brain's
own messages (avoiding re-ingestion loops).

Config (in brain.yaml):
    persona:
        enforce_global: false          # true = same persona for all users
        default:
            name: "Brain"
            emoji: "🧠"
        users:
            joel:
                name: "Brain"
                emoji: "🧠"
            kush:
                name: "Mango"
                emoji: "🥭"
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class Persona:
    """A brain identity for a specific user."""

    __slots__ = ("name", "emoji")

    def __init__(self, name: str = "Brain", emoji: str = "🧠"):
        self.name = name
        self.emoji = emoji

    def prefix(self) -> str:
        """Message prefix: emoji + space."""
        return f"{self.emoji} "

    def format_message(self, content: str) -> str:
        """Prefix a brain response with this persona's emoji."""
        return f"{self.emoji} {content}"

    def to_dict(self) -> dict:
        return {"name": self.name, "emoji": self.emoji}

    def __repr__(self):
        return f"Persona({self.name!r}, {self.emoji!r})"


class PersonaRegistry:
    """Manages per-user personas with a global default.

    Args:
        config: persona config dict (see module docstring)
    """

    def __init__(self, config: dict = None):
        config = config or {}
        default = config.get("default", {})
        self._default = Persona(
            name=default.get("name", "Brain"),
            emoji=default.get("emoji", "🧠"),
        )
        self._enforce_global = config.get("enforce_global", False)
        self._users: dict[str, Persona] = {}
        self._lock = threading.Lock()

        # Load user personas from config
        for user, pconfig in config.get("users", {}).items():
            self._users[user] = Persona(
                name=pconfig.get("name", self._default.name),
                emoji=pconfig.get("emoji", self._default.emoji),
            )

    @property
    def default(self) -> Persona:
        return self._default

    def get(self, author: str) -> Persona:
        """Get persona for a user. Returns default if not configured or enforced."""
        if self._enforce_global:
            return self._default
        with self._lock:
            return self._users.get(author, self._default)

    def set(self, author: str, name: str = None, emoji: str = None):
        """Set or update a user's persona at runtime."""
        with self._lock:
            existing = self._users.get(author)
            self._users[author] = Persona(
                name=name or (existing.name if existing else self._default.name),
                emoji=emoji or (existing.emoji if existing else self._default.emoji),
            )

    def all_emojis(self) -> set[str]:
        """Return all known persona emojis (for message filtering)."""
        with self._lock:
            emojis = {self._default.emoji}
            emojis.update(p.emoji for p in self._users.values())
            return emojis

    def is_own_message(self, text: str) -> bool:
        """Check if a message was sent by the brain (starts with a persona emoji).

        Also detects persona emoji at the start of a quoted block:
            > 🧠 some previous brain response
            🧠 direct brain message
        """
        if not text:
            return False

        emojis = self.all_emojis()
        stripped = text.lstrip()

        # Direct message starting with persona emoji
        for emoji in emojis:
            if stripped.startswith(emoji):
                return True

        # Quoted message: lines starting with > followed by persona emoji
        # (when a user quotes a brain message in their reply)
        # We only check if the ENTIRE message is a quote of the brain
        # A user quoting brain in a reply (mixed content) is NOT filtered
        lines = stripped.split("\n")
        if all(self._is_quote_of_brain(line, emojis) or not line.strip() for line in lines):
            return True

        return False

    def _is_quote_of_brain(self, line: str, emojis: set[str]) -> bool:
        """Check if a line is a quote of a brain message."""
        stripped = line.lstrip()
        if not stripped.startswith(">"):
            return False
        after_quote = stripped[1:].lstrip()
        return any(after_quote.startswith(e) for e in emojis)

    def list_users(self) -> dict[str, dict]:
        """List all user persona assignments."""
        with self._lock:
            result = {"_default": self._default.to_dict()}
            for user, persona in self._users.items():
                result[user] = persona.to_dict()
            result["_enforce_global"] = self._enforce_global
            return result
