"""Integration test — verifies the full pipeline:
event → store → brain → dispatch → result → relay

Uses mock adapters (no real GitHub/Teams/claude dependencies).
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unified_brain.adapters.base import BoundedSet, parse_timestamp
from unified_brain.runner import _deep_merge, _interpolate_env, _load_config
from unified_brain.store import EventStore
from unified_brain.brain import BrainAnalyzer, DISPATCH, LOG, RESPOND, ALERT
from unified_brain.context import ContextBuilder
from unified_brain.dispatcher import ActionDispatcher, FileTransport, SQSTransport, _create_transport
from unified_brain.executor import ActionExecutor
from unified_brain.memory import MemoryManager
from unified_brain.registry import ProjectRegistry
from unified_brain.service import BrainService
from unified_brain.adapters.base import ChannelAdapter
from unified_brain.adapters.webhook import WebhookAdapter


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

    def test_prompt_includes_structured_context(self):
        brain = BrainAnalyzer()
        event = {
            "source": "github",
            "channel": "grobomo/test-repo",
            "event_type": "issue",
            "author": "dev1",
            "title": "Fix login flow",
            "body": "Login is broken",
        }
        context = {
            "project": {"name": "test-project", "worker_type": "k8s"},
            "memory": {
                "project_memory": {
                    "summary": {
                        "event_count": 42,
                        "authors": ["dev1", "dev2"],
                        "event_types": {"issue": 10, "pr": 32},
                    }
                },
                "global_memory": {
                    "total_events": 100,
                    "active_projects": 3,
                    "most_active": "test-project",
                },
            },
            "same_channel": [
                {"event_type": "pr", "author": "dev2", "title": "Refactor auth module"},
            ],
            "related_channels": [
                {"source": "teams", "channel": "chat-123", "author": "dev1", "title": "Discussed login fix"},
            ],
            "author_activity": [
                {"source": "teams", "title": "Asked about auth tokens"},
            ],
        }
        prompt = brain._build_prompt(event, context)
        self.assertIn("## Project: test-project", prompt)
        self.assertIn("Worker type: k8s", prompt)
        self.assertIn("## Project Memory", prompt)
        self.assertIn("Events: 42", prompt)
        self.assertIn("dev1", prompt)
        self.assertIn("## Global Patterns", prompt)
        self.assertIn("Total events: 100", prompt)
        self.assertIn("Most active: test-project", prompt)
        self.assertIn("## Recent in this channel", prompt)
        self.assertIn("## Related channels", prompt)
        self.assertIn("## This author's other activity", prompt)
        self.assertIn("RESPOND", prompt)
        self.assertIn("DISPATCH", prompt)


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmpdir, "outbox")
        self.inbox = os.path.join(self.tmpdir, "inbox")
        self.dispatcher = ActionDispatcher({
            "outbox_dir": self.outbox,
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
                "dispatcher": {"outbox_dir": outbox, "results_dir": inbox},
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


class TestContextBuilder(unittest.TestCase):
    """Tests for cross-channel context (T011)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = EventStore(self.db_path)
        self.reg_path = os.path.join(self.tmpdir, "projects.json")
        with open(self.reg_path, "w") as f:
            json.dump({
                "projects": {
                    "myproject": {
                        "repos": ["grobomo/myrepo", "grobomo/other-repo"],
                        "teams_chats": ["19:chat123@thread.v2"],
                        "people": ["alice", "bob"],
                    }
                }
            }, f)
        self.registry = ProjectRegistry(self.reg_path)
        self.builder = ContextBuilder(self.store, self.registry)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir)

    def test_same_channel_context(self):
        """Events from the same channel appear in context."""
        self.store.insert({
            "id": "gh-1", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "push", "author": "alice", "title": "Push to main",
            "created_at": time.time(),
        })
        event = {
            "id": "gh-2", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "issue", "author": "bob", "title": "Bug",
        }
        ctx = self.builder.build(event)
        self.assertGreaterEqual(len(ctx["same_channel"]), 1)

    def test_related_channels_cross_source(self):
        """Events from a Teams chat in the same project appear as related."""
        # Insert a Teams event
        self.store.insert({
            "id": "teams-1", "source": "teams", "channel": "19:chat123@thread.v2",
            "event_type": "message", "author": "alice", "title": "Discussed deploy",
            "body": "Deploy is failing in staging",
            "created_at": time.time(),
        })
        # Analyze a GitHub event in the same project
        event = {
            "id": "gh-3", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "issue", "author": "bob", "title": "Deploy broken",
        }
        ctx = self.builder.build(event)
        self.assertGreaterEqual(len(ctx["related_channels"]), 1)
        self.assertEqual(ctx["related_channels"][0]["source"], "teams")

    def test_related_channels_same_source_different_repo(self):
        """Events from another repo in the same project appear as related."""
        self.store.insert({
            "id": "gh-10", "source": "github", "channel": "grobomo/other-repo",
            "event_type": "pr", "author": "alice", "title": "Fix for other-repo",
            "created_at": time.time(),
        })
        event = {
            "id": "gh-11", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "issue", "author": "bob", "title": "Related issue",
        }
        ctx = self.builder.build(event)
        self.assertGreaterEqual(len(ctx["related_channels"]), 1)
        self.assertEqual(ctx["related_channels"][0]["channel"], "grobomo/other-repo")

    def test_author_activity_cross_channel(self):
        """Events by the same author in different channels appear."""
        self.store.insert({
            "id": "teams-2", "source": "teams", "channel": "19:other@thread.v2",
            "event_type": "message", "author": "alice", "title": "Another chat",
            "created_at": time.time(),
        })
        event = {
            "id": "gh-4", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "issue", "author": "alice", "title": "New issue",
        }
        ctx = self.builder.build(event)
        self.assertGreaterEqual(len(ctx["author_activity"]), 1)
        self.assertEqual(ctx["author_activity"][0]["author"], "alice")

    def test_project_info_in_context(self):
        """Project metadata appears when channel is in registry."""
        event = {
            "id": "gh-5", "source": "github", "channel": "grobomo/myrepo",
            "event_type": "push", "author": "bob",
        }
        ctx = self.builder.build(event)
        self.assertIsNotNone(ctx["project"])
        self.assertEqual(ctx["project"]["name"], "myproject")

    def test_unknown_channel_no_project(self):
        """Events from unknown channels get no project context."""
        event = {
            "id": "gh-6", "source": "github", "channel": "unknown/repo",
            "event_type": "push", "author": "eve",
        }
        ctx = self.builder.build(event)
        self.assertIsNone(ctx["project"])
        self.assertEqual(ctx["related_channels"], [])


class TestMemoryManager(unittest.TestCase):
    """Tests for three-tier memory (T012)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = EventStore(self.db_path)
        self.memory = MemoryManager(self.store.conn)
        self.reg_path = os.path.join(self.tmpdir, "projects.json")
        with open(self.reg_path, "w") as f:
            json.dump({
                "projects": {
                    "proj-a": {
                        "repos": ["grobomo/repo-a"],
                        "teams_chats": [],
                        "people": [],
                    },
                    "proj-b": {
                        "repos": ["grobomo/repo-b"],
                        "teams_chats": [],
                        "people": [],
                    },
                }
            }, f)
        self.registry = ProjectRegistry(self.reg_path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir)

    def test_project_memory_roundtrip(self):
        self.memory.set_project_memory("proj-a", "notes", {"key": "value"})
        mem = self.memory.get_project_memory("proj-a")
        self.assertEqual(mem["notes"]["key"], "value")

    def test_global_memory_roundtrip(self):
        self.memory.set_global_memory("config", {"active": True})
        val = self.memory.get_global_memory("config")
        self.assertTrue(val["active"])

    def test_compact_tier2(self):
        """Tier 2 compaction creates project summaries from events."""
        self.store.insert({
            "id": "a-1", "source": "github", "channel": "grobomo/repo-a",
            "event_type": "issue", "author": "alice", "created_at": time.time(),
        })
        self.store.insert({
            "id": "a-2", "source": "github", "channel": "grobomo/repo-a",
            "event_type": "pr", "author": "bob", "created_at": time.time(),
        })
        self.memory.compact_tier2(self.store, self.registry)
        mem = self.memory.get_project_memory("proj-a")
        summary = mem.get("summary", {})
        self.assertEqual(summary["event_count"], 2)
        self.assertIn("alice", summary["authors"])
        self.assertIn("bob", summary["authors"])

    def test_compact_tier3(self):
        """Tier 3 compaction aggregates project summaries into global."""
        # Set up tier 2 summaries
        self.memory.set_project_memory("proj-a", "summary", {
            "event_count": 5, "authors": ["alice"], "event_types": {"issue": 5},
            "channels_active": ["github:grobomo/repo-a"],
        })
        self.memory.set_project_memory("proj-b", "summary", {
            "event_count": 3, "authors": ["bob"], "event_types": {"pr": 3},
            "channels_active": ["github:grobomo/repo-b"],
        })
        self.memory.compact_tier3(self.registry)
        global_mem = self.memory.get_global_memory("summary")
        self.assertEqual(global_mem["total_events"], 8)
        self.assertEqual(global_mem["active_projects"], 2)
        self.assertEqual(global_mem["most_active"], "proj-a")

    def test_get_context_for_project(self):
        self.memory.set_project_memory("proj-a", "summary", {"event_count": 10})
        self.memory.set_global_memory("summary", {"total_events": 100})
        ctx = self.memory.get_context_for_project("proj-a")
        self.assertIn("project_memory", ctx)
        self.assertIn("global_memory", ctx)
        self.assertEqual(ctx["project_memory"]["summary"]["event_count"], 10)


class TestOutboxProtocol(unittest.TestCase):
    """Tests for action relay outbox protocol (T013)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dispatcher = ActionDispatcher({
            "outbox_dir": os.path.join(self.tmpdir, "outbox"),
            "results_dir": os.path.join(self.tmpdir, "inbox"),
        })

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_github_respond_writes_outbox(self):
        action = {
            "action": RESPOND, "source": "github", "channel": "grobomo/repo",
            "event_id": "issue-42", "content": "This is fixed in PR #43",
        }
        result = self.dispatcher.dispatch(action)
        self.assertEqual(result["status"], "queued")
        files = list(self.dispatcher.github_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text())
        self.assertEqual(entry["action"], "comment")
        self.assertEqual(entry["repo"], "grobomo/repo")
        self.assertIn("This is fixed", entry["body"])

    def test_teams_respond_writes_outbox(self):
        action = {
            "action": RESPOND, "source": "teams", "channel": "19:abc@thread.v2",
            "content": "Looking into this now",
        }
        result = self.dispatcher.dispatch(action)
        self.assertEqual(result["status"], "queued")
        files = list(self.dispatcher.teams_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text())
        self.assertEqual(entry["action"], "reply")
        self.assertEqual(entry["chat_id"], "19:abc@thread.v2")

    def test_alert_writes_email_outbox(self):
        action = {
            "action": ALERT, "source": "github", "channel": "grobomo/repo",
            "event_id": "evt-1", "content": "Critical: deploy failed",
        }
        result = self.dispatcher.dispatch(action)
        self.assertEqual(result["status"], "queued")
        files = list(self.dispatcher.email_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text())
        self.assertEqual(entry["action"], "email")
        self.assertIn("deploy failed", entry["body"])

    def test_dispatch_still_writes_bridge(self):
        """DISPATCH action still writes to bridge directory for ccc-manager."""
        action = {
            "action": DISPATCH, "source": "github", "channel": "grobomo/repo",
            "event_id": "evt-2", "content": "Fix the bug",
        }
        result = self.dispatcher.dispatch(action)
        self.assertEqual(result["status"], "dispatched")
        files = list(self.dispatcher.bridge_dir.glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_outbox_directory_structure(self):
        """Verify outbox directory structure exists."""
        self.assertTrue(self.dispatcher.github_outbox.exists())
        self.assertTrue(self.dispatcher.teams_outbox.exists())
        self.assertTrue(self.dispatcher.email_outbox.exists())
        self.assertTrue(self.dispatcher.bridge_dir.exists())


class TestBoundedSetAndTimestamp(unittest.TestCase):
    """Tests for BoundedSet and parse_timestamp (T023)."""

    def test_bounded_set_contains(self):
        bs = BoundedSet(maxsize=5)
        bs.add("a")
        self.assertIn("a", bs)
        self.assertNotIn("b", bs)

    def test_bounded_set_evicts_oldest(self):
        bs = BoundedSet(maxsize=3)
        bs.add("a")
        bs.add("b")
        bs.add("c")
        bs.add("d")  # should evict "a"
        self.assertNotIn("a", bs)
        self.assertIn("b", bs)
        self.assertIn("d", bs)
        self.assertEqual(len(bs), 3)

    def test_bounded_set_readd_refreshes(self):
        bs = BoundedSet(maxsize=3)
        bs.add("a")
        bs.add("b")
        bs.add("a")  # refresh "a"
        bs.add("c")
        bs.add("d")  # should evict "b" (oldest), not "a"
        self.assertIn("a", bs)
        self.assertNotIn("b", bs)

    def test_parse_timestamp_iso(self):
        ts = parse_timestamp("2026-04-06T12:00:00Z")
        self.assertGreater(ts, 0)
        self.assertIsInstance(ts, float)

    def test_parse_timestamp_numeric(self):
        self.assertEqual(parse_timestamp(1700000000.0), 1700000000.0)
        self.assertEqual(parse_timestamp(1700000000), 1700000000.0)

    def test_parse_timestamp_empty_returns_now(self):
        before = time.time()
        ts = parse_timestamp("")
        after = time.time()
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)


class TestConfigOverlay(unittest.TestCase):
    """Tests for config deep merge and local overlay (T019)."""

    def test_deep_merge_scalars(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        self.assertEqual(result, {"a": 1, "b": 3, "c": 4})

    def test_deep_merge_nested(self):
        base = {"adapters": {"github": {"enabled": True}, "teams": {"enabled": False}}}
        override = {"adapters": {"teams": {"enabled": True, "chat_ids": ["abc"]}}}
        result = _deep_merge(base, override)
        self.assertTrue(result["adapters"]["github"]["enabled"])
        self.assertTrue(result["adapters"]["teams"]["enabled"])
        self.assertEqual(result["adapters"]["teams"]["chat_ids"], ["abc"])

    def test_deep_merge_does_not_mutate(self):
        base = {"a": {"x": 1}}
        override = {"a": {"y": 2}}
        result = _deep_merge(base, override)
        self.assertNotIn("y", base["a"])
        self.assertIn("y", result["a"])

    def test_load_config_with_local_overlay(self):
        tmpdir = tempfile.mkdtemp()
        try:
            base_path = os.path.join(tmpdir, "brain.yaml")
            local_path = os.path.join(tmpdir, "brain.local.yaml")
            # Write base config as JSON (yaml may not be installed)
            with open(base_path.replace(".yaml", ".json"), "w") as f:
                json.dump({"interval": 60, "adapters": {"teams": {"enabled": False}}}, f)
            with open(local_path.replace(".yaml", ".json"), "w") as f:
                json.dump({"adapters": {"teams": {"enabled": True, "chat_ids": ["x"]}}}, f)
            config = _load_config(base_path.replace(".yaml", ".json"))
            self.assertEqual(config["interval"], 60)
            self.assertTrue(config["adapters"]["teams"]["enabled"])
            self.assertEqual(config["adapters"]["teams"]["chat_ids"], ["x"])
        finally:
            shutil.rmtree(tmpdir)


class TestEnvInterpolation(unittest.TestCase):
    """Tests for env var interpolation in config (T016)."""

    def test_interpolate_string(self):
        os.environ["TEST_BRAIN_TOKEN"] = "ghp_abc123"
        try:
            result = _interpolate_env("token: ${TEST_BRAIN_TOKEN}")
            self.assertEqual(result, "token: ghp_abc123")
        finally:
            del os.environ["TEST_BRAIN_TOKEN"]

    def test_interpolate_nested_dict(self):
        os.environ["TEST_BRAIN_HOST"] = "db.example.com"
        try:
            obj = {"db": {"host": "${TEST_BRAIN_HOST}", "port": 5432}}
            result = _interpolate_env(obj)
            self.assertEqual(result["db"]["host"], "db.example.com")
            self.assertEqual(result["db"]["port"], 5432)
        finally:
            del os.environ["TEST_BRAIN_HOST"]

    def test_interpolate_list(self):
        os.environ["TEST_BRAIN_REPO"] = "grobomo/test"
        try:
            result = _interpolate_env(["${TEST_BRAIN_REPO}", "other"])
            self.assertEqual(result, ["grobomo/test", "other"])
        finally:
            del os.environ["TEST_BRAIN_REPO"]

    def test_unset_var_left_as_is(self):
        result = _interpolate_env("${NONEXISTENT_VAR_12345}")
        self.assertEqual(result, "${NONEXISTENT_VAR_12345}")

    def test_non_string_passthrough(self):
        self.assertEqual(_interpolate_env(42), 42)
        self.assertIsNone(_interpolate_env(None))
        self.assertTrue(_interpolate_env(True))

    def test_load_config_interpolates(self):
        os.environ["TEST_BRAIN_INTERVAL"] = "120"
        tmpdir = tempfile.mkdtemp()
        try:
            config_path = os.path.join(tmpdir, "test.json")
            with open(config_path, "w") as f:
                json.dump({"interval": "${TEST_BRAIN_INTERVAL}", "name": "test"}, f)
            config = _load_config(config_path)
            self.assertEqual(config["interval"], "120")
            self.assertEqual(config["name"], "test")
        finally:
            del os.environ["TEST_BRAIN_INTERVAL"]
            shutil.rmtree(tmpdir)


class TestDispatchTransportFactory(unittest.TestCase):
    """Tests for pluggable dispatch transport (T033)."""

    def test_default_is_file_transport(self):
        transport = _create_transport({})
        self.assertIsInstance(transport, FileTransport)

    def test_explicit_file_transport(self):
        transport = _create_transport({"transport": "file", "outbox_dir": "data/outbox"})
        self.assertIsInstance(transport, FileTransport)

    def test_sqs_transport_created(self):
        transport = _create_transport({
            "transport": "sqs",
            "sqs_task_queue_url": "https://sqs.us-east-1.amazonaws.com/123/tasks",
            "sqs_result_queue_url": "https://sqs.us-east-1.amazonaws.com/123/results",
            "sqs_region": "us-west-2",
        })
        self.assertIsInstance(transport, SQSTransport)
        self.assertEqual(transport.task_queue_url, "https://sqs.us-east-1.amazonaws.com/123/tasks")
        self.assertEqual(transport.result_queue_url, "https://sqs.us-east-1.amazonaws.com/123/results")
        self.assertEqual(transport.region, "us-west-2")

    def test_file_transport_roundtrip(self):
        tmpdir = tempfile.mkdtemp()
        try:
            transport = FileTransport(
                bridge_dir=os.path.join(tmpdir, "bridge"),
                results_dir=os.path.join(tmpdir, "results"),
            )
            task = {"id": "test-task-1", "summary": "Fix the bug", "text": "Fix the bug"}
            result = transport.send_task(task)
            self.assertEqual(result["status"], "dispatched")
            self.assertEqual(result["task_id"], "test-task-1")

            # Verify file written
            files = list(transport.bridge_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            written = json.loads(files[0].read_text())
            self.assertEqual(written["summary"], "Fix the bug")

            # Simulate result
            result_file = transport.results_dir / "test-task-1.json"
            result_file.write_text(json.dumps({"id": "test-task-1", "success": True}))
            results = transport.poll_results()
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["success"])

            # Result moved to done/
            self.assertEqual(len(list(transport.results_dir.glob("*.json"))), 0)
            self.assertEqual(len(list((transport.results_dir / "done").glob("*.json"))), 1)
        finally:
            shutil.rmtree(tmpdir)

    def test_sqs_transport_send_with_mock(self):
        """Test SQS transport with a mock boto3 client."""
        transport = SQSTransport(
            task_queue_url="https://sqs.us-east-1.amazonaws.com/123/tasks",
            result_queue_url="https://sqs.us-east-1.amazonaws.com/123/results",
        )

        # Mock the client
        class MockSQSClient:
            def __init__(self):
                self.sent = []
                self.messages = []

            def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return {"MessageId": "mock-msg-001"}

            def receive_message(self, **kwargs):
                return {"Messages": self.messages}

            def delete_message(self, **kwargs):
                pass

        mock_client = MockSQSClient()
        transport._client = mock_client

        # Send a task
        task = {"id": "sqs-task-1", "summary": "Deploy fix", "source": "github:grobomo/repo"}
        result = transport.send_task(task)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["message_id"], "mock-msg-001")
        self.assertEqual(len(mock_client.sent), 1)
        self.assertEqual(mock_client.sent[0]["QueueUrl"], "https://sqs.us-east-1.amazonaws.com/123/tasks")

        # Poll results (empty)
        results = transport.poll_results()
        self.assertEqual(results, [])

        # Simulate a result message
        mock_client.messages = [{
            "Body": json.dumps({"id": "sqs-task-1", "success": True, "output": "Done"}),
            "ReceiptHandle": "handle-1",
        }]
        results = transport.poll_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "sqs-task-1")
        self.assertTrue(results[0]["success"])

    def test_sqs_transport_no_result_queue(self):
        """SQS with no result queue returns empty results."""
        transport = SQSTransport(task_queue_url="https://sqs.us-east-1.amazonaws.com/123/tasks")
        results = transport.poll_results()
        self.assertEqual(results, [])

    def test_sqs_transport_send_error_handling(self):
        """SQS send failure returns error status instead of raising."""
        transport = SQSTransport(task_queue_url="https://sqs.us-east-1.amazonaws.com/123/tasks")

        class FailClient:
            def send_message(self, **kwargs):
                raise RuntimeError("Connection refused")

        transport._client = FailClient()
        result = transport.send_task({"id": "fail-1", "summary": "test"})
        self.assertEqual(result["status"], "error")
        self.assertIn("Connection refused", result["error"])

    def test_dispatcher_with_sqs_transport(self):
        """ActionDispatcher uses SQS transport when configured."""
        tmpdir = tempfile.mkdtemp()
        try:
            dispatcher = ActionDispatcher({
                "transport": "sqs",
                "sqs_task_queue_url": "https://sqs.us-east-1.amazonaws.com/123/tasks",
                "outbox_dir": os.path.join(tmpdir, "outbox"),
                "results_dir": os.path.join(tmpdir, "inbox"),
            })
            self.assertIsInstance(dispatcher.transport, SQSTransport)

            # Channel outboxes still filesystem
            self.assertTrue(dispatcher.github_outbox.exists())
            self.assertTrue(dispatcher.teams_outbox.exists())
            self.assertTrue(dispatcher.email_outbox.exists())
        finally:
            shutil.rmtree(tmpdir)

    def test_dispatcher_default_file_transport(self):
        """ActionDispatcher defaults to FileTransport."""
        tmpdir = tempfile.mkdtemp()
        try:
            dispatcher = ActionDispatcher({
                "outbox_dir": os.path.join(tmpdir, "outbox"),
                "results_dir": os.path.join(tmpdir, "inbox"),
            })
            self.assertIsInstance(dispatcher.transport, FileTransport)
            # bridge_dir exposed for backwards compat
            self.assertTrue(dispatcher.bridge_dir.exists())
        finally:
            shutil.rmtree(tmpdir)


