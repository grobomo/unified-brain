"""Reflection implementer — file edit/rollback, brain prompt enrichment, monitoring.

T055: The bridge between the brain's analysis and actual hook-runner module changes.
Handles file backup/edit/rollback, enriches the brain prompt with reflection context,
tracks prediction accuracy in Tier 2 memory, and integrates with the service loop.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .reflection import (
    BACKOFF_INTERVALS,
    Prediction,
    ReflectionTask,
    ReflectionTaskStore,
    TaskState,
    compute_prediction_accuracy,
)
from .utils import DEFAULT_HOOKS_DIR, read_score_file

logger = logging.getLogger(__name__)


class FileEditor:
    """Manages file backup, edit, and rollback for hook-runner modules.

    All edits are scoped to a configured base directory (default: ~/.claude/hooks/).
    """

    def __init__(self, base_dir: str = ""):
        self.base_dir = Path(base_dir or DEFAULT_HOOKS_DIR)

    def _resolve(self, target_file: str) -> Path:
        """Resolve target_file to an absolute path within base_dir."""
        path = Path(target_file)
        if not path.is_absolute():
            path = self.base_dir / path
        # Security: ensure we stay within base_dir
        try:
            path.resolve().relative_to(self.base_dir.resolve())
        except ValueError:
            raise ValueError(f"Target file {target_file} is outside {self.base_dir}")
        return path

    def read(self, target_file: str) -> str:
        """Read file contents. Returns empty string if missing."""
        path = self._resolve(target_file)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def backup(self, target_file: str) -> str:
        """Read and return current file contents for backup."""
        return self.read(target_file)

    def write(self, target_file: str, content: str) -> None:
        """Write content to file."""
        path = self._resolve(target_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("Wrote %d bytes to %s", len(content), path)

    def rollback(self, target_file: str, backup_content: str) -> None:
        """Restore file from backup content."""
        self.write(target_file, backup_content)
        logger.info("Rolled back %s", target_file)


class ReflectionMonitor:
    """Monitors reflection tasks, advances backoff, triggers rollback.

    Called each service cycle to check MONITORING tasks against score changes.
    """

    def __init__(self, task_store: ReflectionTaskStore, file_editor: FileEditor,
                 memory=None, score_file: str = ""):
        self.task_store = task_store
        self.file_editor = file_editor
        self.memory = memory
        self.score_file = score_file

    def read_score(self) -> dict:
        """Read the current reflection-score.json."""
        return read_score_file(self.score_file)

    def check_monitoring_tasks(self) -> list:
        """Check all MONITORING tasks that are due for a checkpoint.

        Returns list of (task, action) tuples where action is:
        - "advancing": score positive, moving to next backoff interval
        - "verified": passed final checkpoint, ready for VERIFIED state
        - "rolled_back": score dropped or prediction mismatch, rolled back
        - "failed": max attempts exceeded, task abandoned
        """
        due_tasks = self.task_store.list_due_for_check()
        results = []

        for task in due_tasks:
            action = self._evaluate_checkpoint(task)
            results.append((task, action))

        return results

    def _evaluate_checkpoint(self, task: ReflectionTask) -> str:
        """Evaluate a single checkpoint for a monitoring task."""
        score_data = self.read_score()
        current_points = score_data.get("points", 0)
        score_delta = current_points - task.score_baseline

        # Compute prediction accuracy
        pred_accuracy = 0.0
        if task.prediction:
            pred_accuracy = compute_prediction_accuracy(
                task.prediction,
                actual_score_delta=score_delta,
            )

        # Decision: score dropped or prediction mismatch?
        score_dropped = current_points < task.score_baseline
        prediction_mismatch = pred_accuracy < 0.5

        if score_dropped or prediction_mismatch:
            reason = "score_dropped" if score_dropped else "prediction_mismatch"
            task.add_checkpoint(current_points, pred_accuracy, "rolled_back",
                                f"{reason}: delta={score_delta}, accuracy={pred_accuracy:.2f}")
            self._rollback_task(task)

            if task.exceeds_max_attempts():
                task.transition(TaskState.ROLLED_BACK)
                task.state = TaskState.CLOSED  # Force close — max attempts exceeded
                task.closed_at = time.time()
                self.task_store.save(task)
                self._record_prediction_accuracy(task, pred_accuracy)
                return "failed"

            task.transition(TaskState.ROLLED_BACK)
            task.reset_for_retry()
            task.transition(TaskState.ANALYZING)
            self.task_store.save(task)
            self._record_prediction_accuracy(task, pred_accuracy)
            return "rolled_back"

        # Score positive and prediction accurate enough
        task.add_checkpoint(current_points, pred_accuracy, "advancing",
                            f"delta={score_delta}, accuracy={pred_accuracy:.2f}")

        if task.is_final_check:
            task.transition(TaskState.VERIFIED)
            task.closed_at = time.time()
            self.task_store.save(task)
            self._record_prediction_accuracy(task, pred_accuracy)
            return "verified"

        task.advance_backoff()
        self.task_store.save(task)
        return "advancing"

    def _rollback_task(self, task: ReflectionTask) -> None:
        """Roll back a task's file change from backup."""
        if task.backup_content and task.target_file:
            try:
                self.file_editor.rollback(task.target_file, task.backup_content)
                logger.info("Rolled back %s for task %s", task.target_file, task.task_id)
            except Exception as e:
                logger.error("Rollback failed for task %s: %s", task.task_id, e)

    def _record_prediction_accuracy(self, task: ReflectionTask, accuracy: float) -> None:
        """Record prediction accuracy in Tier 2 memory for per-module calibration."""
        if not self.memory or not task.target_file:
            return

        module_name = Path(task.target_file).stem
        project_key = f"hook-runner/{module_name}"

        try:
            existing = self.memory.get_project_memory(project_key)
            history = existing.get("prediction_history", [])
        except Exception:
            history = []

        history.append({
            "task_id": task.task_id,
            "accuracy": round(accuracy, 3),
            "timestamp": time.time(),
            "prediction": task.prediction.to_dict() if task.prediction else None,
            "outcome": task.monitor_checkpoints[-1].result if task.monitor_checkpoints else "unknown",
        })

        # Keep last 20 predictions per module
        history = history[-20:]

        # Compute rolling accuracy
        accuracies = [h["accuracy"] for h in history]
        rolling_avg = sum(accuracies) / len(accuracies) if accuracies else 0.0

        self.memory.set_project_memory(project_key, "prediction_history", history)
        self.memory.set_project_memory(project_key, "rolling_accuracy", round(rolling_avg, 3))

    def implement_task(self, task: ReflectionTask, new_content: str,
                       prediction: Prediction) -> None:
        """Apply a fix to a hook-runner module.

        1. Backup current file content
        2. Record prediction
        3. Write the new content
        4. Transition to MONITORING
        5. Save to store
        """
        if not task.target_file:
            raise ValueError("Task has no target_file")

        # Backup
        task.backup_content = self.file_editor.backup(task.target_file)

        # Record prediction and score baseline
        task.prediction = prediction
        score_data = self.read_score()
        task.score_baseline = score_data.get("points", 0)

        # Write the fix
        self.file_editor.write(task.target_file, new_content)
        task.implemented_at = time.time()

        # Transition
        if task.state == TaskState.ANALYZING:
            task.transition(TaskState.IMPLEMENTING)
        task.transition(TaskState.MONITORING)

        self.task_store.save(task)
        logger.info("Implemented task %s on %s (baseline score: %s)",
                     task.task_id, task.target_file, task.score_baseline)


