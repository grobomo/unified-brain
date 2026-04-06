# Spec 005: Portable Deployment — Local, RONE K8s, AWS EC2

## Problem

The brain is architecturally sound but non-functional. Every external integration is hardcoded to one developer's local filesystem:
- GitHub adapter: `sys.path.insert(0, ~/Documents/ProjectsCL1/_grobomo/github-agent/)`
- Teams adapter: `sys.path.insert(0, ~/Documents/ProjectsCL1/_tmemu/teams-agent/)`
- Brain analyzer: `subprocess.run([claude, -p, ...])`
- Dispatcher: writes JSON files to local `data/outbox/`

The brain itself (analyze, context, memory, store) is environment-agnostic. The problem is the **plugins** — adapters, LLM backend, and dispatchers are not.

## Design Principle

**The brain is the constant. Everything else is a pluggable variable.**

```
                    ┌─────────────┐
   Adapters ──────→ │    BRAIN    │ ──────→ Dispatchers
   (input plugins)  │  (constant) │         (output plugins)
                    │  store.py   │
                    │  brain.py   │
                    │  context.py │
                    │  memory.py  │
                    └─────────────┘
                          │
                      LLM Backend
                      (plugin)
```

### Plugin types
1. **Adapters** (input): poll sources for events → normalized dicts
2. **LLM Backend**: analyze events → action decisions
3. **Dispatchers** (output): route actions to targets

### Environment profiles
| Component | Local | RONE K8s | AWS EC2 |
|-----------|-------|----------|---------|
| GitHub adapter | `gh` CLI directly | `gh` CLI in container | `gh` CLI or API |
| Teams adapter | MS Graph API (token from msgraph-lib) | MS Graph API (token from K8s Secret) | MS Graph API (token from env) |
| LLM backend | `claude -p` subprocess | Claude API (HTTP) | Claude API (HTTP) |
| Dispatcher | Filesystem outbox + ccc-manager bridge | K8s PVC bridge or SQS | SQS |
| Config secrets | `brain.local.yaml` file | K8s Secrets → env vars | EC2 env vars / SSM |

## Tasks

### T030: Self-contained GitHub adapter — use `gh` CLI directly, no github-agent dependency
- Replace `from github.poller import GitHubPoller` with direct `gh api` calls
- `gh api repos/{owner}/{repo}/issues`, `gh api repos/{owner}/{repo}/pulls`, etc.
- Normalize in-place — no external normalizer dependency
- Works identically everywhere `gh` is installed (local, container, EC2)

### T031: Self-contained Teams adapter — inline MS Graph calls, no teams-agent dependency
- Replace `from lib.msgraph.auth import TokenManager` with direct `requests` calls
- Token acquisition: client_credentials flow with tenant_id/client_id/client_secret from config
- `GET /chats/{id}/messages` — simple REST, no external lib needed
- Works identically everywhere with network access and credentials

### T032: Pluggable LLM backend — subprocess OR HTTP API
- Abstract `LLMBackend` interface: `analyze(prompt) -> str`
- `SubprocessBackend`: current `claude -p` (for local)
- `APIBackend`: Anthropic HTTP API with API key from config/env
- Config selects which backend: `brain.llm_backend: subprocess|api`
- API backend works in containers/EC2 without claude CLI installed

### T033: Pluggable dispatcher — filesystem OR SQS
- Abstract dispatcher routing — config selects transport per action type
- `FileDispatcher`: current filesystem outbox (local, K8s PVC)
- `SQSDispatcher`: AWS SQS for DISPATCH actions (EC2)
- Config: `dispatcher.transport: file|sqs`

### T034: Verify local deployment — run brain service, confirm events flow end-to-end
- Start with interval=3
- Confirm GitHub events ingested via gh CLI
- Confirm Teams events ingested via Graph API
- Confirm brain analyzes and dispatches
- Confirm health endpoint responds
- Fix any bugs found

### T035: Deploy to RONE K8s
- Build Docker image with gh CLI installed
- Push to RONE registry
- Apply k8s/ manifests with real config
- K8s Secrets for GitHub token, Teams credentials, Claude API key
- Verify pod starts, polls, analyzes, dispatches

### T036: Deploy to AWS EC2
- EC2 spot instance via aws skill
- Install deps (Python, gh CLI)
- SQS dispatcher for ccc-manager integration
- CloudWatch logs
- Verify end-to-end

## Sequencing

T030 → T031 → T032 → T033 → T034 (local works) → T035 (RONE) → T036 (AWS)

Each task produces a working, testable increment. T034 is the gate — local must work before deploying anywhere.
