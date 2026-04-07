"""Loop pattern analyzer — detects and tracks unproductive loops.

T059: When self-reflection flags "unproductive loop" issues, the brain:
  1. Identifies root cause from command history
  2. Suggests automation fix (e.g. "create a deploy.sh")
  3. Tracks recurrence in Tier 3 memory (global patterns)

Data sources:
  - self-reflection.jsonl — reflection verdicts with loop issues
  - hook-log.jsonl — individual Bash commands for root cause analysis
"""

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# Keywords that indicate unproductive loop issues
LOOP_KEYWORDS = [
    "loop", "retry", "retries", "failed attempt", "multiple failed",
    "cherry-pick", "rebase conflict", "manual patch", "manual patching",
    "wait for .* to fail", "struggling with infrastructure",
    "branch gymnastics", "abort.*switch.*delete", "repeated",
    "same operation", "unproductive",
]

LOOP_PATTERN = re.compile(
    "|".join(LOOP_KEYWORDS), re.IGNORECASE
)

# Command patterns that suggest loops (same command repeated)
RETRY_PATTERNS = [
    re.compile(r"git (cherry-pick|rebase|merge)", re.IGNORECASE),
    re.compile(r"git (checkout|branch -[dD]|reset)", re.IGNORECASE),
    re.compile(r"(curl|wget|scp|rsync).*\b(upload|deploy|push)\b", re.IGNORECASE),
    re.compile(r"(zip|tar|gzip).*&&.*(upload|cp|mv|scp)", re.IGNORECASE),
    re.compile(r"kubectl.*(apply|rollout|delete)", re.IGNORECASE),
]


class LoopPattern:
    """A detected loop pattern with root cause and suggested fix."""

    def __init__(self, pattern_id: str, description: str, root_cause: str,
                 suggested_fix: str, commands: list[str], project: str = "",
                 branch: str = "", severity: str = "medium"):
        self.pattern_id = pattern_id
        self.description = description
        self.root_cause = root_cause
        self.suggested_fix = suggested_fix
        self.commands = commands
        self.project = project
        self.branch = branch
        self.severity = severity
        self.detected_at = time.time()

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "description": self.description,
            "root_cause": self.root_cause,
            "suggested_fix": self.suggested_fix,
            "commands": self.commands[:10],  # Cap stored commands
            "project": self.project,
            "branch": self.branch,
            "severity": self.severity,
            "detected_at": self.detected_at,
        }


def is_loop_issue(issue: dict) -> bool:
    """Check if a reflection issue is about an unproductive loop."""
    desc = issue.get("description", "")
    fix = issue.get("fix", "")
    return bool(LOOP_PATTERN.search(desc) or LOOP_PATTERN.search(fix))


def extract_bash_commands(hook_log_path: str, max_lines: int = 200) -> list[dict]:
    """Read recent Bash commands from hook-log.jsonl.

    Returns list of {command, ts, result} dicts for Bash tool entries.
    """
    path = os.path.expanduser(hook_log_path)
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    # Take last N lines
    lines = lines[-max_lines:]

    commands = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("tool") != "Bash":
            continue

        cmd = entry.get("command", entry.get("input", ""))
        if not cmd:
            continue

        commands.append({
            "command": cmd[:200],
            "ts": entry.get("ts", ""),
            "result": entry.get("result", entry.get("decision", "")),
        })

    return commands


def find_repeated_commands(commands: list[dict], threshold: int = 3) -> list[dict]:
    """Find commands that were repeated (possible retry loops).

    Groups by normalized command prefix, returns groups with >= threshold occurrences.
    """
    # Normalize: strip timestamps, UUIDs, paths that vary
    def normalize(cmd: str) -> str:
        # Remove UUIDs
        cmd = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', cmd)
        # Remove hashes
        cmd = re.sub(r'[0-9a-f]{40}', '<HASH>', cmd)
        # Keep first 60 chars as the "signature"
        return cmd[:60].strip()

    groups: dict[str, list[dict]] = {}
    for cmd in commands:
        key = normalize(cmd["command"])
        groups.setdefault(key, []).append(cmd)

    repeated = []
    for key, cmds in groups.items():
        if len(cmds) >= threshold:
            repeated.append({
                "signature": key,
                "count": len(cmds),
                "commands": [c["command"] for c in cmds[:5]],
            })

    # Sort by count descending
    repeated.sort(key=lambda x: x["count"], reverse=True)
    return repeated


