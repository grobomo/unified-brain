# Spec 007: Self-Reflection Plugin — Closed-Loop Self-Improvement

## Problem

Hook-runner (`_grobomo/hook-runner`) has a self-reflection system that calls `claude -p` on every Stop event to review gate decisions. It works but each call is stateless — no memory of past sessions, patterns, or corrections. The same issues get flagged repeatedly; lessons learned in one session are forgotten by the next.

Worse: self-reflection only *reports* issues. Nobody acts on the findings. The brain should not just analyze — it should **fix problems, verify the fix works, and roll back if it doesn't**.

## Design: Closed-Loop Self-Improvement

The brain is not a passive analyzer. It's a closed-loop controller:

```
    ┌──────────────────────────────────────────────────────┐
    │                                                      │
    ▼                                                      │
DETECT ──→ ANALYZE ──→ IMPLEMENT ──→ MONITOR ──→ VERIFY ──┘
  │           │            │            │           │
  │           │            │            │           ├─ score positive for 30m? → CLOSE ✓
  │           │            │            │           └─ score dropped? → ROLLBACK → re-ANALYZE
  │           │            │            │
  │           │            │            └─ exponential backoff: 30s, 1m, 5m, 15m, 30m
  │           │            └─ edit hook-runner module files directly
  │           └─ use three-tier memory for pattern recognition
  └─ poll hook-log.jsonl + self-reflection.jsonl + reflection-score.json
```

### Lifecycle of a Self-Reflection Task

1. **DETECT**: HookRunnerAdapter (T053, done) ingests hook events. Brain sees a pattern worth fixing (e.g. spec-gate blocking 40% of edits with false positives).

2. **ANALYZE**: Brain uses three-tier memory to understand the pattern:
   - Tier 1: recent gate decisions from current session
   - Tier 2: per-module patterns across sessions (block rate, false positive rate)
   - Tier 3: global correction history (what fixes worked before)
   - Produces a `ReflectionTask` with diagnosis, proposed fix, and target file.

3. **IMPLEMENT**: Brain edits the hook-runner module file directly. Creates a backup first. Records the change in the task's history.

4. **MONITOR**: Brain watches reflection-score.json and hook-log.jsonl for the impact of the change. Uses exponential backoff verification windows:
   - Check at 30 seconds
   - Check at 1 minute
   - Check at 5 minutes
   - Check at 15 minutes
   - Check at 30 minutes

5. **VERIFY** at each checkpoint:
   - **Score still positive** → advance to next backoff interval
   - **Score dropped** → ROLLBACK the change, re-enter ANALYZE with the new data, develop different solution, re-IMPLEMENT, restart monitoring from 30s
   - **30 minutes of sustained positive score** → task CLOSED successfully

### ReflectionTask State Machine

```
PENDING → ANALYZING → IMPLEMENTING → MONITORING → VERIFIED → CLOSED
                ↑                         │
                └── ROLLED_BACK ←─────────┘ (score dropped)
```

Each task tracks:
- `task_id`: unique identifier
- `state`: current lifecycle state
- `diagnosis`: what pattern was detected
- `proposed_fix`: what change to make
- `target_file`: which hook-runner module to edit
- `backup_content`: original file content (for rollback)
- `implemented_at`: when the fix was applied
- `prediction`: structured prediction of expected outcome (see below)
- `monitor_checkpoints`: list of (timestamp, score, prediction_match, result) tuples
- `backoff_index`: current position in [30, 60, 300, 900, 1800] seconds
- `attempts`: how many implement-monitor cycles this task has gone through
- `max_attempts`: cap to prevent infinite loops (default 3)
- `score_baseline`: score at time of implementation (used to detect drops)

### Prediction-Outcome Loop

**Before every change, the brain must predict what it expects to see.**

```python
prediction = {
    "expected_score_delta": +5,          # "I expect score to increase by ~5"
    "expected_block_rate_change": -0.25,  # "block rate should drop 25%"
    "expected_affected_modules": ["spec-gate.js"],
    "confidence": 0.7,                   # how sure the brain is
    "reasoning": "Adding *.test.ts to allowlist should eliminate test file false positives",
    "timeframe_seconds": 300,            # "I expect to see this within 5 minutes"
}
```

At each monitoring checkpoint, the brain compares actual outcome to prediction:

- **Prediction matches outcome** → score stays positive, advance to next backoff interval
- **Outcome better than predicted** → score DROPS (prediction was wrong) → re-analyze
- **Outcome worse than predicted** → score DROPS → rollback + re-analyze
- **Outcome matches but via different mechanism** → score DROPS → re-analyze

