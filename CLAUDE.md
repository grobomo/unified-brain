# Unified Brain

Multi-channel brain service. Processes events from GitHub, Teams, and future sources through a single LLM-powered analyzer with shared context and three-tier memory.

## Heritage
Extracted from github-agent/core/ (specs 001-010). The core modules (store.py, brain.py, dispatcher.py, context.py, memory.py, compactor.py) were designed source-agnostic from the start — this project makes that explicit.

## Key design decisions
- **Single process, single DB** — one SQLite+FTS database for all channels
- **Channel adapters** — each source (GitHub, Teams) is a thin adapter that yields normalized events
- **Three-tier memory** — Tier 1: hot cache (24h events), Tier 2: per-source JSON (repo/chat context), Tier 3: account-level summary
- **Rule-based fallback** — works without LLM when claude CLI is unavailable

## Related projects
- `_grobomo/github-agent/` — GitHub monitoring agent (source of core/ modules)
- `_tmemu/teams-agent/` — Teams monitoring agent (source of Teams adapter)
- `~/Documents/ProjectsCL1/msgraph-lib/` — shared MS Graph token management
