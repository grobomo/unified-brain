# Unified Brain — TODO

## Vision
One brain processing ALL input channels (GitHub + Teams + future sources). Single process, single DB, single LLM context window spanning all sources. Channel pollers are dumb relays that feed events into the brain.

## Architecture
```
Channels (dumb relays)              Brain (this project)
──────────────────────              ────────────────────
github-agent poller ──→ EventStore ← teams-agent poller
                            │
                            ▼
                    Brain Analyzer (claude -p)
                    - Three-tier memory (hot + repo + account)
                    - Cross-channel context
                    - Rule-based fallback
                            │
                            ▼
                    Dispatcher
                    - RESPOND (gh comment / Teams reply)
                    - DISPATCH (CCC worker)
                    - ALERT (email)
                    - LOG (context only)
```

## What exists today
- github-agent/core/ — EventStore (SQLite+FTS), brain.py, dispatcher.py, context.py, memory.py, compactor.py
- teams-agent/ — ImportanceClassifier, MessageStore, pipeline, notifier
- Both are completely separate: different schemas, different brains, different DBs

## Phase 1: Foundation
- [ ] T001: Initialize project — git, publish.json, CLAUDE.md, .gitignore, requirements.txt
- [ ] T002: Unified EventStore schema — extend github-agent's core/store.py to handle both GH and Teams records. Source field distinguishes origin. Channel field = repo name or chat ID.
- [ ] T003: Channel adapter interface — abstract base class for channel adapters. Each adapter polls one source and yields normalized events.
- [ ] T004: GitHub channel adapter — wrap github-agent's poller into an adapter that yields EventStore records
- [ ] T005: Teams channel adapter — wrap teams-agent's poller into an adapter that yields EventStore records
- [ ] T006: Brain service — single process that runs all adapters, feeds events into brain analyzer, dispatches actions

## Phase 2: Cross-Channel Intelligence
- [ ] T007: Cross-channel context — when analyzing a GitHub event, include relevant Teams context (same person, same project) and vice versa
- [ ] T008: Unified memory — single three-tier memory system spanning all channels
- [ ] T009: Project registry — shared project catalog that maps repos, chats, and people to projects

## Phase 3: Deployment
- [ ] T010: Silent service deployment — Windows scheduled task, VBS launcher, process guard (port from github-agent)
- [ ] T011: Health monitoring — heartbeat, watchdog, log rotation, circuit breaker (port from github-agent)
- [ ] T012: Tests — adapter tests, brain integration tests, cross-channel context tests

## Dependencies
- github-agent at `_grobomo/github-agent/` — core/ modules to extract/reuse
- teams-agent at `_tmemu/teams-agent/` — poller + classifier to wrap as adapter
- msgraph-lib at `~/Documents/ProjectsCL1/msgraph-lib/` — shared token management

## Session Handoff
New project. Created from github-agent Spec 007a-c. Start with T001-T003 (foundation).