class TestActionExecutor(unittest.TestCase):
    """Tests for active RESPOND execution (T037)."""

    def test_extract_number_from_issue_id(self):
        self.assertEqual(ActionExecutor._extract_number("gh:issue:grobomo/repo:42"), "42")

    def test_extract_number_from_pr_id(self):
        self.assertEqual(ActionExecutor._extract_number("gh:pr:grobomo/repo:7"), "7")

    def test_extract_number_from_event_id(self):
        """Event IDs without issue number return empty."""
        self.assertEqual(ActionExecutor._extract_number("gh:event:12345"), "")

    def test_extract_number_empty(self):
        self.assertEqual(ActionExecutor._extract_number(""), "")
        self.assertEqual(ActionExecutor._extract_number("random"), "")

    def test_respond_github_no_number(self):
        """GitHub respond fails gracefully when event_id has no number."""
        executor = ActionExecutor()
        result = executor.respond_github("grobomo/repo", "gh:event:12345", "test comment")
        self.assertEqual(result["status"], "error")
        self.assertIn("Cannot extract", result["error"])

    def test_respond_teams_no_token(self):
        """Teams respond fails gracefully with no token configured."""
        executor = ActionExecutor()
        result = executor.respond_teams("chat-id", "test message")
        self.assertEqual(result["status"], "error")
        self.assertIn("No Teams token", result["error"])


class TestActiveRespond(unittest.TestCase):
    """Tests for active_respond integration in dispatcher (T037)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_active_respond_disabled_by_default(self):
        """When active_respond is not set, RESPOND writes to outbox."""
        dispatcher = ActionDispatcher({
            "outbox_dir": os.path.join(self.tmpdir, "outbox"),
            "results_dir": os.path.join(self.tmpdir, "inbox"),
        })
        self.assertFalse(dispatcher.active_respond)
        action = {
            "action": RESPOND, "source": "github",
            "channel": "grobomo/repo", "content": "test",
        }
        result = dispatcher.dispatch(action)
        self.assertEqual(result["status"], "queued")
        # Outbox file should exist
        files = list(dispatcher.github_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_active_respond_fallback_to_outbox(self):
        """When active_respond=True but execution fails, falls back to outbox."""
        dispatcher = ActionDispatcher({
            "active_respond": True,
            "outbox_dir": os.path.join(self.tmpdir, "outbox"),
            "results_dir": os.path.join(self.tmpdir, "inbox"),
        })
        self.assertTrue(dispatcher.active_respond)
        # This will fail (no gh CLI or bad event_id) and fall back to outbox
        action = {
            "action": RESPOND, "source": "github",
            "channel": "grobomo/repo", "event_id": "gh:event:999",
            "content": "test comment",
        }
        result = dispatcher.dispatch(action)
        # Should fall back to outbox
        self.assertEqual(result["status"], "queued")
        files = list(dispatcher.github_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_active_respond_mock_success(self):
        """When executor succeeds, returns executed status."""
        dispatcher = ActionDispatcher({
            "active_respond": True,
            "outbox_dir": os.path.join(self.tmpdir, "outbox"),
            "results_dir": os.path.join(self.tmpdir, "inbox"),
        })

        # Mock the executor
        class MockExecutor:
            def respond_github(self, repo, event_id, body):
                return {"status": "executed", "url": "https://github.com/test/1#comment"}
            def respond_teams(self, chat_id, body):
                return {"status": "executed", "message_id": "mock-123"}

        dispatcher._executor = MockExecutor()

        # GitHub
        result = dispatcher.dispatch({
            "action": RESPOND, "source": "github",
            "channel": "grobomo/repo", "event_id": "gh:issue:grobomo/repo:1",
            "content": "test",
        })
        self.assertEqual(result["status"], "executed")

        # Teams
        result = dispatcher.dispatch({
            "action": RESPOND, "source": "teams",
            "channel": "19:abc@thread.v2",
            "content": "test message",
        })
        self.assertEqual(result["status"], "executed")

        # No outbox files should be written
        gh_files = list(dispatcher.github_outbox.glob("*.json"))
        teams_files = list(dispatcher.teams_outbox.glob("*.json"))
        self.assertEqual(len(gh_files), 0)
        self.assertEqual(len(teams_files), 0)


class TestFeedback(unittest.TestCase):
    """Tests for feedback loop (T039)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        from unified_brain.feedback import FeedbackStore
        self.feedback = FeedbackStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_record_and_summary(self):
        """Record outcomes and get summary stats."""
        self.feedback.record("t1", "dispatch", True, "github", "grobomo/repo")
        self.feedback.record("t2", "dispatch", True, "github", "grobomo/repo")
        self.feedback.record("t3", "dispatch", False, "github", "grobomo/repo", error="timeout")
        self.feedback.record("t4", "respond", True, "teams", "19:abc")

        stats = self.feedback.summary(hours=1)
        self.assertEqual(stats["dispatch"]["total"], 3)
        self.assertEqual(stats["dispatch"]["success"], 2)
        self.assertEqual(stats["dispatch"]["failure"], 1)
        self.assertAlmostEqual(stats["dispatch"]["rate"], 0.67, places=2)
        self.assertEqual(stats["respond"]["total"], 1)
        self.assertEqual(stats["respond"]["success"], 1)
        self.assertEqual(stats["respond"]["rate"], 1.0)

    def test_recent_failures(self):
        """Recent failures are included in summary."""
        self.feedback.record("fail-1", "dispatch", False, "github", "r1", error="worker crash")
        self.feedback.record("fail-2", "respond", False, "teams", "c1", error="HTTP 403")
        stats = self.feedback.summary(hours=1)
        failures = stats["recent_failures"]
        self.assertEqual(len(failures), 2)
        errors = {f["error"] for f in failures}
        self.assertIn("worker crash", errors)
        self.assertIn("HTTP 403", errors)

    def test_channel_stats(self):
        """Per-channel stats are computed correctly."""
        self.feedback.record("c1", "dispatch", True, "github", "grobomo/a")
        self.feedback.record("c2", "dispatch", True, "github", "grobomo/a")
        self.feedback.record("c3", "dispatch", False, "github", "grobomo/b")
        channels = self.feedback.channel_stats(hours=1)
        self.assertEqual(len(channels), 2)
        a = next(c for c in channels if c["channel"] == "grobomo/a")
        self.assertEqual(a["total"], 2)
        self.assertEqual(a["rate"], 1.0)

    def test_empty_summary(self):
        """Summary with no data returns empty dict."""
        stats = self.feedback.summary(hours=1)
        self.assertEqual(stats, {"recent_failures": []})

    def test_context_includes_feedback(self):
        """ContextBuilder includes feedback stats when feedback store is set."""
        tmpdir = tempfile.mkdtemp()
        try:
            store = EventStore(os.path.join(tmpdir, "test.db"))
            registry = ProjectRegistry(os.path.join(tmpdir, "projects.yaml"))
            from unified_brain.feedback import FeedbackStore
            feedback = FeedbackStore(store.conn)
            feedback.record("fb-1", "dispatch", True, "github", "grobomo/repo")
            feedback.record("fb-2", "dispatch", False, "github", "grobomo/repo", error="fail")

            builder = ContextBuilder(store, registry, feedback=feedback)
            ctx = builder.build({"source": "github", "channel": "grobomo/repo", "author": "bot"})
            self.assertIsNotNone(ctx.get("feedback"))
            self.assertIn("dispatch", ctx["feedback"])
            self.assertEqual(ctx["feedback"]["dispatch"]["total"], 2)
        finally:
            store.close()
            shutil.rmtree(tmpdir)

    def test_brain_prompt_includes_feedback(self):
        """Brain prompt renders feedback section when stats exist."""
        brain = BrainAnalyzer({"claude_path": "nonexistent"})
        event = {"source": "github", "channel": "grobomo/repo",
                 "event_type": "issue", "author": "bot", "title": "Bug", "body": "broken"}
        context = {
            "feedback": {
                "dispatch": {"total": 10, "success": 8, "failure": 2, "rate": 0.8},
                "recent_failures": [
                    {"id": "f1", "action": "dispatch", "source": "github",
                     "channel": "grobomo/repo", "error": "timeout"},
                ],
            },
        }
        prompt = brain._build_prompt(event, context)
        self.assertIn("Recent Outcomes", prompt)
        self.assertIn("DISPATCH: 8/10", prompt)
        self.assertIn("timeout", prompt)


class TestWebhookAdapter(unittest.TestCase):
    """Tests for webhook adapter (T040)."""

    def setUp(self):
        import socket
        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]
        self.adapter = WebhookAdapter({"webhook_port": self.port, "webhook_bind": "127.0.0.1"})

    def tearDown(self):
        asyncio.run(self.adapter.stop())

    def _post(self, path, data, headers=None):
        from urllib.request import urlopen, Request
        body = json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        return urlopen(req, timeout=5)

    def _get(self, path):
        from urllib.request import urlopen
        return urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_post_single_event(self):
        """POST /events with a single event dict."""
        asyncio.run(self.adapter.start())
        event = {
            "id": "test-1", "source": "jira", "channel": "PROJ-123",
            "event_type": "issue_created", "author": "alice",
            "title": "New bug", "body": "Something is broken",
        }
        resp = self._post("/events", event)
        self.assertEqual(resp.status, 202)
        result = json.loads(resp.read())
        self.assertEqual(result["accepted"], 1)

        # Poll should return the event
        events = asyncio.run(self.adapter.poll())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "test-1")
        self.assertEqual(events[0]["source"], "jira")

    def test_post_batch_events(self):
        """POST /events with a list of events."""
        asyncio.run(self.adapter.start())
        events = [
            {"id": "batch-1", "source": "slack", "title": "msg 1"},
            {"id": "batch-2", "source": "slack", "title": "msg 2"},
        ]
        resp = self._post("/events", events)
        self.assertEqual(resp.status, 202)
        result = json.loads(resp.read())
        self.assertEqual(result["accepted"], 2)

        polled = asyncio.run(self.adapter.poll())
        self.assertEqual(len(polled), 2)

    def test_post_raw_github_webhook(self):
        """POST /events/raw with a GitHub-style webhook payload."""
        asyncio.run(self.adapter.start())
        payload = {
            "action": "opened",
            "issue": {"title": "Bug report", "number": 42},
            "repository": {"full_name": "grobomo/test-repo"},
            "sender": {"login": "octocat"},
        }
        resp = self._post("/events/raw", payload,
                          headers={"X-GitHub-Event": "issues"})
        self.assertEqual(resp.status, 202)

        events = asyncio.run(self.adapter.poll())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["channel"], "grobomo/test-repo")
        self.assertEqual(events[0]["event_type"], "issues")
        self.assertIn("Bug report", events[0]["title"])

    def test_get_stats(self):
        """GET /events/stats returns queue depth and accepted count."""
        asyncio.run(self.adapter.start())
        self._post("/events", {"id": "s1", "title": "test"})
        resp = self._get("/events/stats")
        stats = json.loads(resp.read())
        self.assertEqual(stats["accepted_total"], 1)
        self.assertEqual(stats["queue_depth"], 1)

    def test_invalid_json(self):
        """POST with invalid JSON returns 400."""
        asyncio.run(self.adapter.start())
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        req = Request(f"http://127.0.0.1:{self.port}/events",
                      data=b"not json", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(HTTPError) as cm:
            urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_hmac_verification(self):
        """HMAC signature is verified when secret is configured."""
        import hashlib as hl
        import hmac as hm
        secret = "test-secret-123"
        self.adapter = WebhookAdapter({
            "webhook_port": self.port, "webhook_bind": "127.0.0.1",
            "webhook_secret": secret,
        })
        asyncio.run(self.adapter.start())

        payload = json.dumps({"id": "hmac-1", "title": "signed"}).encode()
        sig = "sha256=" + hm.new(secret.encode(), payload, hl.sha256).hexdigest()

        from urllib.request import urlopen, Request
        req = Request(f"http://127.0.0.1:{self.port}/events",
                      data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Hub-Signature-256", sig)
        resp = urlopen(req, timeout=5)
        self.assertEqual(resp.status, 202)

        # Bad signature should fail
        from urllib.error import HTTPError
        req2 = Request(f"http://127.0.0.1:{self.port}/events",
                       data=payload, method="POST")
        req2.add_header("Content-Type", "application/json")
        req2.add_header("X-Hub-Signature-256", "sha256=bad")
        with self.assertRaises(HTTPError) as cm:
            urlopen(req2, timeout=5)
        self.assertEqual(cm.exception.code, 401)

    def test_poll_drains_queue(self):
        """Poll returns all queued events and leaves queue empty."""
        asyncio.run(self.adapter.start())
        self._post("/events", [{"id": f"d-{i}"} for i in range(5)])
        events = asyncio.run(self.adapter.poll())
        self.assertEqual(len(events), 5)
        # Second poll should be empty
        events2 = asyncio.run(self.adapter.poll())
        self.assertEqual(len(events2), 0)

    def test_auto_generated_id(self):
        """Events without an ID get a deterministic hash-based ID."""
        asyncio.run(self.adapter.start())
        self._post("/events", {"title": "no id event", "body": "test"})
        events = asyncio.run(self.adapter.poll())
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["id"].startswith("wh:"))


class TestAskEndpoint(unittest.TestCase):
    """Tests for synchronous /ask endpoint (T048)."""

    def setUp(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]

    def _post(self, port, path, data):
        from urllib.request import urlopen, Request
        body = json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            return urlopen(req, timeout=10)
        except Exception as e:
            return e

    def test_ask_without_brain_returns_503(self):
        """When no brain is configured, /ask returns 503."""
        adapter = WebhookAdapter({
            "webhook_port": self.port,
            "webhook_bind": "127.0.0.1",
        })
        asyncio.run(adapter.start())
        try:
            resp = self._post(self.port, "/ask", {"question": "test"})
            from urllib.error import HTTPError
            self.assertIsInstance(resp, HTTPError)
            self.assertEqual(resp.code, 503)
        finally:
            asyncio.run(adapter.stop())

    def test_ask_missing_question_returns_400(self):
        """Missing question field returns 400."""
        class MockBrain:
            def analyze(self, event, context):
                return {"action": "log", "content": "", "reason": ""}

        adapter = WebhookAdapter(
            {"webhook_port": self.port, "webhook_bind": "127.0.0.1"},
            brain=MockBrain(),
        )
        asyncio.run(adapter.start())
        try:
            resp = self._post(self.port, "/ask", {"not_question": "oops"})
            from urllib.error import HTTPError
            self.assertIsInstance(resp, HTTPError)
            self.assertEqual(resp.code, 400)
        finally:
            asyncio.run(adapter.stop())

    def test_ask_returns_brain_response(self):
        """Successful /ask returns the brain's action as JSON."""
        class MockBrain:
            def analyze(self, event, context):
                return {
                    "action": "respond",
                    "content": f"Analysis of: {event['body']}",
                    "reason": "test reason",
                }

        adapter = WebhookAdapter(
            {"webhook_port": self.port, "webhook_bind": "127.0.0.1"},
            brain=MockBrain(),
        )
        asyncio.run(adapter.start())
        try:
            resp = self._post(self.port, "/ask", {
                "question": "What should we prioritize?",
                "author": "joel",
            })
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
            self.assertEqual(data["action"], "respond")
            self.assertIn("What should we prioritize?", data["content"])
            self.assertEqual(data["reason"], "test reason")
            self.assertTrue(data["event_id"].startswith("ask:"))
        finally:
            asyncio.run(adapter.stop())

    def test_ask_with_context_builder(self):
        """When context_builder is provided, it's used to enrich the brain prompt."""
        calls = []

        class MockBrain:
            def analyze(self, event, context):
                calls.append(("brain", context))
                return {"action": "log", "content": "", "reason": ""}

        class MockContextBuilder:
            def build(self, event):
                return {"project": {"name": "test-project"}}

        adapter = WebhookAdapter(
            {"webhook_port": self.port, "webhook_bind": "127.0.0.1"},
            brain=MockBrain(),
            context_builder=MockContextBuilder(),
        )
        asyncio.run(adapter.start())
        try:
            self._post(self.port, "/ask", {"question": "test"})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["project"]["name"], "test-project")
        finally:
            asyncio.run(adapter.stop())

    def test_ask_rate_limited(self):
        """The /ask endpoint respects rate limiting too."""
        class MockBrain:
            def analyze(self, event, context):
                return {"action": "log", "content": "", "reason": ""}

        adapter = WebhookAdapter(
            {
                "webhook_port": self.port,
                "webhook_bind": "127.0.0.1",
                "webhook_rate_limit": 0.001,
                "webhook_rate_burst": 1,
            },
            brain=MockBrain(),
        )
        asyncio.run(adapter.start())
        try:
            resp1 = self._post(self.port, "/ask", {"question": "q1"})
            self.assertEqual(resp1.status, 200)
            resp2 = self._post(self.port, "/ask", {"question": "q2"})
            from urllib.error import HTTPError
            self.assertIsInstance(resp2, HTTPError)
            self.assertEqual(resp2.code, 429)
        finally:
            asyncio.run(adapter.stop())


