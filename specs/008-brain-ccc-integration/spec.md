# Spec 008: Brain in a Jar — Persistent Thinker + CCC Workers

## Why

Today the brain is a batch processor — it polls, analyzes, writes JSON files to an outbox, and nobody reads them. It calls `claude -p` directly for thinking, duplicating what CCC workers already do. ccc-manager exists as a half-built middleman that adds nothing. The brain can't actually get work done, and nobody can talk to it.

The vision: **a brain that's always on, always aware, always reachable, and can actually make things happen.**

## The Two Projects

### unified-brain (THINKER)
A Python service running as a K8s deployment in RONE. Single process, single SQLite DB. Always running. It:
- **Ingests** events from every channel (GitHub, Teams, email, calendar, hook-runner, system-monitor)
- **Remembers** everything in three-tier memory (hot events → project summaries → global patterns)
- **Thinks** by calling `claude -p` (local) or Anthropic API (K8s/EC2) for analysis
- **Decides** what to do: respond, dispatch work, alert, or just remember
- **Dispatches** work to CCC workers when action is needed
- **Monitors** dispatched work, verifies outcomes, re-dispatches on failure
- **Chats** with the user in real-time via SSH
- **Idles** between events — compacts memory, runs self-reflection, surfaces insights

### ccc (DOER, currently `claude-portable`)
A containerized Claude Code runtime. Stateless. Spins up, does a job, dies. It:
- **Receives** task JSON from brain (via bridge directory or message queue)
- **Executes** full Claude Code sessions — file edits, git, tests, PRs, API calls
- **Reports** results back to brain
- Has workers (local subprocess, K8s pod, EC2 spot), fleet management, health checks

### ccc-manager (ARCHIVED)
Was trying to be the dispatcher between brain and workers. Brain does this better with full context. Archive it, extract any useful bridge/transport code.

## How Brain Stays Persistent and Always Available

### The Service Loop
```python
while running:
    # 1. Ingest — poll all adapters for new events
    for adapter in adapters:
        events = adapter.poll()
        store.insert(events)

    # 2. Think — analyze unprocessed events
    for event in store.get_unprocessed():
        action = brain.analyze(event, context)  # calls claude -p or API
        execute(action)  # respond, dispatch to CCC, alert, or log

    # 3. Monitor — check on dispatched CCC work
    for task in active_tasks:
        result = ccc_bridge.check(task)
        if result:
            verify_outcome(task, result)  # prediction vs actual

    # 4. Idle work — productive waiting
    if cycle_count % N == 0:
        memory.compact()          # promote T1 → T2 → T3
        self_reflect()            # review recent decisions
        surface_insights()        # "daydream" — proactive analysis
        check_calendar()          # upcoming meetings needing prep

    # 5. Sleep until next cycle
    sleep(interval)  # 60s default, configurable
```

### Environment Context (always loaded)
Brain has full awareness via its adapters and memory:
- **GitHub**: all grobomo repos — issues, PRs, pushes, CI failures (via `gh` CLI)
- **Teams**: squad chats, DMs (via MS Graph API, msgraph-lib tokens)
- **Email**: inbox — case updates, customer requests (via MS Graph)
- **Calendar**: meetings, deadlines (via MS Graph)
- **Hook-runner**: Claude Code session events, self-reflection results (via JSONL files)
- **System-monitor**: focus-steal events, process anomalies (via JSON files)
- **Three-tier memory**: everything it's ever seen, compacted into summaries and patterns

### Persistence
- **SQLite DB** on K8s PVC — survives pod restarts
- **Memory tiers** in the same DB — no external state
- **Heartbeat JSON** — other services can check if brain is alive
- **K8s liveness/readiness probes** on `/healthz` endpoint
- **Pod restart policy: Always** — K8s restarts it if it crashes

### SSH Access (Always Reachable)
```
$ ssh brain@rone-brain-host -p 2222
Unified Brain — Interactive Chat (session: joel-1712456789)
Commands: /clear, /history, /status, /tasks, /quit

You: what's happening in hook-runner?
Brain [respond]: Hook-runner has an open PR #206 (T338-T340: no-rules gate +
  repeated instruction detection). CI is failing on the Tests workflow. The
  branch 206-T338-no-rules-gate was created today. I'd recommend investigating
  the CI failure — it's likely a test that needs updating for the new gate module.

You: fix it
Brain [dispatch]: Dispatching to CCC worker. Task: investigate and fix CI
  failure in hook-runner PR #206. I'll monitor and report back.
  [task:ccc-7a3f → worker:k8s-pod-1]

You: /status
Brain: 536 events in last 48h. 3 active CCC tasks. Memory: 8 projects tracked.
  Score: 72/100. Last reflection: clean (2 min ago).
```