def find_matching_patterns(commands: list[dict]) -> list[dict]:
    """Find commands matching known retry/loop patterns."""
    matches = []
    for cmd_entry in commands:
        cmd = cmd_entry["command"]
        for pattern in RETRY_PATTERNS:
            if pattern.search(cmd):
                matches.append({
                    "command": cmd[:200],
                    "pattern": pattern.pattern,
                    "ts": cmd_entry.get("ts", ""),
                })
                break
    return matches


def analyze_loop(issue: dict, commands: list[dict], project: str = "",
                 branch: str = "") -> LoopPattern:
    """Analyze a loop issue with command history to identify root cause.

    This is the rule-based analyzer. When an LLM backend is available,
    the brain enriches this with deeper analysis.
    """
    desc = issue.get("description", "")
    fix = issue.get("fix", "")
    severity = issue.get("severity", "medium")

    # Find repeated commands
    repeated = find_repeated_commands(commands)
    pattern_matches = find_matching_patterns(commands)

    # Build root cause from evidence
    root_cause_parts = []
    if repeated:
        top = repeated[0]
        root_cause_parts.append(
            f"Command repeated {top['count']}x: {top['signature']}"
        )
    if pattern_matches:
        root_cause_parts.append(
            f"{len(pattern_matches)} commands match retry patterns "
            f"(e.g. {pattern_matches[0]['command'][:60]})"
        )
    if not root_cause_parts:
        root_cause_parts.append(f"Reflection flagged: {desc[:200]}")

    root_cause = "; ".join(root_cause_parts)

    # Build suggested fix
    suggested_fix = fix if fix else _suggest_fix(repeated, pattern_matches, desc)

    # Collect command strings for the pattern record
    cmd_strs = []
    for r in repeated[:3]:
        cmd_strs.extend(r["commands"][:2])
    for m in pattern_matches[:3]:
        cmd_strs.append(m["command"])

    pattern_id = f"loop-{int(time.time())}-{hash(desc) % 10000:04d}"

    return LoopPattern(
        pattern_id=pattern_id,
        description=desc[:500],
        root_cause=root_cause,
        suggested_fix=suggested_fix,
        commands=cmd_strs[:10],
        project=project,
        branch=branch,
        severity=severity,
    )


def _suggest_fix(repeated: list, pattern_matches: list, desc: str) -> str:
    """Generate a fix suggestion from the evidence."""
    # Check for deploy/upload loops
    if any("deploy" in str(r).lower() or "upload" in str(r).lower() for r in repeated):
        return "Create a deploy.sh script that handles build, upload, and verify in one step"
    if any("deploy" in str(m).lower() or "upload" in str(m).lower() for m in pattern_matches):
        return "Create a deploy.sh script that handles build, upload, and verify in one step"

    # Check for git conflict loops
    if any("cherry-pick" in str(r).lower() or "rebase" in str(r).lower() for r in repeated):
        return "Resolve merge strategy upstream — rebase/cherry-pick loops indicate a branching problem"
    if any("cherry-pick" in m.get("pattern", "") or "rebase" in m.get("pattern", "")
           for m in pattern_matches):
        return "Resolve merge strategy upstream — rebase/cherry-pick loops indicate a branching problem"

    # Check for kubectl loops
    if any("kubectl" in str(r).lower() for r in repeated):
        return "Create a deploy script with health checks and rollback instead of manual kubectl retries"

    # Generic
    if repeated:
        return f"Automate the repeated operation: {repeated[0]['signature'][:60]}"

    return "Identify root cause and automate — stop retrying manually"


