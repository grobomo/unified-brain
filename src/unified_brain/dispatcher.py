"""Action dispatcher — routes brain decisions to their targets.

RESPOND -> post to originating channel (GitHub comment, Teams reply)
DISPATCH -> write task JSON to ccc-manager bridge (filesystem or SQS)
ALERT -> send email notification
LOG -> store in memory only

Transport is pluggable: filesystem outbox (local, K8s PVC) or SQS (AWS EC2).
Config selects transport: dispatcher.transport = file|sqs
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .brain import RESPOND, DISPATCH, ALERT, LOG

logger = logging.getLogger(__name__)


class DispatchTransport(ABC):
    """Abstract transport for DISPATCH actions (tasks to ccc-manager)."""

    @abstractmethod
    def send_task(self, task: dict) -> dict:
        """Send a task dict. Returns status dict."""
        ...

    @abstractmethod
    def poll_results(self) -> list[dict]:
        """Poll for completed task results."""
        ...


class FileTransport(DispatchTransport):
    """Filesystem-based dispatch: writes JSON to bridge directory.

    Used locally and on K8s (PVC-backed directory). ccc-manager's
    BridgeInput reads from the same directory.
    """

    def __init__(self, bridge_dir: str | Path, results_dir: str | Path):
        self.bridge_dir = Path(bridge_dir)
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def send_task(self, task: dict) -> dict:
        task_id = task["id"]
        task_path = self.bridge_dir / f"{task_id}.json"
        task_path.write_text(json.dumps(task, indent=2))
        return {"status": "dispatched", "task_id": task_id, "path": str(task_path)}

    def poll_results(self) -> list[dict]:
        results = []
        for path in self.results_dir.glob("*.json"):
            try:
                result = json.loads(path.read_text())
                results.append(result)
                done_dir = self.results_dir / "done"
                done_dir.mkdir(exist_ok=True)
                path.rename(done_dir / path.name)
            except (json.JSONDecodeError, OSError):
                pass
        return results


class SQSTransport(DispatchTransport):
    """AWS SQS-based dispatch: sends task JSON as SQS messages.

    Used on EC2. ccc-manager's SQSInput reads from the task queue.
    Results come back on a separate result queue.
    """

    def __init__(self, task_queue_url: str, result_queue_url: str = "",
                 region: str = "us-east-1"):
        self.task_queue_url = task_queue_url
        self.result_queue_url = result_queue_url
        self.region = region
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("sqs", region_name=self.region)
        return self._client

    def send_task(self, task: dict) -> dict:
        task_id = task["id"]
        try:
            resp = self.client.send_message(
                QueueUrl=self.task_queue_url,
                MessageBody=json.dumps(task),
                MessageAttributes={
                    "task_id": {"StringValue": task_id, "DataType": "String"},
                    "source": {"StringValue": task.get("source", ""), "DataType": "String"},
                },
            )
            msg_id = resp.get("MessageId", "")
            logger.info(f"SQS task sent: {task_id} -> {msg_id}")
            return {"status": "dispatched", "task_id": task_id, "message_id": msg_id}
        except Exception as e:
            logger.error(f"SQS send failed for {task_id}: {e}")
            return {"status": "error", "task_id": task_id, "error": str(e)}

    def poll_results(self) -> list[dict]:
        if not self.result_queue_url:
            return []
        results = []
        try:
            resp = self.client.receive_message(
                QueueUrl=self.result_queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
            )
            for msg in resp.get("Messages", []):
                try:
                    result = json.loads(msg["Body"])
                    results.append(result)
                    self.client.delete_message(
                        QueueUrl=self.result_queue_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception as e:
            logger.error(f"SQS poll failed: {e}")
        return results


def _create_transport(config: dict) -> DispatchTransport:
    """Factory: create dispatch transport from config."""
    transport_type = config.get("transport", "file")

    if transport_type == "sqs":
        return SQSTransport(
            task_queue_url=config.get("sqs_task_queue_url", ""),
            result_queue_url=config.get("sqs_result_queue_url", ""),
            region=config.get("sqs_region", "us-east-1"),
        )

    # Default: filesystem
    outbox_dir = config.get("outbox_dir", "data/outbox")
    return FileTransport(
        bridge_dir=str(Path(outbox_dir) / "dispatch"),
        results_dir=config.get("results_dir", "data/inbox"),
    )


class ActionDispatcher:
    """Routes actions from the brain to their targets.

    Uses an outbox model for channel actions:
      data/outbox/dispatch/ — tasks for ccc-manager
      data/outbox/github/  — GitHub actions (comment, label, close)
      data/outbox/teams/   — Teams actions (reply, mention)
      data/outbox/email/   — Email actions (alert, notify)
    Each channel agent polls its outbox folder and executes.

    DISPATCH transport is pluggable: file (default) or sqs.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.transport = _create_transport(self.config)

        # Channel outboxes (always filesystem — these are local agent queues)
        self.outbox_dir = Path(self.config.get("outbox_dir", "data/outbox"))
        self.github_outbox = self.outbox_dir / "github"
        self.github_outbox.mkdir(parents=True, exist_ok=True)
        self.teams_outbox = self.outbox_dir / "teams"
        self.teams_outbox.mkdir(parents=True, exist_ok=True)
        self.email_outbox = self.outbox_dir / "email"
        self.email_outbox.mkdir(parents=True, exist_ok=True)

        # Backwards compat: expose bridge_dir and results_dir for tests
        if isinstance(self.transport, FileTransport):
            self.bridge_dir = self.transport.bridge_dir
            self.results_dir = self.transport.results_dir
        else:
            # SQS mode: still create dirs for channel outboxes, but dispatch goes to SQS
            self.bridge_dir = self.outbox_dir / "dispatch"
            self.bridge_dir.mkdir(parents=True, exist_ok=True)
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
        """Send task to ccc-manager via configured transport (file or SQS).

        Format matches ccc-manager's BridgeInput/SQSInput expectations.
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

        return self.transport.send_task(task)

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
        """Poll for completed tasks via configured transport."""
        return self.transport.poll_results()
