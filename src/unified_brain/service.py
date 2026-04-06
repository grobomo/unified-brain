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
from .memory import MemoryManager
from .metrics import (
    events_ingested, events_processed, brain_decisions,
    dispatch_results, cycle_duration, cycle_count,
    adapter_errors, brain_errors,
)
from .registry import ProjectRegistry

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
        self.context_builder = ContextBuilder(
            self.store, self.registry, self.config.get("context", {}),
            memory=self.memory,
        )
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
            except Exception as e:
                adapter_errors.inc(adapter=adapter.name)
                log.error(f"[{adapter.name}] Poll error: {e}")

        # 2. Process unprocessed events through brain
        events = self.store.get_unprocessed(limit=self.batch_size)
        for event in events:
            try:
                # Build cross-channel context (same project, same author)
                context = self.context_builder.build(event)
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
                elif action.get("action") == "respond" and status == "executed":
                    from .metrics import respond_results
                    respond_results.inc(outcome="success", source=event.get("source", ""))

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

        # 4. Periodic memory compaction
        self._cycle_count += 1
        if self._cycle_count % self._compact_interval == 0:
            try:
                self.memory.compact_tier2(self.store, self.registry)
                self.memory.compact_tier3(self.registry)
                log.debug("[memory] Compaction complete")
            except Exception as e:
                log.error(f"[memory] Compaction error: {e}")

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

        if not source or not channel:
            log.debug(f"[relay] No channel_context in result {result.get('id')}, skipping relay")
            return

        success = result.get("success", False)
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
