"""System monitor channel adapter — polls JSON event files for focus-steal events.

Ingests events from ~/.system-monitor/events/*.json, normalizing each file
into a brain event. Consumed files are renamed to .processed to avoid reprocessing.

Config:
    events_dir: str — path to events directory (default ~/.system-monitor/events)
    enabled: bool — enable this adapter
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .base import ChannelAdapter, parse_timestamp

logger = logging.getLogger(__name__)

_DEFAULT_EVENTS_DIR = os.path.join(os.path.expanduser("~"), ".system-monitor", "events")


def _normalize_event(filepath: str, data: dict) -> dict | None:
    """Parse a system-monitor JSON event into a normalized event dict."""
    event_type = data.get("type", "unknown")
    ts = data.get("timestamp", "")
    process = data.get("process", {})
    classification = data.get("classification", "UNKNOWN")
    source_project = data.get("source_project")
    parent_chain = data.get("parent_chain", "")

    proc_name = process.get("name", "unknown")
    pid = process.get("pid", 0)
    cmd_line = process.get("command_line", "")
    exe_path = process.get("exe_path", "")

    # Build a readable title
    if source_project:
        title = f"{event_type}: {proc_name} from {source_project}"
    else:
        title = f"{event_type}: {proc_name} (pid {pid})"

    # Build body with useful context
    body_parts = [f"Process: {proc_name} (pid {pid})"]
    if exe_path:
        body_parts.append(f"Path: {exe_path}")
    if cmd_line:
        body_parts.append(f"Command: {cmd_line[:500]}")
    if parent_chain:
        body_parts.append(f"Chain: {parent_chain}")
    body_parts.append(f"Classification: {classification}")
    if source_project:
        body_parts.append(f"Source project: {source_project}")

    # Use filename as stable event ID
    event_id = f"sysmon:{Path(filepath).stem}"

    return {
        "id": event_id,
        "source": "system-monitor",
        "channel": "focus-events",
        "event_type": event_type,
        "author": proc_name,
        "title": title,
        "body": "\n".join(body_parts),
        "created_at": parse_timestamp(ts) if ts else 0,
        "metadata": {
            "classification": classification,
            "source_project": source_project,
            "process_name": proc_name,
            "pid": pid,
            "exe_path": exe_path,
            "command_line": cmd_line[:1000],
            "parent_chain": parent_chain,
            "original_file": os.path.basename(filepath),
        },
    }


class SystemMonitorAdapter(ChannelAdapter):
    """Polls system-monitor event JSON files and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("system-monitor", config)
        self._events_dir = os.path.expanduser(
            self.config.get("events_dir", _DEFAULT_EVENTS_DIR)
        )

    @property
    def source(self) -> str:
        return "system-monitor"

    async def start(self):
        logger.info(f"SystemMonitor adapter started: events_dir={self._events_dir}")

    async def poll(self) -> list[dict]:
        events = []

        if not os.path.isdir(self._events_dir):
            return events

        try:
            filenames = sorted(os.listdir(self._events_dir))
        except OSError as e:
            logger.error(f"Error listing {self._events_dir}: {e}")
            return events

        for filename in filenames:
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(self._events_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                event = _normalize_event(filepath, data)
                if event:
                    events.append(event)

                # Mark as consumed by renaming to .processed
                processed_path = filepath + ".processed"
                os.rename(filepath, processed_path)

            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON in {filepath}: {e}")
                # Rename bad files too so we don't retry them forever
                try:
                    os.rename(filepath, filepath + ".error")
                except OSError:
                    pass
            except OSError as e:
                logger.error(f"Error reading {filepath}: {e}")

        return events

    async def stop(self):
        logger.info("SystemMonitor adapter stopped")
