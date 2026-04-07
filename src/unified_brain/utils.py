"""Shared utilities used across unified-brain modules."""

import json
import os
from pathlib import Path

DEFAULT_SCORE_FILE = os.path.expanduser("~/.claude/hooks/reflection-score.json")


def read_score_file(score_file: str = "") -> dict:
    """Read hook-runner's reflection-score.json. Returns defaults if missing/corrupt."""
    path = Path(score_file or DEFAULT_SCORE_FILE)
    if not path.exists():
        return {"points": 0, "level": 0, "streak": 0, "interventions": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"points": 0, "level": 0, "streak": 0, "interventions": 0}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins for scalars."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result
