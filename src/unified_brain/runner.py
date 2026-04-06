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
from .adapters.teams import TeamsAdapter

logger = logging.getLogger("unified-brain")


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal health check endpoint."""
    stats = {}

    def do_GET(self):
        if self.path in ("/healthz", "/stats"):
            data = json.dumps(self.stats, indent=2, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
        else:
            self.send_response(404)
            self.end_headers()

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

    # Register adapters based on config
    adapters_config = config.get("adapters", {})
    if adapters_config.get("github", {}).get("enabled", False):
        gh_config = adapters_config["github"]
        service.add_adapter(GitHubAdapter(gh_config))
    if adapters_config.get("teams", {}).get("enabled", False):
        teams_config = adapters_config["teams"]
        service.add_adapter(TeamsAdapter(teams_config))

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
    """Recursively merge override into base. Override wins for scalars."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


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
    parser.add_argument("--config", default="config/brain.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Run single cycle then exit")
    parser.add_argument("--health-port", type=int, default=0, help="Health check port (0 to disable)")
    parser.add_argument("--max-errors", type=int, default=50, help="Circuit breaker threshold")
    parser.add_argument("--interval", type=float, default=None, help="Override poll interval (seconds)")
    parser.add_argument("--log-max-bytes", type=int, default=5_000_000, help="Max log file size")
    parser.add_argument("--log-backup-count", type=int, default=3, help="Rotated log files to keep")
    parser.add_argument("--verbose", "-v", action="store_true")
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
        maxBytes=args.log_max_bytes,
        backupCount=args.log_backup_count,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Load config
    config = _load_config(args.config)
    config["data_dir"] = data_dir
    if args.interval is not None:
        config["interval"] = args.interval

    asyncio.run(run_service(
        config, once=args.once,
        health_port=args.health_port,
        max_errors=args.max_errors,
    ))


if __name__ == "__main__":
    main()
