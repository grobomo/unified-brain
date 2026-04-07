"""Interactive chat mode — persistent brain session with conversation history.

Provides:
  - ChatSession: manages conversation history and feeds it to the brain
  - CLI REPL: `python -m unified_brain chat` for terminal interaction
  - WebSocket endpoint: /ws/chat on the health server for browser clients

Each session maintains a conversation history (capped at max_turns) that
gets included in the brain prompt as additional context, enabling multi-turn
dialogue with the brain.
"""

import asyncio
import hashlib
import json
import logging
import queue
import struct
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


class ChatSession:
    """Persistent conversation session with the brain.

    Maintains a history of exchanges (user question + brain response) and
    includes that history in the brain prompt for multi-turn context.

    Args:
        brain: BrainAnalyzer instance
        context_builder: ContextBuilder instance (optional)
        session_id: unique session identifier
        max_turns: maximum conversation turns to retain
        author: name of the user in this session
    """

    def __init__(self, brain, context_builder=None, session_id: str = None,
                 max_turns: int = 20, author: str = "user", channel: str = "",
                 persona=None):
        self.brain = brain
        self.context_builder = context_builder
        self.session_id = session_id or f"chat:{int(time.time() * 1000)}"
        self.max_turns = max_turns
        self.author = author
        self.channel = channel
        self.persona = persona  # Persona instance (optional)
        self.history: deque[dict] = deque(maxlen=max_turns)
        self.created_at = time.time()

    def ask(self, question: str) -> dict:
        """Send a question to the brain with conversation history.

        Returns:
            Action dict from the brain with action, content, reason fields.
        """
        event = {
            "id": f"chat:{hashlib.sha256(f'{self.session_id}:{len(self.history)}:{question}'.encode()).hexdigest()[:12]}",
            "source": "chat",
            "channel": self.session_id,
            "event_type": "question",
            "author": self.author,
            "title": question[:100],
            "body": question,
            "created_at": time.time(),
            "metadata": {"session_id": self.session_id, "turn": len(self.history)},
        }

        # Build context from store/registry if available
        context = {}
        if self.context_builder:
            try:
                context = self.context_builder.build(event)
            except Exception as e:
                logger.warning(f"Chat context build failed: {e}")

        # Inject conversation history into context
        context["conversation_history"] = list(self.history)

        # Wrap the brain's _build_prompt to include history
        original_prompt = self.brain._build_prompt(event, context)
        prompt = self._inject_history(original_prompt)

        # Call the LLM backend directly with the augmented prompt
        result = self.brain.backend.call(prompt)

        if result:
            action = self.brain._parse_action(result, event)
        else:
            action = self.brain._fallback_analyze(event)

        # Record this exchange in history
        self.history.append({
            "role": "user",
            "content": question,
            "timestamp": event["created_at"],
        })
        self.history.append({
            "role": "assistant",
            "action": action.get("action", "log"),
            "content": action.get("content", ""),
            "reason": action.get("reason", ""),
            "timestamp": time.time(),
        })

        return {
            "action": action.get("action", "log"),
            "content": action.get("content", ""),
            "reason": action.get("reason", ""),
            "event_id": event["id"],
            "session_id": self.session_id,
            "turn": len(self.history) // 2,
        }

    def _inject_history(self, prompt: str) -> str:
        """Add conversation history section to the brain prompt."""
        if not self.history:
            return prompt

        lines = [
            "",
            "## Conversation History",
            f"This is turn {len(self.history) // 2 + 1} in an ongoing conversation.",
            "",
        ]
        for entry in self.history:
            role = entry.get("role", "")
            if role == "user":
                lines.append(f"User: {entry['content']}")
            elif role == "assistant":
                action = entry.get("action", "")
                content = entry.get("content", "")
                lines.append(f"Brain [{action}]: {content}")
            lines.append("")

        # Insert before the "## Response Format" section
        marker = "## Response Format"
        idx = prompt.find(marker)
        if idx >= 0:
            return prompt[:idx] + "\n".join(lines) + "\n" + prompt[idx:]
        else:
            return prompt + "\n".join(lines)

    def clear(self):
        """Clear conversation history."""
        self.history.clear()

    def to_dict(self) -> dict:
        """Serialize session state."""
        result = {
            "session_id": self.session_id,
            "author": self.author,
            "channel": self.channel,
            "created_at": self.created_at,
            "turns": len(self.history) // 2,
            "history": list(self.history),
        }
        if self.persona:
            result["persona"] = self.persona.to_dict()
        return result