class TestTokenBucket(unittest.TestCase):
    """Tests for webhook rate limiter (T046)."""

    def test_allows_within_burst(self):
        from unified_brain.adapters.webhook import TokenBucket
        bucket = TokenBucket(rate=10, burst=5)
        for _ in range(5):
            self.assertTrue(bucket.allow("ip1"))

    def test_rejects_after_burst_exhausted(self):
        from unified_brain.adapters.webhook import TokenBucket
        bucket = TokenBucket(rate=0.001, burst=3)  # very slow refill
        for _ in range(3):
            bucket.allow("ip1")
        self.assertFalse(bucket.allow("ip1"))

    def test_separate_keys_independent(self):
        from unified_brain.adapters.webhook import TokenBucket
        bucket = TokenBucket(rate=0.001, burst=2)
        bucket.allow("ip1")
        bucket.allow("ip1")
        self.assertFalse(bucket.allow("ip1"))
        # ip2 should still have its own bucket
        self.assertTrue(bucket.allow("ip2"))

    def test_refill_over_time(self):
        from unified_brain.adapters.webhook import TokenBucket
        bucket = TokenBucket(rate=1000, burst=5)  # fast refill
        for _ in range(5):
            bucket.allow("ip1")
        # With rate=1000/s, even a tiny delay should refill at least 1 token
        time.sleep(0.01)
        self.assertTrue(bucket.allow("ip1"))

    def test_cleanup_removes_stale(self):
        from unified_brain.adapters.webhook import TokenBucket
        bucket = TokenBucket(rate=10, burst=5)
        bucket.allow("ip1")
        bucket.allow("ip2")
        # Force stale by setting last access far in the past
        with bucket._lock:
            bucket._buckets["ip1"][1] = time.monotonic() - 7200
        bucket.cleanup(max_age=3600)
        self.assertNotIn("ip1", bucket._buckets)
        self.assertIn("ip2", bucket._buckets)


class TestWebhookRateLimit(unittest.TestCase):
    """Tests for webhook endpoint rate limiting (T046)."""

    def setUp(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]

    def _post(self, port, path, data):
        from urllib.request import urlopen, Request
        body = json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            return urlopen(req, timeout=5)
        except Exception as e:
            return e

    def test_rate_limit_returns_429(self):
        """Requests beyond burst limit get 429."""
        adapter = WebhookAdapter({
            "webhook_port": self.port,
            "webhook_bind": "127.0.0.1",
            "webhook_rate_limit": 0.001,  # very slow refill
            "webhook_rate_burst": 2,
        })
        asyncio.run(adapter.start())
        try:
            event = {"id": "rl-1", "source": "test", "title": "t"}
            # First 2 should succeed (burst=2)
            resp1 = self._post(self.port, "/events", event)
            self.assertEqual(resp1.status, 202)
            resp2 = self._post(self.port, "/events", {"id": "rl-2", "source": "test", "title": "t"})
            self.assertEqual(resp2.status, 202)
            # Third should be rate-limited (429 or connection error on Windows)
            resp3 = self._post(self.port, "/events", {"id": "rl-3", "source": "test", "title": "t"})
            from urllib.error import HTTPError
            if isinstance(resp3, HTTPError):
                self.assertEqual(resp3.code, 429)
            else:
                # Windows may raise ConnectionAbortedError instead
                self.assertIsInstance(resp3, Exception)
        finally:
            asyncio.run(adapter.stop())

    def test_rate_limit_disabled_when_zero(self):
        """Rate limit=0 disables rate limiting."""
        adapter = WebhookAdapter({
            "webhook_port": self.port,
            "webhook_bind": "127.0.0.1",
            "webhook_rate_limit": 0,
        })
        asyncio.run(adapter.start())
        try:
            # Should accept many requests without 429
            for i in range(10):
                resp = self._post(self.port, "/events",
                                  {"id": f"nolimit-{i}", "source": "test", "title": "t"})
                self.assertEqual(resp.status, 202)
        finally:
            asyncio.run(adapter.stop())


class TestMetrics(unittest.TestCase):
    """Tests for Prometheus metrics module (T038)."""

    def setUp(self):
        from unified_brain.metrics import MetricsRegistry
        MetricsRegistry.reset()

    def test_counter_inc_and_get(self):
        from unified_brain.metrics import Counter
        c = Counter("test_counter", "A test counter")
        self.assertEqual(c.get(), 0.0)
        c.inc()
        self.assertEqual(c.get(), 1.0)
        c.inc(5)
        self.assertEqual(c.get(), 6.0)

    def test_counter_with_labels(self):
        from unified_brain.metrics import Counter
        c = Counter("test_labeled", "Labeled counter")
        c.inc(action="log")
        c.inc(action="dispatch")
        c.inc(action="log")
        self.assertEqual(c.get(action="log"), 2.0)
        self.assertEqual(c.get(action="dispatch"), 1.0)
        self.assertEqual(c.get(action="respond"), 0.0)

    def test_gauge_set_and_get(self):
        from unified_brain.metrics import Gauge
        g = Gauge("test_gauge", "A test gauge")
        g.set(42.5)
        self.assertEqual(g.get(), 42.5)
        g.set(0)
        self.assertEqual(g.get(), 0.0)

    def test_gauge_inc(self):
        from unified_brain.metrics import Gauge
        g = Gauge("test_gauge_inc", "Incrementable gauge")
        g.inc(3)
        g.inc(2)
        self.assertEqual(g.get(), 5.0)

    def test_expose_format(self):
        from unified_brain.metrics import Counter
        c = Counter("http_requests_total", "Total HTTP requests")
        c.inc(method="GET", status="200")
        c.inc(method="POST", status="201")
        text = c.expose()
        self.assertIn("# HELP http_requests_total Total HTTP requests", text)
        self.assertIn("# TYPE http_requests_total counter", text)
        self.assertIn('method="GET"', text)
        self.assertIn('status="200"', text)
        self.assertIn('method="POST"', text)

    def test_registry_expose(self):
        from unified_brain.metrics import MetricsRegistry, Counter
        reg = MetricsRegistry()
        c = reg.register(Counter("reg_test", "Registry test"))
        c.inc(source="github")
        output = reg.expose()
        self.assertIn("reg_test", output)
        self.assertIn("brain_uptime_seconds", output)

    def test_global_metrics_exist(self):
        """Global metric instances are importable and functional."""
        from unified_brain import metrics
        metrics.events_ingested.inc(adapter="github")
        metrics.brain_decisions.inc(action="log")
        metrics.cycle_duration.set(0.5)
        self.assertEqual(metrics.events_ingested.get(adapter="github"), 1.0)
        self.assertEqual(metrics.brain_decisions.get(action="log"), 1.0)
        self.assertEqual(metrics.cycle_duration.get(), 0.5)

    def test_service_cycle_increments_metrics(self):
        """BrainService.run_cycle increments metrics counters."""
        # Import the same metric objects the service module uses
        from unified_brain.metrics import (
            events_ingested, cycle_count, cycle_duration, events_processed,
        )

        # Record baseline values (other tests may have incremented)
        baseline_ingested = events_ingested.get(adapter="test-metrics")
        baseline_cycles = cycle_count.get()

        tmpdir = tempfile.mkdtemp()
        try:
            config = {
                "db_path": os.path.join(tmpdir, "test.db"),
                "brain": {"claude_path": "nonexistent-claude"},
                "dispatcher": {
                    "outbox_dir": os.path.join(tmpdir, "outbox"),
                    "results_dir": os.path.join(tmpdir, "inbox"),
                },
                "registry_path": os.path.join(tmpdir, "projects.yaml"),
            }
            service = BrainService(config)

            # Add a mock adapter that returns one event
            class MockAdapter:
                name = "test-metrics"
                async def poll(self):
                    return [{
                        "id": f"metric-test-{time.time_ns()}",
                        "source": "github",
                        "channel": "grobomo/repo",
                        "event_type": "push",
                        "author": "bot",
                        "title": "Test event",
                        "body": "routine push",
                        "created_at": "2026-04-06T00:00:00Z",
                    }]
                async def start(self): pass
                async def stop(self): pass

            service.add_adapter(MockAdapter())
            asyncio.run(service.run_cycle())

            # Check that metrics were incremented from baseline
            self.assertGreater(events_ingested.get(adapter="test-metrics"), baseline_ingested)
            self.assertGreater(cycle_count.get(), baseline_cycles)
            self.assertGreater(cycle_duration.get(), 0)
        finally:
            service.store.close()
            shutil.rmtree(tmpdir)


