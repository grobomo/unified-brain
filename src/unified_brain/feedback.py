"""Feedback loop — tracks dispatch outcomes to improve brain decisions.

Records success/failure of dispatched tasks and active responds.
Provides summary stats that the brain prompt uses to learn patterns:
- Which event types lead to successful dispatches
- Which channels have high failure rates
- Recent outcome history
"""

import json
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


class FeedbackStore:
    """Stores and queries dispatch/respond outcomes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                source TEXT,
                channel TEXT,
                event_type TEXT,
                success INTEGER NOT NULL,
                error TEXT,
                duration_ms INTEGER,
                created_at REAL NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_created
            ON feedback (created_at)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_action
            ON feedback (action, success)
        """)
        self.conn.commit()

    def record(self, task_id: str, action: str, success: bool,
               source: str = "", channel: str = "", event_type: str = "",
               error: str = "", duration_ms: int = 0):
        """Record an outcome."""
        try:
            self.conn.execute(
                """INSERT OR REPLACE INTO feedback
                   (id, action, source, channel, event_type, success, error, duration_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, action, source, channel, event_type,
                 1 if success else 0, error, duration_ms, time.time()),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to record feedback for {task_id}: {e}")

    def summary(self, hours: int = 24) -> dict:
        """Get outcome summary for the last N hours.

        Returns:
            Dict with action-level stats:
            {
                "dispatch": {"total": 10, "success": 8, "failure": 2, "rate": 0.8},
                "respond": {"total": 5, "success": 5, "failure": 0, "rate": 1.0},
                "recent_failures": [{"id": ..., "error": ..., "channel": ...}, ...],
            }
        """
        cutoff = time.time() - (hours * 3600)
        stats = {}

        # Per-action success/failure counts
        rows = self.conn.execute(
            """SELECT action, success, COUNT(*) as cnt
               FROM feedback WHERE created_at > ?
               GROUP BY action, success""",
            (cutoff,),
        ).fetchall()

        for action, success, count in rows:
            if action not in stats:
                stats[action] = {"total": 0, "success": 0, "failure": 0, "rate": 0.0}
            stats[action]["total"] += count
            if success:
                stats[action]["success"] += count
            else:
                stats[action]["failure"] += count

        # Compute rates
        for action in stats:
            total = stats[action]["total"]
            if total > 0:
                stats[action]["rate"] = round(stats[action]["success"] / total, 2)

        # Recent failures (last 5)
        failures = self.conn.execute(
            """SELECT id, action, source, channel, error, created_at
               FROM feedback WHERE created_at > ? AND success = 0
               ORDER BY created_at DESC LIMIT 5""",
            (cutoff,),
        ).fetchall()

        stats["recent_failures"] = [
            {"id": r[0], "action": r[1], "source": r[2],
             "channel": r[3], "error": r[4][:100]}
            for r in failures
        ]

        return stats

    def channel_stats(self, hours: int = 24) -> list[dict]:
        """Per-channel success rates for the last N hours."""
        cutoff = time.time() - (hours * 3600)
        rows = self.conn.execute(
            """SELECT source, channel, success, COUNT(*) as cnt
               FROM feedback WHERE created_at > ?
               GROUP BY source, channel, success
               ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()

        channels = {}
        for source, channel, success, count in rows:
            key = f"{source}:{channel}"
            if key not in channels:
                channels[key] = {"source": source, "channel": channel, "total": 0, "success": 0}
            channels[key]["total"] += count
            if success:
                channels[key]["success"] += count

        result = list(channels.values())
        for ch in result:
            ch["rate"] = round(ch["success"] / ch["total"], 2) if ch["total"] else 0.0
        return result
