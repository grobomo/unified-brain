# Spec 007: Self-Reflection Plugin — Hook-Runner as Brain Channel

## Problem

Hook-runner (`_grobomo/hook-runner`) has a self-reflection system that calls `claude -p` on every Stop event to review gate decisions. It works but each call is stateless — no memory of past sessions, patterns, or corrections. The same issues get flagged repeatedly; lessons learned in one session are forgotten by the next.

Unified brain's three-tier memory solves this. By ingesting hook-runner's JSONL logs as events, the brain gains persistent context across sessions — it can spot recurring patterns, track correction streaks, and make increasingly accurate assessments.

## Design

### Architecture

```
hook-runner (PRODUCER)              unified-brain (ANALYZER)
─────────────────────              ─────────────────────────
~/.claude/hooks/                    HookRunnerAdapter
  hook-log.jsonl      ──poll──→      reads new JSONL lines
  self-reflection.jsonl ──poll──→    normalizes to events
                                     stores in EventStore
                                          │
                                     Brain analyzes with:
                                     - Three-tier memory
                                     - Cross-channel context
                                     - Feedback loop
                                          │
                                     Writes findings to:
~/.claude/hooks/                       reflection-findings.json
  reflection-findings.json ←─write─  (RESPOND action via outbox)
```

### T053: HookRunnerAdapter — JSONL file poller

A new channel adapter that tails JSONL files, yielding one event per line.

Source files:
- `~/.claude/hooks/hook-log.jsonl` — every hook module invocation
- `~/.claude/hooks/self-reflection.jsonl` — LLM analysis results

The adapter tracks file position (byte offset) per file, so it only reads new lines on each poll. This is analogous to how the Teams adapter tracks `_seen_ids` — but for append-only files, byte offset is more efficient than ID dedup.

Event normalization:
```python
# hook-log.jsonl line:
{"ts": "...", "event": "PreToolUse", "module": "spec-gate.js", "result": "block", "elapsed_ms": 12}
# → normalized event:
{
    "id": "hooklog:<sha256[:12]>",
    "source": "hook-runner",
    "channel": "hook-log",
    "event_type": "gate_decision",
    "author": "spec-gate.js",
    "title": "PreToolUse: block",
    "body": "<full JSON line>",
    "created_at": <timestamp>,
}
```

### T054: Reflection analysis action

When the brain processes a hook-runner event, it uses the full three-tier memory context:
- **Tier 1** (hot cache): recent gate decisions from this session
- **Tier 2** (project summary): patterns across sessions — which gates fire most, common block reasons
- **Tier 3** (global): cross-project reflection patterns

The brain prompt includes this context and returns a structured analysis (not just the raw event processing). The analysis replaces hook-runner's direct `claude -p` call.

### T055: Reflection bridge

The bridge is the communication protocol between brain and hook-runner:
1. **Brain → hook-runner**: Brain writes `reflection-findings.json` (via RESPOND action to the "hook-runner" channel, routed to a file output)
2. **Hook-runner → brain**: Already handled by T053 (JSONL file polling)

Format of `reflection-findings.json`:
```json
{
    "updated_at": "2026-04-06T20:00:00Z",
    "findings": [
        {
            "type": "pattern",
            "description": "spec-gate blocks 40% of edits — consider adding more file patterns to allowlist",
            "severity": "info",
            "source_events": ["hooklog:abc123", "hooklog:def456"]
        }
    ],
    "recommendations": [
        "Add *.test.ts to spec-gate allowlist",
        "branch-pr-gate false positive rate: 15% — needs CWD fix"
    ]
}
```

## Non-goals
- Modifying hook-runner code from this project (cross-project rule)
- Real-time streaming (poll-based is sufficient)
- Replacing hook-runner's gate enforcement (brain thinks, hook-runner enforces)
