"""Integration test — verifies the full pipeline:
event → store → brain → dispatch → result → relay

Uses mock adapters (no real GitHub/Teams/claude dependencies).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unified_brain.store import EventStore
from unified_brain.brain import BrainAnalyzer, DISPATCH, LOG
from unified_brain.dispatcher import ActionDispatcher
from unified_brain.registry import ProjectRegistry
from unified_brain.service import BrainService
from unified_brain.adapters.base import ChannelAdapter


class MockAdapter(ChannelAdapter):
    """Test adapter that yields pre-configured events."""

    def __init__(self, events=None):
        super().__init__("mock", {})
        self._events = events or []
        self._poll_count = 0

    @property
    def source(self):
        return "mock"

    async def poll(self):
        # Return events only on first poll
        if self._poll_count == 0:
            self._poll_count += 1
            return self._events
        return []


class TestEventStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = EventStore(self.db_path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir)

    def test_insert_and_retrieve(self):
        event = {
            "id": "test-1",
            "source": "github",
            "channel": "grobomo/test-repo",
            "event_type": "issue_opened",
            "author": "alice",
            "title": "Bug report",
            "body": "Something is broken",
            "created_at": time.time(),
        }
        self.store.insert(event)
        unprocessed = self.store.get_unprocessed()
        self.assertEqual(len(unprocessed), 1)
        self.assertEqual(unprocessed[0]["id"], "test-1")

    def test_mark_processed(self):
        self.store.insert({
            "id": "test-2",
            "source": "teams",
            "channel": "chat-123",
            "event_type": "message",
            "created_at": time.time(),
        })
        self.store.mark_processed("test-2")
        self.assertEqual(len(self.store.get_unprocessed()), 0)

    def test_search(self):
        self.store.insert({
            "id": "test-3",
            "source": "github",
            "channel": "grobomo/repo",
            "event_type": "issue",
            "title": "Flaky test failure",
            "body": "The pipeline test keeps timing out",
            "created_at": time.time(),
        })
        results = self.store.search("flaky test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "test-3")

    def test_recent_with_filter(self):
        self.store.insert({
            "id": "gh-1",
            "source": "github",
            "channel": "grobomo/repo",
            "event_type": "push",
            "created_at": time.time(),
        })
        self.store.insert({
            "id": "teams-1",
            "source": "teams",
            "channel": "chat-456",
            "event_type": "message",
            "created_at": time.time(),
        })
        gh_events = self.store.recent(hours=1, source="github")
        self.assertEqual(len(gh_events), 1)
        self.assertEqual(gh_events[0]["source"], "github")

    def test_dedup(self):
        event = {
            "id": "dup-1",
            "source": "github",
            "channel": "repo",
            "event_type": "issue",
            "created_at": time.time(),
        }
        self.store.insert(event)
        self.store.insert(event)  # INSERT OR IGNORE
        self.assertEqual(len(self.store.get_unprocessed()), 1)


class TestBrainAnalyzer(unittest.TestCase):
    def test_fallback_dispatch_on_bug(self):
        brain = BrainAnalyzer({"claude_path": "nonexistent-binary"})
        event = {
            "source": "github",
            "channel": "grobomo/repo",
            "event_type": "issue",
            "title": "Bug in parser",
            "body": "The parser throws an error on empty input",
        }
        action = brain.analyze(event)
        self.assertEqual(action["action"], DISPATCH)

    def test_fallback_log_on_normal(self):
        brain = BrainAnalyzer({"claude_path": "nonexistent-binary"})
        event = {
            "source": "github",
            "channel": "grobomo/repo",
            "event_type": "push",
            "title": "Updated README",
            "body": "Minor formatting changes",
        }
        action = brain.analyze(event)
        self.assertEqual(action["action"], LOG)


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmpdir, "outbox")
        self.inbox = os.path.join(self.tmpdir, "inbox")
        self.dispatcher = ActionDispatcher({
            "bridge_dir": self.outbox,
            "results_dir": self.inbox,
        })

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_dispatch_writes_bridge_json(self):
        action = {
            "action": "dispatch",
            "source": "github",
            "channel": "grobomo/repo",
            "event_id": "test-evt-1",
            "content": "Fix the flaky test",
            "metadata": {"priority": "high"},
        }
        result = self.dispatcher._dispatch_to_manager(action)
        self.assertEqual(result["status"], "dispatched")

        # Verify JSON file exists and is valid
        files = list(self.dispatcher.bridge_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        task = json.loads(files[0].read_text())
        self.assertEqual(task["summary"], "Fix the flaky test")
        self.assertEqual(task["text"], "Fix the flaky test")  # BridgeInput compat
        self.assertEqual(task["priority"], "high")
        self.assertIn("channel_context", task)

    def test_poll_results(self):
        os.makedirs(self.inbox, exist_ok=True)
        result = {
            "id": "brain-123",
            "success": True,
            "output": "Fixed it",
            "channel_context": {"source": "github", "channel": "repo"},
        }
        with open(os.path.join(self.inbox, "brain-123.json"), "w") as f:
            json.dump(result, f)

        results = self.dispatcher.poll_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "brain-123")

        # File should be moved to done/
        self.assertEqual(len(list(self.dispatcher.results_dir.glob("*.json"))), 0)
        done_files = list((self.dispatcher.results_dir / "done").glob("*.json"))
        self.assertEqual(len(done_files), 1)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.reg_path = os.path.join(self.tmpdir, "projects.json")
        with open(self.reg_path, "w") as f:
            json.dump({
                "projects": {
                    "hackathon": {
                        "repos": ["grobomo/hackathon26"],
                        "teams_chats": ["19:abc@thread.v2"],
                        "people": ["alice", "bob"],
                    }
                }
            }, f)
        self.registry = ProjectRegistry(self.reg_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_find_by_repo(self):
        proj = self.registry.find_by_repo("grobomo/hackathon26")
        self.assertIsNotNone(proj)
        self.assertEqual(proj["name"], "hackathon")

    def test_find_by_chat(self):
        proj = self.registry.find_by_chat("19:abc@thread.v2")
        self.assertIsNotNone(proj)

    def test_find_by_person(self):
        proj = self.registry.find_by_person("Alice")  # case insensitive
        self.assertIsNotNone(proj)

    def test_find_by_channel(self):
        proj = self.registry.find_by_channel("github", "grobomo/hackathon26")
        self.assertIsNotNone(proj)

    def test_not_found(self):
        self.assertIsNone(self.registry.find_by_repo("unknown/repo"))


class TestServiceIntegration(unittest.TestCase):
    """End-to-end: event → brain → dispatch → result → relay."""

    def test_full_pipeline(self):
        tmpdir = tempfile.mkdtemp()
        try:
            outbox = os.path.join(tmpdir, "outbox")
            inbox = os.path.join(tmpdir, "inbox")
            db_path = os.path.join(tmpdir, "brain.db")

            # 1. Create service with mock adapter
            service = BrainService({
                "db_path": db_path,
                "brain": {"claude_path": "nonexistent"},  # force fallback
                "dispatcher": {"bridge_dir": outbox, "results_dir": inbox},
                "interval": 1,
            })

            # 2. Add mock adapter with a bug report event
            mock_event = {
                "id": "gh:repo:issue:42",
                "source": "github",
                "channel": "grobomo/test-repo",
                "event_type": "issue",
                "author": "bob",
                "title": "Bug in parser",
                "body": "The parser throws an error on empty input",
                "created_at": time.time(),
            }
            service.add_adapter(MockAdapter([mock_event]))

            # 3. Run one cycle — should ingest, analyze (fallback -> DISPATCH), write bridge JSON
            asyncio.run(service.run_cycle())

            # 4. Verify bridge JSON was written
            bridge_files = list(service.dispatcher.bridge_dir.glob("*.json"))
            self.assertEqual(len(bridge_files), 1, "Expected one bridge task file")
            task = json.loads(bridge_files[0].read_text())
            self.assertIn("Investigate", task["summary"])
            self.assertEqual(task["channel_context"]["source"], "github")

            # 5. Simulate ccc-manager writing a result
            os.makedirs(inbox, exist_ok=True)
            result = {
                "id": task["id"],
                "success": True,
                "output": "Fixed parser to handle empty input",
                "pr_url": "https://github.com/grobomo/test-repo/pull/43",
                "channel_context": task["channel_context"],
            }
            with open(os.path.join(inbox, f"{task['id']}.json"), "w") as f:
                json.dump(result, f)

            # 6. Run another cycle — should pick up result and relay
            asyncio.run(service.run_cycle())

            # 7. Result should be moved to done/
            done_files = list((service.dispatcher.results_dir / "done").glob("*.json"))
            self.assertEqual(len(done_files), 1)

            # 8. Event should be marked processed
            self.assertEqual(len(service.store.get_unprocessed()), 0)

        finally:
            service.store.close()
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    unittest.main()
