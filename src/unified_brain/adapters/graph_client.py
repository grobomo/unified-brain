"""Shared MS Graph API client for Teams, Email, and Calendar adapters.

Supports two auth modes:
- token_path: path to a JSON file with access_token (local, uses msgraph-lib)
- client_credentials: tenant_id/client_id/client_secret (containers/EC2)
"""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class GraphClient:
    """Minimal MS Graph client using only stdlib (urllib)."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    GRAPH_URL = "https://graph.microsoft.com/v1.0"

    def __init__(self, tenant_id: str = "", client_id: str = "", client_secret: str = "",
                 token_path: str = ""):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self._token = None
        self._token_expires = 0

    def _get_token(self) -> str:
        """Acquire token — from file or client_credentials flow."""
        if self.token_path:
            return self._get_token_from_file()

        if self._token and time.time() < self._token_expires - 60:
            return self._token

        url = self.TOKEN_URL.format(tenant_id=self.tenant_id)
        data = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()

        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
                self._token = body["access_token"]
                self._token_expires = time.time() + body.get("expires_in", 3600)
                return self._token
        except (URLError, HTTPError, KeyError) as e:
            logger.error(f"Token acquisition failed: {e}")
            raise

    def _get_token_from_file(self) -> str:
        """Read access_token from a JSON file."""
        import os
        path = os.path.expanduser(self.token_path)
        try:
            with open(path) as f:
                data = json.load(f)
            token = data.get("access_token", "")
            if not token:
                logger.error(f"No access_token in {path}")
            return token
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to read token from {path}: {e}")
            return ""

    def get(self, path: str, params: dict = None, headers: dict = None) -> dict:
        """GET from MS Graph API."""
        token = self._get_token()
        url = f"{self.GRAPH_URL}{path}"
        if params:
            url += "?" + urlencode(params)

        req = Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            logger.error(f"Graph API error {e.code}: {path}")
            return {}
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Graph API request failed: {e}")
            return {}
