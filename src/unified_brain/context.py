"""Cross-channel context builder — enriches brain analysis with related events.

When analyzing a GitHub event, pulls in Teams messages from the same project
and events from the same author. Gives the brain a unified view across channels.
"""

import logging
from typing import Optional

from .store import EventStore
from .registry import ProjectRegistry

log = logging.getLogger(__name__)


class ContextBuilder:
    """Builds cross-channel context for brain analysis."""

    def __init__(self, store: EventStore, registry: ProjectRegistry, config: dict = None,
                 memory=None, feedback=None):
        self.store = store
        self.registry = registry
        self.memory = memory
        self.feedback = feedback
        self.config = config or {}
        self.context_hours = self.config.get("context_hours", 48)
        self.max_related = self.config.get("max_related_events", 10)
        self.max_author = self.config.get("max_author_events", 5)

    def build(self, event: dict) -> dict:
        """Build context for an event.

        Returns:
            Dict with keys:
                same_channel: recent events from the same channel
                related_channels: events from other channels in the same project
                author_activity: recent events by the same author across all channels
                project: project info from registry (if found)
        """
        source = event.get("source", "")
        channel = event.get("channel", "")
        author = event.get("author", "")

        context = {
            "same_channel": self._same_channel(channel),
            "related_channels": [],
            "author_activity": [],
            "project": None,
            "memory": None,
            "feedback": None,
        }

        # Include feedback stats if available
        if self.feedback:
            context["feedback"] = self.feedback.summary(hours=self.context_hours)

        # Find project via registry
        project = self.registry.find_by_channel(source, channel)
        if project:
            project_name = project.get("name")
            context["project"] = {
                "name": project_name,
                "worker_type": project.get("worker_type"),
            }
            context["related_channels"] = self._related_channels(
                project, source, channel
            )
            # Include memory context if available
            if self.memory and project_name:
                context["memory"] = self.memory.get_context_for_project(project_name)

        # Author activity across all channels
        if author:
            context["author_activity"] = self._author_activity(
                author, source, channel
            )

        return context

    def _same_channel(self, channel: str) -> list[dict]:
        """Recent events from the same channel."""
        events = self.store.recent(
            hours=self.context_hours, channel=channel
        )
        return self._summarize_events(events[:self.max_related])

    def _related_channels(self, project: dict, current_source: str, current_channel: str) -> list[dict]:
        """Events from other channels in the same project."""
        related = []

        # Check all repos in the project
        for repo in project.get("repos", []):
            if current_source == "github" and repo == current_channel:
                continue
            events = self.store.recent(
                hours=self.context_hours, source="github", channel=repo
            )
            related.extend(events)

        # Check all Teams chats in the project
        for chat_id in project.get("teams_chats", []):
            if current_source == "teams" and chat_id == current_channel:
                continue
            events = self.store.recent(
                hours=self.context_hours, source="teams", channel=chat_id
            )
            related.extend(events)

        # Sort by time, take most recent
        related.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        return self._summarize_events(related[:self.max_related])

    def _author_activity(self, author: str, current_source: str, current_channel: str) -> list[dict]:
        """Recent events by this author in other channels."""
        author_events = self.store.recent(
            hours=self.context_hours, author=author,
            limit=self.max_author + 10,  # fetch extra to filter current channel
        )
        # Exclude events from the current channel
        filtered = [
            e for e in author_events
            if not (e.get("source") == current_source and e.get("channel") == current_channel)
        ]
        return self._summarize_events(filtered[:self.max_author])

    def _summarize_events(self, events: list[dict]) -> list[dict]:
        """Extract key fields for context (not the full event)."""
        return [
            {
                "id": e.get("id"),
                "source": e.get("source"),
                "channel": e.get("channel"),
                "event_type": e.get("event_type"),
                "author": e.get("author"),
                "title": e.get("title"),
                "body": (e.get("body") or "")[:500],
                "created_at": e.get("created_at"),
            }
            for e in events
        ]