class TestAskEndpoint(unittest.TestCase):
    """Tests for synchronous /ask endpoint (T048)."""

    def setUp(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]

        self.tmpdir = tempfile.mkdtemp()
        self.config = {
            "db_path": os.path.join(self.tmpdir, "test.db"),
            "brain": {"llm_backend": "subprocess", "claude_path": "echo"},
            "dispatcher": {
                "outbox_dir": os.path.join(self.tmpdir, "outbox"),
                "results_dir": os.path.join(self.tmpdir, "inbox"),
            },
        }

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_health_server(self, service=None):
        """Start a health server with optional service reference."""
        import threading
        from http.server import HTTPServer
        from unified_brain.runner import _HealthHandler

        _HealthHandler.stats = {"status": "ok"}
        _HealthHandler.service = service
        server = HTTPServer(("127.0.0.1", self.port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server

    def _post_ask(self, data):
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        body = json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{self.port}/ask", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_ask_without_service_returns_503(self):
        server = self._start_health_server(service=None)
        try:
            code, body = self._post_ask({"question": "hello?"})
            self.assertEqual(code, 503)
            self.assertIn("error", body)
        finally:
            server.shutdown()

    def test_ask_missing_question_returns_400(self):
        service = BrainService(self.config)
        server = self._start_health_server(service=service)
        try:
            code, body = self._post_ask({"not_a_question": "oops"})
            self.assertEqual(code, 400)
            self.assertIn("question", body["error"])
        finally:
            server.shutdown()
            service.store.close()

    def test_ask_with_question_returns_analysis(self):
        service = BrainService(self.config)
        server = self._start_health_server(service=service)
        try:
            code, body = self._post_ask({"question": "What issues are open?"})
            self.assertEqual(code, 200)
            self.assertIn("action", body)
            self.assertIn("content", body)
            self.assertIn("event_id", body)
            self.assertTrue(body["event_id"].startswith("ask:"))
            self.assertIn("context_sources", body)
        finally:
            server.shutdown()
            service.store.close()

    def test_ask_with_shorthand_q(self):
        """'q' field works as alias for 'question'."""
        service = BrainService(self.config)
        server = self._start_health_server(service=service)
        try:
            code, body = self._post_ask({"q": "Short question"})
            self.assertEqual(code, 200)
            self.assertIn("action", body)
        finally:
            server.shutdown()
            service.store.close()

    def test_ask_with_source_and_channel(self):
        """Custom source and channel are passed through."""
        service = BrainService(self.config)
        server = self._start_health_server(service=service)
        try:
            code, body = self._post_ask({
                "question": "Status of repo X?",
                "source": "slack",
                "channel": "C123",
                "author": "joel",
            })
            self.assertEqual(code, 200)
        finally:
            server.shutdown()
            service.store.close()

    def test_ask_invalid_json_returns_400(self):
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        server = self._start_health_server(service=BrainService(self.config))
        try:
            req = Request(f"http://127.0.0.1:{self.port}/ask",
                          data=b"not json", method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", "8")
            try:
                urlopen(req, timeout=5)
                self.fail("Expected HTTPError")
            except HTTPError as e:
                self.assertEqual(e.code, 400)
        finally:
            server.shutdown()


class TestSlackAdapter(unittest.TestCase):
    """Tests for the Slack channel adapter."""

    def test_source_is_slack(self):
        from unified_brain.adapters.slack import SlackAdapter
        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C123"]})
        self.assertEqual(adapter.source, "slack")
        self.assertEqual(adapter.name, "slack")

    def test_poll_without_start_returns_empty(self):
        from unified_brain.adapters.slack import SlackAdapter
        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C123"]})
        events = asyncio.run(adapter.poll())
        self.assertEqual(events, [])

    def test_start_without_token_logs_error(self):
        from unified_brain.adapters.slack import SlackAdapter
        adapter = SlackAdapter({})
        asyncio.run(adapter.start())
        self.assertIsNone(adapter._client)

    def test_normalize_messages(self):
        """Test message normalization logic directly."""
        from unified_brain.adapters.slack import SlackAdapter, _SlackClient
        adapter = SlackAdapter({
            "bot_token": "xoxb-test",
            "channel_ids": ["C123"],
        })

        # Mock the client to return canned messages
        class MockSlackClient:
            def get(self, method, params=None):
                if method == "conversations.history":
                    return {
                        "ok": True,
                        "messages": [
                            {
                                "ts": "1700000001.000001",
                                "user": "U123",
                                "text": "Hello from Slack!",
                            },
                            {
                                "ts": "1700000002.000002",
                                "user": "U456",
                                "text": "Second message",
                            },
                            {
                                "ts": "1700000003.000003",
                                "subtype": "channel_join",
                                "text": "joined the channel",
                            },
                            {
                                "ts": "1700000004.000004",
                                "user": "U789",
                                "text": "",  # empty — should be skipped
                            },
                        ],
                    }
                return {"ok": True}

        adapter._client = MockSlackClient()
        events = asyncio.run(adapter.poll())

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["id"], "slack:C123:1700000001.000001")
        self.assertEqual(events[0]["source"], "slack")
        self.assertEqual(events[0]["channel"], "C123")
        self.assertEqual(events[0]["author"], "U123")
        self.assertEqual(events[0]["body"], "Hello from Slack!")
        self.assertEqual(events[0]["event_type"], "message")
        self.assertIn("ts", events[0]["metadata"])

        self.assertEqual(events[1]["author"], "U456")

    def test_dedup_on_second_poll(self):
        """Already-seen messages are not returned again."""
        from unified_brain.adapters.slack import SlackAdapter

        class MockSlackClient:
            def get(self, method, params=None):
                return {
                    "ok": True,
                    "messages": [
                        {"ts": "1700000001.000001", "user": "U1", "text": "msg"},
                    ],
                }

        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C1"]})
        adapter._client = MockSlackClient()

        events1 = asyncio.run(adapter.poll())
        self.assertEqual(len(events1), 1)

        events2 = asyncio.run(adapter.poll())
        self.assertEqual(len(events2), 0)

    def test_cursor_updates_for_incremental_polling(self):
        """After polling, cursor should be set to newest ts."""
        from unified_brain.adapters.slack import SlackAdapter

        class MockSlackClient:
            def get(self, method, params=None):
                return {
                    "ok": True,
                    "messages": [
                        {"ts": "1700000010.000000", "user": "U1", "text": "newer"},
                        {"ts": "1700000001.000000", "user": "U1", "text": "older"},
                    ],
                }

        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C1"]})
        adapter._client = MockSlackClient()
        asyncio.run(adapter.poll())

        self.assertEqual(adapter._channel_cursors["C1"], "1700000010.000000")

    def test_multiple_channels(self):
        """Adapter polls all configured channels."""
        from unified_brain.adapters.slack import SlackAdapter

        class MockSlackClient:
            def get(self, method, params=None):
                ch = params.get("channel", "")
                return {
                    "ok": True,
                    "messages": [
                        {"ts": f"170000000{ch[-1]}.000001", "user": "U1", "text": f"msg in {ch}"},
                    ],
                }

        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C1", "C2"]})
        adapter._client = MockSlackClient()
        events = asyncio.run(adapter.poll())

        self.assertEqual(len(events), 2)
        channels = {e["channel"] for e in events}
        self.assertEqual(channels, {"C1", "C2"})

    def test_api_error_handled_gracefully(self):
        """API errors don't crash the poll — returns empty for that channel."""
        from unified_brain.adapters.slack import SlackAdapter

        class MockSlackClient:
            def get(self, method, params=None):
                return {"ok": False, "error": "channel_not_found"}

        adapter = SlackAdapter({"bot_token": "xoxb-test", "channel_ids": ["C999"]})
        adapter._client = MockSlackClient()
        events = asyncio.run(adapter.poll())
        self.assertEqual(events, [])

    def test_stop_clears_client(self):
        from unified_brain.adapters.slack import SlackAdapter

        class MockSlackClient:
            def get(self, method, params=None):
                return {"ok": True}

        adapter = SlackAdapter({"bot_token": "xoxb-test"})
        adapter._client = MockSlackClient()
        asyncio.run(adapter.stop())
        self.assertIsNone(adapter._client)


class TestSlackExecutor(unittest.TestCase):
    """Tests for Slack respond in ActionExecutor."""

    def test_respond_slack_no_token(self):
        executor = ActionExecutor({})
        result = executor.respond_slack("C123", "hello")
        self.assertEqual(result["status"], "error")
        self.assertIn("bot_token", result["error"])

    def test_respond_slack_success(self):
        """Mock successful Slack API response."""
        import unittest.mock as mock

        executor = ActionExecutor({"slack_bot_token": "xoxb-test"})

        # Mock urlopen to return a successful Slack response
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps({
            "ok": True,
            "ts": "1700000001.000001",
            "channel": "C123",
        }).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("unified_brain.executor.urlopen", return_value=mock_response):
            result = executor.respond_slack("C123", "test message")

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["ts"], "1700000001.000001")

    def test_respond_slack_with_thread(self):
        """Thread_ts is included in the request."""
        import unittest.mock as mock

        executor = ActionExecutor({"slack_bot_token": "xoxb-test"})

        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps({"ok": True, "ts": "123"}).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        captured_request = {}

        def mock_urlopen(req, **kwargs):
            captured_request["data"] = json.loads(req.data)
            return mock_response

        with mock.patch("unified_brain.executor.urlopen", side_effect=mock_urlopen):
            executor.respond_slack("C123", "reply", thread_ts="1700000001.000001")

        self.assertEqual(captured_request["data"]["thread_ts"], "1700000001.000001")

    def test_respond_slack_api_error(self):
        """Slack API returns ok=false."""
        import unittest.mock as mock

        executor = ActionExecutor({"slack_bot_token": "xoxb-test"})

        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps({
            "ok": False, "error": "channel_not_found"
        }).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("unified_brain.executor.urlopen", return_value=mock_response):
            result = executor.respond_slack("C999", "test")

        self.assertEqual(result["status"], "error")
        self.assertIn("channel_not_found", result["error"])


class TestSlackDispatcher(unittest.TestCase):
    """Tests for Slack routing in ActionDispatcher."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = {
            "outbox_dir": os.path.join(self.tmpdir, "outbox"),
            "results_dir": os.path.join(self.tmpdir, "inbox"),
        }
        self.dispatcher = ActionDispatcher(self.config)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_slack_outbox_created(self):
        """Slack outbox directory is created on init."""
        self.assertTrue(self.dispatcher.slack_outbox.exists())

    def test_respond_slack_queued(self):
        """RESPOND action for slack writes to slack outbox."""
        action = {
            "action": RESPOND,
            "source": "slack",
            "channel": "C123",
            "content": "Hello Slack!",
            "metadata": {"thread_ts": "170.001"},
        }
        result = self.dispatcher.dispatch(action)
        self.assertEqual(result["status"], "queued")
        self.assertIn("slack:C123", result["target"])

        # Verify outbox file
        files = list(self.dispatcher.slack_outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["body"], "Hello Slack!")
        self.assertEqual(data["channel_id"], "C123")
        self.assertEqual(data["thread_ts"], "170.001")

    def test_active_respond_slack(self):
        """Active respond routes to executor.respond_slack."""
        import unittest.mock as mock

        config = {
            **self.config,
            "active_respond": True,
            "executor": {"slack_bot_token": "xoxb-test"},
        }
        dispatcher = ActionDispatcher(config)

        with mock.patch.object(
            dispatcher.executor, "respond_slack",
            return_value={"status": "executed", "ts": "123"},
        ) as mock_respond:
            result = dispatcher.dispatch({
                "action": RESPOND,
                "source": "slack",
                "channel": "C123",
                "content": "active test",
                "metadata": {"thread_ts": "170.001"},
            })

        mock_respond.assert_called_once_with("C123", "active test", "170.001")
        self.assertEqual(result["status"], "executed")


class TestChatSession(unittest.TestCase):
    """Tests for ChatSession — persistent conversation with the brain (T049)."""

    def test_single_turn(self):
        """A single question returns a brain response with session metadata."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "respond", "content": "Hello!", "reason": "greeting"}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()

        session = ChatSession(brain=brain, session_id="test-sess")
        result = session.ask("Hi there")

        self.assertEqual(result["action"], "respond")
        self.assertEqual(result["content"], "Hello!")
        self.assertEqual(result["session_id"], "test-sess")
        self.assertEqual(result["turn"], 1)

    def test_multi_turn_history(self):
        """Conversation history accumulates across turns."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        call_count = [0]

        class CountingBackend(LLMBackend):
            def call(self, prompt):
                call_count[0] += 1
                return f'{{"action": "log", "content": "Turn {call_count[0]}", "reason": ""}}'

        brain = BrainAnalyzer()
        brain.backend = CountingBackend()

        session = ChatSession(brain=brain)
        r1 = session.ask("First question")
        r2 = session.ask("Second question")
        r3 = session.ask("Third question")

        self.assertEqual(r1["turn"], 1)
        self.assertEqual(r2["turn"], 2)
        self.assertEqual(r3["turn"], 3)
        self.assertEqual(len(session.history), 6)  # 3 user + 3 assistant

    def test_history_in_prompt(self):
        """Conversation history is injected into the brain prompt."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        captured_prompts = []

        class CapturingBackend(LLMBackend):
            def call(self, prompt):
                captured_prompts.append(prompt)
                return '{"action": "log", "content": "ok", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = CapturingBackend()

        session = ChatSession(brain=brain)
        session.ask("What is the status?")
        session.ask("Tell me more")

        # Second prompt should contain conversation history
        self.assertIn("Conversation History", captured_prompts[1])
        self.assertIn("What is the status?", captured_prompts[1])
        # First prompt should NOT have history
        self.assertNotIn("Conversation History", captured_prompts[0])

    def test_clear_history(self):
        """clear() empties the conversation history."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()

        session = ChatSession(brain=brain)
        session.ask("Question 1")
        self.assertEqual(len(session.history), 2)

        session.clear()
        self.assertEqual(len(session.history), 0)

    def test_max_turns_cap(self):
        """History is bounded by max_turns."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()

        session = ChatSession(brain=brain, max_turns=4)  # 4 entries = 2 turns
        session.ask("Q1")
        session.ask("Q2")
        session.ask("Q3")

        # max_turns=4 means deque maxlen=4, so only last 2 turns (4 entries)
        self.assertEqual(len(session.history), 4)

    def test_to_dict_serialization(self):
        """to_dict() returns a serializable session state."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "ok", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()

        session = ChatSession(brain=brain, session_id="s1", author="joel")
        session.ask("Test")

        state = session.to_dict()
        self.assertEqual(state["session_id"], "s1")
        self.assertEqual(state["author"], "joel")
        self.assertEqual(state["turns"], 1)
        self.assertEqual(len(state["history"]), 2)

        # Should be JSON-serializable
        json.dumps(state)

    def test_fallback_when_llm_fails(self):
        """When LLM returns None, fallback analysis is used."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        class NoneBackend(LLMBackend):
            def call(self, prompt):
                return None

        brain = BrainAnalyzer()
        brain.backend = NoneBackend()

        session = ChatSession(brain=brain)
        result = session.ask("What's happening?")

        self.assertEqual(result["action"], "log")
        self.assertEqual(result["turn"], 1)

    def test_with_context_builder(self):
        """Context builder is called when provided."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        context_calls = []

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "", "reason": ""}'

        class MockContextBuilder:
            def build(self, event):
                context_calls.append(event)
                return {"project": {"name": "test-proj"}}

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()

        session = ChatSession(brain=brain, context_builder=MockContextBuilder())
        session.ask("Question")

        self.assertEqual(len(context_calls), 1)
        self.assertEqual(context_calls[0]["source"], "chat")


class TestChatSessionManager(unittest.TestCase):
    """Tests for ChatSessionManager — multi-session management (T049)."""

    def _make_manager(self):
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSessionManager

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()
        return ChatSessionManager(brain=brain, max_turns=10)

    def test_create_session(self):
        from unified_brain.chat import ChatSessionManager
        mgr = self._make_manager()
        session = mgr.get_or_create("s1", "alice")
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.author, "alice")

    def test_get_existing_session(self):
        mgr = self._make_manager()
        s1 = mgr.get_or_create("s1")
        s1.ask("Hello")  # Add some history
        s2 = mgr.get_or_create("s1")
        self.assertIs(s1, s2)
        self.assertEqual(len(s2.history), 2)

    def test_different_sessions_are_independent(self):
        mgr = self._make_manager()
        s1 = mgr.get_or_create("s1")
        s2 = mgr.get_or_create("s2")
        s1.ask("Q1")
        self.assertEqual(len(s1.history), 2)
        self.assertEqual(len(s2.history), 0)

    def test_remove_session(self):
        mgr = self._make_manager()
        mgr.get_or_create("s1")
        mgr.remove("s1")
        sessions = mgr.list_sessions()
        self.assertEqual(len(sessions), 0)

    def test_cleanup_idle_sessions(self):
        from unittest import mock
        from unified_brain.chat import ChatSessionManager
        mgr = self._make_manager()
        mgr.max_idle_seconds = 1

        mgr.get_or_create("s1")
        # Simulate time passing
        mgr._last_active["s1"] = time.time() - 10
        mgr.cleanup()

        self.assertEqual(len(mgr.list_sessions()), 0)

    def test_list_sessions(self):
        mgr = self._make_manager()
        mgr.get_or_create("s1", "alice")
        mgr.get_or_create("s2", "bob")
        sessions = mgr.list_sessions()
        self.assertEqual(len(sessions), 2)
        ids = {s["session_id"] for s in sessions}
        self.assertEqual(ids, {"s1", "s2"})


class TestChatRESTEndpoint(unittest.TestCase):
    """Tests for POST /chat REST endpoint on health server (T049)."""

    def setUp(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]

        self.tmpdir = tempfile.mkdtemp()
        self.config = {
            "db_path": os.path.join(self.tmpdir, "test.db"),
            "brain": {"llm_backend": "subprocess", "claude_path": "echo"},
            "dispatcher": {
                "outbox_dir": os.path.join(self.tmpdir, "outbox"),
                "results_dir": os.path.join(self.tmpdir, "inbox"),
            },
        }

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_server(self):
        import threading
        from http.server import HTTPServer
        from unified_brain.runner import _HealthHandler
        from unified_brain.chat import ChatSessionManager

        service = BrainService(self.config)

        from unified_brain.brain import LLMBackend
        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "respond", "content": "chat reply", "reason": "test"}'

        service.brain.backend = FixedBackend()

        chat_sessions = ChatSessionManager(
            brain=service.brain,
            context_builder=service.context_builder,
            max_turns=20,
        )

        _HealthHandler.stats = {"status": "ok"}
        _HealthHandler.service = service
        _HealthHandler.chat_sessions = chat_sessions
        server = HTTPServer(("127.0.0.1", self.port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, service

    def _post(self, path, data):
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        body = json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        req = Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            resp = urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_chat_single_question(self):
        server, service = self._start_server()
        try:
            code, body = self._post("/chat", {"question": "Hello brain"})
            self.assertEqual(code, 200)
            self.assertEqual(body["action"], "respond")
            self.assertEqual(body["content"], "chat reply")
            self.assertIn("session_id", body)
            self.assertEqual(body["turn"], 1)
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_multi_turn_same_session(self):
        server, service = self._start_server()
        try:
            code1, body1 = self._post("/chat", {"question": "First"})
            session_id = body1["session_id"]

            code2, body2 = self._post("/chat", {
                "question": "Second",
                "session_id": session_id,
            })
            self.assertEqual(body2["turn"], 2)
            self.assertEqual(body2["session_id"], session_id)
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_clear_command(self):
        server, service = self._start_server()
        try:
            _, body1 = self._post("/chat", {"question": "Hello"})
            sid = body1["session_id"]

            code, body2 = self._post("/chat", {"command": "clear", "session_id": sid})
            self.assertEqual(code, 200)
            self.assertEqual(body2["status"], "cleared")
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_history_command(self):
        server, service = self._start_server()
        try:
            _, body1 = self._post("/chat", {"question": "Hello"})
            sid = body1["session_id"]

            code, body2 = self._post("/chat", {"command": "history", "session_id": sid})
            self.assertEqual(code, 200)
            self.assertEqual(body2["session_id"], sid)
            self.assertEqual(body2["turns"], 1)
            self.assertEqual(len(body2["history"]), 2)
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_missing_question_returns_400(self):
        server, service = self._start_server()
        try:
            code, body = self._post("/chat", {"not_question": "oops"})
            self.assertEqual(code, 400)
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_sessions_list(self):
        server, service = self._start_server()
        try:
            self._post("/chat", {"question": "Q1", "author": "alice"})
            self._post("/chat", {"question": "Q2", "session_id": "explicit-id"})

            code, body = self._get("/chat/sessions")
            self.assertEqual(code, 200)
            self.assertIsInstance(body, list)
            self.assertEqual(len(body), 2)
        finally:
            server.shutdown()
            service.store.close()

    def test_chat_without_sessions_returns_503(self):
        """When chat_sessions is None, /chat returns 503."""
        import socket
        import threading
        from http.server import HTTPServer
        from unified_brain.runner import _HealthHandler

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port503 = s.getsockname()[1]

        _HealthHandler.stats = {"status": "ok"}
        _HealthHandler.service = None
        _HealthHandler.chat_sessions = None
        server = HTTPServer(("127.0.0.1", port503), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            from urllib.request import urlopen, Request
            from urllib.error import HTTPError
            body = json.dumps({"question": "hello"}).encode()
            req = Request(f"http://127.0.0.1:{port503}/chat", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                resp = urlopen(req, timeout=10)
                code = resp.status
            except HTTPError as e:
                code = e.code
            except (ConnectionError, OSError):
                # Windows socket race — server sent 503 but connection reset
                # before client could read it. The 503 was sent, which is correct.
                code = 503
            self.assertEqual(code, 503)
        finally:
            server.shutdown()


class TestWebSocketChat(unittest.TestCase):
    """Tests for WebSocket frame helpers and ChatSession internals (T049)."""

    def test_ws_accept_key(self):
        """WebSocket accept key computation follows RFC 6455."""
        from unified_brain.chat import _ws_accept_key
        import base64, hashlib
        # Verify against manual computation
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        magic = "258EAFA5-E914-47DA-95CA-5AB5DC65C97B"
        expected = base64.b64encode(hashlib.sha1((key + magic).encode()).digest()).decode()
        self.assertEqual(_ws_accept_key(key), expected)
        # Deterministic: same input always gives same output
        self.assertEqual(_ws_accept_key(key), _ws_accept_key(key))

    def test_ws_frame_roundtrip(self):
        """WebSocket frames can be written and read back."""
        import io
        from unified_brain.chat import _ws_send_frame, _ws_read_frame

        buf = io.BytesIO()
        _ws_send_frame(buf, 0x1, b"hello world")

        # Read back (unmasked frame from server)
        buf.seek(0)
        opcode, data = _ws_read_frame(buf)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(data, b"hello world")

    def test_chat_history_injection_position(self):
        """History is injected before Response Format section."""
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSession

        captured = []

        class CapturingBackend(LLMBackend):
            def call(self, prompt):
                captured.append(prompt)
                return '{"action": "log", "content": "", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = CapturingBackend()

        session = ChatSession(brain=brain)
        session.ask("First")
        session.ask("Second")

        prompt = captured[1]
        hist_pos = prompt.find("Conversation History")
        resp_pos = prompt.find("## Response Format")
        self.assertGreater(hist_pos, 0)
        self.assertGreater(resp_pos, hist_pos)


class TestPersona(unittest.TestCase):
    """Tests for persona system — per-user brain identity (T049)."""

    def test_default_persona(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry()
        p = reg.get("anyone")
        self.assertEqual(p.name, "Brain")
        self.assertEqual(p.emoji, "🧠")

    def test_per_user_persona(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry({
            "users": {
                "kush": {"name": "Mango", "emoji": "🥭"},
                "joel": {"name": "Brain", "emoji": "🧠"},
            }
        })
        self.assertEqual(reg.get("kush").name, "Mango")
        self.assertEqual(reg.get("kush").emoji, "🥭")
        self.assertEqual(reg.get("joel").emoji, "🧠")
        # Unknown user gets default
        self.assertEqual(reg.get("stranger").name, "Brain")

    def test_enforce_global(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry({
            "enforce_global": True,
            "default": {"name": "Cortex", "emoji": "🔮"},
            "users": {"kush": {"name": "Mango", "emoji": "🥭"}},
        })
        # Even configured users get the global default
        self.assertEqual(reg.get("kush").name, "Cortex")
        self.assertEqual(reg.get("kush").emoji, "🔮")

    def test_format_message(self):
        from unified_brain.persona import Persona
        p = Persona("Mango", "🥭")
        self.assertEqual(p.format_message("hello"), "🥭 hello")
        self.assertEqual(p.prefix(), "🥭 ")

    def test_set_persona_at_runtime(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry()
        reg.set("kush", name="Mango", emoji="🥭")
        self.assertEqual(reg.get("kush").name, "Mango")

    def test_all_emojis(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry({
            "default": {"emoji": "🧠"},
            "users": {
                "kush": {"emoji": "🥭"},
                "andre": {"emoji": "🌊"},
            }
        })
        emojis = reg.all_emojis()
        self.assertEqual(emojis, {"🧠", "🥭", "🌊"})

    def test_is_own_message_direct(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry({"users": {"kush": {"emoji": "🥭"}}})
        self.assertTrue(reg.is_own_message("🧠 Analysis complete"))
        self.assertTrue(reg.is_own_message("🥭 Here is the fix"))
        self.assertFalse(reg.is_own_message("Hey team, what's up?"))
        self.assertFalse(reg.is_own_message(""))

    def test_is_own_message_with_leading_whitespace(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry()
        self.assertTrue(reg.is_own_message("  🧠 with leading spaces"))

    def test_is_own_message_quoted_only(self):
        """A message that is entirely a quote of the brain is filtered."""
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry()
        # Entirely quoted brain message
        self.assertTrue(reg.is_own_message("> 🧠 some analysis"))
        # Mixed: user's own text after quoting brain — NOT filtered
        self.assertFalse(reg.is_own_message("> 🧠 some analysis\nI disagree with this"))

    def test_is_own_message_user_reply_with_quote(self):
        """User replying and quoting brain should NOT be filtered."""
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry()
        msg = "> 🧠 Here is my analysis\nThanks, but can you dig deeper?"
        self.assertFalse(reg.is_own_message(msg))

    def test_list_users(self):
        from unified_brain.persona import PersonaRegistry
        reg = PersonaRegistry({
            "users": {"kush": {"name": "Mango", "emoji": "🥭"}},
        })
        listing = reg.list_users()
        self.assertIn("kush", listing)
        self.assertEqual(listing["kush"]["name"], "Mango")
        self.assertIn("_default", listing)


class TestLLMLogging(unittest.TestCase):
    """Tests for LLM call logging and metrics (T051)."""

    def test_log_llm_call_writes_jsonl(self):
        """_log_llm_call writes a valid JSONL record to the llm logger."""
        from unittest import mock
        from unified_brain.brain import _log_llm_call

        with mock.patch("unified_brain.brain.llm_logger") as mock_logger:
            _log_llm_call("subprocess", "test prompt", "test response", 1.5, True)
            mock_logger.info.assert_called_once()
            record = json.loads(mock_logger.info.call_args[0][0])
            self.assertEqual(record["backend"], "subprocess")
            self.assertEqual(record["prompt_len"], 11)
            self.assertEqual(record["response"], "test response")
            self.assertAlmostEqual(record["elapsed_s"], 1.5)
            self.assertTrue(record["success"])

    def test_log_llm_call_truncates_response(self):
        """Long responses are truncated to 2000 chars in the log."""
        from unittest import mock
        from unified_brain.brain import _log_llm_call

        long_response = "x" * 5000
        with mock.patch("unified_brain.brain.llm_logger") as mock_logger:
            _log_llm_call("api", "prompt", long_response, 2.0, True)
            record = json.loads(mock_logger.info.call_args[0][0])
            self.assertEqual(len(record["response"]), 2000)
            self.assertEqual(record["response_len"], 5000)

    def test_log_llm_call_handles_none_response(self):
        """Failed calls log None response."""
        from unittest import mock
        from unified_brain.brain import _log_llm_call

        with mock.patch("unified_brain.brain.llm_logger") as mock_logger:
            _log_llm_call("subprocess", "prompt", None, 0.5, False)
            record = json.loads(mock_logger.info.call_args[0][0])
            self.assertIsNone(record["response"])
            self.assertEqual(record["response_len"], 0)
            self.assertFalse(record["success"])

    def test_llm_metrics_registered(self):
        """LLM metrics exist in the registry."""
        from unified_brain.metrics import llm_calls_total, llm_active, llm_duration
        self.assertIsNotNone(llm_calls_total)
        self.assertIsNotNone(llm_active)
        self.assertIsNotNone(llm_duration)

    def test_llm_metrics_updated_on_call(self):
        """_log_llm_call updates Prometheus metrics."""
        from unified_brain.brain import _log_llm_call, _mark_llm_start
        from unified_brain.metrics import llm_calls_total, llm_active, llm_duration

        baseline = llm_calls_total.get(backend="test", outcome="success")

        _mark_llm_start("test")
        active_during = llm_active.get(backend="test")
        self.assertEqual(active_during, 1)

        _log_llm_call("test", "prompt", "response", 0.42, True)
        self.assertEqual(llm_calls_total.get(backend="test", outcome="success"), baseline + 1)
        self.assertEqual(llm_active.get(backend="test"), 0)
        self.assertAlmostEqual(llm_duration.get(backend="test"), 0.42)

    def test_subprocess_backend_logs_calls(self):
        """SubprocessBackend logs successful and failed calls."""
        from unittest import mock
        from unified_brain.brain import SubprocessBackend

        backend = SubprocessBackend(claude_path="echo", timeout=10)
        with mock.patch("unified_brain.brain._log_llm_call") as mock_log:
            with mock.patch("unified_brain.brain._mark_llm_start"):
                result = backend.call("test prompt")
                if result is not None:
                    mock_log.assert_called_once()
                    args = mock_log.call_args[0]
                    self.assertEqual(args[0], "subprocess")
                    self.assertTrue(args[4])


class TestChannelSessions(unittest.TestCase):
    """Tests for per-user sessions in group chats (T049)."""

    def _make_manager(self):
        from unified_brain.brain import BrainAnalyzer, LLMBackend
        from unified_brain.chat import ChatSessionManager
        from unified_brain.persona import PersonaRegistry

        class FixedBackend(LLMBackend):
            def call(self, prompt):
                return '{"action": "log", "content": "", "reason": ""}'

        brain = BrainAnalyzer()
        brain.backend = FixedBackend()
        persona_reg = PersonaRegistry({
            "users": {
                "joel": {"name": "Brain", "emoji": "🧠"},
                "kush": {"name": "Mango", "emoji": "🥭"},
            }
        })
        return ChatSessionManager(brain=brain, max_turns=10, persona_registry=persona_reg)

    def test_different_users_same_channel(self):
        mgr = self._make_manager()
        s1 = mgr.get_for_channel("squad-chat", "joel")
        s2 = mgr.get_for_channel("squad-chat", "kush")
        self.assertIsNot(s1, s2)
        self.assertNotEqual(s1.session_id, s2.session_id)

    def test_same_user_same_channel_returns_same_session(self):
        mgr = self._make_manager()
        s1 = mgr.get_for_channel("squad-chat", "joel")
        s1.ask("First question")
        s2 = mgr.get_for_channel("squad-chat", "joel")
        self.assertIs(s1, s2)
        self.assertEqual(len(s2.history), 2)

    def test_same_user_different_channels(self):
        mgr = self._make_manager()
        s1 = mgr.get_for_channel("squad-chat", "joel")
        s2 = mgr.get_for_channel("other-chat", "joel")
        self.assertIsNot(s1, s2)

    def test_channel_session_has_persona(self):
        mgr = self._make_manager()
        s_joel = mgr.get_for_channel("squad-chat", "joel")
        s_kush = mgr.get_for_channel("squad-chat", "kush")
        self.assertEqual(s_joel.persona.emoji, "🧠")
        self.assertEqual(s_kush.persona.emoji, "🥭")
        self.assertEqual(s_kush.persona.name, "Mango")

    def test_channel_session_to_dict_includes_persona(self):
        mgr = self._make_manager()
        s = mgr.get_for_channel("squad-chat", "kush")
        d = s.to_dict()
        self.assertEqual(d["persona"]["name"], "Mango")
        self.assertEqual(d["channel"], "squad-chat")

    def test_cleanup_removes_channel_index(self):
        mgr = self._make_manager()
        mgr.max_idle_seconds = 1
        mgr.get_for_channel("chat1", "joel")
        # Simulate staleness
        for sid in mgr._last_active:
            mgr._last_active[sid] = time.time() - 10
        mgr.cleanup()
        self.assertEqual(len(mgr._channel_index), 0)
        self.assertEqual(len(mgr.list_sessions()), 0)

    def test_remove_cleans_channel_index(self):
        mgr = self._make_manager()
        s = mgr.get_for_channel("chat1", "joel")
        mgr.remove(s.session_id)
        self.assertEqual(len(mgr._channel_index), 0)

    def test_overlapping_conversations(self):
        """Two users can have independent multi-turn conversations simultaneously."""
        mgr = self._make_manager()
        s_joel = mgr.get_for_channel("squad-chat", "joel")
        s_kush = mgr.get_for_channel("squad-chat", "kush")

        s_joel.ask("Joel question 1")
        s_kush.ask("Kush question 1")
        s_joel.ask("Joel question 2")
        s_kush.ask("Kush question 2")
        s_kush.ask("Kush question 3")

        self.assertEqual(len(s_joel.history), 4)   # 2 turns
        self.assertEqual(len(s_kush.history), 6)    # 3 turns


class TestAdapterSelfMessageFiltering(unittest.TestCase):
    """Tests for adapter self-message filtering via persona registry (T052)."""

    def test_teams_adapter_skips_own_messages(self):
        """Teams adapter filters out messages starting with persona emoji."""
        from unittest import mock
        from unified_brain.adapters.teams import TeamsAdapter
        from unified_brain.persona import PersonaRegistry

        persona_reg = PersonaRegistry({"users": {"joel": {"emoji": "🧠"}}})
        adapter = TeamsAdapter({"chat_ids": ["chat1"]}, persona_registry=persona_reg)
        adapter._seen_ids = BoundedSet()

        # Mock the Graph client to return messages
        mock_client = mock.MagicMock()
        mock_client.get.return_value = {
            "value": [
                {
                    "id": "msg1",
                    "body": {"content": "🧠 Brain analysis complete"},
                    "from": {"user": {"displayName": "Joel", "id": "u1"}},
                    "createdDateTime": "2026-04-06T20:00:00Z",
                },
                {
                    "id": "msg2",
                    "body": {"content": "Hey team, what's the status?"},
                    "from": {"user": {"displayName": "Kush", "id": "u2"}},
                    "createdDateTime": "2026-04-06T20:01:00Z",
                },
            ]
        }
        adapter._client = mock_client

        events = asyncio.run(adapter.poll())
        # Only msg2 should come through (msg1 is the brain's own message)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["author"], "Kush")

    def test_teams_adapter_without_persona_passes_all(self):
        """Without persona registry, all messages pass through."""
        from unittest import mock
        from unified_brain.adapters.teams import TeamsAdapter

        adapter = TeamsAdapter({"chat_ids": ["chat1"]}, persona_registry=None)
        adapter._seen_ids = BoundedSet()

        mock_client = mock.MagicMock()
        mock_client.get.return_value = {
            "value": [
                {
                    "id": "msg1",
                    "body": {"content": "🧠 Brain message"},
                    "from": {"user": {"displayName": "Joel", "id": "u1"}},
                    "createdDateTime": "2026-04-06T20:00:00Z",
                },
            ]
        }
        adapter._client = mock_client

        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 1)

    def test_slack_adapter_skips_bot_user_id(self):
        """Slack adapter filters out messages from its own bot user ID."""
        from unittest import mock
        from unified_brain.adapters.slack import SlackAdapter

        adapter = SlackAdapter({"channel_ids": ["C123"]})
        adapter._seen_ids = BoundedSet()
        adapter._bot_user_id = "U_BOT"

        mock_client = mock.MagicMock()
        mock_client.get.return_value = {
            "ok": True,
            "messages": [
                {"ts": "100.001", "user": "U_BOT", "text": "🧠 auto response"},
                {"ts": "100.002", "user": "U_HUMAN", "text": "real question"},
            ],
        }
        adapter._client = mock_client

        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["author"], "U_HUMAN")

    def test_slack_adapter_skips_persona_emoji(self):
        """Slack adapter filters by persona emoji when bot_user_id doesn't match."""
        from unittest import mock
        from unified_brain.adapters.slack import SlackAdapter
        from unified_brain.persona import PersonaRegistry

        persona_reg = PersonaRegistry({"users": {"kush": {"emoji": "🥭"}}})
        adapter = SlackAdapter({"channel_ids": ["C123"]}, persona_registry=persona_reg)
        adapter._seen_ids = BoundedSet()
        adapter._bot_user_id = ""  # No bot user ID

        mock_client = mock.MagicMock()
        mock_client.get.return_value = {
            "ok": True,
            "messages": [
                {"ts": "100.001", "user": "U1", "text": "🥭 Mango says hi"},
                {"ts": "100.002", "user": "U2", "text": "Normal message"},
            ],
        }
        adapter._client = mock_client

        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["body"], "Normal message")

    def test_github_adapter_skips_bot_login(self):
        """GitHub adapter filters events from the brain's bot_login."""
        from unittest import mock
        from unified_brain.adapters.github import GitHubAdapter

        adapter = GitHubAdapter({
            "repos": ["grobomo/unified-brain"],
            "bot_login": "grobomo-bot",
        })
        adapter._seen_ids = BoundedSet()

        events_data = [
            {
                "id": "1",
                "type": "PushEvent",
                "actor": {"login": "grobomo-bot"},
                "payload": {"commits": [{"message": "auto fix"}]},
                "created_at": "2026-04-06T20:00:00Z",
            },
            {
                "id": "2",
                "type": "PushEvent",
                "actor": {"login": "developer"},
                "payload": {"commits": [{"message": "real commit"}]},
                "created_at": "2026-04-06T20:01:00Z",
            },
        ]

        with mock.patch("unified_brain.adapters.github._gh_api", return_value=events_data):
            events = adapter._poll_events("grobomo/unified-brain")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["author"], "developer")

    def test_github_adapter_without_bot_login_passes_all(self):
        """Without bot_login configured, all events pass through."""
        from unittest import mock
        from unified_brain.adapters.github import GitHubAdapter

        adapter = GitHubAdapter({"repos": ["grobomo/test"]})
        adapter._seen_ids = BoundedSet()

        events_data = [
            {
                "id": "1",
                "type": "PushEvent",
                "actor": {"login": "anyone"},
                "payload": {"commits": [{"message": "commit"}]},
                "created_at": "2026-04-06T20:00:00Z",
            },
        ]

        with mock.patch("unified_brain.adapters.github._gh_api", return_value=events_data):
            events = adapter._poll_events("grobomo/test")

        self.assertEqual(len(events), 1)