The key principle: **accuracy of understanding matters as much as the outcome itself**. An unexpectedly good result means the brain doesn't understand *why* things work. That's dangerous — it can't generalize, can't predict side effects, can't make reliable future decisions.

This mirrors human cognition: disappointment when reality doesn't match expectations triggers self-analysis, even when the surprise is positive. The re-analysis updates the brain's model of what actually works vs. what it *thought* would work.

Prediction accuracy is tracked in Tier 2 memory per module, so the brain learns calibration over time: "My predictions for spec-gate changes are 80% accurate, but only 40% for branch-gate — I need to be more careful with branch-gate changes."

### Score Monitoring

`~/.claude/hooks/reflection-score.json` contains:
```json
{
    "points": 150,
    "level": 3,
    "streak": 5,
    "interventions": 2,
    "last_updated": "2026-04-06T20:00:00Z"
}
```

### Brain-Owned Score

The brain maintains its own score, independent of the hook-runner gamification score. **Prediction accuracy is the primary driver. User interrupt frequency is secondary.**

```python
score_components = {
    # PRIMARY: Did reality match what the brain expected?
    "prediction_accuracy": {
        "weight": 0.7,
        "value": rolling_average(last_10_predictions),  # 0.0 to 1.0
        # Accurate predictions earn points. Inaccurate ones cost points.
        # No predictions = no score movement (can't game by doing nothing)
    },
    # SECONDARY: Is the user having to intervene/correct?
    "user_interrupt_rate": {
        "weight": 0.3,
        "value": 1.0 - (interrupts_per_hour / baseline_interrupts),
        # Fewer interrupts = higher score
        # But user may be AFK for hours — this signal goes stale
        # Prediction accuracy keeps scoring even when user is away
    },
}
```

Why this ordering:
- The brain can score itself 24/7 based on prediction accuracy, even when the user is away for hours
- User interrupts are valuable signal but intermittent — they go stale during long AFK periods
- A brain that makes accurate predictions but gets interrupted occasionally is better than one that avoids interrupts by doing nothing
- Prediction accuracy forces the brain to *understand* the system, not just react to it

The brain computes `prediction_accuracy = 1 - abs(actual_delta - predicted_delta) / max(abs(predicted_delta), 1)`. If accuracy < 0.5, it's a prediction failure regardless of whether the outcome was "good" or "bad".

Score triggers:
1. **Prediction match** (accuracy >= 0.7) → score increases
2. **Prediction mismatch** (accuracy < 0.5) → score drops → re-analysis triggered
3. **Points decreased** from baseline → score drops → rollback + re-analysis
4. **User interrupt** during monitoring → score drops (secondary signal)

### File Editing

The brain edits hook-runner JS modules directly:
- Read current file content → store as backup
- Apply the fix (regex change, allowlist addition, etc.)
- Write the file
- If rollback needed: restore from backup

This is safe because:
- Hook-runner modules are loaded fresh on each invocation (no hot-reload needed)
- Backups enable instant rollback
- Max attempts cap prevents infinite edit loops
- All changes are logged in the task history

## Tasks

### T053: HookRunnerAdapter — JSONL file poller (DONE)
Brain-side adapter that reads hook-log.jsonl and self-reflection.jsonl.

### T054: ReflectionTask lifecycle manager
- Task state machine (PENDING → ANALYZING → IMPLEMENTING → MONITORING → VERIFIED → CLOSED)
- Score monitoring with exponential backoff (30s, 1m, 5m, 15m, 30m)
- Rollback on score drop, re-analyze with max_attempts cap
- Task persistence (SQLite, alongside EventStore)
- Score file poller for reflection-score.json

### T055: Reflection implementer
- File backup + edit + rollback for hook-runner modules
- Brain prompt enrichment for hook-runner events (three-tier memory context)
- DISPATCH action handler that triggers the implement step
- Integration: wire into service loop (check monitoring tasks each cycle)

### T056: Reflection bridge protocol
- Hook-runner → brain: events via JSONL (T053, done)
- Brain → hook-runner: reflection-findings.json with active tasks, findings, recommendations
- Score polling from reflection-score.json
- End-to-end test: detect pattern → implement fix → monitor → verify → close

## Non-goals
- Modifying hook-runner's core loading logic (it already loads modules fresh each time)
- Real-time streaming (poll-based is sufficient at service interval)
- Editing modules outside ~/.claude/hooks/ (scoped to hook-runner's directory)
