# Spec 006: Interactive Chat, Personas, and LLM Observability

## Problem

The brain currently processes events in a fire-and-forget loop: event in → action out. There's no way to have a conversation with the brain — ask follow-up questions, maintain context across turns, or interact in real-time. The `/ask` endpoint (T048) is stateless — each call is independent with no memory of previous questions.

Additionally:
- **No LLM audit trail**: `claude -p` calls are invisible. If the brain makes a bad decision, there's no way to review what prompt was sent or what came back.
- **No identity management**: The brain responds as "Joel Ginsberg" in Teams/Slack. When it posts a message, it looks identical to a human message. No way to distinguish brain messages from human messages, causing re-ingestion loops where the brain analyzes its own output.
- **No per-user context in group chats**: In a group chat, everyone shares the same brain interaction. If Joel and Kush are both talking to the brain simultaneously, their conversations collide.

## Design

### 1. Interactive Chat Sessions

Persistent conversation sessions that maintain history across turns.

```
User: "What's the status of unified-brain?"
Brain [respond]: "3 open PRs, CI green, 601 events in store..."
User: "What about the failing test?"    ← has context from turn 1
Brain [dispatch]: "Investigating TestTokenBucket flaky test..."
```

Each session is a `ChatSession` object with:
- Capped history (deque, default 20 turns)
- Unique session_id
- History injected into the brain prompt before the "Response Format" section
- Three access modes: CLI REPL, REST `/chat`, WebSocket `/ws/chat`

### 2. Per-User Sessions in Group Chats

Sessions keyed on `(channel, author)` — not just session_id.

```
Squad Chat:
  Joel → Brain: "What's the CI status?"     → session chat:squad:joel
  Kush → Brain: "Check the memory leak"     → session chat:squad:kush
  Joel → Brain: "Can you fix it?"           → continues joel's session
```

`ChatSessionManager` maintains a `_channel_index: dict[(channel, author) → session_id]` for adapter use. The existing `get_or_create(session_id)` method stays for direct API/WebSocket clients; `get_for_channel(channel, author)` is the adapter-facing method.

### 3. Persona System

Per-user brain identity: name + emoji prefix. Solves two problems:
- **Visual distinction**: Brain messages are clearly not human messages
- **Self-filtering**: Adapters skip messages starting with any known persona emoji

```yaml
# brain.yaml
persona:
  enforce_global: false     # true = same persona for everyone
  default:
    name: "Brain"
    emoji: "🧠"
  users:
    joel:
      name: "Brain"
      emoji: "🧠"
    kush:
      name: "Mango"
      emoji: "🥭"
```

`PersonaRegistry`:
- `get(author)` → returns user's Persona or default
- `is_own_message(text)` → checks if text starts with any persona emoji (direct or in quote block `> 🧠`)
- `all_emojis()` → set of all emojis for adapter filtering
- `set(author, name, emoji)` → runtime updates
- `enforce_global` flag → ignores per-user config, uses default for all

Quote handling: A message that is *entirely* a quote of the brain (`> 🧠 ...`) is filtered. A user reply that quotes the brain then adds their own text is NOT filtered — that's the user talking.

### 4. LLM Call Logging + Metrics

Every `claude -p` and API call is:
- **Logged** to `data/llm.jsonl` as one JSON record per line (prompt excerpt, full response, timing, success/failure)
- **Metered** via Prometheus: `brain_llm_calls_total{backend,outcome}`, `brain_llm_active_calls{backend}` (concurrency gauge), `brain_llm_last_duration_seconds{backend}`

The `llm.jsonl` log uses its own logger (`unified-brain.llm`) with `propagate=False` so it doesn't duplicate into `brain.log`.

## Tasks

### T049: Interactive chat mode
- `ChatSession` with conversation history, injected into brain prompt
- `ChatSessionManager` with per-user channel sessions via `get_for_channel(channel, author)`
- CLI REPL: `python -m unified_brain chat` with `/clear`, `/history`, `/quit`
- REST: `POST /chat` with `session_id` for multi-turn, `command: clear|history`
- WebSocket: `/ws/chat` with RFC 6455 framing, JSON protocol
- `GET /chat/sessions` to list active sessions

### T050: Persona system
- `PersonaRegistry` with per-user name+emoji, global default, `enforce_global` flag
- `Persona.format_message(content)` → prefixes with emoji
- `PersonaRegistry.is_own_message(text)` → detects brain messages (direct + quoted)
- Wired into `ChatSessionManager` — sessions get persona from registry
- Config in `brain.yaml` under `persona:` key

### T051: LLM call logging and metrics
- `_log_llm_call()` writes JSONL to `unified-brain.llm` logger
- `_mark_llm_start()` increments `brain_llm_active_calls` gauge
- Both `SubprocessBackend` and `APIBackend` instrumented
- `data/llm.jsonl` with rotating file handler in runner
- Three new Prometheus metrics: `llm_calls_total`, `llm_active_calls`, `llm_last_duration_seconds`

### T052: Adapter self-message filtering
- Teams adapter: check `persona_registry.is_own_message(text)` before yielding events
- Slack adapter: same check
- GitHub adapter: check if comment author matches bot account
- Persona registry passed to adapters at registration time

## Non-goals (future)
- Worker context-reset for long DISPATCH tasks (ccc-manager concern)
- Brain prompt A/B testing
- Persistent session storage (currently in-memory only)
- Admin dashboard for session management