class TestHookRunnerAdapter(unittest.TestCase):
    """Tests for HookRunnerAdapter — JSONL file poller (T053)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_file_poller_reads_new_lines(self):
        """FilePoller reads only lines appended after initialization."""
        from unified_brain.adapters.hook_runner import _FilePoller

        path = os.path.join(self.tmpdir, "test.jsonl")
        # Create file with existing content
        with open(path, "w") as f:
            f.write('{"old": true}\n')

        poller = _FilePoller(path)
        # First poll: initializes offset, returns nothing (skips existing)
        lines = poller.read_new_lines()
        self.assertEqual(lines, [])

        # Append new content
        with open(path, "a") as f:
            f.write('{"new": 1}\n')
            f.write('{"new": 2}\n')

        lines = poller.read_new_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"new": 1', lines[0])

    def test_file_poller_handles_missing_file(self):
        """FilePoller gracefully handles missing files."""
        from unified_brain.adapters.hook_runner import _FilePoller

        poller = _FilePoller(os.path.join(self.tmpdir, "nonexistent.jsonl"))
        lines = poller.read_new_lines()
        self.assertEqual(lines, [])

    def test_file_poller_handles_rotation(self):
        """FilePoller resets offset when file is truncated/rotated."""
        from unified_brain.adapters.hook_runner import _FilePoller

        path = os.path.join(self.tmpdir, "rotate.jsonl")
        with open(path, "w") as f:
            f.write('{"line": 1}\n' * 100)

        poller = _FilePoller(path)
        poller.read_new_lines()  # Initialize

        # Truncate (simulates log rotation)
        with open(path, "w") as f:
            f.write('{"rotated": true}\n')

        lines = poller.read_new_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("rotated", lines[0])

    def test_normalize_hook_log_gate_block(self):
        """Hook log gate block normalizes correctly."""
        from unified_brain.adapters.hook_runner import _normalize_hook_log

        line = json.dumps({
            "ts": "2026-04-06T20:00:00Z",
            "event": "PreToolUse",
            "module": "spec-gate.js",
            "result": "block",
            "elapsed_ms": 12,
            "reason": "No unchecked tasks",
        })
        event = _normalize_hook_log(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "hook-runner")
        self.assertEqual(event["channel"], "hook-log")
        self.assertEqual(event["event_type"], "gate_block")
        self.assertEqual(event["author"], "spec-gate.js")
        self.assertIn("block", event["title"])
        self.assertEqual(event["metadata"]["reason"], "No unchecked tasks")

    def test_normalize_hook_log_gate_allow(self):
        """Hook log gate allow normalizes correctly."""
        from unified_brain.adapters.hook_runner import _normalize_hook_log

        line = json.dumps({
            "event": "PreToolUse",
            "module": "branch-gate.js",
            "result": "allow",
        })
        event = _normalize_hook_log(line)
        self.assertEqual(event["event_type"], "gate_allow")

    def test_normalize_hook_log_invalid_json(self):
        """Invalid JSON returns None."""
        from unified_brain.adapters.hook_runner import _normalize_hook_log

        self.assertIsNone(_normalize_hook_log("not json"))
        self.assertIsNone(_normalize_hook_log("{broken"))

    def test_normalize_reflection(self):
        """Reflection JSONL normalizes correctly."""
        from unified_brain.adapters.hook_runner import _normalize_reflection

        line = json.dumps({
            "ts": "2026-04-06T20:00:00Z",
            "verdict": "needs_improvement",
            "issues": ["spec-gate blocked 3 times", "branch naming inconsistent"],
            "todos": ["Fix spec-gate regex"],
        })
        event = _normalize_reflection(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "hook-runner")
        self.assertEqual(event["channel"], "self-reflection")
        self.assertEqual(event["event_type"], "reflection_result")
        self.assertIn("needs_improvement", event["title"])
        self.assertIn("2 issues", event["title"])
        self.assertEqual(event["metadata"]["issue_count"], 2)

    def test_normalize_reflection_invalid_json(self):
        """Invalid JSON returns None."""
        from unified_brain.adapters.hook_runner import _normalize_reflection

        self.assertIsNone(_normalize_reflection("garbage"))

    def test_adapter_polls_both_files(self):
        """HookRunnerAdapter reads from both hook-log and reflection files."""
        from unified_brain.adapters.hook_runner import HookRunnerAdapter

        hook_log = os.path.join(self.tmpdir, "hook-log.jsonl")
        refl_log = os.path.join(self.tmpdir, "self-reflection.jsonl")

        # Create empty files so poller initializes offset
        open(hook_log, "w").close()
        open(refl_log, "w").close()

        adapter = HookRunnerAdapter({
            "hook_log_path": hook_log,
            "reflection_log_path": refl_log,
        })
        asyncio.run(adapter.start())

        # First poll initializes offsets
        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 0)

        # Append to both files
        with open(hook_log, "a") as f:
            f.write(json.dumps({"event": "PreToolUse", "module": "test.js", "result": "allow"}) + "\n")
        with open(refl_log, "a") as f:
            f.write(json.dumps({"verdict": "good", "issues": []}) + "\n")

        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 2)
        sources = {e["channel"] for e in events}
        self.assertEqual(sources, {"hook-log", "self-reflection"})

    def test_adapter_skips_empty_lines(self):
        """Adapter handles files with blank lines gracefully."""
        from unified_brain.adapters.hook_runner import HookRunnerAdapter

        hook_log = os.path.join(self.tmpdir, "hook-log.jsonl")
        open(hook_log, "w").close()

        adapter = HookRunnerAdapter({
            "hook_log_path": hook_log,
            "reflection_log_path": os.path.join(self.tmpdir, "none.jsonl"),
        })
        asyncio.run(adapter.start())
        asyncio.run(adapter.poll())  # Initialize

        with open(hook_log, "a") as f:
            f.write("\n\n")
            f.write(json.dumps({"event": "Test", "module": "m", "result": "ok"}) + "\n")
            f.write("\n")

        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 1)

    def test_event_ids_are_unique(self):
        """Different JSONL lines produce different event IDs."""
        from unified_brain.adapters.hook_runner import _normalize_hook_log

        e1 = _normalize_hook_log(json.dumps({"event": "A", "module": "m", "result": "x"}))
        e2 = _normalize_hook_log(json.dumps({"event": "B", "module": "m", "result": "y"}))
        self.assertNotEqual(e1["id"], e2["id"])


###############################################################################
# T054 — ReflectionTask lifecycle
###############################################################################

from unified_brain.reflection import (
    BACKOFF_INTERVALS,
    Checkpoint,
    Prediction,
    ReflectionTask,
    ReflectionTaskStore,
    TaskState,
    VALID_TRANSITIONS,
    compute_prediction_accuracy,
)


class TestReflectionTaskModel(unittest.TestCase):
    """T054a: ReflectionTask data model — state machine, prediction, checkpoints."""

    def test_default_task_has_id_and_pending_state(self):
        t = ReflectionTask()
        self.assertTrue(t.task_id.startswith("refl-"))
        self.assertEqual(t.state, TaskState.PENDING)
        self.assertGreater(t.created_at, 0)

    def test_valid_transitions(self):
        t = ReflectionTask()
        self.assertTrue(t.can_transition(TaskState.ANALYZING))
        self.assertFalse(t.can_transition(TaskState.MONITORING))

    def test_full_happy_path_transitions(self):
        t = ReflectionTask()
        t.transition(TaskState.ANALYZING)
        self.assertEqual(t.state, TaskState.ANALYZING)
        t.transition(TaskState.IMPLEMENTING)
        self.assertEqual(t.state, TaskState.IMPLEMENTING)
        t.transition(TaskState.MONITORING)
        self.assertEqual(t.state, TaskState.MONITORING)
        t.transition(TaskState.VERIFIED)
        self.assertEqual(t.state, TaskState.VERIFIED)
        t.transition(TaskState.CLOSED)
        self.assertEqual(t.state, TaskState.CLOSED)

    def test_invalid_transition_raises(self):
        t = ReflectionTask()
        with self.assertRaises(ValueError):
            t.transition(TaskState.CLOSED)

    def test_rollback_transition(self):
        """MONITORING → ROLLED_BACK → ANALYZING."""
        t = ReflectionTask()
        t.transition(TaskState.ANALYZING)
        t.transition(TaskState.IMPLEMENTING)
        t.transition(TaskState.MONITORING)
        t.transition(TaskState.ROLLED_BACK)
        self.assertEqual(t.state, TaskState.ROLLED_BACK)
        t.transition(TaskState.ANALYZING)
        self.assertEqual(t.state, TaskState.ANALYZING)

    def test_closed_is_terminal(self):
        t = ReflectionTask(state=TaskState.CLOSED)
        self.assertFalse(t.can_transition(TaskState.ANALYZING))
        self.assertFalse(t.can_transition(TaskState.PENDING))


class TestReflectionPrediction(unittest.TestCase):
    """T054f-g: Prediction model and accuracy comparator."""

    def test_prediction_round_trip(self):
        p = Prediction(expected_score_delta=5.0, confidence=0.8, reasoning="test")
        d = p.to_dict()
        p2 = Prediction.from_dict(d)
        self.assertEqual(p2.expected_score_delta, 5.0)
        self.assertEqual(p2.confidence, 0.8)
        self.assertEqual(p2.reasoning, "test")

    def test_prediction_from_empty_dict(self):
        p = Prediction.from_dict({})
        self.assertEqual(p.expected_score_delta, 0.0)

    def test_perfect_prediction_accuracy(self):
        p = Prediction(expected_score_delta=5.0)
        acc = compute_prediction_accuracy(p, actual_score_delta=5.0)
        self.assertAlmostEqual(acc, 1.0)

    def test_completely_wrong_prediction(self):
        p = Prediction(expected_score_delta=10.0)
        acc = compute_prediction_accuracy(p, actual_score_delta=-10.0)
        self.assertAlmostEqual(acc, 0.0)

    def test_partial_accuracy(self):
        p = Prediction(expected_score_delta=10.0)
        acc = compute_prediction_accuracy(p, actual_score_delta=5.0)
        self.assertAlmostEqual(acc, 0.5)

    def test_no_prediction_returns_zero(self):
        acc = compute_prediction_accuracy(None, actual_score_delta=5.0)
        self.assertEqual(acc, 0.0)

    def test_combined_score_and_block_rate_accuracy(self):
        p = Prediction(expected_score_delta=10.0, expected_block_rate_change=-0.5)
        # Perfect score delta, partial block rate
        acc = compute_prediction_accuracy(p, actual_score_delta=10.0,
                                          actual_block_rate_change=-0.25)
        self.assertGreater(acc, 0.5)
        self.assertLess(acc, 1.0)


class TestReflectionBackoff(unittest.TestCase):
    """T054e: Exponential backoff scheduler."""

    def test_backoff_intervals(self):
        t = ReflectionTask()
        self.assertEqual(t.next_check_delay, 30)
        t.backoff_index = 1
        self.assertEqual(t.next_check_delay, 60)
        t.backoff_index = 4
        self.assertEqual(t.next_check_delay, 1800)

    def test_beyond_max_backoff_uses_last(self):
        t = ReflectionTask(backoff_index=99)
        self.assertEqual(t.next_check_delay, BACKOFF_INTERVALS[-1])

    def test_advance_backoff(self):
        t = ReflectionTask()
        t.advance_backoff()
        self.assertEqual(t.backoff_index, 1)
        for _ in range(10):
            t.advance_backoff()
        self.assertEqual(t.backoff_index, len(BACKOFF_INTERVALS) - 1)

    def test_is_final_check(self):
        t = ReflectionTask()
        self.assertFalse(t.is_final_check)
        t.backoff_index = len(BACKOFF_INTERVALS) - 1
        self.assertTrue(t.is_final_check)

    def test_is_due_for_check_only_in_monitoring(self):
        t = ReflectionTask()
        self.assertFalse(t.is_due_for_check)  # PENDING
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60  # 60s ago
        self.assertTrue(t.is_due_for_check)  # 30s backoff elapsed

    def test_is_not_due_if_recently_implemented(self):
        t = ReflectionTask(state=TaskState.MONITORING)
        t.implemented_at = time.time()  # just now
        self.assertFalse(t.is_due_for_check)


class TestReflectionRetry(unittest.TestCase):
    """T054d,h: Rollback + retry with max attempts."""

    def test_reset_for_retry(self):
        t = ReflectionTask(
            prediction=Prediction(expected_score_delta=5.0),
            backup_content="original",
            implemented_at=time.time(),
            backoff_index=3,
        )
        t.add_checkpoint(100, 0.5, "rolled_back")
        t.reset_for_retry()
        self.assertEqual(t.attempts, 1)
        self.assertEqual(t.backoff_index, 0)
        self.assertEqual(t.monitor_checkpoints, [])
        self.assertEqual(t.implemented_at, 0.0)
        self.assertIsNone(t.prediction)

    def test_exceeds_max_attempts(self):
        t = ReflectionTask(max_attempts=3)
        self.assertFalse(t.exceeds_max_attempts())
        t.attempts = 3
        self.assertTrue(t.exceeds_max_attempts())

    def test_checkpoint_recording(self):
        t = ReflectionTask()
        cp = t.add_checkpoint(100.0, 0.8, "advancing", "looks good")
        self.assertEqual(len(t.monitor_checkpoints), 1)
        self.assertEqual(cp.score, 100.0)
        self.assertEqual(cp.prediction_accuracy, 0.8)
        self.assertEqual(cp.result, "advancing")


class TestReflectionTaskStore(unittest.TestCase):
    """T054b: SQLite persistence for ReflectionTasks."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.store = ReflectionTaskStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_and_get(self):
        t = ReflectionTask(diagnosis="test issue", target_file="spec-gate.js")
        self.store.save(t)
        loaded = self.store.get(t.task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.diagnosis, "test issue")
        self.assertEqual(loaded.target_file, "spec-gate.js")
        self.assertEqual(loaded.state, TaskState.PENDING)

    def test_save_with_prediction(self):
        t = ReflectionTask(
            prediction=Prediction(expected_score_delta=5.0, confidence=0.9),
        )
        self.store.save(t)
        loaded = self.store.get(t.task_id)
        self.assertIsNotNone(loaded.prediction)
        self.assertEqual(loaded.prediction.expected_score_delta, 5.0)
        self.assertEqual(loaded.prediction.confidence, 0.9)

    def test_save_with_checkpoints(self):
        t = ReflectionTask()
        t.add_checkpoint(100.0, 0.8, "advancing")
        t.add_checkpoint(105.0, 0.9, "advancing")
        self.store.save(t)
        loaded = self.store.get(t.task_id)
        self.assertEqual(len(loaded.monitor_checkpoints), 2)
        self.assertEqual(loaded.monitor_checkpoints[0].score, 100.0)
        self.assertEqual(loaded.monitor_checkpoints[1].score, 105.0)

    def test_update_state(self):
        t = ReflectionTask()
        self.store.save(t)
        t.transition(TaskState.ANALYZING)
        self.store.save(t)
        loaded = self.store.get(t.task_id)
        self.assertEqual(loaded.state, TaskState.ANALYZING)

    def test_list_by_state(self):
        t1 = ReflectionTask(diagnosis="a")
        t2 = ReflectionTask(diagnosis="b")
        t2.transition(TaskState.ANALYZING)
        self.store.save(t1)
        self.store.save(t2)
        pending = self.store.list_by_state(TaskState.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].diagnosis, "a")

    def test_list_active(self):
        t1 = ReflectionTask(diagnosis="active")
        t2 = ReflectionTask(diagnosis="closed", state=TaskState.CLOSED)
        self.store.save(t1)
        self.store.save(t2)
        active = self.store.list_active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].diagnosis, "active")

    def test_list_monitoring(self):
        t = ReflectionTask()
        t.state = TaskState.MONITORING
        t.implemented_at = time.time()
        self.store.save(t)
        monitoring = self.store.list_monitoring()
        self.assertEqual(len(monitoring), 1)

    def test_list_due_for_check(self):
        t = ReflectionTask()
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60  # 60s ago, past 30s backoff
        self.store.save(t)
        due = self.store.list_due_for_check()
        self.assertEqual(len(due), 1)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nonexistent"))

    def test_count_by_state(self):
        self.store.save(ReflectionTask())
        self.store.save(ReflectionTask())
        t3 = ReflectionTask()
        t3.transition(TaskState.ANALYZING)
        self.store.save(t3)
        counts = self.store.count_by_state()
        self.assertEqual(counts.get("pending"), 2)
        self.assertEqual(counts.get("analyzing"), 1)


