"""Brain-owned score — prediction accuracy + user interrupt rate.

T056: The brain scores itself on understanding, not just outcomes.
Primary driver: prediction accuracy (70%). Secondary: user interrupt rate (30%).

The brain can score itself 24/7 based on prediction accuracy, even when
the user is away. User interrupts are valuable signal but intermittent.
"""

import json
import logging
import time
from collections import deque
from pathlib import Path

from .metrics import registry, Counter, Gauge
from .utils import read_score_file

logger = logging.getLogger(__name__)

# Prometheus metrics for brain score
reflection_predictions = registry.register(
    Counter("brain_reflection_predictions_total", "Total predictions made by outcome")
)
reflection_score_gauge = registry.register(
    Gauge("brain_reflection_score", "Current brain reflection score")
)
reflection_accuracy_gauge = registry.register(
    Gauge("brain_reflection_prediction_accuracy", "Rolling prediction accuracy")
)


class BrainScore:
    """Brain's self-scoring system based on prediction accuracy and user interrupts.

    Score = prediction_accuracy * 0.7 + user_interrupt_score * 0.3

    Prediction accuracy: rolling average over last N predictions (default 10).
    User interrupt rate: 1 - (interrupts_per_hour / baseline), read from
    reflection-score.json "interventions" field.
    """

    PREDICTION_WEIGHT = 0.7
    INTERRUPT_WEIGHT = 0.3

    def __init__(self, data_dir: str = "data", max_predictions: int = 10,
                 score_file: str = ""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.score_path = self.data_dir / "brain-score.json"
        self.max_predictions = max_predictions
        self.score_file = score_file

        # Rolling prediction tracker
        self._predictions: deque = deque(maxlen=max_predictions)
        self._score = 0.0
        self._interrupt_baseline = 5.0  # Expected interrupts/hour
        self._total_predictions = 0

        # Load persisted state
        self._load()

    def _load(self):
        """Load persisted score state from disk."""
        if not self.score_path.exists():
            return
        try:
            data = json.loads(self.score_path.read_text(encoding="utf-8"))
            self._predictions = deque(data.get("predictions", []),
                                      maxlen=self.max_predictions)
            self._score = data.get("score", 0.0)
            self._interrupt_baseline = data.get("interrupt_baseline", 5.0)
            self._total_predictions = data.get("total_predictions", 0)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load brain score: %s", e)

    def save(self):
        """Persist score state to disk."""
        data = {
            "score": round(self._score, 4),
            "prediction_accuracy": round(self.prediction_accuracy, 4),
            "user_interrupt_score": round(self.user_interrupt_score, 4),
            "predictions": list(self._predictions),
            "total_predictions": self._total_predictions,
            "interrupt_baseline": self._interrupt_baseline,
            "updated_at": time.time(),
        }
        try:
            self.score_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error("Failed to save brain score: %s", e)

        # Update Prometheus
        reflection_score_gauge.set(self._score)
        reflection_accuracy_gauge.set(self.prediction_accuracy)

    def record_prediction(self, accuracy: float, task_id: str = "",
                          details: str = ""):
        """Record a prediction outcome.

        Args:
            accuracy: 0.0 to 1.0 — how accurate the prediction was
            task_id: Optional task ID for tracking
            details: Optional description
        """
        entry = {
            "accuracy": round(max(0.0, min(1.0, accuracy)), 4),
            "task_id": task_id,
            "details": details,
            "timestamp": time.time(),
        }
        self._predictions.append(entry)
        self._total_predictions += 1

        # Update Prometheus
        outcome = "match" if accuracy >= 0.7 else "mismatch"
        reflection_predictions.inc(outcome=outcome)

        self._recompute()
        self.save()

    @property
    def prediction_accuracy(self) -> float:
        """Rolling average prediction accuracy (0.0 to 1.0)."""
        if not self._predictions:
            return 0.0
        return sum(p["accuracy"] for p in self._predictions) / len(self._predictions)

    @property
    def user_interrupt_score(self) -> float:
        """Score based on user interrupt rate. 1.0 = no interrupts, 0.0 = too many."""
        score_data = self._read_hook_score()
        interventions = score_data.get("interventions", 0)
        if self._interrupt_baseline <= 0:
            return 1.0
        return max(0.0, 1.0 - (interventions / self._interrupt_baseline))

    @property
    def score(self) -> float:
        """Current composite brain score."""
        return self._score

    @property
    def total_predictions(self) -> int:
        return self._total_predictions

    def _recompute(self):
        """Recompute the composite score."""
        self._score = (
            self.prediction_accuracy * self.PREDICTION_WEIGHT
            + self.user_interrupt_score * self.INTERRUPT_WEIGHT
        )

    def _read_hook_score(self) -> dict:
        """Read hook-runner's reflection-score.json for interrupt data."""
        return read_score_file(self.score_file)

    def to_dict(self) -> dict:
        """Return score breakdown for reporting."""
        return {
            "score": round(self._score, 4),
            "prediction_accuracy": round(self.prediction_accuracy, 4),
            "user_interrupt_score": round(self.user_interrupt_score, 4),
            "total_predictions": self._total_predictions,
            "recent_predictions": list(self._predictions),
        }


def write_reflection_findings(findings_path: str, task_store, brain_score: BrainScore):
    """Write reflection-findings.json for hook-runner consumption.

    Bridge file that hook-runner reads to understand what the brain has found.
    Contains active tasks, recent findings, and score breakdown.
    """
    findings = {
        "updated_at": time.time(),
        "brain_score": brain_score.to_dict(),
        "active_tasks": [],
        "recent_findings": [],
    }

    # Active tasks
    active = task_store.list_active()
    for t in active[:10]:
        findings["active_tasks"].append({
            "task_id": t.task_id,
            "state": t.state.value,
            "diagnosis": t.diagnosis[:300] if t.diagnosis else "",
            "target_file": t.target_file,
            "attempts": t.attempts,
            "created_at": t.created_at,
        })

    # Recently closed tasks (last 10)
    try:
        rows = task_store.conn.execute(
            """SELECT * FROM reflection_tasks
               WHERE state IN ('closed', 'verified')
               ORDER BY closed_at DESC LIMIT 10"""
        ).fetchall()
        for row in rows:
            t = task_store._row_to_task(row)
            last_cp = t.monitor_checkpoints[-1] if t.monitor_checkpoints else None
            findings["recent_findings"].append({
                "task_id": t.task_id,
                "state": t.state.value,
                "diagnosis": t.diagnosis[:300] if t.diagnosis else "",
                "target_file": t.target_file,
                "attempts": t.attempts,
                "prediction_accuracy": round(last_cp.prediction_accuracy, 3) if last_cp else 0.0,
                "closed_at": t.closed_at,
            })
    except Exception:
        pass

    path = Path(findings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(findings, indent=2), encoding="utf-8")
        logger.info("Wrote reflection findings to %s", path)
    except OSError as e:
        logger.error("Failed to write findings: %s", e)
