# Unified Brain — TODO

## Vision
The THINKER in a two-part system. Ingests all communication channels (GitHub, Teams, future sources), analyzes with LLM + three-tier memory, and dispatches work to ccc-manager (the DOER). Single process, single DB, single LLM context window spanning all sources.

ccc-central is absorbed — its event monitoring maps here, its CCC spawning maps to ccc-manager.

## Architecture
```
Communication Channels         unified-brain (THINKER)              ccc-manager (DOER)
─────────────────────         ─────────────────────                ──────────────────
GitHub events      ──→  EventStore (SQLite+FTS)                   Workers:
Teams messages     ──→  Brain Analyzer (claude -p)                 - Local (claude -p)
Webhook/API        ──→  Three-tier Memory           ──DISPATCH──→  - K8s (kubectl exec)
                        Cross-channel Context                      - EC2 (SSH/SSM)
                        Project Registry                           Fleet coordination
                             │                      ←─RESULT────  Verification
                             ▼                                     Prometheus metrics
                        Action Router
                        ├─ RESPOND → GitHub comment / Teams reply
                        ├─ DISPATCH → ccc-manager bridge/SQS
                        ├─ ALERT → email
                        └─ LOG → memory only
```

## What exists today
- unified-brain — self-contained: EventStore (SQLite+FTS), brain.py (pluggable LLM), dispatcher.py (pluggable transport), context.py, memory.py, feedback.py, metrics.py, executor.py, loop_analyzer.py, adapters (GitHub, Teams, Webhook, HookRunner, SystemMonitor), focus_steal.py (action router), ccc_bridge.py (CCC dispatch + result monitor), ssh_server.py, idle_loop.py, ~317 tests
- ccc-manager — **ARCHIVED** (absorbed by brain). Useful patterns: dispatcher bridge, worktree isolation, fleet heartbeat
- Active respond: ActionExecutor posts GitHub comments + Teams messages directly via APIs
- Observability: Prometheus /metrics endpoint, feedback loop tracks dispatch outcomes
- Deployment: local (subprocess LLM, file dispatcher), K8s (API LLM, file dispatcher on PVC), EC2 (API LLM, SQS dispatcher)

## Integration with ccc-manager
- **Brain → Manager**: Write task JSON to bridge directory or SQS queue. ccc-manager's BridgeInput/SQSInput picks it up.
- **Manager → Brain**: ccc-manager writes result JSON to bridge completedDir. Brain polls for completed tasks.
- **Shared**: Project registry maps repos → configs → workers. Both projects reference same registry.
- **Protocol**: Task JSON = `{ id, source, type, summary, details, priority, channel_context }`. Result JSON = `{ id, success, output, error }`.

## Phase 1: Foundation
- [x] T001: Initialize project — git, publish.json, CLAUDE.md, .gitignore, requirements.txt, secret-scan CI
- [x] T002: Unified EventStore schema — SQLite+FTS with source/channel fields, full-text search, recent/unprocessed queries
- [x] T003: Channel adapter interface — abstract base class with poll/start/stop, normalized event dict contract
- [x] T004: GitHub channel adapter — wraps github-agent's GitHubPoller + normalizer into ChannelAdapter
- [x] T005: Teams channel adapter — wraps teams-agent's MS Graph poller into ChannelAdapter
- [x] T006: Brain service — service loop with adapters, brain analyzer, action dispatcher, result polling

## Phase 2: ccc-manager Integration
- [x] T007: Task dispatch adapter — bridge JSON with BridgeInput-compatible fields (text, classification, request_id)
- [x] T008: Result poller + relay — polls completedDir, relays results to originating channel via RESPOND action
- [x] T009: Project registry — YAML/JSON catalog with reverse-lookup indices (repo→project, chat→project, person→project)
- [x] T010: Integration test — 15 tests covering store, brain, dispatcher, registry, and full E2E pipeline

## Phase 3: Cross-Channel Intelligence
- [x] T011: Cross-channel context — ContextBuilder enriches brain with same-project + same-author events across channels
- [x] T012: Unified memory — MemoryManager with Tier 1 (hot events), Tier 2 (project summaries), Tier 3 (global patterns), compaction
- [x] T013: Action relay protocol — outbox model (github/, teams/, email/, dispatch/) with JSON action files

