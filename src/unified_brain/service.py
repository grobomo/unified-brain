"""Brain service — main loop that runs adapters, analyzes events, dispatches actions.

Single process, single DB. Polls all channel adapters on a configurable interval,
feeds events into the brain analyzer, and dispatches resulting actions.
"""

import asyncio
import logging
import time

from .store import EventStore
from .brain import BrainAnalyzer, RESPOND
from .context import ContextBuilder
from .dispatcher import ActionDispatcher
from .feedback import FeedbackStore
from .memory import MemoryManager
from .metrics import (
    events_ingested, events_processed, brain_decisions,
    dispatch_results, respond_results, cycle_duration, cycle_count,
    adapter_errors, brain_errors,
)
from .registry import ProjectRegistry
from .ccc_bridge import CCCBridge
from .focus_steal import handle_focus_steal
from .idle_loop import IdleLoop
from .loop_analyzer import LoopAnalyzer
from .reflection import ReflectionTaskStore

log = logging.getLogger("unified-brain")


class BrainService:
    """Main service loop."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.store = EventStore(self.config.get("db_path", "data/brain.db"))
        self.brain = BrainAnalyzer(self.config.get("brain", {}))
        self.dispatcher = ActionDispatcher(self.config.get("dispatcher", {}))
        self.registry = ProjectRegistry(self.config.get("registry_path", "config/projects.yaml"))
        self.memory = MemoryManager(self.store.conn, self.config.get("memory", {}))
        self.feedback = FeedbackStore(self.store.conn)
        self.reflection_store = ReflectionTaskStore(self.store.conn)
        self.reflection_monitor = None  # Set by runner if reflection is enabled
        self.loop_analyzer = LoopAnalyzer(
            memory=self.memory,
            hook_log_path=self.config.get("hook_log_path", ""),
        )
        ccc_config = self.config.get("ccc_bridge", {})
        self.ccc_bridge = CCCBridge(ccc_config) if ccc_config.get("enabled") else None
        self.context_builder = ContextBuilder(
            self.store, self.registry, self.config.get("context", {}),
            memory=self.memory, feedback=self.feedback,
        )
        self.idle_loop: IdleLoop | None = None  # Set by runner via create_idle_loop()
        self.adapters = []
        self.interval = self.config.get("interval", 60)
        self._cycle_count = 0
        self._compact_interval = self.config.get("compact_every_n_cycles", 10)
        self.batch_size = self.config.get("batch_size", 100)
        self.running = False

    def add_adapter(self, adapter):
        """Register a channel adapter."""
        self.adapters.append(adapter)

    async def run_cycle(self):
        """Run one poll-analyze-dispatch cycle."""
        t0 = time.monotonic()

        # 1. Poll all adapters for new events
        for adapter in self.adapters:
            try:
                events = await adapter.poll()
                for event in events:
                    self.store.insert(event)
                if events:
                    events_ingested.inc(len(events), adapter=adapter.name)
                    log.info(f"[{adapter.name}] Ingested {len(events)} events")

                    # Check hook-runner reflection events for loop patterns
                    for event in events:
                        if (event.get("source") == "hook-runner"
                                and event.get("channel") == "self-reflection"):
                            try:
                                patterns = self.loop_analyzer.process_reflection_event(event)
                                for p in patterns:
                                    log.info("[loop] %s -> fix: %s",
                                             p.root_cause[:60], p.suggested_fix[:60])
                            except Exception as e:
                                log.error("[loop] Analysis error: %s", e)

                    # Route system-monitor focus-steal events
                    for event in events:
                        if (event.get("source") == "system-monitor"
                                and event.get("event_type") == "focus_steal"):
                            try:
                                action = handle_focus_steal(
                                    event, memory=self.memory,
                                    config=self.config.get("focus_steal", {}),
                                )
                                if action and action.get("action") == "dispatch":
                                    self.dispatcher.dispatch(action)
                                    log.info("[focus] Dispatched fix for %s",
                                             event.get("metadata", {}).get("source_project", "?"))
                                elif action:
                                    log.info("[focus] %s", action.get("content", "")[:100])
                            except Exception as e:
                                log.error("[focus] Error handling focus-steal: %s", e)
            except Exception as e:
                adapter_errors.inc(adapter=adapter.name)
                log.error(f"[{adapter.name}] Poll error: {e}")

        # 2. Process unprocessed events through brain
        events = self.store.get_unprocessed(limit=self.batch_size)
        for event in events:
            try:
                # Build cross-channel context (same project, same author)
                context = self.context_builder.build(event)
                # Enrich with loop pattern history for hook-runner events
                loop_ctx = self.loop_analyzer.get_context_for_prompt()
                if loop_ctx:
                    context["loop_context"] = loop_ctx
                action = self.brain.analyze(event, context)
                result = self.dispatcher.dispatch(action)
                self.store.mark_processed(event["id"])
                events_processed.inc(source=event.get("source", "unknown"))
                brain_decisions.inc(action=action.get("action", "log"))

                # Track dispatch outcomes
                status = result.get("status", "") if isinstance(result, dict) else ""
                if action.get("action") == "dispatch":
                    outcome = "success" if status == "dispatched" else "failure"
                    dispatch_results.inc(outcome=outcome)
                elif action.get("action") == "respond":
                    if status == "executed":
                        respond_results.inc(outcome="success", source=event.get("source", ""))
                        self.feedback.record(
                            task_id=event.get("id", ""),
                            action="respond",
                            success=True,
                            source=event.get("source", ""),
                            channel=event.get("channel", ""),
                        )
                    elif status in ("queued", "error"):
                        self.feedback.record(
                            task_id=event.get("id", ""),
                            action="respond",
                            success=status == "queued",
                            source=event.get("source", ""),
                            channel=event.get("channel", ""),
                            error=result.get("error", "") if status == "error" else "",
                        )

                log.info(f"[brain] {event['id']} -> {action.get('action')}")
            except Exception as e:
                brain_errors.inc()
                log.error(f"[brain] Error processing {event.get('id')}: {e}")

        # 3. Check for results from ccc-manager and relay to originating channels
        results = self.dispatcher.poll_results()
        for result in results:
            success = result.get("success", False)
            task_id = result.get("id", "?")
            log.info(f"[result] Task {task_id}: {'success' if success else 'failed'}")
            self._relay_result(result)

        # 3b. Check CCC bridge for completed worker tasks
        if self.ccc_bridge:
            try:
                ccc_results = self.ccc_bridge.poll_results()
                for result in ccc_results:
                    task_id = result.get("id", "?")
                    success = result.get("success", False)
                    log.info(f"[ccc] Task {task_id}: {'success' if success else 'failed'}")

                    # Record feedback for prediction/outcome tracking
                    self.feedback.record(
                        task_id=task_id,
                        action="ccc_work",
                        success=success,
                        source="ccc",
                        channel=result.get("worker", "unknown"),
                        error=result.get("error", "") if not success else "",
                    )

                    # Re-dispatch on failure (up to max retries via feedback count)
                    if not success:
                        stats = self.feedback.summary(hours=24)
                        ccc_stats = stats.get("ccc_work", {})
                        total = ccc_stats.get("total", 0)
                        failed = ccc_stats.get("failed", 0)
                        failure_rate = failed / total if total > 0 else 0
                        if failure_rate < 0.5:  # Only retry if not chronic failure
                            log.info(f"[ccc] Re-dispatching failed task {task_id}")
                            self.ccc_bridge.dispatch({
                                "id": f"{task_id}-retry",
                                "content": f"Retry: {result.get('error', 'unknown error')}",
                                "metadata": {"retry_of": task_id},
                            })
                        else:
                            log.warning(f"[ccc] High failure rate ({failure_rate:.0%}), "
                                        f"not retrying {task_id}")

                    # Relay result to originating channel if available
                    self._relay_result(result)
            except Exception as e:
                log.error(f"[ccc] Result poll error: {e}")

        # 4. Check reflection monitoring tasks (closed-loop self-improvement)
        if self.reflection_monitor:
            try:
                results = self.reflection_monitor.check_monitoring_tasks()
                for task, action in results:
                    log.info("[reflection] Task %s: %s (attempt %d)",
                             task.task_id, action, task.attempts)
            except Exception as e:
                log.error("[reflection] Monitor error: %s", e)

        # 5. Periodic memory compaction
        self._cycle_count += 1
        if self._cycle_count % self._compact_interval == 0:
            try:
                self.memory.compact_tier2(self.store, self.registry)
                self.memory.compact_tier3(self.registry)
                log.debug("[memory] Compaction complete")
            except Exception as e:
                log.error(f"[memory] Compaction error: {e}")

        # 6. Run idle loop tasks (memory, CCC check, reflection, insights)
        if self.idle_loop:
            try:
                self.idle_loop.tick()
            except Exception as e:
                log.error(f"[idle] Tick error: {e}")

        elapsed = time.monotonic() - t0
        cycle_duration.set(elapsed)
        cycle_count.inc()

    async def start(self):
        """Start the service loop."""
        self.running = True
        log.info(f"Starting with {len(self.adapters)} adapters, interval={self.interval}s")

        for adapter in self.adapters:
            await adapter.start()

        while self.running:
            await self.run_cycle()
            await asyncio.sleep(self.interval)

    def _relay_result(self, result: dict):
        """Relay a ccc-manager result back to the originating channel."""
        ctx = result.get("channel_context", {})
        source = ctx.get("source")
        channel = ctx.get("channel")

        # Record feedback
        success = result.get("success", False)
        self.feedback.record(
            task_id=result.get("id", ""),
            action="dispatch",
            success=success,
            source=source or "",
            channel=channel or "",
            error=result.get("error", "") if not success else "",
        )

        if not source or not channel:
            log.debug(f"[relay] No channel_context in result {result.get('id')}, skipping relay")
            return

        output = result.get("output", "")
        pr_url = result.get("pr_url", "")

        if success:
            summary = f"Task completed: {output[:200]}"
            if pr_url:
                summary += f"\nPR: {pr_url}"
        else:
            error = result.get("error", "unknown error")
            summary = f"Task failed: {error[:200]}"

        # Create a RESPOND action to post the summary
        relay_action = {
            "action": RESPOND,
            "source": source,
            "channel": channel,
            "event_id": ctx.get("event_id"),
            "content": summary,
            "metadata": {"result_id": result.get("id")},
        }

        try:
            self.dispatcher.dispatch(relay_action)
            log.info(f"[relay] Result {result.get('id')} relayed to {source}:{channel}")
        except Exception as e:
            log.error(f"[relay] Failed to relay result {result.get('id')}: {e}")

    async def stop(self):
        """Stop the service loop."""
        self.running = False
        for adapter in self.adapters:
            await adapter.stop()
        self.store.close()
        log.info("Stopped")
