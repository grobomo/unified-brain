"""Brain analyzer — LLM-powered event analysis with cross-channel context.

Processes events from EventStore, decides actions (RESPOND, DISPATCH, ALERT, LOG),
and maintains three-tier memory.

LLM backend is pluggable: subprocess (claude -p) or HTTP API (Anthropic).
"""

import json
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)
llm_logger = logging.getLogger("unified-brain.llm")


def _log_llm_call(backend_type: str, prompt: str, response: str | None,
                   elapsed: float, success: bool):
    """Log an LLM call to the dedicated LLM logger as JSONL + update metrics."""
    from .metrics import llm_calls_total, llm_active, llm_duration

    outcome = "success" if success else "failure"
    llm_calls_total.inc(backend=backend_type, outcome=outcome)
    llm_active.inc(-1, backend=backend_type)
    llm_duration.set(elapsed, backend=backend_type)

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend": backend_type,
        "prompt_len": len(prompt),
        "prompt_first_200": prompt[:200],
        "response_len": len(response) if response else 0,
        "response": response[:2000] if response else None,
        "elapsed_s": round(elapsed, 2),
        "success": success,
    }
    llm_logger.info(json.dumps(record, ensure_ascii=False))


def _mark_llm_start(backend_type: str):
    """Mark the start of an LLM call for concurrency tracking."""
    from .metrics import llm_active
    llm_active.inc(1, backend=backend_type)

# Action types the brain can produce
RESPOND = "respond"    # Reply in the originating channel (GitHub comment, Teams message)
DISPATCH = "dispatch"  # Send task to ccc-manager for worker execution
ALERT = "alert"        # Send email/notification
LOG = "log"            # Store in memory only, no action


class LLMBackend(ABC):
    """Abstract LLM backend — pluggable per environment."""

    @abstractmethod
    def call(self, prompt: str) -> str | None:
        """Send prompt, return response text or None on failure."""
        ...


class SubprocessBackend(LLMBackend):
    """Calls `claude -p` as a subprocess. For local development."""

    def __init__(self, claude_path: str = "claude", timeout: int = 60):
        self.claude_path = claude_path
        self.timeout = timeout

    def call(self, prompt: str) -> str | None:
        _mark_llm_start("subprocess")
        t0 = time.monotonic()
        try:
            env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "unified-brain"}
            result = subprocess.run(
                [self.claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=self.timeout,
                env=env,
            )
            if result.returncode == 0:
                response = result.stdout.strip()
                _log_llm_call("subprocess", prompt, response, time.monotonic() - t0, True)
                return response
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        _log_llm_call("subprocess", prompt, None, time.monotonic() - t0, False)
        return None


class APIBackend(LLMBackend):
    """Calls Anthropic Messages API directly. For containers/EC2/K8s."""

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514",
                 max_tokens: int = 1024, timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout

    def call(self, prompt: str) -> str | None:
        _mark_llm_start("api")
        body = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = Request(self.API_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", "2023-06-01")

        t0 = time.monotonic()
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                content = data.get("content", [])
                if content and content[0].get("type") == "text":
                    response = content[0]["text"].strip()
                    _log_llm_call("api", prompt, response, time.monotonic() - t0, True)
                    return response
        except (URLError, HTTPError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Anthropic API error: {e}")
        _log_llm_call("api", prompt, None, time.monotonic() - t0, False)
        return None


def _create_backend(config: dict) -> LLMBackend:
    """Factory: create LLM backend from config."""
    backend_type = config.get("llm_backend", "subprocess")

    if backend_type == "api":
        api_key = config.get("api_key", os.environ.get("ANTHROPIC_API_KEY", ""))
        if not api_key:
            logger.warning("No API key for Anthropic — falling back to subprocess")
            return SubprocessBackend(config.get("claude_path", "claude"), config.get("timeout", 60))
        return APIBackend(
            api_key=api_key,
            model=config.get("model", "claude-sonnet-4-20250514"),
            max_tokens=config.get("max_tokens", 1024),
            timeout=config.get("timeout", 60),
        )

    return SubprocessBackend(config.get("claude_path", "claude"), config.get("timeout", 60))


class BrainAnalyzer:
    """Analyzes events and produces actions."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.backend = _create_backend(self.config)
        # Keep for backwards compat in tests
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
        result = self.backend.call(prompt)

        if result:
            return self._parse_action(result, event)

        # Rule-based fallback when LLM is unavailable
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
                project_mem = memory.get("project_memory", {})
                if project_mem:
                    summary = project_mem.get("summary", {})
                    if summary:
                        parts.append("")
                        parts.append("## Project Memory")
                        parts.append(f"Events: {summary.get('event_count', 0)}, "
                                     f"Authors: {', '.join(summary.get('authors', []))}, "
                                     f"Types: {summary.get('event_types', {})}")
                global_mem = memory.get("global_memory")
                if global_mem:
                    parts.append("")
                    parts.append("## Global Patterns")
                    parts.append(f"Total events: {global_mem.get('total_events', 0)}, "
                                 f"Active projects: {global_mem.get('active_projects', 0)}, "
                                 f"Most active: {global_mem.get('most_active', 'unknown')}")

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

            # Loop pattern history (from loop analyzer)
            loop_ctx = context.get("loop_context")
            if loop_ctx:
                parts.append("")
                parts.append(loop_ctx)

            # Feedback from previous actions
            feedback = context.get("feedback")
            if feedback:
                has_stats = any(k != "recent_failures" and isinstance(feedback.get(k), dict)
                                for k in feedback)
                if has_stats:
                    parts.append("")
                    parts.append("## Recent Outcomes (feedback loop)")
                    for action_name, action_stats in feedback.items():
                        if action_name == "recent_failures" or not isinstance(action_stats, dict):
                            continue
                        parts.append(
                            f"- {action_name.upper()}: {action_stats.get('success', 0)}/{action_stats.get('total', 0)} "
                            f"succeeded ({action_stats.get('rate', 0):.0%})"
                        )
                failures = feedback.get("recent_failures", [])
                if failures:
                    parts.append("Recent failures:")
                    for f in failures[:3]:
                        parts.append(f"  - {f.get('action')} on {f.get('source')}:{f.get('channel')}: {f.get('error', '?')}")

        parts.extend([
            "",
            "## Response Format",
            "Reply with ONLY a JSON object, no markdown:",
            '{"action": "respond|dispatch|alert|log", "content": "message or task description", "reason": "brief explanation"}',
        ])

        return "\n".join(parts)

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
