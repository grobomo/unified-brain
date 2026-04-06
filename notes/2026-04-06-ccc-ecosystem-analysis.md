# CCC Ecosystem Analysis — 2026-04-06

Research into how unified-brain, ccc-central, ccc-manager, and ccc (claude-portable CLI) relate, overlap, and should integrate.

## The Four Projects

### 1. unified-brain (Python, empty — TODO.md + CLAUDE.md only)

**Location**: `_grobomo/unified-brain/`
**Status**: Brand new. No code. On branch `001-T001-project-foundation`.
**Heritage**: Extracted from github-agent/core/ specs 007a-c.

**Vision**: Multi-channel brain service. Single process, single SQLite+FTS database, single LLM context window. Ingests events from GitHub, Teams, future sources. Analyzes with `claude -p` and three-tier memory (hot cache / per-source JSON / account-level summary). Dispatches actions: RESPOND, DISPATCH, ALERT, LOG.

**Planned components**:
- EventStore (SQLite+FTS) — unified schema for all channels
- Channel adapters (GitHub, Teams) — thin pollers yielding normalized events
- Brain analyzer — `claude -p` with cross-channel context
- Three-tier memory — hot (24h), source (per-repo/chat), account (global)
- Dispatcher — routes actions to channels or to CCC workers
- Project registry — maps repos, chats, people to projects

### 2. ccc-central (no language, no git — just a TODO.md)

**Location**: `_grobomo/ccc-central/`
**Status**: Empty. Single TODO.md file. No git repo.

**Vision**: Central monitoring and dispatch system. Watches GitHub repos, Teams messages (via RONE bridge), and SSH connections. Spawns Claude Code sessions (`claude -p`) to respond to activity. Designed for local now, AWS Lambda later.

**Planned components**:
- GitHub event poller (all repos for grobomo + tmemu)
- Event classification and routing
- CCC spawner — starts `claude -p` sessions
- Teams integration via rone-teams-poller
- Lambda packaging for serverless deployment

### 3. ccc-manager (Node.js, mature — 5 specs completed, published to GitHub)

**Location**: `_grobomo/ccc-manager/`
**Status**: Fully built. Published. Comprehensive test suite.

**Vision**: Universal manager runtime with pluggable components for CCC fleets. Monitors health, receives tasks from multiple sources, dispatches fixes via SHTD pipeline, verifies results.

**Built components**:

| Category | Components |
|----------|------------|
| **Monitors** | process, log, cron |
| **Inputs** | BridgeInput (dir polling), AlertInput (in-memory), GitHubInput (gh CLI), SQSInput, WebhookInput |
| **Dispatchers** | SHTDDispatcher (rule-based), ClaudeDispatcher (AI via `claude -p`), SQSDispatcher (distributed) |
| **Workers** | LocalWorker (child_process), K8sWorker (kubectl exec), EC2Worker (SSH/SSM) |
| **Verifiers** | TestSuiteVerifier (command-based) |
| **Notifiers** | WebhookNotifier, FileNotifier |
| **Fleet** | FleetCoordinator (heartbeat, claims, stale detection via shared FS) |
| **Infrastructure** | State persistence (queue, history, metrics), priority queue, task claiming, dedup, Prometheus metrics, health endpoints (/healthz, /readyz, /metrics, /fleet), hot config reload, multi-instance orchestrator, worktree isolation, sharder (cartesian/round-robin/chunk), parallel dispatch, SQS-based distributed dispatch, drain on shutdown |

**Key architecture**:
- Zero npm dependencies — Node.js built-ins only
- Per-project YAML configs define which monitors/inputs/workers to use
- Plugin system — custom components via file path imports
- Multi-manager mode — run N configs with shared health endpoint

### 4. ccc (Python CLI in claude-portable)

**Location**: `_grobomo/claude-portable/ccc` (single Python file, ~2600 lines)
**Status**: Production. Actively used for fleet management.

**Vision**: EC2 fleet launcher and manager. The user-facing CLI for the entire CCC system.