## Phase 4: Deployment
- [x] T014: Silent service deployment — runner.py with CLI, process guard, lock file, signal handling, log rotation
- [x] T015: Health monitoring — heartbeat JSON, HTTP /healthz endpoint, circuit breaker (max_errors)
- [x] T016: Multi-environment awareness and portability (local service, RONE k8s, AWS EC2)
- [x] T017: Tests — 57 integration tests covering store, brain, dispatcher, registry, context, memory, outbox, env interpolation, transport factory, SQS mock, E2E pipeline

## Dependencies
- ccc-manager at `_grobomo/ccc-manager/` — task dispatch and worker execution (receives DISPATCH actions)
- `gh` CLI — GitHub adapter (pre-installed on all target environments)
- `boto3` — SQS transport only (EC2 deploy), not required for local/K8s

## Phase 5: Operational Polish
- [x] T018: Scheduled service — scripts/start.sh, stop.sh, status.sh, run.bat, install-service.ps1 (needs admin for schtasks)
- [x] T019: Enable Teams adapter — config overlay for secrets, 47 msgs ingested from 3 chats
- [x] T020: Connect to ccc-manager bridge — config created, bridge verified (ccc-manager/config/unified-brain.yaml)
- [x] T021: Archive ccc-central — marked as absorbed, TODO.md updated with redirect

## Phase 6: Hardening
- [x] T022: Optimize store queries — add author filter to recent(), add author index, avoid full scans in context builder
- [x] T023: Fix memory leak in adapters — BoundedSet (10K cap), DRY parse_timestamp in base adapter
- [x] T024: Registry local overlay — projects.local.json for Teams chat IDs, deep-merged at load

## Phase 7: Intelligence
- [x] T025: Improve brain LLM prompt — structured memory context, action guidelines, cross-channel awareness
- [x] T026: Add README for public repo

## Phase 8: Bug Fixes & Cleanup
- [x] T027: Fix brain prompt memory keys — was referencing wrong keys, memory sections never rendered
- [x] T028: DRY deep_merge — extract to utils.py, used by both runner and registry
- [x] T029: CI test workflow — GitHub Actions runs pytest on push/PR to main

## Phase 9: Portable Deployment (spec 005)
Brain is the constant. Adapters, LLM backend, dispatchers are pluggable per environment.
- [x] T030: Self-contained GitHub adapter — use `gh` CLI directly, no github-agent dependency
- [x] T031: Self-contained Teams adapter — inline MS Graph calls, token_path for local, client_credentials for containers
- [x] T032: Pluggable LLM backend — subprocess (`claude -p`) OR HTTP API (Anthropic)
- [x] T033: Pluggable dispatcher — filesystem outbox OR SQS
- [x] T034: Verify local deployment — 601 events (554 GitHub + 47 Teams), brain analyzing, health endpoint on :8790
- [x] T035: Deploy to RONE K8s — Dockerfile (gh CLI, PYTHONPATH), manifests (API backend, network policy, tmpfs, secret ref), deploy script
- [x] T036: Deploy to AWS EC2 — CloudFormation (spot instance, SQS queues, IAM, CloudWatch, systemd), deploy script

## Phase 10: Active Actions & Observability
- [x] T037: Active RESPOND — ActionExecutor posts GitHub comments via `gh api`, Teams messages via Graph API, fallback to outbox
- [x] T038: Prometheus metrics — lightweight stdlib module, counters+gauges with labels, /metrics endpoint, 10 metric series, 74 tests
- [x] T039: Feedback loop — FeedbackStore (SQLite), records dispatch/respond outcomes, summary stats in brain prompt, 80 tests
- [x] T040: Webhook adapter — HTTP server, POST /events + /events/raw + GET /events/stats, HMAC verification, 88 tests

## Phase 11: Code Quality & Packaging
- [x] T041: Cleanup — move respond_results import to module level, remove duplicate variable in _relay_result
- [x] T042: Update docs — README reflects 88 tests, webhook adapter, metrics, feedback, active respond; TODO "What exists" updated
- [x] T043: Docker Compose + monitoring — docker-compose.yaml (brain + Prometheus + Grafana), Grafana dashboard, .env.example

