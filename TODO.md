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
- [x] T016: Multi-environment awareness and portability (local service, RONE k8s, AWS EC2)
- [x] T017: Tests — 47 integration tests covering store, brain, dispatcher, registry, context, memory, outbox, env interpolation, E2E pipeline

## Dependencies
- github-agent at `_grobomo/github-agent/` — core/ modules to extract/reuse
- teams-agent at `_tmemu/teams-agent/` — poller + classifier to wrap as adapter
- ccc-manager at `_grobomo/ccc-manager/` — task dispatch and worker execution
- msgraph-lib at `~/Documents/ProjectsCL1/msgraph-lib/` — shared MS Graph token management

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
- [ ] T033: Pluggable dispatcher — filesystem outbox OR SQS
- [x] T034: Verify local deployment — 601 events (554 GitHub + 47 Teams), brain analyzing, health endpoint on :8790
- [ ] T035: Deploy to RONE K8s — Docker image, push to registry, apply manifests, verify
- [ ] T036: Deploy to AWS EC2 — spot instance, SQS dispatcher, CloudWatch logs, verify

## Session Handoff
PRs #1-15 merged. CI green. Phases 1-8 complete (foundation through hardening).
Phase 9 in progress — making the brain ACTUALLY FUNCTIONAL by removing all local filesystem deps.
- Current adapters are broken: they import from github-agent and teams-agent via hardcoded local paths
- Brain calls `claude -p` subprocess — won't work in containers
- Dispatcher writes to local filesystem — no SQS option
- Spec: specs/005-portable-deployment/SPEC.md
- Next task: T030 — self-contained GitHub adapter using `gh` CLI directly