**Built capabilities**:
- `ccc` — Connect to / launch EC2 instances (worker or dispatcher role)
- `ccc list` / `ccc stop` / `ccc kill` — Instance lifecycle
- `ccc --role dispatcher` — Launch a dedicated t3.small dispatcher instance
- `ccc teams <chat_id>` — Poll Teams chat for @claude mentions, dispatch to workers
- `ccc work` — Show what each worker is doing (branch, PR, task progress, stages)
- `ccc dashboard` — Fleet-wide view: instances, branches, PRs, processes
- `ccc board` — Pipeline status from dispatcher (worker phases, tasks)
- `ccc cancel` / `ccc maint` — Worker control (kill Claude, pause pickup)
- `ccc health` / `ccc interrupt` / `ccc pull` — HTTP API calls to workers
- `ccc bundle` — Config bundle scrape/build/deploy/patch
- `ccc offload` — Send work to cloud, get web URL
- `ccc cleanup` — Codebase review for dead code
- `ccc-client.sh` — REST API client for dispatcher (submit/status/poll/cancel/workers/tasks)

**Key architecture**:
- Manages EC2 instances with stop/start lifecycle
- Docker compose on each instance (claude-portable container)
- Dispatcher role runs `dispatcher-daemon.sh` (separate from workers)
- Workers run `continuous-claude.sh` with stage-based pipeline (WHY->RESEARCH->REVIEW->PLAN->TESTS->IMPLEMENT->VERIFY->PR)
- Teams integration via `scripts/teams-dispatch.py`
- S3 state sync between instances
- IAM roles, security groups, SSH key management
- Fleet key distribution (dispatcher syncs `.pem` files to all workers)
- ccc.config.json for fleet settings (region, instance type, max instances, Teams chat ID)

## How ccc CLI Differs from ccc-manager

These are NOT the same thing and serve different layers:

| Aspect | ccc-manager | ccc CLI |
|--------|-------------|---------|
| **Runtime** | Node.js long-running service | Python CLI (run-and-exit or poll loop) |
| **Where it runs** | On each managed instance (or centrally) | On the user's local machine |
| **Workers** | Abstract (Local/K8s/EC2 via code) | Concrete EC2 instances via AWS API |
| **Task input** | Bridge files, GitHub issues, SQS, webhooks | Teams chat polling, SSH |
| **Task execution** | `claude -p` via dispatchers | `continuous-claude.sh` (stage pipeline) |
| **Config** | Per-project YAML | `ccc.config.json` (fleet-wide) |
| **State** | JSON files in state/ dir | EC2 tags + S3 sync |
| **Dispatch model** | Fire-and-verify (analyze->dispatch->verify) | Fire-and-track (submit->poll->complete) |

**ccc-manager is infrastructure-agnostic** — it doesn't care where workers are. It dispatches tasks and verifies results via pluggable worker classes.

**ccc CLI is EC2-specific** — it manages the actual cloud instances, SSH connections, Docker containers. Its dispatcher role runs a daemon that accepts tasks via HTTP API and assigns them to worker instances.

**They can layer**: ccc CLI launches and manages the EC2 fleet. ccc-manager runs ON those instances (or centrally) to handle task dispatch, verification, and fleet coordination at the software level. The ccc CLI's `ccc-client.sh` REST API could become another input source for ccc-manager.

## Overlap Matrix

### Event Ingestion
| Capability | unified-brain | ccc-central | ccc-manager | ccc CLI |
|-----------|--------------|-------------|-------------|---------|
| GitHub events | Planned (channel adapter) | Planned (poller) | Built (`GitHubInput`) | - |
| Teams messages | Planned (channel adapter) | Planned (via RONE bridge) | Built (`BridgeInput`) | Built (`ccc teams`) |
| Webhook/HTTP | - | - | Built (`WebhookInput`) | - |
| SQS queue | - | - | Built (`SQSInput`) | - |
| File bridge | - | - | Built (`BridgeInput`) | - |

### Analysis / Intelligence
| Capability | unified-brain | ccc-central | ccc-manager | ccc CLI |
|-----------|--------------|-------------|-------------|---------|
| LLM analysis | Planned (brain + memory) | Planned (event classification) | Built (`ClaudeDispatcher`) | - |
| Cross-channel context | Planned (core feature) | - | - | - |
| Three-tier memory | Planned (core feature) | - | - | - |
| Rule-based fallback | Planned | - | Built (`SHTDDispatcher`) | - |

