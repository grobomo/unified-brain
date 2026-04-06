"""Brain service — main loop that runs adapters, analyzes events, dispatches actions.

Single process, single DB. Polls all channel adapters on a configurable interval,
feeds events into the brain analyzer, and dispatches resulting actions.
"""

import asyncio
import logging
import time

from .store import EventStore
from .brain import BrainAnalyzer
from .dispatcher import ActionDispatcher

log = logging.getLogger("unified-brain")


class BrainService:
    """Main service loop."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.store = EventStore(self.config.get("db_path", "data/brain.db"))
        self.brain = BrainAnalyzer(self.config.get("brain", {}))
        self.dispatcher = ActionDispatcher(self.config.get("dispatcher", {}))
        self.adapters = []
        self.interval = self.config.get("interval", 60)
        self.running = False

    def add_adapter(self, adapter):
        """Register a channel adapter."""
        self.adapters.append(adapter)

    async def run_cycle(self):
        """Run one poll-analyze-dispatch cycle."""
        # 1. Poll all adapters for new events
        for adapter in self.adapters:
            try:
                events = await adapter.poll()
                for event in events:
                    self.store.insert(event)
                if events:
                    log.info(f"[{adapter.name}] Ingested {len(events)} events")
            except Exception as e:
                log.error(f"[{adapter.name}] Poll error: {e}")

        # 2. Process unprocessed events through brain
        events = self.store.get_unprocessed()
        for event in events:
            try:
                # Build context from recent events in same channel
                context = {
                    "recent": self.store.recent(hours=24, channel=event["channel"]),
                }
                action = self.brain.analyze(event, context)
                self.dispatcher.dispatch(action)
                self.store.mark_processed(event["id"])
                log.info(f"[brain] {event['id']} -> {action.get('action')}")
            except Exception as e:
                log.error(f"[brain] Error processing {event.get('id')}: {e}")

        # 3. Check for results from ccc-manager
        results = self.dispatcher.poll_results()
        for result in results:
            log.info(f"[result] Task {result.get('id')}: {'success' if result.get('success') else 'failed'}")
            # TODO: relay result to originating channel via channel_context

    async def start(self):
        """Start the service loop."""
        self.running = True
        log.info(f"Starting with {len(self.adapters)} adapters, interval={self.interval}s")

        for adapter in self.adapters:
            await adapter.start()

        while self.running:
            await self.run_cycle()
            await asyncio.sleep(self.interval)

    async def stop(self):
        """Stop the service loop."""
        self.running = False
        for adapter in self.adapters:
            await adapter.stop()
        self.store.close()
        log.info("Stopped")