## Phase 12: Multi-Channel Expansion & Hardening
- [x] T044: Slack channel adapter — poll Slack channels via Web API (Bot token), yield normalized events, BoundedSet dedup
- [x] T045: Slack respond — ActionExecutor.respond_slack via chat.postMessage, outbox fallback, dispatcher wiring
- [x] T046: Webhook rate limiting — token bucket per IP, configurable burst/rate, 429 responses
- [x] T047: Update docs — README and TODO reflect Slack adapter, rate limiting, test count
- [x] T048: Synchronous /ask endpoint — POST question, get brain analysis back as HTTP response (conversational mode)
- [x] T049: Interactive chat mode — persistent brain session with conversation history, CLI REPL + WebSocket endpoint
- [x] T050: Persona system — per-user brain identity (name + emoji), self-message filtering
- [x] T051: LLM call logging and metrics — JSONL audit trail, Prometheus counters, concurrency gauge
- [x] T052: Adapter self-message filtering — skip brain's own messages in Teams/Slack/GitHub

## Phase 13: Self-Reflection Plugin
Source: `_grobomo/hook-runner` (v2.10.0) — T331 in hook-runner/TODO.md depends on these.
Hook-runner has a self-reflection system that calls `claude -p` on every Stop event to review
gate decisions. It works but has no persistent memory — each call is stateless. The three-tier
memory in unified-brain solves this. Hook-runner's related TODOs: T330 (scope enforcement),
T331 (migrate to brain plugin), T332 (lightweight session summaries as interim).

Data files (all in `~/.claude/hooks/`):
- `hook-log.jsonl` — every hook module invocation (event, module, result, timing)
- `self-reflection.jsonl` — LLM analysis results (verdict, issues, todos)
- `reflection-score.json` — gamified score (points, level, streak, intervention counts)
- `reflection-claude-log.jsonl` — full claude -p audit (prompt, response, timing)

- [x] T053: Hook-runner channel adapter — ingests hook-log.jsonl + self-reflection.jsonl as events
- [x] T054: ReflectionTask lifecycle — state machine (detect → predict → implement → monitor → verify → close), exponential backoff (30s→30m), rollback on prediction mismatch, max 3 attempts
- [x] T055: Reflection implementer — file backup/edit/rollback for hook modules, brain prompt enrichment with prediction history, per-module calibration in Tier 2 memory
- [x] T056: Brain-owned score — prediction accuracy (70%) + user interrupt rate (30%), rolling tracker, score persistence, Prometheus metrics, reflection-findings.json bridge

## Phase 13b: Loop Detection (from hook-runner T335)
Source: `_grobomo/hook-runner` session 2026-04-06d.
Hook-runner T335 added unproductive loop detection to self-reflection prompt (failed commands,
retry patterns, cherry-pick conflicts, manual patching). Self-reflection flags the pattern, but
brain should do the deep analysis: identify root cause, suggest systemic fix (automate pipeline),
track whether the pattern recurs across sessions.

- [x] T059: Loop pattern analyzer — detects unproductive loops from reflection events, identifies root cause from Bash command history, suggests automation fixes, tracks recurrence in Tier 3 memory (global patterns). 21 tests.

## Phase 14: Hardening & Cleanup
- [x] T057: DRY score file reading — extract read_score_file to utils.py, deduplicate from implementer.py + score.py
- [x] T058: Update README + docs — reflect spec 007 (reflection plugin, 252 tests, brain score)

## Phase 15: System Monitor Integration
Source: `_grobomo/system-monitor` focus guard module.
System-monitor emits JSON events to `~/.system-monitor/events/` when cmd.exe/python.exe/powershell.exe
processes appear (potential focus stealers). Brain needs a channel adapter to consume these events,
analyze the source project, write TODOs in offending projects, and dispatch fix sessions.

- [x] T060: System monitor channel adapter — polls `~/.system-monitor/events/*.json`, yields normalized events (type=focus_steal), dedup by event filename, marks consumed (rename to .processed). 12 tests.
- [x] T061: Focus-steal action router — when brain sees focus_steal event with `source_project` set, write a TODO in that project's TODO.md with fix instructions (CREATE_NO_WINDOW, -WindowStyle Hidden, etc), then dispatch `context_reset.py --project-dir <project>` to start a fix session. 10 tests.
- [x] T062: Focus-steal without source_project — when source is unknown (e.g. Azure CLI, Intune agent), log to Tier 2 memory as "system noise" for pattern tracking, no dispatch. Covered by T061 tests.

