"""Action dispatcher — routes brain decisions to their targets.

RESPOND -> post to originating channel (GitHub comment, Teams reply)
DISPATCH -> write task JSON to ccc-manager bridge directory or SQS
ALERT -> send email notification
LOG -> store in memory only
"""

import json
import time
from pathlib import Path

from .brain import RESPOND, DISPATCH, ALERT, LOG


class ActionDispatcher:
    """Routes actions from the brain to their targets."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        # Bridge directory for ccc-manager integration
        self.bridge_dir = Path(self.config.get("bridge_dir", "data/outbox"))
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        # Results directory (ccc-manager writes results here)
        self.results_dir = Path(self.config.get("results_dir", "data/inbox"))
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def dispatch(self, action: dict):
        """Route an action to its target."""
        action_type = action.get("action", LOG)

        if action_type == RESPOND:
            return self._respond(action)
        elif action_type == DISPATCH:
            return self._dispatch_to_manager(action)
        elif action_type == ALERT:
            return self._alert(action)
        else:
            return self._log(action)

    def _respond(self, action: dict) -> dict:
        """Post a response to the originating channel."""
        source = action.get("source", "")
        channel = action.get("channel", "")
        content = action.get("content", "")

        if source == "github":
            return self._respond_github(channel, content, action)
        elif source == "teams":
            return self._respond_teams(channel, content, action)
        else:
            return {"status": "unsupported", "source": source}

    def _respond_github(self, channel: str, content: str, action: dict) -> dict:
        """Post a GitHub comment. Requires gh CLI."""
        # TODO: Implement via gh CLI or GitHub API
        return {"status": "pending", "target": f"github:{channel}"}

    def _respond_teams(self, channel: str, content: str, action: dict) -> dict:
        """Send a Teams message. Requires teams-chat CLI."""
        # TODO: Implement via teams_chat.py
        return {"status": "pending", "target": f"teams:{channel}"}

    def _dispatch_to_manager(self, action: dict) -> dict:
        """Write task JSON to ccc-manager bridge directory.

        Format matches ccc-manager's BridgeInput expectations:
        - BridgeInput reads: text (-> summary), classification (-> type), request_id (-> id)
        - We include both canonical and BridgeInput-compatible field names
        """
        task_id = f"brain-{int(time.time() * 1000)}"
        content = action.get("content", "")
        task = {
            # Canonical fields (ccc-manager base)
            "id": task_id,
            "source": f"{action.get('source')}:{action.get('channel')}",
            "type": "fix",
            "summary": content,
            "priority": action.get("metadata", {}).get("priority", "normal"),
            # BridgeInput compatibility fields
            "request_id": task_id,
            "text": content,
            "classification": "fix",
            # Context for result relay
            "details": action.get("metadata", {}),
            "channel_context": {
                "reply_to": f"{action.get('source')}:{action.get('channel')}:{action.get('event_id')}",
                "source": action.get("source"),
                "channel": action.get("channel"),
                "event_id": action.get("event_id"),
            },
        }

        task_path = self.bridge_dir / f"{task_id}.json"
        task_path.write_text(json.dumps(task, indent=2))

        return {"status": "dispatched", "task_id": task_id, "path": str(task_path)}

    def _alert(self, action: dict) -> dict:
        """Send an alert (email, webhook, etc.)."""
        # TODO: Implement via email-manager or webhook
        return {"status": "pending", "target": "alert"}

    def _log(self, action: dict) -> dict:
        """Store in memory only — no external action."""
        return {"status": "logged", "event_id": action.get("event_id")}

    def poll_results(self) -> list[dict]:
        """Poll ccc-manager's results directory for completed tasks."""
        results = []
        for path in self.results_dir.glob("*.json"):
            try:
                result = json.loads(path.read_text())
                results.append(result)
                # Move to processed
                done_dir = self.results_dir / "done"
                done_dir.mkdir(exist_ok=True)
                path.rename(done_dir / path.name)
            except (json.JSONDecodeError, OSError):
                pass
        return results