class ChatSessionManager:
    """Manages multiple concurrent chat sessions.

    Sessions are identified by session_id. In group chats (Teams, Slack),
    each (channel, author) pair gets its own session so multiple users
    can have independent overlapping conversations with the brain.

    Stale sessions are cleaned up after max_idle_seconds of inactivity.
    """

    def __init__(self, brain, context_builder=None, max_turns: int = 20,
                 max_idle_seconds: float = 3600, persona_registry=None):
        self.brain = brain
        self.context_builder = context_builder
        self.max_turns = max_turns
        self.max_idle_seconds = max_idle_seconds
        self.persona_registry = persona_registry
        self._sessions: dict[str, ChatSession] = {}
        self._channel_index: dict[tuple[str, str], str] = {}  # (channel, author) -> session_id
        self._last_active: dict[str, float] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str = None, author: str = "user") -> ChatSession:
        """Get existing session or create a new one by explicit session_id."""
        with self._lock:
            if session_id and session_id in self._sessions:
                self._last_active[session_id] = time.time()
                return self._sessions[session_id]

            session = ChatSession(
                brain=self.brain,
                context_builder=self.context_builder,
                session_id=session_id,
                max_turns=self.max_turns,
                author=author,
            )
            self._sessions[session.session_id] = session
            self._last_active[session.session_id] = time.time()
            return session

    def get_for_channel(self, channel: str, author: str) -> ChatSession:
        """Get or create a session for a (channel, author) pair.

        Used by group chat adapters (Teams, Slack) so each user in a
        channel has their own independent conversation with the brain.
        Each session gets the user's persona from the registry.
        """
        key = (channel, author)
        with self._lock:
            if key in self._channel_index:
                sid = self._channel_index[key]
                if sid in self._sessions:
                    self._last_active[sid] = time.time()
                    return self._sessions[sid]

            persona = None
            if self.persona_registry:
                persona = self.persona_registry.get(author)

            session = ChatSession(
                brain=self.brain,
                context_builder=self.context_builder,
                session_id=f"chat:{channel}:{author}",
                max_turns=self.max_turns,
                author=author,
                channel=channel,
                persona=persona,
            )
            self._sessions[session.session_id] = session
            self._channel_index[key] = session.session_id
            self._last_active[session.session_id] = time.time()
            return session

    def remove(self, session_id: str):
        """Remove a session."""
        with self._lock:
            self._sessions.pop(session_id, None)
            self._last_active.pop(session_id, None)
            # Clean channel index
            stale_keys = [k for k, v in self._channel_index.items() if v == session_id]
            for k in stale_keys:
                del self._channel_index[k]

    def cleanup(self):
        """Remove idle sessions."""
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, last in self._last_active.items()
                if now - last > self.max_idle_seconds
            ]
            for sid in stale:
                del self._sessions[sid]
                del self._last_active[sid]
            # Clean channel index for removed sessions
            if stale:
                stale_set = set(stale)
                stale_keys = [k for k, v in self._channel_index.items() if v in stale_set]
                for k in stale_keys:
                    del self._channel_index[k]
                logger.info(f"Cleaned up {len(stale)} idle chat sessions")

    def list_sessions(self) -> list[dict]:
        """List active sessions."""
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]


# --- WebSocket support (RFC 6455, minimal implementation) ---

def _ws_accept_key(key: str) -> str:
    """Compute Sec-WebSocket-Accept from client key."""
    import base64
    magic = "258EAFA5-E914-47DA-95CA-5AB5DC65C97B"
    accept = hashlib.sha1((key + magic).encode()).digest()
    return base64.b64encode(accept).decode()


def _ws_read_frame(rfile) -> tuple[int, bytes] | None:
    """Read one WebSocket frame. Returns (opcode, payload) or None on close."""
    try:
        b0 = rfile.read(1)
        b1 = rfile.read(1)
        if not b0 or not b1:
            return None

        opcode = b0[0] & 0x0F
        masked = b1[0] & 0x80
        length = b1[0] & 0x7F

        if length == 126:
            raw = rfile.read(2)
            if len(raw) < 2:
                return None
            length = struct.unpack(">H", raw)[0]
        elif length == 127:
            raw = rfile.read(8)
            if len(raw) < 8:
                return None
            length = struct.unpack(">Q", raw)[0]

        mask_key = rfile.read(4) if masked else b""
        data = rfile.read(length)

        if masked and mask_key:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

        return (opcode, data)
    except (OSError, ConnectionError):
        return None


