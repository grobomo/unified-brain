# Unified Brain

Multi-channel brain service. Ingests events from GitHub, Teams, and future sources through a single LLM-powered analyzer with shared context and three-tier memory.

## Architecture

```
Communication Channels         unified-brain (THINKER)              ccc-manager (DOER)
─────────────────────         ─────────────────────                ──────────────────
GitHub events      ──→  EventStore (SQLite+FTS)                   Workers:
Teams messages     ──→  Brain Analyzer (claude -p)                 - Local (claude -p)
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
- **Channel adapters** — thin adapters normalize events from each source (GitHub, Teams)
- **Cross-channel context** — when analyzing a GitHub issue, the brain sees related Teams discussions
- **Three-tier memory** — Tier 1: hot cache (24h events), Tier 2: per-project summaries, Tier 3: global patterns
- **Rule-based fallback** — works without LLM when `claude` CLI is unavailable
- **Outbox pattern** — actions written as JSON files to channel-specific directories
- **ccc-manager integration** — dispatches tasks via bridge directory, polls for results
- **Environment portability** — runs locally, in K8s, or on EC2 via env var interpolation

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

# Kubernetes
kubectl apply -k k8s/
```

## Configuration

Base config in `config/brain.yaml`. Secrets go in `config/brain.local.yaml` (gitignored).

Environment variables can be injected via `${VAR_NAME}` syntax in any config value — useful for K8s Secrets and EC2 environments.

```yaml
# config/brain.yaml
interval: 300
db_path: data/brain.db

adapters:
  github:
    enabled: true
    token: ${GITHUB_TOKEN}
    repos:
      - owner/repo
  teams:
    enabled: false
```

## Project Registry

`config/projects.json` maps repos, Teams chats, and people to projects. The brain uses this for cross-channel context (linking a GitHub issue to related Teams discussions).

Secret fields (Teams chat IDs) go in `config/projects.local.json` (gitignored), which is deep-merged at load time.

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -v
```

48 integration tests covering: store, brain, dispatcher, registry, context, memory, outbox, adapters, config overlay, env interpolation.

## Dependencies

Zero external dependencies for core functionality. Python 3.12+ stdlib only.

Optional: `pyyaml` for YAML config files (falls back to JSON without it).