### Task Dispatch
| Capability | unified-brain | ccc-central | ccc-manager | ccc CLI |
|-----------|--------------|-------------|-------------|---------|
| Task routing | Planned (action router) | Planned (CCC spawner) | Built (full pipeline) | Built (SSH-based) |
| Priority queue | - | - | Built (4-level) | - |
| Parallel dispatch | - | - | Built (sharder + parallelDispatch) | - |
| Dependency resolution | - | - | Built (dependsOn + writeSet) | - |
| Retry with backoff | - | - | Built (maxRetries) | - |
| Dedup | - | - | Built (configurable window) | - |

### Worker Management
| Capability | unified-brain | ccc-central | ccc-manager | ccc CLI |
|-----------|--------------|-------------|-------------|---------|
| Local workers | - | - | Built (`LocalWorker`) | - |
| K8s workers | - | - | Built (`K8sWorker`) | - |
| EC2 workers | - | - | Built (`EC2Worker`) | Built (launch/stop/kill/connect) |
| Worktree isolation | - | - | Built (`WorktreeManager`) | - |
| Fleet coordination | - | - | Built (`FleetCoordinator`) | Built (dashboard, work, board) |
| Task claiming | - | - | Built (file-based locks) | - |

## Proposed Architecture

```
User / Teams / GitHub
        |
        v
+--------------------------------------------------+
|              unified-brain (THINKER)              |
|                                                   |
|  Channel Adapters -> EventStore (SQLite+FTS)      |
|  Brain Analyzer (claude -p + three-tier memory)   |
|  Cross-channel Context + Project Registry         |
|                                                   |
|  Action Router:                                   |
|  +- RESPOND -> GitHub/Teams (direct)              |
|  +- DISPATCH -> writes task JSON ------+          |
|  +- ALERT -> email                     |          |
|  +- LOG -> memory only                 |          |
+----------------------------------------|----------+
                                         |
            +----------------------------+
            | Bridge dir (local) or SQS (AWS)
            v
+--------------------------------------------------+
|             ccc-manager (DOER)                    |
|                                                   |
|  BridgeInput/SQSInput -> Priority Queue -> Dedup  |
|  ClaudeDispatcher (AI plan) or SHTD (rule-based)  |
|  Sharder -> Parallel Dispatch                     |
|                                                   |
|  Workers:                                         |
|  +- LocalWorker (claude -p, child_process)        |
|  +- K8sWorker (kubectl exec into RONE pods)       |
|  +- EC2Worker (SSH/SSM into ccc instances)        |
|                                                   |
|  Verification -> Retry -> Notification            |
|  Fleet coordination (heartbeat, claims)           |
|  Result -> writes to completedDir --------+       |
+--------------------------------------------|----- +
                                             |
            +--------------------------------+
            | Result JSON
            v
+--------------------------------------------------+
|              unified-brain (result pickup)         |
|                                                   |
|  Result poller reads completedDir                 |
|  Updates memory with outcome                      |
|  Posts summary to originating channel             |
|  (Teams reply, GitHub comment, etc.)              |
+--------------------------------------------------+

+--------------------------------------------------+
|              ccc CLI (FLEET OPS)                  |
|                                                   |
|  EC2 lifecycle (launch/stop/kill/connect)         |
|  Teams polling (ccc teams -> dispatch)            |
|  Fleet visibility (dashboard, work, board)        |
|  Worker control (cancel, maint, interrupt)        |
|  Instance management (bundle, scp, vnc)           |
+--------------------------------------------------+
```

## Real-World Flow: "smells like machine learning" Teams Chat

Shared repo, multiple communication streams, tools in both AWS and RONE:

