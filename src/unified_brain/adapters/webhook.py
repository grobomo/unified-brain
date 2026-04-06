"""Webhook channel adapter — receives events via HTTP POST.

Push-based alternative to polling adapters. Runs an HTTP server that
accepts JSON events and queues them for the service loop.

Endpoints:
  POST /events       — submit one or more events (JSON body)
  POST /events/raw   — submit a raw payload with source hint (for GitHub webhooks, etc.)
  GET  /events/stats — queue depth and accepted count

Events are queued in-memory and drained each poll cycle by the service.
"""

import hashlib
import json
import logging
import queue
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from .base import ChannelAdapter, parse_timestamp

logger = logging.getLogger(__name__)


class _WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for incoming webhook events."""

    # Shared state set by WebhookAdapter before server starts
    event_queue: queue.Queue = None
    accepted_count: int = 0
    secret: str = ""
    _lock = threading.Lock()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 1_000_000:  # 1MB limit
            self._respond(413, {"error": "Payload too large"})
            return

        body = self.rfile.read(content_length)

        # Optional HMAC verification
        if self.secret:
            sig = self.headers.get("X-Hub-Signature-256", "")
            if not self._verify_signature(body, sig):
                self._respond(401, {"error": "Invalid signature"})
                return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        if self.path == "/events":
            events = self._normalize_events(payload)
        elif self.path == "/events/raw":
            events = self._normalize_raw(payload)
        else:
            self._respond(404, {"error": "Not found"})
            return

        for event in events:
            self.event_queue.put(event)

        with self._lock:
            _WebhookHandler.accepted_count += len(events)

        self._respond(202, {"accepted": len(events)})

    def do_GET(self):
        if self.path == "/events/stats":
            with self._lock:
                stats = {
                    "queue_depth": self.event_queue.qsize(),
                    "accepted_total": _WebhookHandler.accepted_count,
                }
            self._respond(200, stats)
        else:
            self._respond(404, {"error": "Not found"})

    def _normalize_events(self, payload) -> list[dict]:
        """Normalize a JSON payload into event dicts.

        Accepts either a single event dict or a list of events.
        """
        if isinstance(payload, list):
            raw_events = payload
        elif isinstance(payload, dict):
            raw_events = [payload]
        else:
            return []

        events = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            event = {
                "id": raw.get("id", f"wh:{hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:12]}"),
                "source": raw.get("source", "webhook"),
                "channel": raw.get("channel", ""),
                "event_type": raw.get("event_type", raw.get("type", "webhook")),
                "author": raw.get("author", raw.get("sender", "")),
                "title": raw.get("title", raw.get("subject", "")),
                "body": raw.get("body", raw.get("message", raw.get("content", ""))),
                "created_at": parse_timestamp(raw.get("created_at", raw.get("timestamp", ""))),
                "metadata": raw.get("metadata", {}),
            }
            events.append(event)
        return events

    def _normalize_raw(self, payload: dict) -> list[dict]:
        """Normalize a raw webhook payload (e.g., GitHub webhook).

        Uses the source hint from query params or headers.
        """
        source = payload.get("_source", self.headers.get("X-Event-Source", "webhook"))
        event_type = payload.get("_type", self.headers.get("X-GitHub-Event", "webhook"))

        event_id = f"wh:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]}"

        # Try to extract common fields from various webhook formats
        event = {
            "id": event_id,
            "source": source,
            "channel": payload.get("repository", {}).get("full_name", ""),
            "event_type": event_type,
            "author": (payload.get("sender", {}).get("login", "")
                       or payload.get("user", {}).get("login", "")),
            "title": (payload.get("action", "")
                      + " " + (payload.get("issue", {}).get("title", "")
                               or payload.get("pull_request", {}).get("title", ""))).strip(),
            "body": json.dumps(payload)[:5000],
            "created_at": parse_timestamp(payload.get("created_at", "")),
            "metadata": {"raw_keys": list(payload.keys())[:20]},
        }
        return [event]

    def _verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify HMAC-SHA256 signature (GitHub webhook style)."""
        import hmac as hmac_mod
        if not signature.startswith("sha256="):
            return False
        expected = hmac_mod.new(
            self.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac_mod.compare_digest(f"sha256={expected}", signature)

    def _respond(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass  # suppress access logs


class WebhookAdapter(ChannelAdapter):
    """Push-based adapter: receives events via HTTP webhook.

    Config:
        webhook_port: int — port to listen on (default 8791)
        webhook_secret: str — HMAC secret for signature verification (optional)
        webhook_bind: str — bind address (default 0.0.0.0)
    """

    def __init__(self, config: dict = None):
        super().__init__("webhook", config)
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def source(self) -> str:
        return "webhook"

    async def poll(self) -> list[dict]:
        """Drain the queue of received events."""
        events = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    async def start(self):
        """Start the HTTP server in a background thread."""
        port = self.config.get("webhook_port", 8791)
        bind = self.config.get("webhook_bind", "0.0.0.0")
        secret = self.config.get("webhook_secret", "")

        _WebhookHandler.event_queue = self._queue
        _WebhookHandler.accepted_count = 0
        _WebhookHandler.secret = secret

        self._server = HTTPServer((bind, port), _WebhookHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info(f"Webhook adapter listening on {bind}:{port}")

    async def stop(self):
        """Shut down the HTTP server."""
        if self._server:
            self._server.shutdown()
            logger.info("Webhook adapter stopped")
