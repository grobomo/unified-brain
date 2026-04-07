# Spec 008 Tasks — Brain in a Jar + CCC Worker Integration

## T063: Archive ccc-manager
- [x] Mark ccc-manager as absorbed in its TODO.md
- [x] Redirect to brain Phase 16
- [x] Catalogue useful code (dispatcher, worktree, fleet)

## T064: CCC bridge adapter
- [x] CCCBridge class — dispatch tasks to git relay repo
- [x] Write task JSON to requests/pending/
- [x] Poll completed/failed dirs for results
- [x] Tests (10)

## T065: CCC result monitor
- [x] Wire CCCBridge poll into BrainService.run_cycle()
- [x] Record feedback for prediction/outcome tracking
- [x] Retry on failure with chronic failure guard
- [x] Relay results to originating channel
- [x] Tests (5)

## T066: SSH chat server
- [x] BrainSSHServer with asyncssh
- [x] Auto-generate host key
- [x] Authorized keys support (optional)
- [x] Drop users into ChatSession REPL over PTY
- [x] Line editing (backspace, ctrl-c, ctrl-d)
- [x] Tests (7)

## T067: Idle loop
- [x] IdleTask with per-task intervals, error tracking, due check
- [x] IdleLoop.tick() runs all due tasks
- [x] create_idle_loop factory wires standard tasks
- [x] Periodic memory compaction
- [x] Check dispatched CCC work status
- [x] Self-reflection cycle (when monitor available)
- [x] Proactive insights — loop patterns, feedback trends
- [x] Tests (10)

## T068: Email adapter
- [x] MS Graph inbox poller via _GraphClient
- [x] HTML body stripping, folder/filter config
- [x] Normalize emails as events (subject, sender, recipients, importance)
- [x] BoundedSet dedup
- [x] Tests (12)

## T069: Calendar adapter
- [x] MS Graph calendarView poller with lookahead window
- [x] Normalize meetings, cancellations, all-day events
- [x] Attendee tracking (capped at 20), online meeting URLs
- [x] Tests (11)

## T070-T072: Port ccc-manager patterns
- [ ] T070: Worktree isolation (Python rewrite)
- [ ] T071: Write-set validation (Python rewrite)
- [ ] T072: Fleet heartbeat (Python rewrite)
