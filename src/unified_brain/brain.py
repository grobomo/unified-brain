"""Brain analyzer — LLM-powered event analysis with cross-channel context.

Processes events from EventStore, decides actions (RESPOND, DISPATCH, ALERT, LOG),
and maintains three-tier memory.

Heritage: github-agent/core/brain.py, extended for multi-channel + memory tiers.
"""

import json
import subprocess
import time
from pathlib import Path


# Action types the brain can produce
RESPOND = "respond"    # Reply in the originating channel (GitHub comment, Teams message)
DISPATCH = "dispatch"  # Send task to ccc-manager for worker execution
ALERT = "alert"        # Send email/notification
LOG = "log"            # Store in memory only, no action


class BrainAnalyzer:
    """Analyzes events and produces actions."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.claude_path = self.config.get("claude_path", "claude")
        self.timeout = self.config.get("timeout", 60)

    def analyze(self, event: dict, context: dict = None) -> dict:
        """Analyze an event and return an action.

        Args:
            event: Normalized event from EventStore
            context: Cross-channel context (recent events, memory, project info)

        Returns:
            Action dict with:
                action: str — RESPOND, DISPATCH, ALERT, or LOG
                channel: str — target channel for the action
                content: str — message body or task description
                metadata: dict — action-specific data
        """
        prompt = self._build_prompt(event, context)
        result = self._call_claude(prompt)

        if result:
            return self._parse_action(result, event)

        # Rule-based fallback when claude is unavailable
        return self._fallback_analyze(event)

    def _build_prompt(self, event: dict, context: dict = None) -> str:
        parts = [
            "You are the brain of a multi-channel monitoring system that watches GitHub repos and Teams chats.",
            "Analyze this event and decide what action to take.",
            "",
            "## Actions",
            "- RESPOND: Reply in the originating channel (GitHub comment or Teams message). Use for questions, acknowledgments, quick answers.",
            "- DISPATCH: Send a task to a worker for execution (bug fixes, investigations, code changes). Use for work that needs doing.",
            "- ALERT: Send an email notification. Use for urgent issues, security events, or things needing human attention.",
            "- LOG: Record in memory only, no action. Use for routine updates, FYI messages, status reports.",
            "",
            "## Event",
            f"Source: {event.get('source')}",
            f"Channel: {event.get('channel')}",
            f"Type: {event.get('event_type')}",
            f"Author: {event.get('author')}",
            f"Title: {event.get('title')}",
            f"Body: {event.get('body', '')[:2000]}",
        ]

        if context:
            # Project context
            project = context.get("project")
            if project:
                parts.append("")
                parts.append(f"## Project: {project.get('name', 'unknown')}")
                parts.append(f"Worker type: {project.get('worker_type', 'local')}")

            # Memory (project summary + global patterns)
            memory = context.get("memory")
            if memory:
                if memory.get("project_summary"):
                    parts.append("")
                    parts.append("## Project Memory")
                    parts.append(memory["project_summary"][:1000])
                if memory.get("global_summary"):
                    parts.append("")
                    parts.append("## Global Patterns")
                    parts.append(memory["global_summary"][:500])

            # Same-channel recent activity
            same = context.get("same_channel", [])
            if same:
                parts.append("")
                parts.append(f"## Recent in this channel ({len(same)} events)")
                for e in same[:5]:
                    parts.append(f"- [{e.get('event_type')}] {e.get('author')}: {e.get('title', '')[:100]}")

            # Cross-channel activity
            related = context.get("related_channels", [])
            if related:
                parts.append("")
                parts.append(f"## Related channels ({len(related)} events)")
                for e in related[:5]:
                    parts.append(f"- [{e.get('source')}/{e.get('channel')}] {e.get('author')}: {e.get('title', '')[:100]}")

            # Author activity across channels
            author = context.get("author_activity", [])
            if author:
                parts.append("")
                parts.append(f"## This author's other activity ({len(author)} events)")
                for e in author[:3]:
                    parts.append(f"- [{e.get('source')}] {e.get('title', '')[:100]}")

        parts.extend([
            "",
            "## Response Format",
            "Reply with ONLY a JSON object, no markdown:",
            '{"action": "respond|dispatch|alert|log", "content": "message or task description", "reason": "brief explanation"}',
        ])

        return "\n".join(parts)

    def _call_claude(self, prompt: str) -> str | None:
        try:
            import os
            env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "unified-brain"}
            result = subprocess.run(
                [self.claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=self.timeout,
                env=env,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _parse_action(self, raw: str, event: dict) -> dict:
        # Try to extract JSON from response
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(cleaned)
            return {
                "action": parsed.get("action", LOG),
                "channel": event.get("channel"),
                "source": event.get("source"),
                "event_id": event.get("id"),
                "content": parsed.get("content", ""),
                "reason": parsed.get("reason", ""),
                "metadata": parsed.get("metadata", {}),
            }
        except (json.JSONDecodeError, KeyError):
            return self._fallback_analyze(event)

    def _fallback_analyze(self, event: dict) -> dict:
        """Rule-based fallback when LLM is unavailable."""
        # Simple heuristics
        event_type = event.get("event_type", "")
        body = (event.get("body") or "").lower()

        # Issues mentioning "bug", "error", "broken" -> DISPATCH
        if event_type in ("issue", "alert") and any(w in body for w in ("bug", "error", "broken", "fix")):
            return {
                "action": DISPATCH,
                "channel": event.get("channel"),
                "source": event.get("source"),
                "event_id": event.get("id"),
                "content": f"Investigate: {event.get('title')}",
                "reason": "Rule-based: issue mentions error/bug keywords",
                "metadata": {},
            }

        # Everything else -> LOG
        return {
            "action": LOG,
            "channel": event.get("channel"),
            "source": event.get("source"),
            "event_id": event.get("id"),
            "content": "",
            "reason": "Rule-based: no action trigger detected",
            "metadata": {},
        }
