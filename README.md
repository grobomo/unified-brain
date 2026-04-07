# Unified Brain

Multi-channel brain service. Ingests events from GitHub, Teams, Slack, and webhook sources through a single LLM-powered analyzer with shared context and three-tier memory.

## Architecture

```
Communication Channels         unified-brain (THINKER)              ccc-manager (DOER)
─────────────────────         ─────────────────────                ──────────────────
GitHub events      ──→  EventStore (SQLite+FTS)                   Workers:
Teams messages     ──→  Brain Analyzer (pluggable LLM)             - Local (claude -p)
Slack messages     ──→  Three-tier Memory           ──DISPATCH──→  - K8s (kubectl exec)
Webhook/API        ──→  Cross-channel Context                      - EC2 (SSH/SSM)
                        Project Registry                           Fleet coordination
                             │                      ←─RESULT────  Verification
                             ▼
                        Action Router
                        ├─ RESPOND → GitHub comment / Teams reply / Slack message
                        ├─ DISPATCH → ccc-manager bridge/SQS
                        ├─ ALERT → email
                        └─ LOG → memory only
```

## Features

- **Single process, single DB** — one SQLite+FTS database for all channels
- **5 channel adapters** — GitHub (gh CLI), Teams (MS Graph), Slack (Web API), Webhooks (HTTP POST), Hook-runner (JSONL file poller)
- **Webhook ingestion** — HTTP endpoint accepts events via POST (`/events`, `/events/raw`), HMAC-SHA256 verification, token bucket rate limiting per IP
- **Cross-channel context** — when analyzing a GitHub issue, the brain sees related Teams/Slack discussions
- **Three-tier memory** — Tier 1: hot cache (24h events), Tier 2: per-project summaries, Tier 3: global patterns
- **Interactive chat** — CLI REPL, REST `/chat`, WebSocket `/ws/chat` with persistent per-user conversation sessions
- **Persona system** — per-user brain identity (name + emoji), self-message filtering in all adapters
- **Closed-loop self-improvement** — detects patterns in hook-runner events, predicts outcomes before changes, implements fixes, monitors with exponential backoff, rolls back on prediction mismatch
- **Brain-owned score** — prediction accuracy (70%) + user interrupt rate (30%), rolling tracker, Prometheus metrics
- **Feedback loop** — tracks dispatch/respond outcomes, feeds success/failure patterns back to the brain prompt
- **Active respond** — posts GitHub comments, Teams messages, and Slack messages directly via channel APIs, falls back to outbox
- **Pluggable LLM backend** — subprocess (`claude -p`) for local, Anthropic HTTP API for containers/EC2
- **Pluggable dispatch transport** — filesystem outbox (local/K8s) or SQS (EC2)
- **LLM observability** — JSONL audit trail (`data/llm.jsonl`), 16 Prometheus metric series
- **Rule-based fallback** — works without LLM when `claude` CLI is unavailable
- **Outbox pattern** — actions written as JSON files to channel-specific directories
- **ccc-manager integration** — dispatches tasks via bridge directory or SQS, polls for results
- **Environment portability** — runs locally, in K8s, or on EC2 with config-driven plugins

## Quick Start

```bash
# Local
bash scripts/start.sh

# Single cycle (test mode)
PYTHONPATH=src python -m unified_brain --config config/brain.yaml --once

# With health endpoint
PYTHONPATH=src python -m unified_brain --health-port 8790

# Docker Compose (brain + Prometheus + Grafana)
docker compose up -d

# Kubernetes (RONE)
kubectl apply -k k8s/
kubectl create secret generic unified-brain-secrets \
  --from-literal=GITHUB_TOKEN=ghp_... \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...

# AWS EC2 (spot instance + SQS)
./scripts/deploy-ec2.sh --vpc-id vpc-xxx --subnet-id subnet-xxx
```

## Configuration

Base config in `config/brain.yaml`. Secrets go in `config/brain.local.yaml` (gitignored).

Environment variables can be injected via `${VAR_NAME}` syntax in any config value — useful for K8s Secrets and EC2 environments.

```yaml
# config/brain.yaml
interval: 300
db_path: data/brain.db

brain:
  llm_backend: subprocess  # or: api (uses ANTHROPIC_API_KEY)

dispatcher:
  transport: file          # or: sqs (uses SQS_TASK_QUEUE_URL)
  outbox_dir: data/outbox

adapters:
  github:
    enabled: true
    repos:
      - owner/repo
  teams:
    enabled: false
  slack:
    enabled: false
    bot_token: ${SLACK_BOT_TOKEN}
    channel_ids:
      - C0123456789
  webhook:
    enabled: false
    webhook_port: 8791
    webhook_rate_limit: 10.0  # requests/sec per IP (0 to disable)
    webhook_rate_burst: 20    # max burst per IP
    # webhook_secret: hmac-secret  # optional HMAC verification
  hook_runner:
    enabled: false
    hook_log_path: ~/.claude/hooks/hook-log.jsonl
    reflection_log_path: ~/.claude/hooks/self-reflection.jsonl
    reflection:
      hooks_dir: ~/.claude/hooks
      score_file: ~/.claude/hooks/reflection-score.json
```

## Project Registry

`config/projects.json` maps repos, Teams chats, and people to projects. The brain uses this for cross-channel context (linking a GitHub issue to related Teams discussions).

Secret fields (Teams chat IDs) go in `config/projects.local.json` (gitignored), which is deep-merged at load time.

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -v
```

252 integration tests covering: store, brain, dispatcher, registry, context, memory, outbox, adapters (GitHub, Teams, Slack, Webhook, Hook-runner), transport factory, SQS mock, config overlay, env interpolation, active respond, Prometheus metrics, feedback loop, rate limiting, interactive chat, persona system, LLM logging, self-message filtering, reflection task lifecycle, file edit/rollback, prediction-outcome scoring, brain score, E2E self-improvement loops.

## Dependencies

Zero external dependencies for core functionality. Python 3.12+ stdlib only.

- `gh` CLI — required for GitHub adapter
- `pyyaml` — optional, for YAML config files (falls back to JSON)
- `boto3` — optional, for SQS dispatch transport (EC2 deployments only)
