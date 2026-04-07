"""EventStore — unified SQLite+FTS storage for all channel events.

Single database, single schema. Source field distinguishes origin (github, teams, etc.).
Channel field = repo name or chat ID.

Heritage: github-agent/core/store.py, extended for multi-channel.
"""

import json
import sqlite3
import time
from pathlib import Path


class EventStore:
    """Unified event store backed by SQLite with FTS5 full-text search."""

    def __init__(self, db_path: str = "data/brain.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,       -- 'github', 'teams', 'webhook', etc.
                channel TEXT NOT NULL,       -- repo name, chat ID, etc.
                event_type TEXT NOT NULL,    -- 'issue', 'comment', 'message', 'pr', etc.
                author TEXT,
                title TEXT,
                body TEXT,
                metadata TEXT,              -- JSON blob for source-specific data
                created_at REAL NOT NULL,
                ingested_at REAL NOT NULL,
                processed INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
            CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel);
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed);
            CREATE INDEX IF NOT EXISTS idx_events_author ON events(author COLLATE NOCASE);

            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                title, body, author, channel,
                content='events',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, title, body, author, channel)
                VALUES (new.rowid, new.title, new.body, new.author, new.channel);
            END;

            CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, title, body, author, channel)
                VALUES ('delete', old.rowid, old.title, old.body, old.author, old.channel);
            END;
        """)

    def insert(self, event: dict) -> str:
        """Insert a normalized event. Returns the event ID."""
        event_id = event.get("id", f"{event['source']}-{int(time.time() * 1000)}")
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)

        self.conn.execute(
            """INSERT OR IGNORE INTO events
               (id, source, channel, event_type, author, title, body, metadata, created_at, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event["source"],
                event["channel"],
                event["event_type"],
                event.get("author"),
                event.get("title"),
                event.get("body"),
                metadata,
                event.get("created_at", time.time()),
                time.time(),
            ),
        )
        self.conn.commit()
        return event_id

    def get_unprocessed(self, limit: int = 50) -> list[dict]:
        """Return unprocessed events, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE processed = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, event_id: str):
        """Mark an event as processed."""
        self.conn.execute(
            "UPDATE events SET processed = 1 WHERE id = ?", (event_id,)
        )
        self.conn.commit()

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all events."""
        rows = self.conn.execute(
            """SELECT events.* FROM events_fts
               JOIN events ON events.rowid = events_fts.rowid
               WHERE events_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, hours: int = 24, source: str = None, channel: str = None,
               author: str = None, limit: int = 0) -> list[dict]:
        """Get recent events, optionally filtered by source/channel/author."""
        cutoff = time.time() - (hours * 3600)
        sql = "SELECT * FROM events WHERE created_at > ?"
        params: list = [cutoff]
        if source:
            sql += " AND source = ?"
            params.append(source)
        if channel:
            sql += " AND channel = ?"
            params.append(channel)
        if author:
            sql += " AND author = ? COLLATE NOCASE"
            params.append(author)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
