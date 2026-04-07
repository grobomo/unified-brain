"""Hook-runner channel adapter — polls JSONL log files for hook events.

Ingests hook-log.jsonl and self-reflection.jsonl from hook-runner,
normalizing each line into brain events. Uses byte offset tracking
for efficient incremental reads of append-only files.

Config:
    hook_log_path: str — path to hook-log.jsonl (default ~/.claude/hooks/hook-log.jsonl)
    reflection_log_path: str — path to self-reflection.jsonl
    enabled: bool — enable this adapter
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from .base import ChannelAdapter, parse_timestamp

logger = logging.getLogger(__name__)


class _FilePoller:
    """Tracks byte offset in an append-only file, yields new lines each poll."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self._offset = 0
        # Start at end of file on first poll (don't replay entire history)
        self._initialized = False

    def read_new_lines(self) -> list[str]:
        """Read lines appended since last poll. Returns list of line strings."""
        if not os.path.exists(self.path):
            return []

        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []

        # First poll: seek to end (only process new lines going forward)
        if not self._initialized:
            self._offset = size
            self._initialized = True
            return []

        # File was truncated/rotated — reset to beginning
        if size < self._offset:
            logger.info(f"File rotated: {self.path} (was {self._offset}, now {size})")
            self._offset = 0

        if size == self._offset:
            return []

        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()

            lines = [line.strip() for line in data.split("\n") if line.strip()]
            return lines
        except OSError as e:
            logger.error(f"Error reading {self.path}: {e}")
            return []


def _normalize_hook_log(line: str) -> dict | None:
    """Parse a hook-log.jsonl line into a normalized event."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_name = data.get("event", "unknown")
    module = data.get("module", "unknown")
    result = data.get("result", "")
    elapsed = data.get("elapsed_ms", 0)

    # Determine event type from the log entry
    if result in ("block", "blocked"):
        event_type = "gate_block"
    elif result in ("allow", "allowed", "pass"):
        event_type = "gate_allow"
    elif "error" in str(result).lower():
        event_type = "gate_error"
    else:
        event_type = "gate_decision"

    line_hash = hashlib.sha256(line.encode()).hexdigest()[:12]
    ts = data.get("ts", data.get("timestamp", ""))

    return {
        "id": f"hooklog:{line_hash}",
        "source": "hook-runner",
        "channel": "hook-log",
        "event_type": event_type,
        "author": module,
        "title": f"{event_name}: {result}" if result else event_name,
        "body": line[:5000],
        "created_at": parse_timestamp(ts) if ts else time.time(),
        "metadata": {
            "event": event_name,
            "module": module,
            "result": result,
            "elapsed_ms": elapsed,
            "reason": data.get("reason", ""),
        },
    }


def _normalize_reflection(line: str) -> dict | None:
    """Parse a self-reflection.jsonl line into a normalized event."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    verdict = data.get("verdict", "unknown")
    issues = data.get("issues", [])
    todos = data.get("todos", [])

    line_hash = hashlib.sha256(line.encode()).hexdigest()[:12]
    ts = data.get("ts", data.get("timestamp", ""))

    title = f"Reflection: {verdict}"
    if issues:
        title += f" ({len(issues)} issues)"

    body_parts = []
    if issues:
        body_parts.append("Issues: " + "; ".join(str(i) for i in issues[:10]))
    if todos:
        body_parts.append("TODOs: " + "; ".join(str(t) for t in todos[:10]))

    return {
        "id": f"reflect:{line_hash}",
        "source": "hook-runner",
        "channel": "self-reflection",
        "event_type": "reflection_result",
        "author": "self-reflection",
        "title": title,
        "body": "\n".join(body_parts) if body_parts else line[:2000],
        "created_at": parse_timestamp(ts) if ts else time.time(),
        "metadata": {
            "verdict": verdict,
            "issue_count": len(issues),
            "todo_count": len(todos),
        },
    }


class HookRunnerAdapter(ChannelAdapter):
    """Polls hook-runner JSONL logs and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("hook-runner", config)
        hooks_dir = os.path.expanduser("~/.claude/hooks")
        self._hook_log = _FilePoller(
            self.config.get("hook_log_path", os.path.join(hooks_dir, "hook-log.jsonl"))
        )
        self._reflection_log = _FilePoller(
            self.config.get("reflection_log_path", os.path.join(hooks_dir, "self-reflection.jsonl"))
        )

    @property
    def source(self) -> str:
        return "hook-runner"

    async def start(self):
        logger.info(f"HookRunner adapter started: "
                    f"hook-log={self._hook_log.path}, "
                    f"reflection={self._reflection_log.path}")

    async def poll(self) -> list[dict]:
        events = []

        # Read new hook log lines
        for line in self._hook_log.read_new_lines():
            event = _normalize_hook_log(line)
            if event:
                events.append(event)

        # Read new reflection lines
        for line in self._reflection_log.read_new_lines():
            event = _normalize_reflection(line)
            if event:
                events.append(event)

        return events

    async def stop(self):
        logger.info("HookRunner adapter stopped")
