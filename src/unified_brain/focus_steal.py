"""Focus-steal action router — handles system-monitor focus_steal events.

When a focus_steal event arrives with a known source_project, this module:
1. Appends a fix TODO to that project's TODO.md
2. Returns a dispatch action to trigger a fix session (context_reset)

When source_project is unknown, it logs to Tier 2 memory as system noise.

Config:
    projects_root: str — root directory containing projects (e.g. ~/Documents/ProjectsCL1)
    context_reset_script: str — path to context_reset.py
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Fix suggestions per process type
_FIX_SUGGESTIONS = {
    "cmd.exe": "Use CREATE_NO_WINDOW flag or subprocess.CREATE_NO_WINDOW in Python",
    "powershell.exe": "Use -WindowStyle Hidden flag",
    "python.exe": "Use subprocess creationflags=0x08000000 (CREATE_NO_WINDOW)",
    "conhost.exe": "Parent process should use CREATE_NO_WINDOW to suppress console host",
    "node.exe": "Use child_process spawn with windowsHide: true",
}


def _get_fix_suggestion(process_name: str, command_line: str) -> str:
    """Return a fix suggestion based on the process that stole focus."""
    name_lower = process_name.lower()
    for key, suggestion in _FIX_SUGGESTIONS.items():
        if key in name_lower:
            return suggestion

    # Generic fallback
    return f"Investigate why {process_name} opens a visible window and suppress it"


def _build_todo_entry(event: dict) -> str:
    """Build a TODO.md entry for a focus-steal event."""
    metadata = event.get("metadata", {})
    process_name = metadata.get("process_name", "unknown")
    pid = metadata.get("pid", 0)
    cmd_line = metadata.get("command_line", "")
    parent_chain = metadata.get("parent_chain", "")
    classification = metadata.get("classification", "UNKNOWN")
    ts = event.get("created_at", time.time())

    if isinstance(ts, (int, float)):
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    else:
        ts_str = str(ts)

    fix = _get_fix_suggestion(process_name, cmd_line)

    lines = [
        f"- [ ] Fix focus-steal: {process_name} (pid {pid}) at {ts_str}",
    ]
    if cmd_line:
        lines.append(f"  - Command: `{cmd_line[:200]}`")
    if parent_chain:
        lines.append(f"  - Chain: {parent_chain}")
    lines.append(f"  - Classification: {classification}")
    lines.append(f"  - Fix: {fix}")

    return "\n".join(lines)


def _find_todo_file(projects_root: str, source_project: str) -> str | None:
    """Resolve the TODO.md path for a source project.

    source_project can be:
      - Relative: "_grobomo/system-monitor"
      - Absolute: "C:/Users/.../system-monitor"
    """
    if os.path.isabs(source_project):
        todo = os.path.join(source_project, "TODO.md")
    else:
        todo = os.path.join(os.path.expanduser(projects_root), source_project, "TODO.md")

    # Check the directory exists (TODO.md may not exist yet, that's OK)
    parent = os.path.dirname(todo)
    if os.path.isdir(parent):
        return todo
    return None


def handle_focus_steal(event: dict, memory=None, config: dict = None) -> dict | None:
    """Handle a focus_steal event from system-monitor.

    Args:
        event: normalized brain event from SystemMonitorAdapter
        memory: MemoryManager instance (for Tier 2 noise tracking)
        config: dict with projects_root and context_reset_script paths

    Returns:
        Action dict (dispatch/log) or None if no action needed.
    """
    config = config or {}
    metadata = event.get("metadata", {})
    source_project = metadata.get("source_project")
    process_name = metadata.get("process_name", "unknown")

    # No source project = system noise
    if not source_project:
        if memory:
            _track_system_noise(memory, event)
        return {
            "action": "log",
            "content": f"System noise: {process_name} (no source project)",
            "metadata": metadata,
        }

    # Known source project — write TODO and optionally dispatch fix
    projects_root = config.get("projects_root", "~/Documents/ProjectsCL1")
    todo_path = _find_todo_file(projects_root, source_project)

    if not todo_path:
        logger.warning(f"Cannot find project dir for {source_project}")
        return {
            "action": "log",
            "content": f"Focus-steal from {source_project} but project dir not found",
            "metadata": metadata,
        }

    # Append TODO entry
    todo_entry = _build_todo_entry(event)
    try:
        _append_todo(todo_path, todo_entry)
        logger.info(f"Wrote focus-steal TODO to {todo_path}")
    except OSError as e:
        logger.error(f"Failed to write TODO to {todo_path}: {e}")
        return {
            "action": "log",
            "content": f"Focus-steal from {source_project}: failed to write TODO: {e}",
            "metadata": metadata,
        }

    # Build dispatch action for context_reset (brain decides if it should actually dispatch)
    context_reset = config.get("context_reset_script", "")
    if context_reset:
        project_dir = os.path.dirname(todo_path)
        return {
            "action": "dispatch",
            "content": f"Fix focus-steal in {source_project}",
            "metadata": {
                **metadata,
                "todo_path": todo_path,
                "command": f"python {context_reset} --project-dir {project_dir}",
            },
        }

    return {
        "action": "log",
        "content": f"Focus-steal TODO written to {source_project} (no dispatch configured)",
        "metadata": {**metadata, "todo_path": todo_path},
    }


def _append_todo(todo_path: str, entry: str):
    """Append a focus-steal entry to a project's TODO.md.

    Creates a Focus-Steal Fixes section if it doesn't exist.
    Avoids duplicating entries with the same process+command.
    """
    section_header = "## Focus-Steal Fixes"

    # Read existing content
    existing = ""
    if os.path.exists(todo_path):
        with open(todo_path, "r", encoding="utf-8") as f:
            existing = f.read()

    # Check for duplicate (same first line = same process/pid/timestamp)
    first_line = entry.split("\n")[0]
    if first_line in existing:
        logger.debug(f"Duplicate focus-steal entry, skipping: {first_line[:80]}")
        return

    if section_header in existing:
        # Append under existing section
        with open(todo_path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")
    else:
        # Create section at end of file
        with open(todo_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n{section_header}\n{entry}\n")


def _track_system_noise(memory, event: dict):
    """Track unknown-source events in Tier 2 memory for pattern analysis."""
    metadata = event.get("metadata", {})
    process_name = metadata.get("process_name", "unknown")

    project_mem = memory.get_project_memory("system-monitor")
    noise_data = project_mem.get("noise_tracking") if project_mem else None
    if not noise_data:
        noise_data = {"processes": {}, "total_count": 0}

    noise_data["total_count"] = noise_data.get("total_count", 0) + 1
    procs = noise_data.get("processes", {})
    procs[process_name] = procs.get(process_name, 0) + 1
    noise_data["processes"] = procs
    noise_data["last_seen"] = time.time()

    memory.set_project_memory("system-monitor", "noise_tracking", noise_data)
