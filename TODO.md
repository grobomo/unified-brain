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
- [ ] T011: Cross-channel context — when analyzing a GitHub event, include relevant Teams context (same person, same project) and vice versa
- [ ] T012: Unified memory — single three-tier memory system spanning all channels
- [ ] T013: Action relay protocol — brain writes action commands to channel-specific outbox folders. Each channel agent polls its outbox and executes (gh comment, Teams reply, email).

## Phase 4: Deployment
- [ ] T014: Silent service deployment — Cross-platform design, process guard (port from github-agent)
- [ ] T015: Health monitoring — heartbeat, watchdog, log rotation, circuit breaker (port from github-agent)
- [ ] T016: Multi-environment awareness and portability (local service, RONE k8s, AWS EC2)
- [ ] T017: Tests — adapter tests, brain integration tests, cross-channel context tests

## Dependencies
- github-agent at `_grobomo/github-agent/` — core/ modules to extract/reuse
- teams-agent at `_tmemu/teams-agent/` — poller + classifier to wrap as adapter
- ccc-manager at `_grobomo/ccc-manager/` — task dispatch and worker execution
- msgraph-lib at `~/Documents/ProjectsCL1/msgraph-lib/` — shared MS Graph token management

## Session Handoff
Phase 1+2 complete. PR #1 open. Branch: 001-T001-project-foundation.
- All source modules built: store, brain, dispatcher, service, registry, adapters (github + teams)
- 15 integration tests passing
- PR: https://github.com/grobomo/unified-brain/pull/1
- ccc-manager TODO.md updated with Phase 50 integration tasks (T181-T184)
- ccc-central should be archived (absorbed into this project + ccc-manager)
- Next: Phase 3 (cross-channel intelligence T011-T013), then implement RESPOND actions (gh CLI, teams_chat.py)
