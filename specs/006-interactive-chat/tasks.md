# Spec 006 Tasks

## T049: Interactive chat mode
- [x] ChatSession with conversation history (deque, max_turns cap)
- [x] History injected into brain prompt before Response Format section
- [x] ChatSessionManager with explicit session_id lookup
- [x] ChatSessionManager.get_for_channel(channel, author) for group chats
- [x] CLI REPL: `python -m unified_brain chat` with /clear, /history, /quit
- [x] REST: POST /chat with session_id persistence, command: clear|history
- [x] WebSocket: /ws/chat with RFC 6455 framing
- [x] GET /chat/sessions to list active sessions
- [x] Tests: ChatSession (7), ChatSessionManager (6), REST endpoint (7), WebSocket (3), channel sessions (8)

## T050: Persona system
- [x] Persona class (name, emoji, format_message, prefix)
- [x] PersonaRegistry (per-user, default, enforce_global, runtime set)
- [x] is_own_message detection (direct, quoted, mixed content)
- [x] Wired into ChatSessionManager — sessions get persona
- [x] PersonaRegistry created in runner from config
- [x] Tests: 12 persona tests + 8 channel/persona tests

## T051: LLM call logging and metrics
- [x] T051a: _log_llm_call JSONL writer + _mark_llm_start concurrency tracker
- [x] T051b: SubprocessBackend instrumented
- [x] T051c: APIBackend instrumented
- [x] T051d: llm.jsonl rotating file handler in runner
- [x] T051e: Three Prometheus metrics: llm_calls_total, llm_active_calls, llm_last_duration_seconds
- [x] T051f: Tests for LLM logging (6 tests: JSONL write, truncation, None response, metrics registered, metrics updated, subprocess instrumented)

## T052: Adapter self-message filtering
- [ ] T052a: PersonaRegistry passed to adapters
- [ ] T052b: Teams adapter skip own messages
- [ ] T052c: Slack adapter skip own messages
- [ ] T052d: GitHub adapter skip bot comments
- [ ] T052e: Tests for filtering
