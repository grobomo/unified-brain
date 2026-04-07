"""Runner — CLI entry point and process management for the brain service.

Handles: signal handling, lock file, heartbeat, health endpoint,
log rotation, circuit breaker. Ported from github-agent/main.py.
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from .service import BrainService
from .adapters.github import GitHubAdapter
from .adapters.hook_runner import HookRunnerAdapter
from .adapters.slack import SlackAdapter
from .adapters.teams import TeamsAdapter
from .adapters.webhook import WebhookAdapter
from .chat import ChatSessionManager, handle_websocket_upgrade, run_repl
from .persona import PersonaRegistry
from .utils import deep_merge

logger = logging.getLogger("unified-brain")


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal health check endpoint with Prometheus metrics, /ask, and WebSocket chat."""
    stats = {}
    service = None  # Set by run_service before starting
    chat_sessions: ChatSessionManager | None = None

    def do_GET(self):
        if self.path == "/ws/chat":
            if not self.chat_sessions:
                self._json_response(503, json.dumps({"error": "Chat not available"}).encode())
                return
            if not handle_websocket_upgrade(self, self.chat_sessions):
                self._json_response(400, json.dumps({"error": "WebSocket upgrade required"}).encode())
            return
        elif self.path == "/chat/sessions":
            if self.chat_sessions:
                sessions = self.chat_sessions.list_sessions()
                self._json_response(200, json.dumps(sessions, indent=2, default=str).encode())
            else:
                self._json_response(200, b"[]")
            return
        elif self.path in ("/healthz", "/stats"):
            data = json.dumps(self.stats, indent=2, default=str)
            self._json_response(200, data.encode())
        elif self.path == "/metrics":
            from .metrics import registry
            body = registry.expose()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/ask":
            self._handle_ask()
        elif self.path == "/chat":
            self._handle_chat()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_ask(self):
        """Synchronous /ask endpoint — submit a question, get brain analysis back."""
        if not self.service:
            self._json_response(503, json.dumps({"error": "Service not ready"}).encode())
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 100_000:
            self._json_response(413, json.dumps({"error": "Payload too large"}).encode())
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, json.dumps({"error": "Invalid JSON"}).encode())
            return

        question = body.get("question", body.get("q", ""))
        if not question:
            self._json_response(400, json.dumps({"error": "Missing 'question' field"}).encode())
            return

        source = body.get("source", "api")
        channel = body.get("channel", "ask")

        # Build a synthetic event from the question
        event = {
            "id": f"ask:{int(time.time() * 1000)}",
            "source": source,
            "channel": channel,
            "event_type": "question",
            "author": body.get("author", "api-user"),
            "title": question[:100],
            "body": question,
            "created_at": time.time(),
            "metadata": body.get("metadata", {}),
        }

        # Build context and analyze
        try:
            context = self.service.context_builder.build(event)
            action = self.service.brain.analyze(event, context)
            response = {
                "action": action.get("action", "log"),
                "content": action.get("content", ""),
                "reason": action.get("reason", ""),
                "event_id": event["id"],
                "context_sources": {
                    "same_channel": len(context.get("same_channel", [])),
                    "related_channels": len(context.get("related_channels", [])),
                    "author_activity": len(context.get("author_activity", [])),
                },
            }
            self._json_response(200, json.dumps(response, indent=2).encode())
        except Exception as e:
            logger.error(f"/ask error: {e}")
            self._json_response(500, json.dumps({"error": str(e)}).encode())

    def _handle_chat(self):
        """REST-based chat endpoint with session persistence.

        POST /chat with:
            {"question": "...", "session_id": "optional", "author": "optional"}
            {"command": "clear", "session_id": "..."}
            {"command": "history", "session_id": "..."}

        Returns brain response with session_id for follow-up requests.
        """
        if not self.chat_sessions:
            self._json_response(503, json.dumps({"error": "Chat not available"}).encode())
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 100_000:
            self._json_response(413, json.dumps({"error": "Payload too large"}).encode())
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, json.dumps({"error": "Invalid JSON"}).encode())
            return

        session_id = body.get("session_id")
        author = body.get("author", "api-user")
        command = body.get("command")

        session = self.chat_sessions.get_or_create(session_id, author)

        if command == "clear":
            session.clear()
            self._json_response(200, json.dumps({"status": "cleared", "session_id": session.session_id}).encode())
            return
        elif command == "history":
            self._json_response(200, json.dumps(session.to_dict(), default=str).encode())
            return

        question = body.get("question", body.get("q", ""))
        if not question:
            self._json_response(400, json.dumps({"error": "Missing 'question' field"}).encode())
            return

        try:
            result = session.ask(question)
            self._json_response(200, json.dumps(result, indent=2).encode())
        except Exception as e:
            logger.error(f"/chat error: {e}")
            self._json_response(500, json.dumps({"error": str(e)}).encode())

    def _json_response(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access logs


class ProcessGuard:
    """Lock file and heartbeat management."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.data_dir / "brain.lock"
        self.heartbeat_path = self.data_dir / "heartbeat.json"

    def acquire(self) -> bool:
        """Acquire lock. Returns False if another instance is running."""
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip())
                # Check if PID is still alive
                if sys.platform == "win32":
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                    if handle:
                        kernel32.CloseHandle(handle)
                        logger.warning(f"Another instance running (PID {pid})")
                        return False
                else:
                    os.kill(pid, 0)
                    logger.warning(f"Another instance running (PID {pid})")
                    return False
            except (ValueError, OSError, ProcessLookupError):
                pass  # Stale lock file

        self.lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def write_heartbeat(self, stats: dict):
        heartbeat = {
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": stats.get("status", "ok"),
            "cycles": stats.get("cycles", 0),
            "events_ingested": stats.get("events_ingested", 0),
            "actions_taken": stats.get("actions_taken", 0),
            "errors": stats.get("errors", 0),
            "adapters": stats.get("adapters", 0),
            "uptime_seconds": time.time() - stats.get("start_time", time.time()),
        }
        tmp = str(self.heartbeat_path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(heartbeat, f, indent=2)
        os.replace(tmp, str(self.heartbeat_path))


async def run_service(config: dict, once: bool = False,
                      health_port: int = 0, max_errors: int = 50):
    """Run the brain service with process management."""
    data_dir = config.get("data_dir", "data")
    guard = ProcessGuard(data_dir)

    if not guard.acquire():
        logger.error("Cannot acquire lock — another instance is running")
        sys.exit(1)

    stats = {
        "status": "ok",
        "cycles": 0,
        "events_ingested": 0,
        "actions_taken": 0,
        "errors": 0,
        "start_time": time.time(),
        "adapters": 0,
    }
    consecutive_errors = 0

    # Health endpoint
    if health_port:
        _HealthHandler.stats = stats
        server = HTTPServer(("0.0.0.0", health_port), _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info(f"Health endpoint on :{health_port}")

    service = BrainService(config)
    _HealthHandler.service = service

    # Persona registry — per-user brain identity
    persona_registry = PersonaRegistry(config.get("persona", {}))

    # Chat session manager for interactive mode
    chat_config = config.get("chat", {})
    chat_sessions = ChatSessionManager(
        brain=service.brain,
        context_builder=service.context_builder,
        max_turns=chat_config.get("max_turns", 20),
        max_idle_seconds=chat_config.get("max_idle_seconds", 3600),
        persona_registry=persona_registry,
    )
    _HealthHandler.chat_sessions = chat_sessions

    # Register adapters based on config (persona_registry for self-message filtering)
    adapters_config = config.get("adapters", {})
    if adapters_config.get("github", {}).get("enabled", False):
        gh_config = adapters_config["github"]
        service.add_adapter(GitHubAdapter(gh_config, persona_registry=persona_registry))
    if adapters_config.get("teams", {}).get("enabled", False):
        teams_config = adapters_config["teams"]
        service.add_adapter(TeamsAdapter(teams_config, persona_registry=persona_registry))
    if adapters_config.get("slack", {}).get("enabled", False):
        slack_config = adapters_config["slack"]
        service.add_adapter(SlackAdapter(slack_config, persona_registry=persona_registry))
    if adapters_config.get("webhook", {}).get("enabled", False):
        wh_config = adapters_config["webhook"]
        service.add_adapter(WebhookAdapter(
            wh_config,
            brain=service.brain,
            context_builder=service.context_builder,
        ))
    if adapters_config.get("hook_runner", {}).get("enabled", False):
        hr_config = adapters_config["hook_runner"]
        service.add_adapter(HookRunnerAdapter(hr_config))

    stats["adapters"] = len(service.adapters)
    logger.info(f"Starting with {len(service.adapters)} adapters, interval={service.interval}s")

    # Signal handling
    shutdown = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    else:
        # Windows: use threading signal
        def _win_handler(sig, frame):
            loop.call_soon_threadsafe(_signal_handler)
        signal.signal(signal.SIGTERM, _win_handler)
        signal.signal(signal.SIGINT, _win_handler)

    # Start adapters
    for adapter in service.adapters:
        await adapter.start()

    try:
        if once:
            await service.run_cycle()
            stats["cycles"] += 1
        else:
            while not shutdown.is_set():
                try:
                    await service.run_cycle()
                    stats["cycles"] += 1
                    consecutive_errors = 0
                except Exception as e:
                    logger.error(f"Cycle error: {e}")
                    stats["errors"] += 1
                    consecutive_errors += 1

                guard.write_heartbeat(stats)

                if max_errors and consecutive_errors >= max_errors:
                    logger.error(f"Circuit breaker: {consecutive_errors} consecutive errors")
                    stats["status"] = "circuit_breaker"
                    guard.write_heartbeat(stats)
                    break

                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=service.interval)
                    break  # shutdown was set
                except asyncio.TimeoutError:
                    pass  # normal timeout, continue loop
    finally:
        await service.stop()
        guard.write_heartbeat(stats)
        guard.release()
        logger.info("Stopped")


def _load_yaml_or_json(path: Path) -> dict:
    """Load a single YAML or JSON file."""
    if not path.exists():
        return {}
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
            return yaml.safe_load(text) or {}
        except ImportError:
            logger.warning("pyyaml not installed, falling back to JSON")
    return json.loads(text)


def _deep_merge(base: dict, override: dict) -> dict:
    """Backwards-compatible alias for deep_merge."""
    return deep_merge(base, override)


_ENV_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _interpolate_env(obj):
    """Replace ${VAR_NAME} patterns with environment variable values.

    Supports strings, lists, and nested dicts. Unset vars are left as-is.
    Works with K8s Secrets (envFrom), EC2 instance env, and local .env files.
    """
    if isinstance(obj, str):
        def _replace(m):
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_RE.sub(_replace, obj)
    if isinstance(obj, dict):
        return {k: _interpolate_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_env(item) for item in obj]
    return obj


def _load_config(path: str) -> dict:
    """Load config from YAML/JSON, then deep-merge a .local overlay if present.

    e.g. config/brain.yaml + config/brain.local.yaml
    The .local file is gitignored and holds secrets like Teams chat IDs.
    After merging, ${VAR_NAME} patterns are replaced with env var values.
    """
    p = Path(path)
    config = _load_yaml_or_json(p)

    # Look for .local overlay (same stem + .local + same suffix)
    local_path = p.parent / f"{p.stem}.local{p.suffix}"
    if local_path.exists():
        local_config = _load_yaml_or_json(local_path)
        config = _deep_merge(config, local_config)
        logger.info(f"Merged local config from {local_path}")

    # Interpolate environment variables
    config = _interpolate_env(config)

    return config


def main():
    parser = argparse.ArgumentParser(description="Unified Brain — multi-channel event analysis")
    sub = parser.add_subparsers(dest="command")

    # Default: run service (also works with no subcommand)
    run_parser = sub.add_parser("run", help="Run the brain service (default)")
    for p in (parser, run_parser):
        p.add_argument("--config", default="config/brain.yaml", help="Config file path")
        p.add_argument("--verbose", "-v", action="store_true")
    run_parser.add_argument("--once", action="store_true", help="Run single cycle then exit")
    run_parser.add_argument("--health-port", type=int, default=0, help="Health check port (0 to disable)")
    run_parser.add_argument("--max-errors", type=int, default=50, help="Circuit breaker threshold")
    run_parser.add_argument("--interval", type=float, default=None, help="Override poll interval (seconds)")
    run_parser.add_argument("--log-max-bytes", type=int, default=5_000_000, help="Max log file size")
    run_parser.add_argument("--log-backup-count", type=int, default=3, help="Rotated log files to keep")

    # Chat subcommand
    chat_parser = sub.add_parser("chat", help="Interactive chat REPL with the brain")
    chat_parser.add_argument("--config", default="config/brain.yaml", help="Config file path")
    chat_parser.add_argument("--author", default="user", help="Your name in the conversation")
    chat_parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.INFO
    data_dir = "data"
    os.makedirs(data_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(data_dir, "brain.log"),
        maxBytes=getattr(args, "log_max_bytes", 5_000_000),
        backupCount=getattr(args, "log_backup_count", 3),
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Dedicated LLM call log — every prompt/response as JSONL
    llm_log = logging.getLogger("unified-brain.llm")
    llm_log.propagate = False  # don't duplicate to main log
    llm_fh = logging.handlers.RotatingFileHandler(
        os.path.join(data_dir, "llm.jsonl"),
        maxBytes=getattr(args, "log_max_bytes", 5_000_000),
        backupCount=getattr(args, "log_backup_count", 3),
    )
    llm_fh.setFormatter(logging.Formatter("%(message)s"))  # raw JSONL
    llm_log.addHandler(llm_fh)
    llm_log.setLevel(logging.INFO)

    # Load config
    config = _load_config(args.config)
    config["data_dir"] = data_dir

    if args.command == "chat":
        # Interactive chat REPL
        from .brain import BrainAnalyzer
        from .context import ContextBuilder
        from .store import EventStore
        from .feedback import FeedbackStore
        from .memory import MemoryManager

        brain = BrainAnalyzer(config.get("brain", {}))
        store = EventStore(config.get("db_path", "data/brain.db"))
        registry = ProjectRegistry(config.get("registry_path", "config/projects.yaml"))
        memory = MemoryManager(store.conn, config.get("memory", {}))
        feedback = FeedbackStore(store.conn)
        context_builder = ContextBuilder(
            store, registry, config.get("context", {}),
            memory=memory, feedback=feedback,
        )
        run_repl(brain, context_builder=context_builder, author=args.author)
    else:
        # Default: run the service
        if getattr(args, "interval", None) is not None:
            config["interval"] = args.interval

        asyncio.run(run_service(
            config, once=getattr(args, "once", False),
            health_port=getattr(args, "health_port", 0),
            max_errors=getattr(args, "max_errors", 50),
        ))


if __name__ == "__main__":
    main()
