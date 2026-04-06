"""Action executor — actively executes RESPOND actions via channel APIs.

When enabled, the dispatcher calls the executor instead of writing to outbox files.
If execution fails, the action is still written to the outbox as a fallback.

GitHub: posts comments via `gh api`
Teams: sends messages via MS Graph API
"""

import json
import logging
import subprocess
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class ActionExecutor:
    """Executes RESPOND actions directly via channel APIs."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._teams_token = None
        self._teams_token_expires = 0

    def respond_github(self, repo: str, event_id: str, body: str) -> dict:
        """Post a comment on a GitHub issue/PR via `gh api`.

        Args:
            repo: owner/repo
            event_id: event ID (e.g. "gh:issue:owner/repo:42")
            body: comment text
        """
        # Extract issue/PR number from event_id
        number = self._extract_number(event_id)
        if not number:
            return {"status": "error", "error": f"Cannot extract issue number from {event_id}"}

        try:
            result = subprocess.run(
                [
                    "gh", "api",
                    f"repos/{repo}/issues/{number}/comments",
                    "-f", f"body={body}",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                resp = json.loads(result.stdout) if result.stdout.strip() else {}
                url = resp.get("html_url", "")
                logger.info(f"GitHub comment posted on {repo}#{number}: {url}")
                return {"status": "executed", "url": url}
            else:
                error = result.stderr[:200] if result.stderr else "unknown error"
                logger.error(f"GitHub comment failed on {repo}#{number}: {error}")
                return {"status": "error", "error": error}
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(f"GitHub comment failed: {e}")
            return {"status": "error", "error": str(e)}

    def respond_teams(self, chat_id: str, body: str) -> dict:
        """Send a message to a Teams chat via MS Graph API.

        Uses the same auth config as the Teams adapter.
        """
        import time

        token = self._get_teams_token()
        if not token:
            return {"status": "error", "error": "No Teams token available"}

        url = f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"
        payload = json.dumps({
            "body": {"contentType": "text", "content": body},
        }).encode()

        req = Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                msg_id = data.get("id", "")
                logger.info(f"Teams message sent to {chat_id[:20]}...: {msg_id}")
                return {"status": "executed", "message_id": msg_id}
        except HTTPError as e:
            error = f"HTTP {e.code}"
            try:
                error += f": {e.read().decode()[:200]}"
            except Exception:
                pass
            logger.error(f"Teams message failed: {error}")
            return {"status": "error", "error": error}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Teams message failed: {e}")
            return {"status": "error", "error": str(e)}

    def _get_teams_token(self) -> str:
        """Get Teams token from config — token_path or client_credentials."""
        import os
        import time

        # Token path mode (local)
        token_path = self.config.get("teams_token_path", "")
        if token_path:
            path = os.path.expanduser(token_path)
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get("access_token", "")
            except (FileNotFoundError, json.JSONDecodeError):
                return ""

        # Client credentials mode (container/EC2)
        if self._teams_token and time.time() < self._teams_token_expires - 60:
            return self._teams_token

        tenant_id = self.config.get("teams_tenant_id", "")
        client_id = self.config.get("teams_client_id", "")
        client_secret = self.config.get("teams_client_secret", "")
        if not all([tenant_id, client_id, client_secret]):
            return ""

        url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        data = urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()

        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
                self._teams_token = body["access_token"]
                self._teams_token_expires = time.time() + body.get("expires_in", 3600)
                return self._teams_token
        except (URLError, HTTPError, KeyError) as e:
            logger.error(f"Teams token acquisition failed: {e}")
            return ""

    @staticmethod
    def _extract_number(event_id: str) -> str:
        """Extract issue/PR number from event ID.

        Handles formats:
          gh:issue:owner/repo:42
          gh:pr:owner/repo:42
          gh:event:12345
        """
        if not event_id:
            return ""
        parts = event_id.split(":")
        # gh:issue:owner/repo:42 or gh:pr:owner/repo:42
        if len(parts) >= 4 and parts[0] == "gh" and parts[1] in ("issue", "pr"):
            return parts[-1]
        return ""
