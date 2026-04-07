# Spec 007 Tasks

## T053: HookRunnerAdapter — JSONL file poller (DONE)
- [x] T053a: HookRunnerAdapter class with file path config, byte offset tracking
- [x] T053b: JSONL line parser with normalization to event dict
- [x] T053c: hook-log.jsonl event types (gate_block, gate_allow, gate_error, gate_decision)
- [x] T053d: self-reflection.jsonl event types (reflection_result)
- [x] T053e: Graceful handling of missing/rotated files
- [x] T053f: Tests for adapter (11 tests)

## T054: ReflectionTask lifecycle manager (DONE)
- [x] T054a: ReflectionTask data model (state machine, prediction, checkpoints, backup)
- [x] T054b: ReflectionTaskStore (SQLite table alongside EventStore)
- [x] T054c: State transitions: PENDING → ANALYZING → IMPLEMENTING → MONITORING → VERIFIED → CLOSED
- [x] T054d: Rollback state: MONITORING → ROLLED_BACK → ANALYZING (on score drop OR prediction mismatch)
- [x] T054e: Exponential backoff scheduler (30s, 1m, 5m, 15m, 30m checkpoints)
- [x] T054f: Prediction model — structured prediction before each change (expected delta, confidence, reasoning, timeframe)
- [x] T054g: Prediction-outcome comparator — accuracy score, match/mismatch detection
- [x] T054h: Max attempts cap (default 3) to prevent infinite loops
- [x] T054i: Tests for lifecycle (32 tests: state transitions, backoff, rollback, prediction matching, store CRUD)

## T055: Reflection implementer
- [ ] T055a: File backup + edit for hook-runner JS modules
- [ ] T055b: Rollback from backup on score drop or prediction mismatch
- [ ] T055c: Brain prompt enrichment for hook-runner events (three-tier memory + prediction history)
- [ ] T055d: Prediction accuracy tracking in Tier 2 memory (per-module calibration)
- [ ] T055e: Wire into service loop — check monitoring tasks each cycle, advance backoff
- [ ] T055f: Tests for file edit, backup, rollback, prompt enrichment

## T056: Brain-owned score + bridge
- [ ] T056a: BrainScore — prediction accuracy (0.7 weight) + user interrupt rate (0.3 weight)
- [ ] T056b: Score poller — reads reflection-score.json for user-interrupt baseline
- [ ] T056c: Rolling prediction accuracy tracker (last 10 predictions)
- [ ] T056d: Score persistence (brain-score.json in data/)
- [ ] T056e: Prometheus metrics for brain score (accuracy gauge, score gauge, prediction count)
- [ ] T056f: reflection-findings.json writer (active tasks, findings, score breakdown)
- [ ] T056g: End-to-end test: detect → predict → implement → monitor → verify → close
- [ ] T056h: End-to-end test: predict wrong → score drops → rollback → re-analyze → predict again