def _ws_send_frame(wfile, opcode: int, data: bytes):
    """Send a WebSocket frame (unmasked, server-to-client)."""
    header = bytes([0x80 | opcode])
    length = len(data)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)

    wfile.write(header + data)
    wfile.flush()


class WebSocketChatHandler:
    """Handles a single WebSocket chat connection.

    Protocol:
        Client sends JSON: {"question": "...", "session_id": "...", "author": "..."}
        Server responds JSON: {"action": "...", "content": "...", "reason": "...", ...}
        Special messages:
            {"command": "clear"} — clear session history
            {"command": "history"} — get conversation history
            {"command": "close"} — close the connection
    """

    def __init__(self, rfile, wfile, session_manager: ChatSessionManager):
        self.rfile = rfile
        self.wfile = wfile
        self.session_manager = session_manager

    def handle(self):
        """Main loop: read frames, process messages, send responses."""
        session = None
        try:
            while True:
                frame = _ws_read_frame(self.rfile)
                if frame is None:
                    break

                opcode, data = frame

                if opcode == 0x8:  # Close
                    _ws_send_frame(self.wfile, 0x8, b"")
                    break
                if opcode == 0x9:  # Ping
                    _ws_send_frame(self.wfile, 0xA, data)  # Pong
                    continue
                if opcode != 0x1:  # Only handle text frames
                    continue

                try:
                    msg = json.loads(data.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_error("Invalid JSON")
                    continue

                # Handle commands
                command = msg.get("command")
                if command == "close":
                    _ws_send_frame(self.wfile, 0x8, b"")
                    break
                elif command == "clear":
                    if session:
                        session.clear()
                    self._send_json({"status": "cleared"})
                    continue
                elif command == "history":
                    history = session.to_dict() if session else {"history": []}
                    self._send_json(history)
                    continue

                # Get or create session
                session_id = msg.get("session_id")
                author = msg.get("author", "user")
                if session is None or (session_id and session_id != session.session_id):
                    session = self.session_manager.get_or_create(session_id, author)

                question = msg.get("question", msg.get("q", msg.get("text", "")))
                if not question:
                    self._send_error("Missing 'question' field")
                    continue

                try:
                    result = session.ask(question)
                    self._send_json(result)
                except Exception as e:
                    logger.error(f"WebSocket chat error: {e}")
                    self._send_error(f"Brain error: {e}")

        except (OSError, ConnectionError):
            pass  # Client disconnected

    def _send_json(self, data: dict):
        payload = json.dumps(data).encode("utf-8")
        _ws_send_frame(self.wfile, 0x1, payload)

    def _send_error(self, message: str):
        self._send_json({"error": message})


def handle_websocket_upgrade(handler, session_manager: ChatSessionManager) -> bool:
    """Handle WebSocket upgrade on an HTTP request handler.

    Called from the health server's do_GET when path is /ws/chat.
    Returns True if the upgrade was handled, False if not a valid upgrade.
    """
    key = handler.headers.get("Sec-WebSocket-Key", "")
    if not key:
        return False

    # Send upgrade response
    accept = _ws_accept_key(key)
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()

    # Hand off to WebSocket handler
    ws = WebSocketChatHandler(handler.rfile, handler.wfile, session_manager)
    ws.handle()
    return True


# --- CLI REPL ---

def run_repl(brain, context_builder=None, author: str = "user"):
    """Run an interactive chat REPL in the terminal.

    Commands:
        /clear   — clear conversation history
        /history — show conversation history
        /quit    — exit the REPL
    """
    session = ChatSession(
        brain=brain,
        context_builder=context_builder,
        author=author,
    )

    print(f"Unified Brain — Interactive Chat (session: {session.session_id})")
    print("Commands: /clear, /history, /quit")
    print()

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue

        if question == "/quit":
            print("Bye.")
            break
        elif question == "/clear":
            session.clear()
            print("History cleared.")
            continue
        elif question == "/history":
            for entry in session.history:
                role = entry.get("role", "")
                if role == "user":
                    print(f"  You: {entry['content']}")
                else:
                    print(f"  Brain [{entry.get('action', '')}]: {entry.get('content', '')}")
            if not session.history:
                print("  (empty)")
            continue

        result = session.ask(question)
        action = result.get("action", "log")
        content = result.get("content", "(no response)")
        reason = result.get("reason", "")

        print(f"Brain [{action}]: {content}")
        if reason:
            print(f"  Reason: {reason}")
        print()