### How Dispatch to CCC Works

Brain writes a task JSON to a bridge directory (local/K8s) or SQS queue (EC2):

```json
{
  "id": "brain-task-1712456789",
  "type": "work",
  "priority": "normal",
  "project": "grobomo/hook-runner",
  "instruction": "CI is failing on PR #206 (branch 206-T338-no-rules-gate). The Tests workflow fails. Checkout the branch, run the tests locally, identify the failure, fix it, push the fix.",
  "context": {
    "pr_url": "https://github.com/grobomo/hook-runner/pull/206",
    "ci_url": "https://github.com/grobomo/hook-runner/actions/runs/...",
    "recent_changes": ["modules/PreToolUse/no-rules-gate.js", "..."]
  },
  "prediction": {
    "expected_outcome": "Test passes after updating test expectations for new gate module",
    "confidence": 0.7
  },
  "callback": {
    "bridge_dir": "/data/bridge/completed",
    "timeout_minutes": 30
  }
}
```

CCC worker picks it up, runs a Claude Code session, writes result JSON:
```json
{
  "id": "brain-task-1712456789",
  "success": true,
  "output": "Fixed: test_no_rules_gate was asserting wrong module count. Updated from 12 to 13. PR pushed, CI green.",
  "pr_url": "https://github.com/grobomo/hook-runner/pull/206",
  "files_changed": ["tests/test_gates.js"],
  "duration_s": 180
}
```

Brain reads the result, compares to prediction, updates score, reports to user via SSH/Teams.

## What Gets Built (Tasks)

### T063: Archive ccc-manager
Mark as absorbed. Extract bridge file transport code if useful. Redirect TODO.md.

### T064: CCC bridge adapter
Brain writes task JSON that CCC (claude-portable) can consume. Uses CCC's existing dispatch mechanism — bridge directory with task files. Brain side: `CccBridge` class that writes tasks and polls for results.

### T065: CCC result monitor
Integrated into brain's service loop (step 3). Polls completed tasks directory. On result: compares to prediction, updates score, reports via appropriate channel (SSH session, Teams, GitHub comment). Re-dispatches on failure (up to max attempts).

### T066: SSH chat server
`asyncssh` server running on a configurable port (default 2222). Each SSH connection gets a `ChatSession` with the full brain context. Supports multiple concurrent users. Commands: `/clear`, `/history`, `/status` (event counts, active tasks, score), `/tasks` (active CCC work), `/quit`.

### T067: Idle loop
Periodic tasks between event cycles:
- Memory compaction (already built, runs every N cycles)
- Self-reflection on recent decisions
- Proactive insight surfacing ("3 PRs have been open > 7 days", "CI failure rate increased this week")
- Calendar prep ("meeting with X in 1h — here's context from recent emails/chats")
- Stale task cleanup (re-dispatch or close tasks past timeout)

### T068: Email adapter
MS Graph inbox poller. Yields normalized events (type=email_received). Filters by configured senders/subjects. Uses msgraph-lib shared token.

### T069: Calendar adapter
MS Graph calendar poller. Yields normalized events (type=meeting_created, meeting_updated, meeting_starting_soon). 15-min lookahead for "starting soon" alerts.

## What Doesn't Change
- Brain's existing adapters (GitHub, Teams, Slack, webhook, hook-runner) — unchanged
- Brain's LLM backend (subprocess/API) — brain still thinks for itself
- Brain's memory, context, reflection, scoring — unchanged
- CCC's worker runtime — unchanged, brain just writes task JSON it can consume
- CCC's fleet management — unchanged, CCC manages its own workers

## Deployment
- **Local**: brain runs as a service (`scripts/start.sh`), CCC workers as subprocesses
- **RONE K8s**: brain = Deployment (1 replica, PVC for DB), CCC = Jobs spawned on demand, SSH via NodePort or LoadBalancer
- **EC2**: brain = systemd service, CCC = spot instances, SQS for task transport