class LoopAnalyzer:
    """Analyzes and tracks unproductive loop patterns.

    Integrates with:
    - HookRunnerAdapter events (source=hook-runner, channel=self-reflection)
    - MemoryManager Tier 3 for global pattern tracking
    - BrainAnalyzer for LLM-enhanced root cause analysis (when available)
    """

    def __init__(self, memory=None, hook_log_path: str = ""):
        from .utils import DEFAULT_HOOKS_DIR
        self.memory = memory
        self.hook_log_path = hook_log_path or os.path.join(
            DEFAULT_HOOKS_DIR, "hook-log.jsonl"
        )
        self._patterns: list[LoopPattern] = []

    def process_reflection_event(self, event: dict) -> list[LoopPattern]:
        """Process a reflection event, detecting any loop patterns.

        Args:
            event: Normalized event from HookRunnerAdapter (channel=self-reflection)

        Returns:
            List of detected LoopPatterns (empty if no loops found)
        """
        # Only process reflection events
        if event.get("channel") != "self-reflection":
            return []

        # Parse the issues from the event body or metadata
        issues = self._extract_issues(event)
        loop_issues = [i for i in issues if is_loop_issue(i)]

        if not loop_issues:
            return []

        # Get recent commands for root cause analysis
        commands = extract_bash_commands(self.hook_log_path)

        # Extract project/branch from event metadata
        body = event.get("body", "")
        metadata = event.get("metadata", {})
        project = metadata.get("project", "")
        branch = metadata.get("branch", "")

        patterns = []
        for issue in loop_issues:
            pattern = analyze_loop(issue, commands, project, branch)
            patterns.append(pattern)
            self._patterns.append(pattern)

            logger.info("[loop] Detected pattern: %s (root: %s)",
                        pattern.description[:80], pattern.root_cause[:80])

        # Store in Tier 3 memory for recurrence tracking
        if patterns and self.memory:
            self._update_global_memory(patterns)

        return patterns

    def _extract_issues(self, event: dict) -> list[dict]:
        """Extract issue dicts from a reflection event."""
        # Try parsing issues from the body (JSONL line stored as body)
        body = event.get("body", "")
        issues = []

        # Try direct body parse
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                issues = data.get("issues", [])
        except (json.JSONDecodeError, TypeError):
            pass

        if issues:
            return issues

        # Try splitting "Issues: ..." format from _normalize_reflection
        if body.startswith("Issues: "):
            parts = body.split("; ")
            for part in parts:
                text = part.replace("Issues: ", "").strip()
                if text:
                    issues.append({"description": text, "severity": "medium"})

        # Fallback: treat the title as the issue description
        if not issues:
            title = event.get("title", "")
            if LOOP_PATTERN.search(title) or LOOP_PATTERN.search(body):
                issues.append({
                    "description": title or body[:200],
                    "severity": "medium",
                })

        return issues

    def _update_global_memory(self, patterns: list[LoopPattern]):
        """Store loop patterns in Tier 3 memory for cross-session tracking."""
        if not self.memory:
            return

        # Read existing patterns
        existing = self.memory.get_global_memory("loop_patterns") or {
            "patterns": [],
            "total_detected": 0,
            "recurrences": {},
        }

        for pattern in patterns:
            existing["total_detected"] = existing.get("total_detected", 0) + 1

            # Track recurrence by root cause signature
            sig = pattern.root_cause[:80]
            recurrences = existing.get("recurrences", {})
            if sig in recurrences:
                recurrences[sig]["count"] = recurrences[sig].get("count", 0) + 1
                recurrences[sig]["last_seen"] = pattern.detected_at
            else:
                recurrences[sig] = {
                    "count": 1,
                    "first_seen": pattern.detected_at,
                    "last_seen": pattern.detected_at,
                    "suggested_fix": pattern.suggested_fix,
                }
            existing["recurrences"] = recurrences

            # Keep last 20 pattern summaries
            existing["patterns"] = existing.get("patterns", [])
            existing["patterns"].append({
                "pattern_id": pattern.pattern_id,
                "description": pattern.description[:200],
                "root_cause": pattern.root_cause[:200],
                "suggested_fix": pattern.suggested_fix[:200],
                "project": pattern.project,
                "severity": pattern.severity,
                "detected_at": pattern.detected_at,
            })
            existing["patterns"] = existing["patterns"][-20:]

        self.memory.set_global_memory("loop_patterns", existing)

    def get_recurrences(self) -> dict:
        """Get recurring loop patterns from Tier 3 memory."""
        if not self.memory:
            return {}
        data = self.memory.get_global_memory("loop_patterns") or {}
        return data.get("recurrences", {})

    def get_context_for_prompt(self) -> str:
        """Build context string for brain prompt enrichment.

        Returns a formatted string describing known loop patterns
        to help the brain avoid repeating them.
        """
        if not self.memory:
            return ""

        data = self.memory.get_global_memory("loop_patterns") or {}
        recurrences = data.get("recurrences", {})
        total = data.get("total_detected", 0)

        if not recurrences:
            return ""

        parts = [f"## Loop Pattern History ({total} total detected)"]

        # Show recurring patterns (count >= 2)
        recurring = {k: v for k, v in recurrences.items() if v.get("count", 0) >= 2}
        if recurring:
            parts.append("Recurring patterns (detected multiple times):")
            for sig, info in sorted(recurring.items(),
                                    key=lambda x: x[1].get("count", 0), reverse=True)[:5]:
                parts.append(f"- [{info['count']}x] {sig}")
                parts.append(f"  Fix: {info.get('suggested_fix', 'N/A')}")

        # Show recent one-off patterns
        recent = data.get("patterns", [])[-3:]
        if recent:
            parts.append("Recent loop detections:")
            for p in recent:
                parts.append(f"- {p.get('description', '')[:100]} "
                             f"(project: {p.get('project', '?')})")

        return "\n".join(parts)
