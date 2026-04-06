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
            "You are analyzing an event from a multi-channel monitoring system.",
            "Decide what action to take: RESPOND, DISPATCH, ALERT, or LOG.",
            "",
            f"Source: {event.get('source')}",
            f"Channel: {event.get('channel')}",
            f"Type: {event.get('event_type')}",
            f"Author: {event.get('author')}",
            f"Title: {event.get('title')}",
            f"Body: {event.get('body', '')[:2000]}",
        ]

        if context:
            parts.append("")
            parts.append("## Context")
            parts.append(json.dumps(context, indent=2, default=str)[:2000])

        parts.extend([
            "",
            "## Response Format (JSON only)",
            '{"action": "respond|dispatch|alert|log", "content": "...", "reason": "..."}',
        ])

        return "\n".join(parts)

    def _call_claude(self, prompt: str) -> str | None:
        try:
            result = subprocess.run(
                [self.claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=self.timeout,
                env={"CLAUDE_CODE_ENTRYPOINT": "unified-brain"},
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
