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

from unified_brain.adapters.base import BoundedSet, parse_timestamp
from unified_brain.runner import _deep_merge, _interpolate_env, _load_config
from unified_brain.store import EventStore
from unified_brain.brain import BrainAnalyzer, DISPATCH, LOG, RESPOND, ALERT
from unified_brain.context import ContextBuilder
from unified_brain.dispatcher import ActionDispatcher
from unified_brain.memory import MemoryManager
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


if __name__ == "__main__":
    unittest.main()
