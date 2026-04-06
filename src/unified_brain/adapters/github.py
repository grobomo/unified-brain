"""GitHub channel adapter — polls GitHub repos via `gh` CLI.

Self-contained: no external dependencies beyond `gh` CLI (installed everywhere).
Polls issues, PRs, events, and workflow failures for configured repos.
"""

import json
import logging
import subprocess
import time

from .base import ChannelAdapter, BoundedSet, parse_timestamp

logger = logging.getLogger(__name__)


def _gh_api(endpoint: str, per_page: int = 30) -> list | dict:
    """Call GitHub API via `gh api`. Returns parsed JSON."""
    try:
        sep = "&" if "?" in endpoint else "?"
        result = subprocess.run(
            ["gh", "api", f"{endpoint}{sep}per_page={per_page}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        if result.stderr:
            logger.debug(f"gh api stderr: {result.stderr[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"gh api error for {endpoint}: {e}")
    return []


class GitHubAdapter(ChannelAdapter):
    """Polls GitHub repos via `gh` CLI and yields normalized events."""

    def __init__(self, config: dict = None):
        super().__init__("github", config)
        self.repos = self.config.get("repos", [])
        self._seen_ids = BoundedSet()

    @property
    def source(self) -> str:
        return "github"

    async def start(self):
        # Verify gh CLI is available
        try:
            result = subprocess.run(
                ["gh", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                logger.info(f"GitHub adapter started, watching {len(self.repos)} repos")
            else:
                logger.error("gh CLI not working — GitHub adapter may fail")
        except FileNotFoundError:
            logger.error("gh CLI not found — GitHub adapter disabled")

    async def poll(self) -> list[dict]:
        events = []
        for repo in self.repos:
            events.extend(self._poll_issues(repo))
            events.extend(self._poll_prs(repo))
            events.extend(self._poll_events(repo))
            events.extend(self._poll_workflow_failures(repo))
        return events

    def _poll_issues(self, repo: str) -> list[dict]:
        items = _gh_api(f"repos/{repo}/issues?state=open&sort=updated&direction=desc")
        if not isinstance(items, list):
            return []
        events = []
        for item in items:
            if item.get("pull_request"):
                continue  # Skip PRs listed as issues
            eid = f"gh:issue:{repo}:{item.get('number')}"
            if eid in self._seen_ids:
                continue
            self._seen_ids.add(eid)
            events.append({
                "id": eid,
                "source": "github",
                "channel": repo,
                "event_type": "issue",
                "author": (item.get("user") or {}).get("login", ""),
                "title": item.get("title", ""),
                "body": (item.get("body") or "")[:5000],
                "created_at": parse_timestamp(item.get("created_at", "")),
                "metadata": {
                    "number": item.get("number"),
                    "state": item.get("state"),
                    "labels": [l.get("name") for l in item.get("labels", [])],
                    "url": item.get("html_url", ""),
                },
            })
        return events

    def _poll_prs(self, repo: str) -> list[dict]:
        items = _gh_api(f"repos/{repo}/pulls?state=open&sort=updated&direction=desc")
        if not isinstance(items, list):
            return []
        events = []
        for item in items:
            eid = f"gh:pr:{repo}:{item.get('number')}"
            if eid in self._seen_ids:
                continue
            self._seen_ids.add(eid)
            events.append({
                "id": eid,
                "source": "github",
                "channel": repo,
                "event_type": "pr",
                "author": (item.get("user") or {}).get("login", ""),
                "title": item.get("title", ""),
                "body": (item.get("body") or "")[:5000],
                "created_at": parse_timestamp(item.get("created_at", "")),
                "metadata": {
                    "number": item.get("number"),
                    "state": item.get("state"),
                    "draft": item.get("draft", False),
                    "url": item.get("html_url", ""),
                },
            })
        return events

    def _poll_events(self, repo: str) -> list[dict]:
        items = _gh_api(f"repos/{repo}/events", per_page=10)
        if not isinstance(items, list):
            return []
        events = []
        for item in items:
            eid = f"gh:event:{item.get('id', '')}"
            if eid in self._seen_ids:
                continue
            self._seen_ids.add(eid)
            event_type = item.get("type", "unknown").replace("Event", "").lower()
            payload = item.get("payload", {})
            # Build a meaningful title from the event
            title = event_type
            if event_type == "push":
                commits = payload.get("commits", [])
                title = f"Push: {len(commits)} commit(s)"
                if commits:
                    title += f" — {commits[-1].get('message', '')[:80]}"
            elif event_type == "create":
                title = f"Created {payload.get('ref_type', 'ref')}: {payload.get('ref', '')}"
            elif event_type == "delete":
                title = f"Deleted {payload.get('ref_type', 'ref')}: {payload.get('ref', '')}"
            elif event_type == "pullrequest":
                action = payload.get("action", "")
                pr = payload.get("pull_request", {})
                pr_num = pr.get("number", "")
                pr_title = pr.get("title", "")
                title = f"PR #{pr_num} {action}" + (f": {pr_title}" if pr_title else "")

            events.append({
                "id": eid,
                "source": "github",
                "channel": repo,
                "event_type": event_type,
                "author": (item.get("actor") or {}).get("login", ""),
                "title": title,
                "body": "",
                "created_at": parse_timestamp(item.get("created_at", "")),
                "metadata": {"type": item.get("type")},
            })
        return events

    def _poll_workflow_failures(self, repo: str) -> list[dict]:
        data = _gh_api(f"repos/{repo}/actions/runs?status=failure&per_page=5")
        if not isinstance(data, dict):
            return []
        runs = data.get("workflow_runs", [])
        events = []
        for run in runs:
            eid = f"gh:run:{repo}:{run.get('id')}"
            if eid in self._seen_ids:
                continue
            self._seen_ids.add(eid)
            events.append({
                "id": eid,
                "source": "github",
                "channel": repo,
                "event_type": "workflow_failure",
                "author": (run.get("actor") or {}).get("login", ""),
                "title": f"CI failure: {run.get('name', '')} on {run.get('head_branch', '')}",
                "body": f"Conclusion: {run.get('conclusion')}, URL: {run.get('html_url', '')}",
                "created_at": parse_timestamp(run.get("created_at", "")),
                "metadata": {
                    "run_id": run.get("id"),
                    "workflow": run.get("name"),
                    "branch": run.get("head_branch"),
                    "url": run.get("html_url", ""),
                },
            })
        return events