def build_reflection_context(task_store: ReflectionTaskStore,
                             memory=None) -> dict:
    """Build reflection context for the brain prompt.

    Returns a dict with:
    - active_tasks: list of active reflection tasks (summary)
    - module_calibration: per-module prediction accuracy from Tier 2 memory
    - recent_outcomes: last 5 task outcomes
    """
    context = {
        "active_tasks": [],
        "module_calibration": {},
        "recent_outcomes": [],
    }

    # Active tasks
    active = task_store.list_active()
    for t in active[:10]:
        context["active_tasks"].append({
            "task_id": t.task_id,
            "state": t.state.value,
            "diagnosis": t.diagnosis[:200] if t.diagnosis else "",
            "target_file": t.target_file,
            "attempts": t.attempts,
            "backoff_index": t.backoff_index,
        })

    # Recently closed tasks
    try:
        rows = task_store.conn.execute(
            """SELECT * FROM reflection_tasks
               WHERE state IN ('closed', 'verified')
               ORDER BY closed_at DESC LIMIT 5"""
        ).fetchall()
        for row in rows:
            t = task_store._row_to_task(row)
            last_acc = (t.monitor_checkpoints[-1].prediction_accuracy
                        if t.monitor_checkpoints else 0.0)
            context["recent_outcomes"].append({
                "task_id": t.task_id,
                "state": t.state.value,
                "diagnosis": t.diagnosis[:100] if t.diagnosis else "",
                "attempts": t.attempts,
                "prediction_accuracy": round(last_acc, 2),
            })
    except Exception:
        pass

    # Module calibration from Tier 2 memory
    if memory:
        try:
            rows = memory.conn.execute(
                "SELECT project, key, value FROM memory_project WHERE key = 'rolling_accuracy'"
            ).fetchall()
            for row in rows:
                project = row[0]
                if project.startswith("hook-runner/"):
                    module_name = project.split("/", 1)[1]
                    try:
                        context["module_calibration"][module_name] = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception:
            pass

    return context


def enrich_prompt_with_reflection(prompt_parts: list, reflection_context: dict) -> None:
    """Append reflection context to brain prompt parts list.

    Modifies prompt_parts in place.
    """
    active = reflection_context.get("active_tasks", [])
    calibration = reflection_context.get("module_calibration", {})
    outcomes = reflection_context.get("recent_outcomes", [])

    if not active and not calibration and not outcomes:
        return

    prompt_parts.append("")
    prompt_parts.append("## Self-Reflection Status")

    if active:
        prompt_parts.append(f"Active tasks: {len(active)}")
        for t in active[:5]:
            prompt_parts.append(
                f"- [{t['state']}] {t['diagnosis'][:80]} "
                f"(target: {t['target_file']}, attempts: {t['attempts']})"
            )

    if calibration:
        prompt_parts.append("")
        prompt_parts.append("Module prediction accuracy:")
        for module, acc in sorted(calibration.items()):
            prompt_parts.append(f"- {module}: {acc:.0%}")

    if outcomes:
        prompt_parts.append("")
        prompt_parts.append("Recent outcomes:")
        for o in outcomes:
            prompt_parts.append(
                f"- {o['task_id']}: {o['state']} "
                f"(accuracy: {o['prediction_accuracy']:.0%}, attempts: {o['attempts']})"
            )
