# Unified Brain

Multi-channel brain service. Ingests events from GitHub, Teams, and future sources through a single LLM-powered analyzer with shared context and three-tier memory.

## Architecture

```
Communication Channels         unified-brain (THINKER)              ccc-manager (DOER)
─────────────────────         ─────────────────────                ──────────────────
GitHub events      ──→  EventStore (SQLite+FTS)                   Workers:
Teams messages     ──→  Brain Analyzer (pluggable LLM)             - Local (claude -p)
Webhook/API        ──→  Three-tier Memory           ──DISPATCH──→  - K8s (kubectl exec)
                        Cross-channel Context                      - EC2 (SSH/SSM)
                        Project Registry                           Fleet coordination
                             │                      ←─RESULT────  Verification
                             ▼
                        Action Router
                        ├─ RESPOND → GitHub comment / Teams reply
                        ├─ DISPATCH → ccc-manager bridge/SQS
                        ├─ ALERT → email
                        └─ LOG → memory only
```

## Features

- **Single process, single DB** — one SQLite+FTS database for all channels
- **Channel adapters** — thin adapters normalize events from each source (GitHub, Teams, Webhooks)
- **Webhook ingestion** — HTTP endpoint accepts events via POST (`/events`, `/events/raw`), with HMAC-SHA256 signature verification
- **Cross-channel context** — when analyzing a GitHub issue, the brain sees related Teams discussions
- **Three-tier memory** — Tier 1: hot cache (24h events), Tier 2: per-project summaries, Tier 3: global patterns
- **Feedback loop** — tracks dispatch/respond outcomes, feeds success/failure patterns back to the brain prompt
- **Active respond** — posts GitHub comments and Teams messages directly via channel APIs, falls back to outbox
- **Pluggable LLM backend** — subprocess (`claude -p`) for local, Anthropic HTTP API for containers/EC2
- **Pluggable dispatch transport** — filesystem outbox (local/K8s) or SQS (EC2)
- **Prometheus metrics** — `/metrics` endpoint with event ingestion rates, brain decisions, dispatch outcomes, cycle duration
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

# Docker
docker build -t unified-brain .
docker run -v $(pwd)/data:/app/data unified-brain

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
  webhook:
    enabled: false
    webhook_port: 8791
    # webhook_secret: hmac-secret  # optional HMAC verification
```

## Project Registry

`config/projects.json` maps repos, Teams chats, and people to projects. The brain uses this for cross-channel context (linking a GitHub issue to related Teams discussions).

Secret fields (Teams chat IDs) go in `config/projects.local.json` (gitignored), which is deep-merged at load time.

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -v
```

88 integration tests covering: store, brain, dispatcher, registry, context, memory, outbox, adapters, transport factory, SQS mock, config overlay, env interpolation, active respond, Prometheus metrics, feedback loop, webhook adapter.

## Dependencies

Zero external dependencies for core functionality. Python 3.12+ stdlib only.

- `gh` CLI — required for GitHub adapter
- `pyyaml` — optional, for YAML config files (falls back to JSON)
- `boto3` — optional, for SQS dispatch transport (EC2 deployments only)
