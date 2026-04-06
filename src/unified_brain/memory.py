"""Unified three-tier memory — shared context across all channels.

Tier 1: Hot cache — raw events from the last 24h (already in EventStore.events table)
Tier 2: Project summaries — per-project JSON, updated after each brain cycle
Tier 3: Global patterns — account-level summary, updated periodically

The compactor promotes data up the tiers:
  T1 (raw events) -> T2 (project summaries) -> T3 (global patterns)
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


class MemoryManager:
    """Manages three-tier memory backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection, config: dict = None):
        self.conn = conn
        self.config = config or {}
        self.tier2_max_age_hours = self.config.get("tier2_max_age_hours", 168)  # 7 days
        self.tier3_max_age_hours = self.config.get("tier3_max_age_hours", 720)  # 30 days
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_project (
                project TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (project, key)
            );

            CREATE TABLE IF NOT EXISTS memory_global (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL NOT NULL
            );
        """)

    # -- Tier 2: Project memory --

    def get_project_memory(self, project: str) -> dict:
        """Get all memory entries for a project."""
        rows = self.conn.execute(
            "SELECT key, value, updated_at FROM memory_project WHERE project = ?",
            (project,),
        ).fetchall()
        result = {}
        for row in rows:
            try:
                result[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                result[row[0]] = row[1]
        return result

    def set_project_memory(self, project: str, key: str, value):
        """Set a memory entry for a project."""
        self.conn.execute(
            """INSERT OR REPLACE INTO memory_project (project, key, value, updated_at)
               VALUES (?, ?, ?, ?)""",
            (project, key, json.dumps(value, default=str), time.time()),
        )
        self.conn.commit()

    # -- Tier 3: Global memory --

    def get_global_memory(self, key: str = None) -> dict:
        """Get global memory entries. If key is None, returns all."""
        if key:
            row = self.conn.execute(
                "SELECT value FROM memory_global WHERE key = ?", (key,)
            ).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    return row[0]
            return None

        rows = self.conn.execute("SELECT key, value FROM memory_global").fetchall()
        result = {}
        for row in rows:
            try:
                result[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                result[row[0]] = row[1]
        return result

    def set_global_memory(self, key: str, value):
        """Set a global memory entry."""
        self.conn.execute(
            """INSERT OR REPLACE INTO memory_global (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (key, json.dumps(value, default=str), time.time()),
        )
        self.conn.commit()

    # -- Compaction --

    def compact_tier2(self, store, registry):
        """Promote Tier 1 events into Tier 2 project summaries.

        For each project in the registry, summarize recent events into
        per-project memory entries (event counts, active authors, key topics).
        """
        now = time.time()
        for project_name, project_data in registry.projects.items():
            events = []

            # Gather events from all project channels
            for repo in project_data.get("repos", []):
                events.extend(store.recent(
                    hours=self.tier2_max_age_hours, source="github", channel=repo
                ))
            for chat_id in project_data.get("teams_chats", []):
                events.extend(store.recent(
                    hours=self.tier2_max_age_hours, source="teams", channel=chat_id
                ))

            if not events:
                continue

            # Build summary
            authors = set()
            event_types = {}
            channels_active = set()
            for e in events:
                if e.get("author"):
                    authors.add(e["author"])
                et = e.get("event_type", "unknown")
                event_types[et] = event_types.get(et, 0) + 1
                channels_active.add(f"{e.get('source')}:{e.get('channel')}")

            summary = {
                "event_count": len(events),
                "authors": sorted(authors),
                "event_types": event_types,
                "channels_active": sorted(channels_active),
                "window_hours": self.tier2_max_age_hours,
                "compacted_at": now,
            }

            self.set_project_memory(project_name, "summary", summary)
            log.debug(f"[compact] T2 summary for {project_name}: {len(events)} events")

    def compact_tier3(self, registry):
        """Promote Tier 2 project summaries into Tier 3 global patterns."""
        now = time.time()
        total_events = 0
        all_authors = set()
        project_activity = {}

        for project_name in registry.projects:
            summary = self.get_project_memory(project_name).get("summary", {})
            if not summary:
                continue
            count = summary.get("event_count", 0)
            total_events += count
            all_authors.update(summary.get("authors", []))
            project_activity[project_name] = count

        if not project_activity:
            return

        global_summary = {
            "total_events": total_events,
            "active_projects": len(project_activity),
            "total_authors": len(all_authors),
            "project_activity": project_activity,
            "most_active": max(project_activity, key=project_activity.get) if project_activity else None,
            "compacted_at": now,
        }

        self.set_global_memory("summary", global_summary)
        log.debug(f"[compact] T3 global: {total_events} events across {len(project_activity)} projects")

    def get_context_for_project(self, project_name: str) -> dict:
        """Get memory context for brain prompt enrichment."""
        project_mem = self.get_project_memory(project_name)
        global_mem = self.get_global_memory("summary")
        return {
            "project_memory": project_mem,
            "global_memory": global_mem,
        }