Event JSON schema (from system-monitor):
```json
{
  "type": "focus_steal",
  "timestamp": "2026-04-06 23:36:56.718",
  "process": { "pid": 356, "name": "python.exe", "exe_path": "...", "command_line": "..." },
  "parent_chain": "python.exe(356) -> bash.exe(5256)",
  "classification": "SAFE",
  "source_project": "_grobomo/system-monitor" or null
}
```

## Phase 16: Brain + CCC Integration
Brain is the thinker. CCC (`_grobomo/claude-portable`, to be renamed `ccc`) is the doer.
Brain decides, CCC executes. ccc-manager is archived — brain replaces it.

Architecture:
```
unified-brain (RONE K8s, persistent)     ccc (claude-portable, on-demand)
────────────────────────────────────     ────────────────────────────────
Ingests: GitHub, Teams, email, cal       Workers: local, K8s pod, EC2
Thinks: claude -p / Anthropic API        Runs: full Claude Code sessions
Decides: RESPOND / WORK / ALERT / LOG   Does: file edits, git, PRs, tests
Monitors: tracks dispatched work         Reports: result JSON back to brain
Remembers: 3-tier memory                 Stateless: spins up, does job, dies
Interactive: SSH chat, /ask endpoint     No UI: headless execution only
```

- [x] T063: Archive ccc-manager — marked absorbed in ccc-manager/TODO.md, redirected to brain Phase 16. Useful code catalogued (dispatcher patterns, worktree isolation, fleet coordination, Helm/CF templates).
- [x] T064: CCC bridge adapter — brain dispatches WORK tasks to ccc (claude-portable) via git relay repo. CCCBridge writes task JSON to requests/pending/, polls completed/failed for results. 10 tests.
- [x] T065: CCC result monitor — brain polls CCCBridge for completed tasks, records feedback, retries on failure (with chronic failure guard), relays results to originating channel. 5 tests.
- [x] T066: SSH chat server — asyncssh server in brain, drops users into chat REPL via `ssh brain@rone-host -p 2222`. Auto-generated host key, optional authorized_keys, PTY line editing. 7 tests.
- [x] T067: Idle loop — IdleTask + IdleLoop with per-task intervals, create_idle_loop factory wires memory compaction, CCC check, reflection, proactive insights. 10 tests.
- [x] T068: Email adapter — MS Graph inbox poller, HTML body stripping, folder/filter config, BoundedSet dedup. 12 tests.
- [x] T069: Calendar adapter — MS Graph calendarView poller, normalizes meetings/cancellations/all-day events, attendee tracking, online meeting URLs. 11 tests.
- [ ] T070: Port worktree isolation — avoid git conflicts when multiple CCC workers touch same repo. Rewrite in Python for brain's dispatcher. Prevents concurrent edits to same files.
- [ ] T071: Port write-set validation — auto-serialize tasks with overlapping file targets. Rewrite in Python.
- [ ] T072: Port fleet heartbeat — peer discovery, stale worker pruning. Rewrite in Python for brain's worker monitoring.

## Session Handoff
68 tasks done (T001-T069 minus T068-T069). ~317 tests. 8 specs.

**This session (T060-T067):**
- T060: SystemMonitorAdapter — polls ~/.system-monitor/events/*.json (12 tests)
- T061-T062: Focus-steal router — writes TODOs in offending projects, tracks noise in Tier 2 (10 tests)
- T063: Archived ccc-manager — redirected to brain Phase 16
- T064: CCCBridge — dispatches WORK to claude-portable via git relay (10 tests)
- T065: CCC result monitor — polls bridge, records feedback, retries with guard (5 tests)
- T066: SSH chat server — asyncssh PTY with auto host key (7 tests)
- T067: Idle loop — per-task intervals for compaction, CCC check, reflection, insights (10 tests)

**Branches pushed (all need PRs to main):**
- 037-T061-focus-steal-router (T060-T062)
- 038-T063-archive-ccc-manager (T063)
- 039-T064-ccc-bridge (T064)
- 040-T065-ccc-result-monitor (T065)
- 041-T066-ssh-chat (T066)
- 042-T067-idle-loop (T067)

**Pre-existing PR chain:** #33/#34/#35 need rebase onto main

**Next:** T068 (email adapter), T069 (calendar adapter), T070-T072 (port ccc-manager patterns)
**Also:** Create PRs for all pushed branches, merge older PRs
