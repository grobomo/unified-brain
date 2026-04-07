"""SSH chat server — persistent brain access via SSH.

Runs an asyncssh server that drops users into a chat REPL.
Brain is always-on and reachable via `ssh brain@host -p 2222`.

Each SSH session gets its own ChatSession with independent conversation
history. Sessions persist while the connection is open.

Config:
    ssh.host: str — bind address (default 0.0.0.0)
    ssh.port: int — listen port (default 2222)
    ssh.host_key: str — path to SSH host key (auto-generated if missing)
    ssh.authorized_keys: str — path to authorized_keys file (optional, allows all if missing)
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# asyncssh is optional — only needed when SSH server is enabled
try:
    import asyncssh
    HAS_ASYNCSSH = True
except ImportError:
    asyncssh = None
    HAS_ASYNCSSH = False


class BrainSSHServer:
    """SSH server that provides chat REPL sessions to the brain."""

    def __init__(self, brain, context_builder=None, config: dict = None):
        self.brain = brain
        self.context_builder = context_builder
        self.config = config or {}
        self.host = self.config.get("host", "0.0.0.0")
        self.port = self.config.get("port", 2222)
        self.host_key_path = os.path.expanduser(
            self.config.get("host_key", "data/ssh_host_key")
        )
        self.authorized_keys_path = self.config.get("authorized_keys", "")
        self._server = None
        self._active_sessions = 0

    def _ensure_host_key(self):
        """Generate SSH host key if it doesn't exist."""
        if not HAS_ASYNCSSH:
            raise RuntimeError("asyncssh not installed: pip install asyncssh")

        if not os.path.exists(self.host_key_path):
            os.makedirs(os.path.dirname(self.host_key_path) or ".", exist_ok=True)
            key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
            key.write_private_key(self.host_key_path)
            logger.info(f"Generated SSH host key: {self.host_key_path}")

    def _load_authorized_keys(self):
        """Load authorized_keys if configured, or return None to allow all."""
        if not self.authorized_keys_path:
            return None
        path = os.path.expanduser(self.authorized_keys_path)
        if os.path.exists(path):
            return asyncssh.read_authorized_keys(path)
        logger.warning(f"authorized_keys not found: {path}, allowing all connections")
        return None

    async def start(self):
        """Start the SSH server."""
        if not HAS_ASYNCSSH:
            logger.error("Cannot start SSH server: asyncssh not installed")
            return

        self._ensure_host_key()
        authorized_keys = self._load_authorized_keys()

        brain = self.brain
        context_builder = self.context_builder
        server_ref = self

        class _Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                peer = conn.get_extra_info("peername")
                logger.info(f"SSH connection from {peer}")

            def connection_lost(self, exc):
                server_ref._active_sessions = max(0, server_ref._active_sessions - 1)

            def begin_auth(self, username):
                if authorized_keys is None:
                    return False  # No auth required
                return True

            def public_key_auth_supported(self):
                return authorized_keys is not None

        def _session_factory(stdin, stdout, stderr):
            """Handle an SSH session — runs the chat REPL."""
            return _handle_ssh_session(
                stdin, stdout, stderr, brain, context_builder, server_ref,
            )

        self._server = await asyncssh.create_server(
            _Server,
            self.host, self.port,
            server_host_keys=[self.host_key_path],
            authorized_client_keys=authorized_keys,
            process_factory=_session_factory,
        )

        logger.info(f"SSH chat server listening on {self.host}:{self.port}")

    async def stop(self):
        """Stop the SSH server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("SSH chat server stopped")

    @property
    def active_sessions(self) -> int:
        return self._active_sessions


async def _handle_ssh_session(process, stdout_unused, stderr_unused,
                              brain, context_builder, server_ref):
    """Handle one SSH session — interactive chat REPL over PTY."""
    from .chat import ChatSession

    server_ref._active_sessions += 1
    stdin = process.stdin
    stdout = process.stdout

    # Get username from the SSH connection
    username = process.get_extra_info("username") or "ssh-user"

    session = ChatSession(
        brain=brain,
        context_builder=context_builder,
        author=username,
    )

    stdout.write(f"Unified Brain — SSH Chat (session: {session.session_id})\r\n")
    stdout.write(f"Connected as: {username}\r\n")
    stdout.write("Commands: /clear, /history, /quit\r\n\r\n")

    try:
        while not process.is_closing():
            stdout.write("You: ")

            try:
                line = await _read_line(stdin)
            except (asyncssh.BreakReceived, asyncssh.TerminalSizeChanged):
                continue
            except (asyncio.CancelledError, asyncssh.DisconnectError):
                break

            if line is None:
                break

            question = line.strip()
            if not question:
                continue

            if question == "/quit":
                stdout.write("Bye.\r\n")
                break
            elif question == "/clear":
                session.clear()
                stdout.write("History cleared.\r\n")
                continue
            elif question == "/history":
                for entry in session.history:
                    role = entry.get("role", "")
                    if role == "user":
                        stdout.write(f"  You: {entry['content']}\r\n")
                    else:
                        stdout.write(f"  Brain [{entry.get('action', '')}]: "
                                     f"{entry.get('content', '')}\r\n")
                if not session.history:
                    stdout.write("  (empty)\r\n")
                continue

            # Call brain (may take a few seconds for LLM)
            stdout.write("  (thinking...)\r\n")

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, session.ask, question,
                )
                action = result.get("action", "log")
                content = result.get("content", "(no response)")
                reason = result.get("reason", "")

                stdout.write(f"Brain [{action}]: {content}\r\n")
                if reason:
                    stdout.write(f"  Reason: {reason}\r\n")
                stdout.write("\r\n")
            except Exception as e:
                logger.error(f"SSH chat error: {e}")
                stdout.write(f"Error: {e}\r\n\r\n")

    except (asyncio.CancelledError, asyncssh.DisconnectError, OSError):
        pass
    finally:
        server_ref._active_sessions = max(0, server_ref._active_sessions - 1)
        process.exit(0)


async def _read_line(stdin) -> str | None:
    """Read a line from SSH stdin (handles character-at-a-time with echo)."""
    buf = []
    while True:
        try:
            data = await asyncio.wait_for(stdin.read(1), timeout=300)
        except asyncio.TimeoutError:
            return None

        if not data:
            return None

        for ch in data:
            if ch in ("\r", "\n"):
                stdin.channel.write("\r\n")
                return "".join(buf)
            elif ch in ("\x03",):  # Ctrl-C
                return "/quit"
            elif ch in ("\x7f", "\x08"):  # Backspace/Delete
                if buf:
                    buf.pop()
                    stdin.channel.write("\x08 \x08")
            elif ch == "\x04":  # Ctrl-D
                return None
            elif ord(ch) >= 32:  # Printable
                buf.append(ch)
                stdin.channel.write(ch)