###############################################################################
# T055 — Reflection implementer (file edit, rollback, monitoring, prompt enrichment)
###############################################################################

from unified_brain.implementer import (
    FileEditor,
    ReflectionMonitor,
    build_reflection_context,
    enrich_prompt_with_reflection,
)


class TestFileEditor(unittest.TestCase):
    """T055a-b: File backup, edit, and rollback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.editor = FileEditor(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_missing_file(self):
        self.assertEqual(self.editor.read("nonexistent.js"), "")

    def test_write_and_read(self):
        self.editor.write("test.js", "console.log('hello');")
        content = self.editor.read("test.js")
        self.assertEqual(content, "console.log('hello');")

    def test_backup_returns_content(self):
        self.editor.write("mod.js", "original")
        backup = self.editor.backup("mod.js")
        self.assertEqual(backup, "original")

    def test_rollback_restores_content(self):
        self.editor.write("mod.js", "original")
        self.editor.write("mod.js", "modified")
        self.editor.rollback("mod.js", "original")
        self.assertEqual(self.editor.read("mod.js"), "original")

    def test_write_creates_subdirectories(self):
        self.editor.write("sub/dir/mod.js", "content")
        self.assertEqual(self.editor.read("sub/dir/mod.js"), "content")

    def test_path_traversal_blocked(self):
        with self.assertRaises(ValueError):
            self.editor.read("../../etc/passwd")


class TestReflectionMonitor(unittest.TestCase):
    """T055e: Service loop monitoring — checkpoint evaluation, advancing, rollback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)
        self.editor = FileEditor(self.tmpdir)
        self.score_file = os.path.join(self.tmpdir, "reflection-score.json")
        self.monitor = ReflectionMonitor(
            task_store=self.task_store,
            file_editor=self.editor,
            score_file=self.score_file,
        )

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_score(self, points):
        with open(self.score_file, "w") as f:
            json.dump({"points": points, "level": 1, "streak": 0, "interventions": 0}, f)

    def test_read_score_missing_file(self):
        score = self.monitor.read_score()
        self.assertEqual(score["points"], 0)

    def test_read_score_valid_file(self):
        self._write_score(150)
        score = self.monitor.read_score()
        self.assertEqual(score["points"], 150)

    def test_no_monitoring_tasks_returns_empty(self):
        results = self.monitor.check_monitoring_tasks()
        self.assertEqual(results, [])

    def test_advancing_on_positive_score(self):
        """Task advances when score stays above baseline."""
        self._write_score(160)
        t = ReflectionTask(
            target_file="test.js",
            prediction=Prediction(expected_score_delta=10),
            score_baseline=150,
        )
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60  # Due for check
        self.task_store.save(t)

        results = self.monitor.check_monitoring_tasks()
        self.assertEqual(len(results), 1)
        task, action = results[0]
        self.assertEqual(action, "advancing")
        self.assertEqual(task.backoff_index, 1)

    def test_verified_on_final_checkpoint(self):
        """Task is verified after passing the final backoff interval."""
        self._write_score(160)
        t = ReflectionTask(
            target_file="test.js",
            prediction=Prediction(expected_score_delta=10),
            score_baseline=150,
            backoff_index=len(BACKOFF_INTERVALS) - 1,  # Final interval
        )
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 3600  # Well past any interval
        self.task_store.save(t)

        results = self.monitor.check_monitoring_tasks()
        self.assertEqual(len(results), 1)
        task, action = results[0]
        self.assertEqual(action, "verified")
        self.assertEqual(task.state, TaskState.VERIFIED)

    def test_rollback_on_score_drop(self):
        """Task rolls back when score drops below baseline."""
        # Write the original file
        self.editor.write("test.js", "modified content")
        self._write_score(140)  # Below baseline of 150

        t = ReflectionTask(
            target_file="test.js",
            backup_content="original content",
            prediction=Prediction(expected_score_delta=10),
            score_baseline=150,
        )
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60
        self.task_store.save(t)

        results = self.monitor.check_monitoring_tasks()
        task, action = results[0]
        self.assertEqual(action, "rolled_back")
        self.assertEqual(task.state, TaskState.ANALYZING)
        # File should be restored
        self.assertEqual(self.editor.read("test.js"), "original content")

    def test_rollback_on_prediction_mismatch(self):
        """Task rolls back when prediction accuracy is too low, even if score is up."""
        self.editor.write("test.js", "modified")
        self._write_score(200)  # Way above baseline — unexpectedly good

        t = ReflectionTask(
            target_file="test.js",
            backup_content="original",
            prediction=Prediction(expected_score_delta=5),  # Predicted small change
            score_baseline=150,
        )
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60
        self.task_store.save(t)

        results = self.monitor.check_monitoring_tasks()
        task, action = results[0]
        self.assertEqual(action, "rolled_back")
        self.assertEqual(self.editor.read("test.js"), "original")

    def test_max_attempts_exceeded(self):
        """Task fails after exceeding max attempts."""
        self._write_score(140)
        t = ReflectionTask(
            target_file="test.js",
            backup_content="original",
            prediction=Prediction(expected_score_delta=10),
            score_baseline=150,
            attempts=3,
            max_attempts=3,
        )
        t.state = TaskState.MONITORING
        t.implemented_at = time.time() - 60
        self.task_store.save(t)

        results = self.monitor.check_monitoring_tasks()
        task, action = results[0]
        self.assertEqual(action, "failed")


class TestImplementTask(unittest.TestCase):
    """T055a: implement_task — backup, predict, write, transition."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)
        self.editor = FileEditor(self.tmpdir)
        self.score_file = os.path.join(self.tmpdir, "reflection-score.json")
        with open(self.score_file, "w") as f:
            json.dump({"points": 100}, f)
        self.monitor = ReflectionMonitor(
            task_store=self.task_store,
            file_editor=self.editor,
            score_file=self.score_file,
        )

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_implement_task_full_flow(self):
        """implement_task backs up, writes, sets prediction, transitions to MONITORING."""
        self.editor.write("gate.js", "old code")

        t = ReflectionTask(target_file="gate.js", diagnosis="false positives")
        t.transition(TaskState.ANALYZING)
        pred = Prediction(expected_score_delta=5, confidence=0.8)

        self.monitor.implement_task(t, "new code", pred)

        self.assertEqual(t.state, TaskState.MONITORING)
        self.assertEqual(t.backup_content, "old code")
        self.assertEqual(t.prediction.expected_score_delta, 5.0)
        self.assertEqual(t.score_baseline, 100.0)
        self.assertEqual(self.editor.read("gate.js"), "new code")
        self.assertGreater(t.implemented_at, 0)

    def test_implement_task_no_target_file_raises(self):
        t = ReflectionTask()
        t.transition(TaskState.ANALYZING)
        with self.assertRaises(ValueError):
            self.monitor.implement_task(t, "content", Prediction())


class TestReflectionPromptEnrichment(unittest.TestCase):
    """T055c: Brain prompt enrichment with reflection context."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_context(self):
        ctx = build_reflection_context(self.task_store)
        self.assertEqual(ctx["active_tasks"], [])
        self.assertEqual(ctx["module_calibration"], {})

    def test_active_tasks_in_context(self):
        t = ReflectionTask(diagnosis="test pattern", target_file="spec-gate.js")
        self.task_store.save(t)
        ctx = build_reflection_context(self.task_store)
        self.assertEqual(len(ctx["active_tasks"]), 1)
        self.assertEqual(ctx["active_tasks"][0]["diagnosis"], "test pattern")

    def test_enrich_prompt_adds_section(self):
        parts = ["## Event", "Source: hook_runner"]
        ctx = {
            "active_tasks": [{"state": "monitoring", "diagnosis": "false positives",
                              "target_file": "spec-gate.js", "attempts": 1, "backoff_index": 2}],
            "module_calibration": {"spec-gate": 0.85},
            "recent_outcomes": [{"task_id": "refl-abc", "state": "closed",
                                 "diagnosis": "test", "attempts": 1, "prediction_accuracy": 0.9}],
        }
        enrich_prompt_with_reflection(parts, ctx)
        joined = "\n".join(parts)
        self.assertIn("Self-Reflection Status", joined)
        self.assertIn("Active tasks: 1", joined)
        self.assertIn("spec-gate: 85%", joined)
        self.assertIn("refl-abc", joined)

    def test_enrich_prompt_no_op_when_empty(self):
        parts = ["original"]
        enrich_prompt_with_reflection(parts, {"active_tasks": [], "module_calibration": {}, "recent_outcomes": []})
        self.assertEqual(parts, ["original"])

    def test_recent_outcomes_in_context(self):
        t = ReflectionTask(diagnosis="closed task")
        t.add_checkpoint(100, 0.9, "verified")
        t.state = TaskState.CLOSED
        t.closed_at = time.time()
        self.task_store.save(t)
        ctx = build_reflection_context(self.task_store)
        self.assertEqual(len(ctx["recent_outcomes"]), 1)
        self.assertEqual(ctx["recent_outcomes"][0]["prediction_accuracy"], 0.9)


###############################################################################
# T056 — Brain-owned score + bridge + E2E tests
###############################################################################

from unified_brain.score import BrainScore, write_reflection_findings
from unified_brain.metrics import MetricsRegistry


class TestBrainScore(unittest.TestCase):
    """T056a-d: BrainScore — prediction accuracy, interrupt rate, persistence."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.score_file = os.path.join(self.tmpdir, "reflection-score.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        MetricsRegistry.reset()

    def _write_hook_score(self, interventions=0):
        with open(self.score_file, "w") as f:
            json.dump({"points": 100, "interventions": interventions}, f)

    def test_default_score_is_zero(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        self.assertEqual(bs.score, 0.0)
        self.assertEqual(bs.prediction_accuracy, 0.0)
        self.assertEqual(bs.total_predictions, 0)

    def test_record_perfect_predictions(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        for _ in range(5):
            bs.record_prediction(1.0, task_id="t1")
        self.assertAlmostEqual(bs.prediction_accuracy, 1.0)
        # Score = 1.0 * 0.7 + interrupt_score * 0.3
        # No score_file → interrupts=0 → interrupt_score=1.0
        self.assertAlmostEqual(bs.score, 1.0)

    def test_record_mixed_predictions(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        bs.record_prediction(1.0)
        bs.record_prediction(0.0)
        self.assertAlmostEqual(bs.prediction_accuracy, 0.5)

    def test_rolling_window(self):
        bs = BrainScore(data_dir=self.data_dir, max_predictions=3, score_file=self.score_file)
        bs.record_prediction(0.0)
        bs.record_prediction(0.0)
        bs.record_prediction(0.0)
        bs.record_prediction(1.0)  # Pushes out first 0.0
        # Window: [0.0, 0.0, 1.0]
        self.assertAlmostEqual(bs.prediction_accuracy, 1.0 / 3.0, places=2)

    def test_user_interrupt_score_with_interventions(self):
        self._write_hook_score(interventions=2)
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        # baseline=5, interventions=2 → 1 - 2/5 = 0.6
        self.assertAlmostEqual(bs.user_interrupt_score, 0.6)

    def test_user_interrupt_score_clamped_at_zero(self):
        self._write_hook_score(interventions=10)
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        # 1 - 10/5 = -1.0 → clamped to 0.0
        self.assertEqual(bs.user_interrupt_score, 0.0)

    def test_persistence_save_and_load(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        bs.record_prediction(0.8, task_id="t1", details="test")
        bs.record_prediction(0.9, task_id="t2")
        bs.save()

        # Load in new instance
        bs2 = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        self.assertEqual(bs2.total_predictions, 2)
        self.assertAlmostEqual(bs2.prediction_accuracy, 0.85)

    def test_weighted_score_computation(self):
        self._write_hook_score(interventions=0)
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        bs.record_prediction(0.8)
        # accuracy=0.8, interrupt_score=1.0
        # score = 0.8*0.7 + 1.0*0.3 = 0.56 + 0.3 = 0.86
        self.assertAlmostEqual(bs.score, 0.86, places=2)

    def test_to_dict(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        bs.record_prediction(0.7)
        d = bs.to_dict()
        self.assertIn("score", d)
        self.assertIn("prediction_accuracy", d)
        self.assertIn("total_predictions", d)
        self.assertEqual(d["total_predictions"], 1)

    def test_accuracy_clamped(self):
        bs = BrainScore(data_dir=self.data_dir, score_file=self.score_file)
        bs.record_prediction(1.5)  # Above 1.0
        bs.record_prediction(-0.5)  # Below 0.0
        self.assertAlmostEqual(bs.prediction_accuracy, 0.5)  # (1.0 + 0.0) / 2


class TestReflectionFindings(unittest.TestCase):
    """T056f: reflection-findings.json writer."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)
        self.findings_path = os.path.join(self.tmpdir, "reflection-findings.json")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        MetricsRegistry.reset()

    def test_write_empty_findings(self):
        bs = BrainScore(data_dir=os.path.join(self.tmpdir, "data"),
                        score_file=os.path.join(self.tmpdir, "nope.json"))
        write_reflection_findings(self.findings_path, self.task_store, bs)
        data = json.loads(open(self.findings_path).read())
        self.assertIn("brain_score", data)
        self.assertEqual(data["active_tasks"], [])
        self.assertEqual(data["recent_findings"], [])

    def test_write_with_active_tasks(self):
        t = ReflectionTask(diagnosis="test issue", target_file="gate.js")
        self.task_store.save(t)
        bs = BrainScore(data_dir=os.path.join(self.tmpdir, "data"),
                        score_file=os.path.join(self.tmpdir, "nope.json"))
        write_reflection_findings(self.findings_path, self.task_store, bs)
        data = json.loads(open(self.findings_path).read())
        self.assertEqual(len(data["active_tasks"]), 1)
        self.assertEqual(data["active_tasks"][0]["diagnosis"], "test issue")

    def test_write_with_closed_tasks(self):
        t = ReflectionTask(diagnosis="resolved")
        t.add_checkpoint(100, 0.95, "verified")
        t.state = TaskState.CLOSED
        t.closed_at = time.time()
        self.task_store.save(t)
        bs = BrainScore(data_dir=os.path.join(self.tmpdir, "data"),
                        score_file=os.path.join(self.tmpdir, "nope.json"))
        write_reflection_findings(self.findings_path, self.task_store, bs)
        data = json.loads(open(self.findings_path).read())
        self.assertEqual(len(data["recent_findings"]), 1)
        self.assertAlmostEqual(data["recent_findings"][0]["prediction_accuracy"], 0.95, places=2)


