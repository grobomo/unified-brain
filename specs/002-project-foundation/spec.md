# Spec 002: Project Foundation

## Problem
unified-brain has git, CLAUDE.md, publish.json, and .gitignore but no code structure, no requirements.txt, no secret-scan CI, and no source modules. Need the scaffolding before implementing EventStore and adapters.

## Solution
Create Python project structure with:
- requirements.txt (minimal deps)
- src/unified_brain/ package with __init__.py
- src/unified_brain/store.py — EventStore placeholder
- src/unified_brain/adapters/ — channel adapter base class
- src/unified_brain/brain.py — brain analyzer placeholder
- src/unified_brain/dispatcher.py — action router placeholder
- .github/workflows/secret-scan.yml — required CI
- .gitignore additions for state/, notes/

## Scope
- Project scaffolding only — no business logic yet
- Placeholders with docstrings and TODO comments
- Secret-scan CI workflow
