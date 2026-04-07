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
            # Third should be rate-limited
            resp3 = self._post(self.port, "/events", {"id": "rl-3", "source": "test", "title": "t"})
            # urlopen raises HTTPError for 429
            from urllib.error import HTTPError
            self.assertIsInstance(resp3, HTTPError)
            self.assertEqual(resp3.code, 429)
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


if __name__ == "__main__":
    unittest.main()
