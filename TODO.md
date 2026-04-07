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
- unified-brain — self-contained: EventStore (SQLite+FTS), brain.py (pluggable LLM), dispatcher.py (pluggable transport), context.py, memory.py, feedback.py, metrics.py, executor.py, adapters (GitHub, Teams, Webhook), 88 tests
- ccc-manager — task execution: BridgeInput/SQSInput, workers (local/K8s/EC2), fleet coordination, verification, Prometheus
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

- [ ] T059: Loop pattern analyzer — when reflection adapter receives "unproductive loop" issues, brain should: (1) identify the root cause from command history, (2) suggest an automation fix (e.g. "create a deploy.sh that handles zip+upload+verify"), (3) track recurrence in Tier 3 memory (global patterns). Data source: `reflection-sessions.jsonl` (has failed command counts) + `hook-log.jsonl` (has individual Bash commands).

## Phase 14: Hardening & Cleanup
- [x] T057: DRY score file reading — extract read_score_file to utils.py, deduplicate from implementer.py + score.py
- [x] T058: Update README + docs — reflect spec 007 (reflection plugin, 252 tests, brain score)

## Session Handoff
PRs #1-33 merged/open. 58 tasks done (T001-T058), spec 007 complete.
- 252 tests passing, zero external deps for core
- Branch 035-T057-dry-cleanup: T057-T058 done (DRY refactor + README update)
- Next: T059 (loop pattern analyzer from hook-runner T335) or merge PRs + new spec
