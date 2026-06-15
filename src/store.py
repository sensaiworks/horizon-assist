"""
Structured note store (SQLite) — the local, queryable record of what the user
captured during assist sessions.

Nothing is written here unless the user explicitly captures a screen in a consented
session. ChromaDB (src/rag.py) handles *semantic* search ("anything about
deployments"); this store handles *exact* and *temporal* questions that vector
search is bad at — "everything John said between 9 and 11am", "the last 20 notes",
counts per speaker/channel. Both stores are keyed on the same MessageEvent.doc_id().

Every row carries the session_id it was captured in, so a session's data can be
purged as a unit — the default retention is session-only. purge_all() wipes
everything for the one-click "purge all stored data" control.

It is also the dedup gate: ingest() uses INSERT OR IGNORE on the doc_id primary key
and returns only the rows that were genuinely new, so re-capturing the same screen
within a session does not duplicate notes.

SQLite is opened with check_same_thread=False because the tray may read the stored
list on a different thread than the one that captured it; writes go through one
connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import MessageEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    doc_id           TEXT PRIMARY KEY,
    session_id       TEXT,            -- assist session this was captured in
    observed_at      TEXT NOT NULL,   -- ISO-8601 capture time (UTC)
    chat_time        TEXT,            -- on-screen timestamp, verbatim
    speaker          TEXT,
    message          TEXT,
    app              TEXT,
    channel          TEXT,
    window_title     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_observed_at ON events(observed_at);
CREATE INDEX IF NOT EXISTS idx_events_session    ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_speaker     ON events(speaker);
CREATE INDEX IF NOT EXISTS idx_events_channel     ON events(channel);
"""


class EventStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Defensive migration: a database created before session tagging lacks the
        # session_id column (CREATE TABLE IF NOT EXISTS won't add it). Add it so an
        # upgraded install keeps working instead of failing on insert.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
        if "session_id" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN session_id TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)"
            )
        self._conn.commit()

    def ingest(self, events: list[MessageEvent]) -> list[MessageEvent]:
        """Insert events, skipping any already stored (by doc_id).

        Returns the subset that were genuinely new — callers should embed and
        notify only on these.
        """
        assert self._conn is not None, "call connect() first"
        new: list[MessageEvent] = []
        for e in events:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO events
                   (doc_id, session_id, observed_at, chat_time, speaker, message,
                    app, channel, window_title)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.doc_id(),
                    e.session_id,
                    e.timestamp.isoformat(),
                    e.chat_time,
                    e.speaker,
                    e.message,
                    e.app,
                    e.channel,
                    e.window_title,
                ),
            )
            if cur.rowcount:
                new.append(e)
        self._conn.commit()
        return new

    def count(self) -> int:
        if self._conn is None:
            return 0
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def recent(self, limit: int = 20) -> list[dict]:
        """Most recently captured events, newest first."""
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY observed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def query_range(
        self,
        start_iso: str,
        end_iso: str,
        speaker: str | None = None,
        channel: str | None = None,
    ) -> list[dict]:
        """Events captured within [start_iso, end_iso), optionally filtered by
        speaker/channel (case-insensitive substring), oldest first."""
        assert self._conn is not None, "call connect() first"
        sql = "SELECT * FROM events WHERE observed_at >= ? AND observed_at < ?"
        params: list = [start_iso, end_iso]
        if speaker:
            sql += " AND lower(speaker) LIKE ?"
            params.append(f"%{speaker.lower()}%")
        if channel:
            sql += " AND lower(channel) LIKE ?"
            params.append(f"%{channel.lower()}%")
        sql += " ORDER BY observed_at ASC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def speakers(self) -> list[str]:
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT DISTINCT speaker FROM events WHERE speaker <> '' ORDER BY speaker"
        ).fetchall()
        return [r[0] for r in rows]

    def channels(self) -> list[str]:
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT DISTINCT channel FROM events WHERE channel <> '' ORDER BY channel"
        ).fetchall()
        return [r[0] for r in rows]

    def expire_older_than(self, cutoff_iso: str) -> int:
        """Delete notes captured before cutoff_iso (for N-day retention). Returns rows removed."""
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute(
            "DELETE FROM events WHERE observed_at < ?", (cutoff_iso,)
        )
        self._conn.commit()
        return cur.rowcount

    def purge_session(self, session_id: str) -> int:
        """Delete all notes captured in one session. Returns rows removed."""
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute(
            "DELETE FROM events WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def purge_all(self) -> int:
        """Delete every stored note (the one-click 'purge all'). Returns rows removed."""
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute("DELETE FROM events")
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
