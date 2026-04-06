"""GitHub channel adapter — wraps github-agent's poller into a ChannelAdapter.

Polls issues, PRs, workflow failures, events, discussions, and notifications
for configured GitHub accounts. Normalizes into EventStore records.

Depends on: github-agent at _grobomo/github-agent/ (poller.py, normalizer.py, gh_cli.py)
"""

import logging
import os
import sys
import time
from typing import Optional

from .base import ChannelAdapter

logger = logging.getLogger(__name__)

# Add github-agent to path so we can import its modules
_GITHUB_AGENT_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "ProjectsCL1", "_grobomo", "github-agent"
)


def _ensure_github_agent():
    """Add github-agent to sys.path if not already there."""
    if _GITHUB_AGENT_DIR not in sys.path:
        sys.path.insert(0, _GITHUB_AGENT_DIR)


class GitHubAdapter(ChannelAdapter):
    """Polls GitHub repos and yields normalized events for EventStore."""

    def __init__(self, config: dict = None):
        super().__init__("github", config)
        self.accounts = self.config.get("accounts", ["grobomo"])
        self.repos = self.config.get("repos")  # None = discover all
        self._pollers = {}
        self._seen_ids: set[str] = set()

    @property
    def source(self) -> str:
        return "github"

    async def start(self):
        _ensure_github_agent()
        from github.poller import GitHubPoller

        for account in self.accounts:
            repos = self.repos if self.repos else None
            self._pollers[account] = GitHubPoller(account, repos)
            logger.info(f"GitHub adapter started for {account}")

    async def poll(self) -> list[dict]:
        _ensure_github_agent()
        from github import normalizer

        events = []

        for account, poller in self._pollers.items():
            try:
                raw = poller.poll_all()
            except Exception as e:
                logger.error(f"GitHub poll failed for {account}: {e}")
                continue

            # Normalize issues
            for repo, issues in raw.get("issues", {}).items():
                for issue in issues:
                    norm = normalizer.normalize_issue(issue, account, repo)
                    if self._is_new(norm):
                        events.append(self._to_event(norm))

            # Normalize PRs
            for repo, prs in raw.get("prs", {}).items():
                for pr in prs:
                    norm = normalizer.normalize_pr(pr, account, repo)
                    if self._is_new(norm):
                        events.append(self._to_event(norm))

            # Normalize repo events
            for repo, repo_events in raw.get("events", {}).items():
                for ev in repo_events:
                    norm = normalizer.normalize_event(ev, account, repo)
                    if self._is_new(norm):
                        events.append(self._to_event(norm))

            # Normalize workflow failures
            for repo, runs in raw.get("workflow_failures", {}).items():
                for run in runs:
                    norm = normalizer.normalize_workflow_run(run, account, repo)
                    if self._is_new(norm):
                        events.append(self._to_event(norm))

            # Normalize discussions
            for repo, discs in raw.get("discussions", {}).items():
                for disc in discs:
                    norm = normalizer.normalize_discussion(disc, account, repo)
                    if self._is_new(norm):
                        events.append(self._to_event(norm))

            # Normalize notifications
            for notif in raw.get("notifications", []):
                norm = normalizer.normalize_notification(notif, account)
                if self._is_new(norm):
                    events.append(self._to_event(norm))

        return events

    def _is_new(self, norm: dict) -> bool:
        eid = norm.get("event_id", "")
        if eid in self._seen_ids:
            return False
        self._seen_ids.add(eid)
        return True

    def _to_event(self, norm: dict) -> dict:
        """Convert github-agent normalized format to unified-brain EventStore format."""
        ts = norm.get("timestamp", "")
        if isinstance(ts, str) and ts:
            # ISO 8601 to unix timestamp
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                created_at = dt.timestamp()
            except (ValueError, TypeError):
                created_at = time.time()
        else:
            created_at = time.time()

        return {
            "id": norm.get("event_id", f"gh-{int(time.time() * 1000)}"),
            "source": "github",
            "channel": norm.get("channel", ""),
            "event_type": norm.get("event_type", "unknown"),
            "author": norm.get("actor", ""),
            "title": norm.get("title", ""),
            "body": norm.get("body", ""),
            "created_at": created_at,
            "metadata": norm.get("metadata", {}),
        }