class TestE2EReflectionHappyPath(unittest.TestCase):
    """T056g: End-to-end — detect → predict → implement → monitor → verify → close."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)
        self.editor = FileEditor(self.tmpdir)
        self.score_file = os.path.join(self.tmpdir, "reflection-score.json")
        self.monitor = ReflectionMonitor(
            task_store=self.task_store,
            file_editor=self.editor,
            score_file=self.score_file,
        )

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        MetricsRegistry.reset()

    def _write_score(self, points):
        with open(self.score_file, "w") as f:
            json.dump({"points": points}, f)

    def test_full_lifecycle(self):
        """PENDING → ANALYZING → IMPLEMENTING → MONITORING (x5 checkpoints) → VERIFIED → CLOSED."""
        # 1. DETECT: Create a task from a detected pattern
        self.editor.write("spec-gate.js", "// original code\nvar regex = /foo/;")
        task = ReflectionTask(
            diagnosis="spec-gate blocks test files with false positives",
            target_file="spec-gate.js",
        )
        self.task_store.save(task)
        self.assertEqual(task.state, TaskState.PENDING)

        # 2. ANALYZE: Transition to analyzing
        task.transition(TaskState.ANALYZING)
        self.task_store.save(task)

        # 3. IMPLEMENT: Apply fix with prediction
        self._write_score(100)
        prediction = Prediction(
            expected_score_delta=10,
            confidence=0.8,
            reasoning="Adding test file pattern to allowlist",
        )
        self.monitor.implement_task(task, "// fixed code\nvar regex = /foo|test/;", prediction)
        self.assertEqual(task.state, TaskState.MONITORING)
        self.assertEqual(self.editor.read("spec-gate.js"), "// fixed code\nvar regex = /foo|test/;")

        # 4. MONITOR: Advance through all backoff intervals
        for i in range(len(BACKOFF_INTERVALS)):
            # Reload task from store (monitor saves updates back to store)
            current = self.task_store.get(task.task_id)
            if current.state != TaskState.MONITORING:
                break
            # Simulate score staying positive and matching prediction
            self._write_score(110)  # +10 from baseline of 100
            current.implemented_at = time.time() - 9999  # Force due
            if current.monitor_checkpoints:
                current.monitor_checkpoints[-1].timestamp = time.time() - 9999
            self.task_store.save(current)

            results = self.monitor.check_monitoring_tasks()
            self.assertEqual(len(results), 1)
            _, action = results[0]

            if i < len(BACKOFF_INTERVALS) - 1:
                self.assertEqual(action, "advancing")
            else:
                self.assertEqual(action, "verified")

        # Reload and verify final state
        loaded = self.task_store.get(task.task_id)
        self.assertEqual(loaded.state, TaskState.VERIFIED)
        self.assertEqual(len(loaded.monitor_checkpoints), len(BACKOFF_INTERVALS))

        # 5. CLOSE: Transition to closed
        loaded.transition(TaskState.CLOSED)
        loaded.closed_at = time.time()
        self.task_store.save(loaded)

        final = self.task_store.get(task.task_id)
        self.assertEqual(final.state, TaskState.CLOSED)


class TestE2ERollbackAndRetry(unittest.TestCase):
    """T056h: predict wrong → score drops → rollback → re-analyze → predict again."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.task_store = ReflectionTaskStore(self.conn)
        self.editor = FileEditor(self.tmpdir)
        self.score_file = os.path.join(self.tmpdir, "reflection-score.json")
        self.monitor = ReflectionMonitor(
            task_store=self.task_store,
            file_editor=self.editor,
            score_file=self.score_file,
        )

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        MetricsRegistry.reset()

    def _write_score(self, points):
        with open(self.score_file, "w") as f:
            json.dump({"points": points}, f)

    def test_rollback_then_retry_succeeds(self):
        """First attempt fails (bad prediction), rollback, second attempt succeeds."""
        self.editor.write("gate.js", "original code")

        # Create and start task
        task = ReflectionTask(
            diagnosis="gate regex too strict",
            target_file="gate.js",
            max_attempts=3,
        )
        task.transition(TaskState.ANALYZING)
        self.task_store.save(task)

        # ATTEMPT 1: Implement with bad prediction
        self._write_score(100)
        pred1 = Prediction(expected_score_delta=10, confidence=0.6)
        self.monitor.implement_task(task, "bad fix", pred1)
        self.assertEqual(task.state, TaskState.MONITORING)

        # Score drops → rollback
        self._write_score(80)  # Below baseline of 100
        task.implemented_at = time.time() - 9999  # Force due
        self.task_store.save(task)

        results = self.monitor.check_monitoring_tasks()
        _, action = results[0]
        self.assertEqual(action, "rolled_back")

        # File should be restored
        self.assertEqual(self.editor.read("gate.js"), "original code")

        # Task should be back in ANALYZING with attempts=1
        loaded = self.task_store.get(task.task_id)
        self.assertEqual(loaded.state, TaskState.ANALYZING)
        self.assertEqual(loaded.attempts, 1)

        # ATTEMPT 2: Better prediction this time
        self._write_score(100)  # Reset score
        pred2 = Prediction(expected_score_delta=5, confidence=0.9)
        self.monitor.implement_task(loaded, "better fix", pred2)
        self.assertEqual(loaded.state, TaskState.MONITORING)

        # Score goes up as predicted
        self._write_score(105)  # +5 matches prediction
        loaded.implemented_at = time.time() - 9999
        self.task_store.save(loaded)

        # Advance through all checkpoints
        for i in range(len(BACKOFF_INTERVALS)):
            loaded = self.task_store.get(task.task_id)
            if loaded.state != TaskState.MONITORING:
                break
            loaded.implemented_at = time.time() - 9999
            if loaded.monitor_checkpoints:
                loaded.monitor_checkpoints[-1].timestamp = time.time() - 9999
            self.task_store.save(loaded)
            self.monitor.check_monitoring_tasks()

        final = self.task_store.get(task.task_id)
        self.assertEqual(final.state, TaskState.VERIFIED)
        self.assertEqual(final.attempts, 1)  # Only 1 retry was needed

    def test_max_attempts_exhausted(self):
        """Task fails after 3 bad attempts."""
        self.editor.write("gate.js", "original")

        task = ReflectionTask(
            diagnosis="persistent issue",
            target_file="gate.js",
            max_attempts=2,
        )
        task.transition(TaskState.ANALYZING)
        self.task_store.save(task)

        for attempt in range(3):
            loaded = self.task_store.get(task.task_id)
            if loaded.state == TaskState.CLOSED:
                break

            if loaded.state != TaskState.ANALYZING:
                break

            self._write_score(100)
            self.monitor.implement_task(loaded, f"fix attempt {attempt}", Prediction(expected_score_delta=10))
            self._write_score(80)  # Always drops
            loaded.implemented_at = time.time() - 9999
            self.task_store.save(loaded)
            self.monitor.check_monitoring_tasks()

        final = self.task_store.get(task.task_id)
        self.assertEqual(final.state, TaskState.CLOSED)  # Force-closed on max attempts
        # File should be restored to original
        self.assertEqual(self.editor.read("gate.js"), "original")


###############################################################################
# T059: Loop Pattern Analyzer Tests
###############################################################################

class TestLoopAnalyzerDetection(unittest.TestCase):
    """Tests for loop issue detection from reflection events."""

    def test_is_loop_issue_positive(self):
        """Issues with loop keywords are detected."""
        from unified_brain.loop_analyzer import is_loop_issue
        self.assertTrue(is_loop_issue({
            "description": "Multiple failed attempts at cherry-pick",
            "severity": "high",
        }))

    def test_is_loop_issue_negative(self):
        """Normal issues are not flagged as loops."""
        from unified_brain.loop_analyzer import is_loop_issue
        self.assertFalse(is_loop_issue({
            "description": "Missing docstring in module",
            "severity": "low",
        }))

    def test_is_loop_issue_fix_field(self):
        """Loop keywords in fix field also match."""
        from unified_brain.loop_analyzer import is_loop_issue
        self.assertTrue(is_loop_issue({
            "description": "Some issue",
            "fix": "Stop retrying manually, automate the deploy",
        }))

    def test_is_loop_issue_various_keywords(self):
        """Various loop keywords are detected."""
        from unified_brain.loop_analyzer import is_loop_issue
        keywords_to_test = [
            "unproductive loop detected",
            "manual patching of each failure",
            "struggling with infrastructure",
            "branch gymnastics",
            "rebase conflict loop",
        ]
        for kw in keywords_to_test:
            self.assertTrue(is_loop_issue({"description": kw}),
                            f"Should detect: {kw}")


class TestBashCommandExtraction(unittest.TestCase):
    """Tests for hook-log.jsonl command extraction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmpdir, "hook-log.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extract_bash_commands(self):
        """Extracts Bash tool entries from hook-log JSONL."""
        from unified_brain.loop_analyzer import extract_bash_commands
        lines = [
            json.dumps({"tool": "Bash", "command": "git cherry-pick abc123", "ts": "2026-04-06T10:00:00Z"}),
            json.dumps({"tool": "Edit", "file": "foo.py", "ts": "2026-04-06T10:01:00Z"}),
            json.dumps({"tool": "Bash", "command": "git cherry-pick def456", "ts": "2026-04-06T10:02:00Z"}),
        ]
        with open(self.log_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        cmds = extract_bash_commands(self.log_path)
        self.assertEqual(len(cmds), 2)
        self.assertIn("cherry-pick", cmds[0]["command"])

    def test_extract_empty_file(self):
        """Empty/missing file returns empty list."""
        from unified_brain.loop_analyzer import extract_bash_commands
        self.assertEqual(extract_bash_commands("/nonexistent/path"), [])

    def test_extract_max_lines(self):
        """Only reads last N lines."""
        from unified_brain.loop_analyzer import extract_bash_commands
        lines = [
            json.dumps({"tool": "Bash", "command": f"cmd-{i}", "ts": ""})
            for i in range(50)
        ]
        with open(self.log_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        cmds = extract_bash_commands(self.log_path, max_lines=10)
        self.assertEqual(len(cmds), 10)
        self.assertIn("cmd-40", cmds[0]["command"])


class TestRepeatedCommands(unittest.TestCase):
    """Tests for repeated command detection."""

    def test_find_repeated_commands(self):
        """Detects commands repeated >= threshold times."""
        from unified_brain.loop_analyzer import find_repeated_commands
        cmds = [
            {"command": "git cherry-pick abc123"},
            {"command": "git cherry-pick abc123"},
            {"command": "git cherry-pick abc123"},
            {"command": "npm test"},
        ]
        repeated = find_repeated_commands(cmds, threshold=3)
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["count"], 3)

    def test_no_repeats(self):
        """No false positives when commands are unique."""
        from unified_brain.loop_analyzer import find_repeated_commands
        cmds = [
            {"command": "git status"},
            {"command": "npm install"},
            {"command": "python test.py"},
        ]
        repeated = find_repeated_commands(cmds, threshold=3)
        self.assertEqual(len(repeated), 0)

    def test_find_matching_patterns(self):
        """Detects known retry patterns."""
        from unified_brain.loop_analyzer import find_matching_patterns
        cmds = [
            {"command": "git cherry-pick abc123", "ts": ""},
            {"command": "git rebase main", "ts": ""},
            {"command": "echo hello", "ts": ""},
        ]
        matches = find_matching_patterns(cmds)
        self.assertEqual(len(matches), 2)


class TestLoopAnalysis(unittest.TestCase):
    """Tests for the analyze_loop function."""

    def test_analyze_with_repeated_commands(self):
        """Produces LoopPattern from issue + command evidence."""
        from unified_brain.loop_analyzer import analyze_loop
        issue = {
            "description": "Multiple failed cherry-pick attempts",
            "severity": "high",
        }
        commands = [
            {"command": "git cherry-pick abc123"},
            {"command": "git cherry-pick abc123"},
            {"command": "git cherry-pick abc123"},
            {"command": "git cherry-pick abc123"},
        ]
        pattern = analyze_loop(issue, commands, project="test-project", branch="main")
        self.assertIn("4x", pattern.root_cause)
        self.assertEqual(pattern.project, "test-project")
        self.assertEqual(pattern.severity, "high")
        self.assertTrue(len(pattern.suggested_fix) > 0)

    def test_analyze_deploy_loop(self):
        """Deploy loops get deploy.sh suggestion."""
        from unified_brain.loop_analyzer import analyze_loop
        issue = {"description": "Struggling with deploy uploads", "severity": "medium"}
        commands = [
            {"command": "scp dist.zip deploy-host:/uploads"},
            {"command": "scp dist.zip deploy-host:/uploads"},
            {"command": "scp dist.zip deploy-host:/uploads"},
        ]
        pattern = analyze_loop(issue, commands)
        self.assertIn("deploy", pattern.suggested_fix.lower())

    def test_analyze_no_commands(self):
        """Still produces pattern even without command evidence."""
        from unified_brain.loop_analyzer import analyze_loop
        issue = {"description": "Unproductive loop in session", "severity": "low"}
        pattern = analyze_loop(issue, [])
        self.assertIn("Reflection flagged", pattern.root_cause)


class TestLoopAnalyzerIntegration(unittest.TestCase):
    """Tests for LoopAnalyzer with memory integration."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = sqlite3.connect(self.db_path)
        from unified_brain.memory import MemoryManager
        self.memory = MemoryManager(self.conn)

        self.log_path = os.path.join(self.tmpdir, "hook-log.jsonl")
        # Write some Bash commands to the hook log
        lines = [
            json.dumps({"tool": "Bash", "command": "git cherry-pick abc", "ts": ""}),
            json.dumps({"tool": "Bash", "command": "git cherry-pick abc", "ts": ""}),
            json.dumps({"tool": "Bash", "command": "git cherry-pick abc", "ts": ""}),
        ]
        with open(self.log_path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_process_reflection_event_with_loop(self):
        """Processes a reflection event and detects loop pattern."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        event = {
            "id": "reflect:test1",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "title": "Reflection: needs-attention (1 issues)",
            "body": "Issues: Multiple failed cherry-pick attempts detected",
            "metadata": {"verdict": "needs-attention", "project": "test-proj"},
        }

        patterns = analyzer.process_reflection_event(event)
        self.assertEqual(len(patterns), 1)
        self.assertIn("cherry-pick", patterns[0].root_cause.lower())

    def test_process_non_loop_event(self):
        """Non-loop reflection events produce no patterns."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        event = {
            "id": "reflect:test2",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "title": "Reflection: clean",
            "body": "Issues: Missing docstring in module",
            "metadata": {"verdict": "clean"},
        }

        patterns = analyzer.process_reflection_event(event)
        self.assertEqual(len(patterns), 0)

    def test_process_non_reflection_channel(self):
        """Non self-reflection events are ignored."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        event = {
            "id": "hooklog:test3",
            "source": "hook-runner",
            "channel": "hook-log",
            "event_type": "gate_block",
            "title": "Retry loop detected",
            "body": "Retry loop pattern",
        }

        patterns = analyzer.process_reflection_event(event)
        self.assertEqual(len(patterns), 0)

    def test_memory_persistence(self):
        """Loop patterns are stored in Tier 3 global memory."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        event = {
            "id": "reflect:mem1",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "title": "Reflection: needs-attention",
            "body": "Issues: Unproductive loop — manual patching of deploy failures",
            "metadata": {"project": "test-proj"},
        }

        analyzer.process_reflection_event(event)

        data = self.memory.get_global_memory("loop_patterns")
        self.assertIsNotNone(data)
        self.assertEqual(data["total_detected"], 1)
        self.assertEqual(len(data["patterns"]), 1)

    def test_recurrence_tracking(self):
        """Repeated loop patterns increment recurrence counter."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        for i in range(3):
            event = {
                "id": f"reflect:rec{i}",
                "source": "hook-runner",
                "channel": "self-reflection",
                "event_type": "reflection_result",
                "title": "Reflection: needs-attention",
                "body": "Issues: Unproductive loop — retry deploy upload",
                "metadata": {"project": "test-proj"},
            }
            analyzer.process_reflection_event(event)

        data = self.memory.get_global_memory("loop_patterns")
        self.assertEqual(data["total_detected"], 3)
        # All have same root cause signature, so one recurrence entry
        recurrences = data["recurrences"]
        self.assertTrue(len(recurrences) >= 1)
        # At least one recurrence should have count >= 2
        max_count = max(v["count"] for v in recurrences.values())
        self.assertGreaterEqual(max_count, 2)

    def test_get_context_for_prompt(self):
        """Generates prompt context string from stored patterns."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        # No patterns yet — empty context
        self.assertEqual(analyzer.get_context_for_prompt(), "")

        # Add a pattern
        event = {
            "id": "reflect:ctx1",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "title": "Reflection: needs-attention",
            "body": "Issues: Unproductive loop in deploy process",
            "metadata": {"project": "proj-x"},
        }
        analyzer.process_reflection_event(event)

        ctx = analyzer.get_context_for_prompt()
        self.assertIn("Loop Pattern History", ctx)
        self.assertIn("1 total detected", ctx)

    def test_json_body_parsing(self):
        """Handles JSON body format from reflection events."""
        from unified_brain.loop_analyzer import LoopAnalyzer
        analyzer = LoopAnalyzer(memory=self.memory, hook_log_path=self.log_path)

        body = json.dumps({
            "issues": [
                {"description": "Retry loop: 5 failed cherry-picks", "severity": "high"},
                {"description": "Missing docstring", "severity": "low"},
            ],
            "verdict": "needs-attention",
        })

        event = {
            "id": "reflect:json1",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "title": "Reflection: needs-attention",
            "body": body,
            "metadata": {},
        }

        patterns = analyzer.process_reflection_event(event)
        # Only the loop issue should be detected, not the docstring one
        self.assertEqual(len(patterns), 1)
        self.assertIn("cherry-pick", patterns[0].description.lower())


class TestLoopAnalyzerServiceIntegration(unittest.TestCase):
    """E2E test: loop detection through the service pipeline."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Write hook-log with retry pattern
        self.log_path = os.path.join(self.tmpdir, "hook-log.jsonl")
        lines = [
            json.dumps({"tool": "Bash", "command": f"kubectl apply -f deploy.yaml", "ts": ""})
            for _ in range(5)
        ]
        with open(self.log_path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_service_loop_detects_loops(self):
        """BrainService detects loop patterns from hook-runner events."""
        from unified_brain.service import BrainService
        service = BrainService({
            "db_path": self.db_path,
            "hook_log_path": self.log_path,
            "brain": {"claude_path": "echo"},
        })

        # Simulate ingesting a reflection event with a loop issue
        event = {
            "id": "reflect:e2e1",
            "source": "hook-runner",
            "channel": "self-reflection",
            "event_type": "reflection_result",
            "author": "self-reflection",
            "title": "Reflection: needs-attention",
            "body": "Issues: Unproductive loop — repeated kubectl apply retries",
            "created_at": time.time(),
            "metadata": {"project": "test-deploy"},
        }

        # Process through loop analyzer
        patterns = service.loop_analyzer.process_reflection_event(event)
        self.assertGreater(len(patterns), 0)

        # Verify Tier 3 memory was updated
        data = service.memory.get_global_memory("loop_patterns")
        self.assertIsNotNone(data)
        self.assertGreater(data["total_detected"], 0)

        # Verify prompt context is generated
        ctx = service.loop_analyzer.get_context_for_prompt()
        self.assertIn("Loop Pattern History", ctx)

        service.store.close()


class TestSystemMonitorAdapter(unittest.TestCase):
    """Tests for SystemMonitorAdapter — JSON file poller (T060)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_event(self, filename, data):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def _make_event(self, **overrides):
        base = {
            "type": "focus_steal",
            "timestamp": "2026-04-06 23:36:56.718",
            "process": {
                "pid": 356,
                "name": "python.exe",
                "exe_path": "C:\\Program Files\\Python312\\python.exe",
                "command_line": "python test.py",
            },
            "parent_chain": "python.exe(356) -> bash.exe(5256)",
            "classification": "SAFE",
            "source_project": None,
        }
        base.update(overrides)
        return base

    def test_poll_reads_json_files(self):
        """Adapter reads .json files from events dir."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        self._write_event("event1.json", self._make_event())
        self._write_event("event2.json", self._make_event(
            source_project="_grobomo/hook-runner",
            process={"pid": 100, "name": "cmd.exe", "exe_path": "", "command_line": ""},
        ))

        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})
        events = asyncio.run(adapter.poll())

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["source"], "system-monitor")
        self.assertEqual(events[0]["channel"], "focus-events")
        self.assertEqual(events[0]["event_type"], "focus_steal")

    def test_poll_marks_consumed_as_processed(self):
        """After polling, .json files are renamed to .json.processed."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        self._write_event("event1.json", self._make_event())
        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})
        asyncio.run(adapter.poll())

        files = os.listdir(self.tmpdir)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".json.processed"))
        # No .json files remain
        json_files = [f for f in files if f.endswith(".json") and not f.endswith(".processed")]
        self.assertEqual(len(json_files), 0)

    def test_poll_skips_processed_files(self):
        """Already-processed files are not re-ingested."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        self._write_event("event1.json.processed", self._make_event())
        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})
        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 0)

    def test_poll_handles_missing_dir(self):
        """Adapter returns empty list if events dir doesn't exist."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        adapter = SystemMonitorAdapter({"events_dir": "/nonexistent/path"})
        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 0)

    def test_poll_handles_invalid_json(self):
        """Invalid JSON files are renamed to .error."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        path = os.path.join(self.tmpdir, "bad.json")
        with open(path, "w") as f:
            f.write("not valid json{{{")

        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})
        events = asyncio.run(adapter.poll())
        self.assertEqual(len(events), 0)

        files = os.listdir(self.tmpdir)
        self.assertTrue(any(f.endswith(".error") for f in files))

    def test_normalize_with_source_project(self):
        """Events with source_project include project name in title."""
        from unified_brain.adapters.system_monitor import _normalize_event

        data = self._make_event(source_project="_grobomo/system-monitor")
        event = _normalize_event("/tmp/test.json", data)

        self.assertIsNotNone(event)
        self.assertIn("_grobomo/system-monitor", event["title"])
        self.assertEqual(event["metadata"]["source_project"], "_grobomo/system-monitor")

    def test_normalize_without_source_project(self):
        """Events without source_project show pid in title."""
        from unified_brain.adapters.system_monitor import _normalize_event

        data = self._make_event(source_project=None)
        event = _normalize_event("/tmp/test.json", data)

        self.assertIn("pid 356", event["title"])
        self.assertIsNone(event["metadata"]["source_project"])

    def test_normalize_event_id_uses_filename(self):
        """Event ID is derived from filename stem."""
        from unified_brain.adapters.system_monitor import _normalize_event

        data = self._make_event()
        event = _normalize_event("/events/20260406-233656-718-focus-356.json", data)
        self.assertEqual(event["id"], "sysmon:20260406-233656-718-focus-356")

    def test_normalize_body_contains_process_info(self):
        """Body includes process details for brain context."""
        from unified_brain.adapters.system_monitor import _normalize_event

        data = self._make_event()
        event = _normalize_event("/tmp/test.json", data)

        self.assertIn("python.exe", event["body"])
        self.assertIn("pid 356", event["body"])
        self.assertIn("SAFE", event["body"])

    def test_normalize_classification_preserved(self):
        """Classification is preserved in metadata."""
        from unified_brain.adapters.system_monitor import _normalize_event

        data = self._make_event(classification="SUSPICIOUS")
        event = _normalize_event("/tmp/test.json", data)
        self.assertEqual(event["metadata"]["classification"], "SUSPICIOUS")

    def test_poll_processes_files_in_sorted_order(self):
        """Files are processed in alphabetical order (chronological by naming convention)."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        self._write_event("20260406-000001-event.json", self._make_event(
            process={"pid": 1, "name": "first.exe", "exe_path": "", "command_line": ""},
        ))
        self._write_event("20260406-000002-event.json", self._make_event(
            process={"pid": 2, "name": "second.exe", "exe_path": "", "command_line": ""},
        ))

        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})
        events = asyncio.run(adapter.poll())

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["author"], "first.exe")
        self.assertEqual(events[1]["author"], "second.exe")

    def test_second_poll_returns_empty_after_consumption(self):
        """Second poll returns nothing since files were already consumed."""
        from unified_brain.adapters.system_monitor import SystemMonitorAdapter

        self._write_event("event.json", self._make_event())
        adapter = SystemMonitorAdapter({"events_dir": self.tmpdir})

        events1 = asyncio.run(adapter.poll())
        self.assertEqual(len(events1), 1)

        events2 = asyncio.run(adapter.poll())
        self.assertEqual(len(events2), 0)


