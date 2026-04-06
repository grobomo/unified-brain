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
    """Routes actions from the brain to their targets.

    Uses an outbox model for channel actions:
      data/outbox/dispatch/ — tasks for ccc-manager
      data/outbox/github/  — GitHub actions (comment, label, close)
      data/outbox/teams/   — Teams actions (reply, mention)
      data/outbox/email/   — Email actions (alert, notify)
    Each channel agent polls its outbox folder and executes.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        # Base outbox directory
        self.outbox_dir = Path(self.config.get("outbox_dir", "data/outbox"))
        # Bridge directory for ccc-manager integration
        self.bridge_dir = self.outbox_dir / "dispatch"
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        # Channel outboxes
        self.github_outbox = self.outbox_dir / "github"
        self.github_outbox.mkdir(parents=True, exist_ok=True)
        self.teams_outbox = self.outbox_dir / "teams"
        self.teams_outbox.mkdir(parents=True, exist_ok=True)
        self.email_outbox = self.outbox_dir / "email"
        self.email_outbox.mkdir(parents=True, exist_ok=True)
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
        """Write a GitHub action to the outbox for the GitHub agent to execute."""
        action_id = f"gh-{int(time.time() * 1000)}"
        event_id = action.get("event_id", "")
        outbox_entry = {
            "id": action_id,
            "action": "comment",
            "repo": channel,
            "event_id": event_id,
            "body": content,
            "created_at": time.time(),
        }
        path = self.github_outbox / f"{action_id}.json"
        path.write_text(json.dumps(outbox_entry, indent=2))
        return {"status": "queued", "target": f"github:{channel}", "outbox_path": str(path)}

    def _respond_teams(self, channel: str, content: str, action: dict) -> dict:
        """Write a Teams action to the outbox for the Teams agent to execute."""
        action_id = f"teams-{int(time.time() * 1000)}"
        outbox_entry = {
            "id": action_id,
            "action": "reply",
            "chat_id": channel,
            "body": content,
            "created_at": time.time(),
        }
        path = self.teams_outbox / f"{action_id}.json"
        path.write_text(json.dumps(outbox_entry, indent=2))
        return {"status": "queued", "target": f"teams:{channel}", "outbox_path": str(path)}

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
        """Write an alert to the email outbox."""
        action_id = f"alert-{int(time.time() * 1000)}"
        outbox_entry = {
            "id": action_id,
            "action": "email",
            "subject": action.get("content", "Brain Alert")[:100],
            "body": action.get("content", ""),
            "source": action.get("source"),
            "channel": action.get("channel"),
            "event_id": action.get("event_id"),
            "created_at": time.time(),
        }
        path = self.email_outbox / f"{action_id}.json"
        path.write_text(json.dumps(outbox_entry, indent=2))
        return {"status": "queued", "target": "email", "outbox_path": str(path)}

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
