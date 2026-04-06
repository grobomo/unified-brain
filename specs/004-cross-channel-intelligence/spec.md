# Spec 004: Cross-Channel Intelligence

## Problem
The brain analyzer only sees events from the same channel. A GitHub issue about "deploy failing" has no visibility into Teams messages where the same person reported the same problem 2 hours earlier. This means duplicate analysis, missed context, and slower response.

## Solution
Three components:
1. **Cross-channel context builder** (T011) — enriches brain analysis with events from related channels (same project via registry, same author)
2. **Unified three-tier memory** (T012) — hot cache (24h), per-source JSON, account-level summary
3. **Action relay protocol** (T013) — outbox folders per channel, agents poll and execute

## Design

### T011: Context Builder
New module `context.py` with `ContextBuilder` class:
- Takes an event + store + registry
- Finds the project via registry (repo lookup or chat lookup)
- Queries related channels from that project
- Also queries by author across all channels
- Returns a structured context dict for the brain prompt

### T012: Unified Memory
New module `memory.py` with `MemoryManager`:
- Tier 1: SQLite table `memory_hot` — raw events from last 24h (already events table)
- Tier 2: `memory_project` table — per-project JSON summaries, updated after each cycle
- Tier 3: `memory_global` table — account-level patterns, updated daily
- Compactor runs on interval, promotes T1→T2→T3

### T013: Action Relay Protocol
Outbox model:
- `data/outbox/github/` — GitHub actions (comment, label, close)
- `data/outbox/teams/` — Teams actions (reply, mention)
- `data/outbox/email/` — Email actions (alert, notify)
- Each channel agent polls its outbox and executes
- Result written to `data/inbox/` for confirmation

## Tests
- Context builder returns cross-channel events for same project
- Context builder returns same-author events across channels
- Memory compactor promotes T1→T2→T3
- Outbox files written with correct structure