1. **unified-brain** has adapters watching:
   - Teams chat "smells like machine learning" for messages
   - GitHub repos (grobomo/*, hackathon26/*) for issues, PRs, commits
   - RONE bridge for k8s events

2. Someone posts in Teams: "The flaky test in repo X keeps failing, can someone fix it?"

3. **Brain analyzer** processes with full context:
   - WHO: teammate Bob, usually works on frontend, has been discussing test reliability
   - WHAT: repo X has 3 CI failures in last 24h (from GitHub adapter)
   - WHERE: the test file `test_pipeline.py` was last modified 5 days ago
   - CONTEXT: similar issue was fixed last month (from tier-3 memory)

4. Brain decides: DISPATCH — writes task JSON to bridge directory:
   ```json
   {
     "id": "brain-1712345678",
     "source": "teams:smells-like-ml",
     "type": "fix",
     "summary": "Fix flaky test in repo X test_pipeline.py",
     "priority": "high",
     "details": {
       "repo": "grobomo/repo-x",
       "ci_failures": 3,
       "test_file": "test_pipeline.py",
       "last_similar_fix": "fix/repair-1711234567"
     },
     "channel_context": {
       "reply_to": "teams:smells-like-ml:msg-abc123",
       "requested_by": "Bob"
     }
   }
   ```

5. **ccc-manager** BridgeInput picks up the task:
   - ClaudeDispatcher analyzes and creates repair plan
   - Sharder splits if needed (e.g., multiple test files)
   - EC2Worker dispatches to a ccc instance (or K8sWorker to RONE pod)
   - Worker creates worktree, runs `claude -p` to fix, runs tests
   - TestSuiteVerifier confirms tests pass
   - Result written to completedDir

6. **unified-brain** result poller picks up:
   ```json
   {
     "id": "brain-1712345678",
     "success": true,
     "output": "Fixed flaky test. Root cause: race condition in async setup. PR #42 created.",
     "pr_url": "https://github.com/grobomo/repo-x/pull/42"
   }
   ```

7. Brain posts to Teams: "Fixed the flaky test in repo X. Root cause was a race condition in async setup. PR #42 is ready for review. cc @Bob"

8. Brain also comments on the GitHub CI failure issue linking the fix.

9. Memory updated: tier-2 (repo-x context) records the fix pattern, tier-1 (hot) notes Bob's request was fulfilled.

## Integration Protocol

### Task JSON (unified-brain -> ccc-manager)

Written to bridge directory or SQS queue. Format matches ccc-manager's BridgeInput:

```json
{
  "id": "brain-<timestamp>",
  "source": "<channel>:<identifier>",
  "type": "fix|investigate|deploy|review",
  "summary": "Human-readable task description",
  "priority": "critical|high|normal|low",
  "details": {
    "repo": "owner/name",
    "files": ["path/to/relevant/file.py"],
    "error_log": "...",
    "ci_url": "..."
  },
  "channel_context": {
    "reply_to": "<channel>:<msg-id>",
    "requested_by": "<person>",
    "project": "<project-registry-key>"
  }
}
```

### Result JSON (ccc-manager -> unified-brain)

Written to bridge completedDir:

```json
{
  "id": "brain-<timestamp>",
  "success": true,
  "output": "What was done",
  "error": null,
  "pr_url": "https://github.com/...",
  "branch": "fix/repair-...",
  "tasks_completed": 3,
  "verification": { "passed": true, "details": "All tests pass" }
}
```

### Project Registry (shared YAML)

Both unified-brain and ccc-manager reference the same registry:

```yaml
projects:
  repo-x:
    repos: [grobomo/repo-x]
    teams_chat: "19:abc...@thread.v2"
    manager_config: config/repo-x.yaml
    worker_type: ec2
  hackathon26:
    repos: [grobomo/hackathon26, altarr/boothapp]
    teams_chat: "19:def...@thread.v2"
    manager_config: config/hackathon26.yaml
    worker_type: k8s
    k8s_namespace: hackathon
```

## ccc-central Disposition

**Absorb entirely.** Its four planned tasks map to:

| ccc-central task | Goes to |
|-----------------|---------|
| T001: GitHub monitor + event classification | unified-brain (GitHub channel adapter) |
| T002: Event router + CCC spawner | unified-brain (action router) + ccc-manager (dispatch) |
| T003: Teams integration | unified-brain (Teams channel adapter) |
| T004: Lambda packaging | ccc-manager (already has SQS distributed dispatch) |

## Action Items

### unified-brain
1. Build foundation (T001-T006): EventStore, adapters, brain service
2. Build ccc-manager integration (T007-T010): dispatch adapter, result poller, project registry, E2E test
3. Build cross-channel intelligence (T011-T013): context, memory, action relay

### ccc-manager
1. Ensure BridgeInput.writeResult() includes all fields brain needs (channel_context passthrough)
2. Add project registry awareness — read shared registry to auto-configure workers per project
3. Support channel_context in task/result passthrough so brain knows where to relay results

### ccc CLI (claude-portable)
1. No changes needed for integration — it's the ops layer
2. `ccc teams` becomes a fallback/override once unified-brain handles Teams
3. Consider adding `ccc brain status` to show unified-brain health

### ccc-central
1. Archive the directory
