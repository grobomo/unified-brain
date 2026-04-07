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
- [ ] Periodic memory compaction
- [ ] Check dispatched work status
- [ ] Self-reflection cycle
- [ ] Proactive insights ("daydreaming")

## T068: Email adapter
- [ ] MS Graph inbox poller
- [ ] Normalize emails as events

## T069: Calendar adapter
- [ ] MS Graph calendar poller
- [ ] Normalize meetings/changes as events

## T070-T072: Port ccc-manager patterns
- [ ] T070: Worktree isolation (Python rewrite)
- [ ] T071: Write-set validation (Python rewrite)
- [ ] T072: Fleet heartbeat (Python rewrite)
