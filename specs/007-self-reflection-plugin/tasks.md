# Spec 007 Tasks

## T053: HookRunnerAdapter — JSONL file poller
- [x] T053a: HookRunnerAdapter class with file path config, byte offset tracking
- [x] T053b: JSONL line parser with normalization to event dict
- [x] T053c: hook-log.jsonl event types (gate_block, gate_allow, gate_error, gate_decision)
- [x] T053d: self-reflection.jsonl event types (reflection_result)
- [x] T053e: Graceful handling of missing/rotated files
- [x] T053f: Tests for adapter (11 tests: parse, normalize, offset tracking, rotation, missing files, unique IDs)

## T054: Reflection analysis action
- [ ] T054a: Brain prompt enrichment for hook-runner events (gate patterns, correction history)
- [ ] T054b: Structured findings format in brain response
- [ ] T054c: Tests for enriched analysis

## T055: Reflection bridge
- [ ] T055a: File-based RESPOND handler for hook-runner channel (writes reflection-findings.json)
- [ ] T055b: Findings JSON format (findings array, recommendations, timestamp)
- [ ] T055c: Wire into ActionExecutor/dispatcher
- [ ] T055d: Tests for bridge write
