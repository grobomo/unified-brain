"""ReflectionTask lifecycle manager — closed-loop self-improvement.

State machine for self-reflection tasks:
  PENDING → ANALYZING → IMPLEMENTING → MONITORING → VERIFIED → CLOSED
                ↑                          │
                └── ROLLED_BACK ←──────────┘ (score drop or prediction mismatch)

Each task tracks: diagnosis, proposed fix, target file, backup,
prediction (expected outcome), monitor checkpoints, and attempts.

Prediction accuracy is the primary score driver (70%). The brain must
predict what it expects to see before making a change. If outcome
doesn't match prediction — even if outcome is "good" — score drops
and re-analysis is triggered.
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class TaskState(str, Enum):
    """ReflectionTask lifecycle states."""
    PENDING = "pending"
    ANALYZING = "analyzing"
    IMPLEMENTING = "implementing"
    MONITORING = "monitoring"
    VERIFIED = "verified"
    CLOSED = "closed"
    ROLLED_BACK = "rolled_back"


# Valid state transitions
VALID_TRANSITIONS = {
    TaskState.PENDING: {TaskState.ANALYZING},
    TaskState.ANALYZING: {TaskState.IMPLEMENTING},
    TaskState.IMPLEMENTING: {TaskState.MONITORING},
    TaskState.MONITORING: {TaskState.VERIFIED, TaskState.ROLLED_BACK},
    TaskState.VERIFIED: {TaskState.CLOSED},
    TaskState.ROLLED_BACK: {TaskState.ANALYZING},
    TaskState.CLOSED: set(),
}

# Exponential backoff intervals in seconds: 30s, 1m, 5m, 15m, 30m
BACKOFF_INTERVALS = [30, 60, 300, 900, 1800]

DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class Prediction:
    """Structured prediction of expected outcome before a change."""
    expected_score_delta: float = 0.0
    expected_block_rate_change: float = 0.0
    expected_affected_modules: list = field(default_factory=list)
    confidence: float = 0.5
    reasoning: str = ""
    timeframe_seconds: int = 300

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Prediction":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Checkpoint:
    """A single monitoring checkpoint result."""
    timestamp: float
    score: float
    prediction_accuracy: float
    result: str  # "advancing", "rolled_back", "verified"
    details: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ReflectionTask:
    """A self-reflection improvement task with full lifecycle tracking."""
    task_id: str = ""
    state: TaskState = TaskState.PENDING
    diagnosis: str = ""
    proposed_fix: str = ""
    target_file: str = ""
    backup_content: str = ""
    prediction: Optional[Prediction] = None
    monitor_checkpoints: list = field(default_factory=list)
    backoff_index: int = 0
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    score_baseline: float = 0.0
    created_at: float = 0.0
    implemented_at: float = 0.0
    closed_at: float = 0.0
    source_event_id: str = ""

    def __post_init__(self):
        if not self.task_id:
            self.task_id = f"refl-{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = time.time()
        if isinstance(self.state, str):
            self.state = TaskState(self.state)

    def can_transition(self, new_state: TaskState) -> bool:
        """Check if a state transition is valid."""
        return new_state in VALID_TRANSITIONS.get(self.state, set())

    def transition(self, new_state: TaskState) -> None:
        """Transition to a new state. Raises ValueError if invalid."""
        if not self.can_transition(new_state):
            raise ValueError(
                f"Invalid transition: {self.state.value} → {new_state.value}. "
                f"Valid targets: {[s.value for s in VALID_TRANSITIONS.get(self.state, set())]}"
            )
        old = self.state
        self.state = new_state
        logger.info("Task %s: %s → %s", self.task_id, old.value, new_state.value)

    @property
    def next_check_delay(self) -> int:
        """Seconds until the next monitoring checkpoint."""
        if self.backoff_index >= len(BACKOFF_INTERVALS):
            return BACKOFF_INTERVALS[-1]
        return BACKOFF_INTERVALS[self.backoff_index]

    @property
    def next_check_time(self) -> float:
        """Absolute time of the next monitoring checkpoint."""
        base = self.implemented_at or time.time()
        if self.monitor_checkpoints:
            base = self.monitor_checkpoints[-1].timestamp
        return base + self.next_check_delay

    @property
    def is_due_for_check(self) -> bool:
        """Whether this task is due for its next monitoring check."""
        if self.state != TaskState.MONITORING:
            return False
        return time.time() >= self.next_check_time

    @property
    def is_final_check(self) -> bool:
        """Whether the current backoff index is the last one."""
        return self.backoff_index >= len(BACKOFF_INTERVALS) - 1

    def add_checkpoint(self, score: float, prediction_accuracy: float,
                       result: str, details: str = "") -> Checkpoint:
        """Record a monitoring checkpoint."""
        cp = Checkpoint(
            timestamp=time.time(),
            score=score,
            prediction_accuracy=prediction_accuracy,
            result=result,
            details=details,
        )
        self.monitor_checkpoints.append(cp)
        return cp

    def advance_backoff(self) -> None:
        """Move to the next backoff interval."""
        if self.backoff_index < len(BACKOFF_INTERVALS) - 1:
            self.backoff_index += 1

    def reset_for_retry(self) -> None:
        """Reset monitoring state for a new attempt after rollback."""
        self.attempts += 1
        self.backoff_index = 0
        self.monitor_checkpoints = []
        self.implemented_at = 0.0
        self.prediction = None
        self.backup_content = ""

    def exceeds_max_attempts(self) -> bool:
        """Whether the task has exceeded its retry limit."""
        return self.attempts >= self.max_attempts


def compute_prediction_accuracy(prediction: Prediction, actual_score_delta: float,
                                actual_block_rate_change: float = 0.0) -> float:
    """Compute how accurate a prediction was. Returns 0.0 to 1.0.

    accuracy = 1 - |actual - predicted| / max(|predicted|, 1)
    Combined across score delta and block rate change.
    """
    if prediction is None:
        return 0.0

    # Score delta accuracy
    score_denom = max(abs(prediction.expected_score_delta), 1.0)
    score_acc = max(0.0, 1.0 - abs(actual_score_delta - prediction.expected_score_delta) / score_denom)

    # Block rate accuracy (if predicted)
    if prediction.expected_block_rate_change != 0.0:
        rate_denom = max(abs(prediction.expected_block_rate_change), 0.01)
        rate_acc = max(0.0, 1.0 - abs(actual_block_rate_change - prediction.expected_block_rate_change) / rate_denom)
        return (score_acc + rate_acc) / 2.0

    return score_acc


class ReflectionTaskStore:
    """SQLite persistence for ReflectionTasks. Uses the same connection as EventStore."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS reflection_tasks (
                task_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                diagnosis TEXT,
                proposed_fix TEXT,
                target_file TEXT,
                backup_content TEXT,
                prediction TEXT,
                monitor_checkpoints TEXT,
                backoff_index INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                score_baseline REAL DEFAULT 0.0,
                created_at REAL NOT NULL,
                implemented_at REAL DEFAULT 0.0,
                closed_at REAL DEFAULT 0.0,
                source_event_id TEXT
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rtasks_state
            ON reflection_tasks (state)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rtasks_created
            ON reflection_tasks (created_at)
        """)
        self.conn.commit()

    def save(self, task: ReflectionTask) -> None:
        """Insert or update a reflection task."""
        pred_json = json.dumps(task.prediction.to_dict()) if task.prediction else None
        cp_json = json.dumps([cp.to_dict() for cp in task.monitor_checkpoints])
        self.conn.execute(
            """INSERT OR REPLACE INTO reflection_tasks
               (task_id, state, diagnosis, proposed_fix, target_file,
                backup_content, prediction, monitor_checkpoints,
                backoff_index, attempts, max_attempts, score_baseline,
                created_at, implemented_at, closed_at, source_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.task_id, task.state.value, task.diagnosis, task.proposed_fix,
             task.target_file, task.backup_content, pred_json, cp_json,
             task.backoff_index, task.attempts, task.max_attempts,
             task.score_baseline, task.created_at, task.implemented_at,
             task.closed_at, task.source_event_id),
        )
        self.conn.commit()

    def get(self, task_id: str) -> Optional[ReflectionTask]:
        """Load a task by ID."""
        row = self.conn.execute(
            "SELECT * FROM reflection_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def list_by_state(self, state: TaskState) -> list:
        """Get all tasks in a given state."""
        rows = self.conn.execute(
            "SELECT * FROM reflection_tasks WHERE state = ? ORDER BY created_at",
            (state.value,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_active(self) -> list:
        """Get all non-closed tasks."""
        rows = self.conn.execute(
            """SELECT * FROM reflection_tasks
               WHERE state NOT IN ('closed', 'verified')
               ORDER BY created_at""",
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_monitoring(self) -> list:
        """Get tasks currently in MONITORING state."""
        return self.list_by_state(TaskState.MONITORING)

    def list_due_for_check(self) -> list:
        """Get MONITORING tasks that are past their next checkpoint time."""
        tasks = self.list_monitoring()
        return [t for t in tasks if t.is_due_for_check]

    def count_by_state(self) -> dict:
        """Get count of tasks per state."""
        rows = self.conn.execute(
            "SELECT state, COUNT(*) as cnt FROM reflection_tasks GROUP BY state"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def _row_to_task(self, row) -> ReflectionTask:
        """Convert a DB row to a ReflectionTask."""
        pred = None
        if row["prediction"]:
            pred = Prediction.from_dict(json.loads(row["prediction"]))

        checkpoints = []
        if row["monitor_checkpoints"]:
            for cp_dict in json.loads(row["monitor_checkpoints"]):
                checkpoints.append(Checkpoint.from_dict(cp_dict))

        return ReflectionTask(
            task_id=row["task_id"],
            state=TaskState(row["state"]),
            diagnosis=row["diagnosis"] or "",
            proposed_fix=row["proposed_fix"] or "",
            target_file=row["target_file"] or "",
            backup_content=row["backup_content"] or "",
            prediction=pred,
            monitor_checkpoints=checkpoints,
            backoff_index=row["backoff_index"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            score_baseline=row["score_baseline"],
            created_at=row["created_at"],
            implemented_at=row["implemented_at"],
            closed_at=row["closed_at"],
            source_event_id=row["source_event_id"] or "",
        )
