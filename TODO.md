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
- github-agent/core/ — EventStore (SQLite+FTS), brain.py, dispatcher.py, context.py, memory.py, compactor.py
- teams-agent/ — ImportanceClassifier, MessageStore, pipeline, notifier
- ccc-manager — full dispatch pipeline: monitors, inputs, dispatchers (SHTD + Claude AI + SQS), workers (local/K8s/EC2), fleet coordination, verification, Prometheus, worktree isolation
- ccc-central — TODO.md only, absorbed into this project + ccc-manager
- Both github-agent and teams-agent are completely separate: different schemas, different brains, different DBs

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
- [ ] T016: Multi-environment awareness and portability (local service, RONE k8s, AWS EC2)
- [x] T017: Tests — 31 integration tests covering store, brain, dispatcher, registry, context, memory, outbox, E2E pipeline

## Dependencies
- github-agent at `_grobomo/github-agent/` — core/ modules to extract/reuse
- teams-agent at `_tmemu/teams-agent/` — poller + classifier to wrap as adapter
- ccc-manager at `_grobomo/ccc-manager/` — task dispatch and worker execution
- msgraph-lib at `~/Documents/ProjectsCL1/msgraph-lib/` — shared MS Graph token management

## Phase 5: Operational Polish
- [x] T018: Scheduled service — scripts/start.sh, stop.sh, status.sh, run.bat, install-service.ps1 (needs admin for schtasks)
- [ ] T019: Enable Teams adapter — configure chat_ids, test with live Teams data
- [x] T020: Connect to ccc-manager bridge — config created, bridge verified (ccc-manager/config/unified-brain.yaml)
- [x] T021: Archive ccc-central — marked as absorbed, TODO.md updated with redirect

## Session Handoff
All phases complete. PRs #1-5 merged. Service verified working locally.
- All source modules: store, brain, context, memory, dispatcher, service, registry, runner, adapters (github + teams)
- 35 integration tests passing
- Live run: 331 GitHub events + 47 Teams messages ingested
- Config overlay: brain.local.yaml for secrets (chat IDs), gitignored
- ccc-manager bridge: config/unified-brain.yaml in ccc-manager, verified working
- ccc-central archived — all functionality absorbed
- Scripts: start.sh, stop.sh, status.sh, run.bat, install-service.ps1
- schtasks needs admin elevation — use `bash scripts/start.sh` to run manually for now
- Remaining: T016 (multi-env K8s/EC2 — future when deploying to infra)
