"""Idle loop — periodic tasks the brain runs between event cycles.

When no new events arrive, the brain stays productive:
1. Compact memory (merge stale Tier 1 → Tier 2 → Tier 3)
2. Check dispatched CCC work (poll bridge for updates)
3. Self-reflection (review recent decisions, calibrate)
4. Proactive insights ("daydream" — surface patterns from memory)

Each task has its own interval so they run at different frequencies.
The idle loop is called once per service cycle; it checks elapsed time
and runs only the tasks that are due.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class IdleTask:
    """A single periodic idle task with its own interval."""

    def __init__(self, name: str, interval_seconds: float, func):
        self.name = name
        self.interval = interval_seconds
        self.func = func
        self.last_run = 0.0
        self.run_count = 0
        self.error_count = 0
        self.last_error = ""

    def is_due(self) -> bool:
        return (time.time() - self.last_run) >= self.interval

    def run(self):
        try:
            self.func()
            self.run_count += 1
            self.last_run = time.time()
            logger.debug(f"[idle] {self.name} completed (run #{self.run_count})")
        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            self.last_run = time.time()  # Still update to avoid rapid retry
            logger.error(f"[idle] {self.name} error: {e}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "interval": self.interval,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "due": self.is_due(),
        }


class IdleLoop:
    """Manages periodic background tasks for the brain.

    Config:
        compact_interval: int — seconds between memory compaction (default 600)
        ccc_check_interval: int — seconds between CCC result checks (default 120)
        reflection_interval: int — seconds between self-reflection (default 1800)
        insight_interval: int — seconds between proactive insights (default 3600)
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._tasks: list[IdleTask] = []

    def add_task(self, name: str, interval_seconds: float, func):
        """Register a periodic idle task."""
        self._tasks.append(IdleTask(name, interval_seconds, func))

    def tick(self):
        """Run all due tasks. Called once per service cycle."""
        for task in self._tasks:
            if task.is_due():
                task.run()

    def status(self) -> list[dict]:
        """Return status of all idle tasks."""
        return [t.to_dict() for t in self._tasks]

    @property
    def task_count(self) -> int:
        return len(self._tasks)


def create_idle_loop(service, config: dict = None) -> IdleLoop:
    """Factory: create an IdleLoop wired to a BrainService's components.

    Registers the standard idle tasks:
    - memory_compact: merge stale events into summaries
    - ccc_check: poll CCC bridge for completed tasks
    - reflection: run self-reflection on recent decisions
    - insight: surface proactive patterns from memory
    """
    config = config or {}
    loop = IdleLoop(config)

    # 1. Memory compaction (default: every 10 minutes)
    compact_interval = config.get("compact_interval", 600)

    def _compact():
        service.memory.compact_tier2(service.store, service.registry)
        service.memory.compact_tier3(service.registry)
        logger.info("[idle] Memory compaction complete")

    loop.add_task("memory_compact", compact_interval, _compact)

    # 2. CCC result check (default: every 2 minutes)
    if service.ccc_bridge:
        ccc_interval = config.get("ccc_check_interval", 120)

        def _ccc_check():
            results = service.ccc_bridge.poll_results()
            for r in results:
                logger.info("[idle] CCC result: %s (%s)",
                            r.get("id", "?"),
                            "ok" if r.get("success") else "fail")
                service._relay_result(r)

        loop.add_task("ccc_check", ccc_interval, _ccc_check)

    # 3. Self-reflection (default: every 30 minutes)
    if service.reflection_monitor:
        reflection_interval = config.get("reflection_interval", 1800)

        def _reflection():
            results = service.reflection_monitor.check_monitoring_tasks()
            for task, action in results:
                logger.info("[idle] Reflection: %s -> %s", task.task_id, action)

        loop.add_task("reflection", reflection_interval, _reflection)

    # 4. Proactive insights (default: every hour)
    insight_interval = config.get("insight_interval", 3600)

    def _insight():
        # Surface interesting patterns from Tier 3 global memory
        global_mem = service.memory.get_global_memory("loop_patterns")
        if global_mem and global_mem.get("total_detected", 0) > 0:
            logger.info("[idle] Loop patterns in memory: %d detected, %d recurring",
                        global_mem.get("total_detected", 0),
                        global_mem.get("recurring_count", 0))

        # Check feedback trends
        stats = service.feedback.summary(hours=24)
        for action, data in stats.items():
            if action == "recent_failures":
                continue
            if isinstance(data, dict) and data.get("failure", 0) > 0:
                rate = data.get("rate", 1.0)
                if rate < 0.8:
                    logger.warning("[idle] Low success rate for %s: %.0f%%",
                                   action, rate * 100)

    loop.add_task("insight", insight_interval, _insight)

    return loop
