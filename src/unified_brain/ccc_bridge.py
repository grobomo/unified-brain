"""CCC bridge — dispatches WORK tasks to claude-portable via git relay.

Brain writes task JSON to a relay repo's requests/pending/ directory.
CCC's git-dispatch.py picks up pending tasks, dispatches to workers,
and writes results to requests/completed/ or requests/failed/.

Brain polls completed/failed dirs for results.

Relay repo layout:
    requests/pending/      Brain writes here
    requests/dispatched/   CCC moves here when worker picks up
    requests/completed/    Worker moves here when done
    requests/failed/       On error

Task JSON (pending):
    { "id": str, "sender": str, "text": str, "context": list[str],
      "source_project": str, "priority": str, "created_at": str }

Result JSON (completed/failed):
    { "id": str, "success": bool, "output": str, "error": str,
      "worker": str, "completed_at": str, "pr_url": str }

Config:
    relay_dir: str — local path to the relay repo (cloned)
    relay_repo_url: str — git URL for relay repo (for clone)
    sender: str — identifier for brain as task sender
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid

logger = logging.getLogger(__name__)


class CCCBridge:
    """Dispatches WORK tasks to CCC workers via git relay repo."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.relay_dir = os.path.expanduser(
            self.config.get("relay_dir", "/data/relay-repo")
        )
        self.relay_repo_url = self.config.get("relay_repo_url", "")
        self.sender = self.config.get("sender", "unified-brain")
        self._pending_dir = os.path.join(self.relay_dir, "requests", "pending")
        self._completed_dir = os.path.join(self.relay_dir, "requests", "completed")
        self._failed_dir = os.path.join(self.relay_dir, "requests", "failed")

    def _ensure_dirs(self):
        """Create relay directory structure if it doesn't exist."""
        for d in [self._pending_dir, self._completed_dir, self._failed_dir,
                  os.path.join(self.relay_dir, "requests", "dispatched")]:
            os.makedirs(d, exist_ok=True)

    def dispatch(self, task: dict) -> dict:
        """Write a WORK task to the relay repo's pending directory.

        Args:
            task: dict with at least 'content' (the prompt/instructions).
                  Optional: 'id', 'source_project', 'priority', 'context'.

        Returns:
            dict with 'status' ('dispatched' or 'error') and 'task_id'.
        """
        self._ensure_dirs()

        task_id = task.get("id", str(uuid.uuid4())[:12])
        source_project = task.get("metadata", {}).get("source_project", "")
        priority = task.get("metadata", {}).get("priority", "normal")
        context = task.get("metadata", {}).get("context", [])

        request = {
            "id": task_id,
            "sender": self.sender,
            "text": task.get("content", ""),
            "context": context if isinstance(context, list) else [],
            "source_project": source_project,
            "priority": priority,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        filepath = os.path.join(self._pending_dir, f"{task_id}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(request, f, indent=2)
            logger.info(f"CCC task dispatched: {task_id} -> {filepath}")

            # Try to git add + commit + push (non-blocking, best-effort)
            self._git_push(f"brain: dispatch {task_id}")

            return {"status": "dispatched", "task_id": task_id}
        except OSError as e:
            logger.error(f"Failed to write CCC task {task_id}: {e}")
            return {"status": "error", "task_id": task_id, "error": str(e)}

    def poll_results(self) -> list[dict]:
        """Poll for completed/failed tasks and return results.

        Reads and removes result files from completed/ and failed/ dirs.
        """
        results = []

        for dirpath, success in [(self._completed_dir, True), (self._failed_dir, False)]:
            if not os.path.isdir(dirpath):
                continue

            try:
                filenames = os.listdir(dirpath)
            except OSError:
                continue

            for filename in filenames:
                if not filename.endswith(".json"):
                    continue

                filepath = os.path.join(dirpath, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    result = {
                        "id": data.get("id", filename.replace(".json", "")),
                        "success": success and data.get("success", success),
                        "output": data.get("output", ""),
                        "error": data.get("error", ""),
                        "worker": data.get("worker", ""),
                        "pr_url": data.get("pr_url", ""),
                        "completed_at": data.get("completed_at", ""),
                    }
                    results.append(result)

                    # Remove consumed result file
                    os.remove(filepath)
                    logger.info(f"CCC result consumed: {result['id']} "
                                f"({'success' if result['success'] else 'failed'})")

                except json.JSONDecodeError as e:
                    logger.warning(f"Bad JSON in result file {filepath}: {e}")
                    try:
                        os.rename(filepath, filepath + ".error")
                    except OSError:
                        pass
                except OSError as e:
                    logger.error(f"Error reading result {filepath}: {e}")

        return results

    def get_pending_count(self) -> int:
        """Return number of pending tasks (not yet picked up by CCC)."""
        if not os.path.isdir(self._pending_dir):
            return 0
        try:
            return len([f for f in os.listdir(self._pending_dir) if f.endswith(".json")])
        except OSError:
            return 0

    def _git_push(self, message: str):
        """Best-effort git add, commit, push on the relay repo."""
        if not os.path.isdir(os.path.join(self.relay_dir, ".git")):
            return

        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.relay_dir, capture_output=True, timeout=15,
            )
            subprocess.run(
                ["git", "commit", "-m", message, "--allow-empty"],
                cwd=self.relay_dir, capture_output=True, timeout=15,
            )
            subprocess.run(
                ["git", "push"],
                cwd=self.relay_dir, capture_output=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"Git push failed (non-fatal): {e}")