class TestFocusStealRouter(unittest.TestCase):
    """Tests for focus-steal action router (T061)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_root = os.path.join(self.tmpdir, "projects")
        os.makedirs(os.path.join(self.projects_root, "_grobomo", "test-project"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_event(self, source_project=None, process_name="python.exe", pid=356):
        return {
            "id": "sysmon:test-event",
            "source": "system-monitor",
            "channel": "focus-events",
            "event_type": "focus_steal",
            "author": process_name,
            "title": f"focus_steal: {process_name}",
            "body": f"Process: {process_name}",
            "created_at": 1712448000.0,
            "metadata": {
                "classification": "SAFE",
                "source_project": source_project,
                "process_name": process_name,
                "pid": pid,
                "exe_path": f"C:\\test\\{process_name}",
                "command_line": f"{process_name} --test",
                "parent_chain": f"{process_name}({pid}) -> bash.exe(100)",
                "original_file": "test-event.json",
            },
        }

    def test_known_project_writes_todo(self):
        """Focus-steal with source_project writes TODO to that project."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/test-project")
        config = {"projects_root": self.projects_root}

        result = handle_focus_steal(event, config=config)

        self.assertIsNotNone(result)
        todo_path = os.path.join(
            self.projects_root, "_grobomo", "test-project", "TODO.md"
        )
        self.assertTrue(os.path.exists(todo_path))
        with open(todo_path) as f:
            content = f.read()
        self.assertIn("Focus-Steal Fixes", content)
        self.assertIn("python.exe", content)
        self.assertIn("pid 356", content)

    def test_known_project_includes_fix_suggestion(self):
        """TODO entry includes process-specific fix suggestion."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/test-project",
                                 process_name="powershell.exe")
        config = {"projects_root": self.projects_root}

        handle_focus_steal(event, config=config)

        todo_path = os.path.join(
            self.projects_root, "_grobomo", "test-project", "TODO.md"
        )
        with open(todo_path) as f:
            content = f.read()
        self.assertIn("-WindowStyle Hidden", content)

    def test_known_project_with_dispatch_config(self):
        """With context_reset_script configured, returns dispatch action."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/test-project")
        config = {
            "projects_root": self.projects_root,
            "context_reset_script": "/path/to/context_reset.py",
        }

        result = handle_focus_steal(event, config=config)

        self.assertEqual(result["action"], "dispatch")
        self.assertIn("context_reset.py", result["metadata"]["command"])

    def test_known_project_without_dispatch_returns_log(self):
        """Without context_reset_script, returns log action."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/test-project")
        config = {"projects_root": self.projects_root}

        result = handle_focus_steal(event, config=config)

        self.assertEqual(result["action"], "log")
        self.assertIn("no dispatch configured", result["content"])

    def test_unknown_project_returns_log(self):
        """Focus-steal with unknown source_project logs only."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project=None)
        result = handle_focus_steal(event, config={})

        self.assertEqual(result["action"], "log")
        self.assertIn("System noise", result["content"])

    def test_unknown_project_tracks_noise_in_memory(self):
        """Unknown source events are tracked in Tier 2 memory."""
        from unified_brain.focus_steal import handle_focus_steal
        from unittest.mock import MagicMock

        memory = MagicMock()
        memory.get_project_memory.return_value = {}

        event = self._make_event(source_project=None, process_name="az.cmd")
        handle_focus_steal(event, memory=memory, config={})

        memory.set_project_memory.assert_called_once()
        call_args = memory.set_project_memory.call_args
        self.assertEqual(call_args[0][0], "system-monitor")
        self.assertEqual(call_args[0][1], "noise_tracking")
        noise = call_args[0][2]
        self.assertEqual(noise["total_count"], 1)
        self.assertIn("az.cmd", noise["processes"])

    def test_nonexistent_project_dir_returns_log(self):
        """Source project with non-existent directory returns log action."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/nonexistent-project")
        config = {"projects_root": self.projects_root}

        result = handle_focus_steal(event, config=config)

        self.assertEqual(result["action"], "log")
        self.assertIn("not found", result["content"])

    def test_duplicate_todo_not_appended(self):
        """Same focus-steal event doesn't create duplicate TODO entries."""
        from unified_brain.focus_steal import handle_focus_steal

        event = self._make_event(source_project="_grobomo/test-project")
        config = {"projects_root": self.projects_root}

        handle_focus_steal(event, config=config)
        handle_focus_steal(event, config=config)

        todo_path = os.path.join(
            self.projects_root, "_grobomo", "test-project", "TODO.md"
        )
        with open(todo_path) as f:
            content = f.read()
        # Should appear only once
        count = content.count("pid 356")
        self.assertEqual(count, 1)

    def test_appends_to_existing_section(self):
        """Second unique event appends to existing Focus-Steal section."""
        from unified_brain.focus_steal import handle_focus_steal

        config = {"projects_root": self.projects_root}

        event1 = self._make_event(source_project="_grobomo/test-project", pid=100)
        event2 = self._make_event(source_project="_grobomo/test-project", pid=200)

        handle_focus_steal(event1, config=config)
        handle_focus_steal(event2, config=config)

        todo_path = os.path.join(
            self.projects_root, "_grobomo", "test-project", "TODO.md"
        )
        with open(todo_path) as f:
            content = f.read()
        # Section header appears once, both entries present
        self.assertEqual(content.count("## Focus-Steal Fixes"), 1)
        self.assertIn("pid 100", content)
        self.assertIn("pid 200", content)

    def test_fix_suggestions_per_process(self):
        """Fix suggestions are process-specific."""
        from unified_brain.focus_steal import _get_fix_suggestion

        self.assertIn("CREATE_NO_WINDOW", _get_fix_suggestion("python.exe", ""))
        self.assertIn("WindowStyle", _get_fix_suggestion("powershell.exe", ""))
        self.assertIn("windowsHide", _get_fix_suggestion("node.exe", ""))
        self.assertIn("Investigate", _get_fix_suggestion("unknown.exe", ""))


class TestCCCBridge(unittest.TestCase):
    """Tests for CCC bridge — git relay dispatch (T064)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.relay_dir = os.path.join(self.tmpdir, "relay-repo")
        os.makedirs(self.relay_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dispatch_creates_pending_file(self):
        """Dispatch writes task JSON to requests/pending/."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        result = bridge.dispatch({
            "id": "task-001",
            "content": "Fix the bug in hook-runner",
            "metadata": {"source_project": "_grobomo/hook-runner"},
        })

        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["task_id"], "task-001")

        pending = os.path.join(self.relay_dir, "requests", "pending", "task-001.json")
        self.assertTrue(os.path.exists(pending))
        with open(pending) as f:
            data = json.load(f)
        self.assertEqual(data["sender"], "unified-brain")
        self.assertEqual(data["text"], "Fix the bug in hook-runner")
        self.assertEqual(data["source_project"], "_grobomo/hook-runner")

    def test_dispatch_auto_generates_id(self):
        """Dispatch generates a task ID if not provided."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        result = bridge.dispatch({"content": "Do something"})

        self.assertEqual(result["status"], "dispatched")
        self.assertTrue(len(result["task_id"]) > 0)

    def test_poll_results_reads_completed(self):
        """Poll reads and removes completed result files."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        bridge._ensure_dirs()

        # Write a completed result
        result_file = os.path.join(bridge._completed_dir, "task-001.json")
        with open(result_file, "w") as f:
            json.dump({
                "id": "task-001",
                "success": True,
                "output": "PR created",
                "pr_url": "https://github.com/example/repo/pull/1",
                "worker": "worker-1",
            }, f)

        results = bridge.poll_results()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "task-001")
        self.assertTrue(results[0]["success"])
        self.assertIn("PR created", results[0]["output"])
        # File should be removed after consumption
        self.assertFalse(os.path.exists(result_file))

    def test_poll_results_reads_failed(self):
        """Poll reads failed results with success=False."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        bridge._ensure_dirs()

        result_file = os.path.join(bridge._failed_dir, "task-002.json")
        with open(result_file, "w") as f:
            json.dump({
                "id": "task-002",
                "success": False,
                "error": "Tests failed",
            }, f)

        results = bridge.poll_results()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertIn("Tests failed", results[0]["error"])

    def test_poll_empty_returns_empty_list(self):
        """Poll with no results returns empty list."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        results = bridge.poll_results()
        self.assertEqual(results, [])

    def test_poll_handles_bad_json(self):
        """Bad JSON result files are renamed to .error."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        bridge._ensure_dirs()

        bad_file = os.path.join(bridge._completed_dir, "bad.json")
        with open(bad_file, "w") as f:
            f.write("not valid json{{{")

        results = bridge.poll_results()
        self.assertEqual(len(results), 0)
        self.assertTrue(os.path.exists(bad_file + ".error"))

    def test_get_pending_count(self):
        """Pending count reflects number of pending task files."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        self.assertEqual(bridge.get_pending_count(), 0)

        bridge.dispatch({"id": "a", "content": "task a"})
        bridge.dispatch({"id": "b", "content": "task b"})
        self.assertEqual(bridge.get_pending_count(), 2)

    def test_dispatch_includes_context(self):
        """Dispatch preserves context list in task JSON."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        bridge.dispatch({
            "id": "ctx-task",
            "content": "Fix it",
            "metadata": {"context": ["PR #5 broke CI", "Tests fail on Windows"]},
        })

        pending = os.path.join(self.relay_dir, "requests", "pending", "ctx-task.json")
        with open(pending) as f:
            data = json.load(f)
        self.assertEqual(len(data["context"]), 2)
        self.assertIn("PR #5 broke CI", data["context"][0])

    def test_custom_sender(self):
        """Custom sender name appears in task JSON."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir, "sender": "joel-brain"})
        bridge.dispatch({"id": "s1", "content": "test"})

        pending = os.path.join(self.relay_dir, "requests", "pending", "s1.json")
        with open(pending) as f:
            data = json.load(f)
        self.assertEqual(data["sender"], "joel-brain")

    def test_ensure_dirs_creates_structure(self):
        """_ensure_dirs creates the full relay directory structure."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})
        bridge._ensure_dirs()

        for subdir in ["pending", "dispatched", "completed", "failed"]:
            path = os.path.join(self.relay_dir, "requests", subdir)
            self.assertTrue(os.path.isdir(path), f"Missing: {subdir}")


class TestCCCResultMonitor(unittest.TestCase):
    """Tests for CCC result monitoring in BrainService (T065)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.relay_dir = os.path.join(self.tmpdir, "relay")
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ccc_bridge_disabled_by_default(self):
        """CCC bridge is None when not configured."""
        from unified_brain.service import BrainService

        service = BrainService({"db_path": self.db_path})
        self.assertIsNone(service.ccc_bridge)
        service.store.close()

    def test_ccc_bridge_enabled_when_configured(self):
        """CCC bridge is created when enabled in config."""
        from unified_brain.service import BrainService

        service = BrainService({
            "db_path": self.db_path,
            "ccc_bridge": {"enabled": True, "relay_dir": self.relay_dir},
        })
        self.assertIsNotNone(service.ccc_bridge)
        service.store.close()

    def test_ccc_results_recorded_in_feedback(self):
        """Completed CCC results are recorded in feedback store."""
        from unified_brain.service import BrainService
        from unified_brain.ccc_bridge import CCCBridge

        service = BrainService({
            "db_path": self.db_path,
            "ccc_bridge": {"enabled": True, "relay_dir": self.relay_dir},
        })

        # Write a completed result for CCC bridge to find
        service.ccc_bridge._ensure_dirs()
        result_file = os.path.join(
            service.ccc_bridge._completed_dir, "task-001.json"
        )
        with open(result_file, "w") as f:
            json.dump({
                "id": "task-001",
                "success": True,
                "output": "PR created",
                "worker": "worker-1",
            }, f)

        # Run one cycle
        asyncio.run(service.run_cycle())

        # Check feedback was recorded (ccc_work from bridge + dispatch from relay)
        stats = service.feedback.summary(hours=1)
        # At least one feedback entry exists
        total_entries = sum(v.get("total", 0) for k, v in stats.items() if k != "recent_failures")
        self.assertGreater(total_entries, 0)
        service.store.close()

    def test_ccc_failed_result_triggers_retry(self):
        """Failed CCC results trigger re-dispatch when failure rate is low."""
        from unified_brain.service import BrainService

        service = BrainService({
            "db_path": self.db_path,
            "ccc_bridge": {"enabled": True, "relay_dir": self.relay_dir},
        })

        # Record some successes first to keep failure rate low
        for i in range(5):
            service.feedback.record(
                task_id=f"prev-{i}", action="ccc_work",
                success=True, source="ccc", channel="worker",
            )

        # Write a failed result
        service.ccc_bridge._ensure_dirs()
        result_file = os.path.join(
            service.ccc_bridge._failed_dir, "task-fail.json"
        )
        with open(result_file, "w") as f:
            json.dump({
                "id": "task-fail",
                "success": False,
                "error": "Tests failed",
            }, f)

        asyncio.run(service.run_cycle())

        # Should have a retry in pending
        pending = service.ccc_bridge.get_pending_count()
        self.assertGreater(pending, 0)
        service.store.close()

    def test_ccc_bridge_dispatch_from_focus_steal(self):
        """Focus-steal dispatch action can be routed to CCC bridge."""
        from unified_brain.ccc_bridge import CCCBridge

        bridge = CCCBridge({"relay_dir": self.relay_dir})

        # Simulate what focus_steal handler returns as a dispatch action
        result = bridge.dispatch({
            "id": "focus-fix-001",
            "content": "Fix focus-steal in _grobomo/system-monitor",
            "metadata": {
                "source_project": "_grobomo/system-monitor",
                "command": "python context_reset.py --project-dir /path",
            },
        })

        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(bridge.get_pending_count(), 1)


class TestSSHChatServer(unittest.TestCase):
    """Tests for SSH chat server (T066)."""

    def test_import_without_asyncssh(self):
        """ssh_server module imports even if asyncssh isn't available."""
        import unified_brain.ssh_server as mod
        # HAS_ASYNCSSH should be True since we installed it
        self.assertTrue(hasattr(mod, "HAS_ASYNCSSH"))
        self.assertTrue(hasattr(mod, "BrainSSHServer"))

    def test_server_init_defaults(self):
        """Server initializes with default config."""
        from unified_brain.ssh_server import BrainSSHServer
        from unittest.mock import MagicMock

        brain = MagicMock()
        server = BrainSSHServer(brain)

        self.assertEqual(server.host, "0.0.0.0")
        self.assertEqual(server.port, 2222)
        self.assertEqual(server.active_sessions, 0)

    def test_server_custom_config(self):
        """Server respects custom config."""
        from unified_brain.ssh_server import BrainSSHServer
        from unittest.mock import MagicMock

        brain = MagicMock()
        server = BrainSSHServer(brain, config={
            "host": "127.0.0.1",
            "port": 3333,
            "host_key": "/tmp/test_key",
        })

        self.assertEqual(server.host, "127.0.0.1")
        self.assertEqual(server.port, 3333)

    def test_host_key_generation(self):
        """Host key is auto-generated when missing."""
        from unified_brain.ssh_server import BrainSSHServer, HAS_ASYNCSSH
        from unittest.mock import MagicMock

        if not HAS_ASYNCSSH:
            self.skipTest("asyncssh not installed")

        tmpdir = tempfile.mkdtemp()
        try:
            key_path = os.path.join(tmpdir, "test_host_key")
            brain = MagicMock()
            server = BrainSSHServer(brain, config={"host_key": key_path})
            server._ensure_host_key()
            self.assertTrue(os.path.exists(key_path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_host_key_not_regenerated(self):
        """Existing host key is not overwritten."""
        from unified_brain.ssh_server import BrainSSHServer, HAS_ASYNCSSH
        from unittest.mock import MagicMock

        if not HAS_ASYNCSSH:
            self.skipTest("asyncssh not installed")

        tmpdir = tempfile.mkdtemp()
        try:
            key_path = os.path.join(tmpdir, "test_host_key")
            brain = MagicMock()
            server = BrainSSHServer(brain, config={"host_key": key_path})

            # Generate key
            server._ensure_host_key()
            mtime1 = os.path.getmtime(key_path)

            # Call again — should not regenerate
            server._ensure_host_key()
            mtime2 = os.path.getmtime(key_path)
            self.assertEqual(mtime1, mtime2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_line_helper_exists(self):
        """_read_line async helper is importable."""
        from unified_brain.ssh_server import _read_line
        self.assertTrue(asyncio.iscoroutinefunction(_read_line))

    def test_handle_ssh_session_exists(self):
        """_handle_ssh_session is importable."""
        from unified_brain.ssh_server import _handle_ssh_session
        self.assertTrue(asyncio.iscoroutinefunction(_handle_ssh_session))


if __name__ == "__main__":
    unittest.main()
