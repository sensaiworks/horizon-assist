"""
Assist session — the unit of consented, user-initiated work.

A session is created only after consent (see src/consent.py). Within it the user
takes explicit actions: capture() grabs one screen on demand, ask() answers a
question over what was captured, list_notes() shows what is stored, and end()/
purge_all() clear it. Nothing happens on a timer; every capture is one deliberate
call. Each stored note is tagged with this session's id so it can be cleared as a
unit — the default retention is session-only.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from .agent import QueryAgent
from .extractor import Extractor
from .mcp_client import HorizonMCPClient
from .models import MessageEvent
from .rag import RAGPipeline
from .store import EventStore


def _new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class AssistSession:
    def __init__(self, config: dict, api_key: str, session_id: str | None = None) -> None:
        self._config = config
        self._api_key = api_key
        self.session_id = session_id or _new_session_id()

        rag_cfg = config["rag"]
        self._store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
        self._rag = RAGPipeline(
            db_path=rag_cfg["db_path"],
            collection_name=rag_cfg["collection_name"],
            embedding_provider=rag_cfg["embedding_provider"],
            voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
            top_k=rag_cfg["top_k"],
        )
        self._connected = False

    # ----------------------------------------------------------- retention
    @property
    def _retention(self) -> str:
        return str(self._config.get("session", {}).get("retention", "session")).strip()

    @property
    def session_only(self) -> bool:
        return self._retention in ("session", "session-only", "")

    @property
    def _retention_days(self) -> int | None:
        r = self._retention
        if r.endswith("d") and r[:-1].isdigit():
            return int(r[:-1])
        return None

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        if self._connected:
            return
        self._store.connect()
        self._rag.connect()
        self._connected = True
        # Enforce N-day retention by sweeping anything older on session start.
        days = self._retention_days
        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            removed = self._store.expire_older_than(cutoff)
            self._rag.delete_older_than(cutoff)
            if removed:
                print(f"Retention: expired {removed} note(s) older than {days} day(s).", flush=True)

    def count(self) -> int:
        return self._store.count()

    def list_notes(self, limit: int = 20) -> list[dict]:
        """The 'view what's stored' list — most recent notes first."""
        return self._store.recent(limit)

    # ------------------------------------------------------------- actions
    async def capture(self) -> tuple[list[MessageEvent], bool]:
        """Capture the remote screen ONCE, on demand, and store what is read.

        Returns (newly_stored_events, is_lock_screen). Opens its own MCP client
        for the single shot so there is no long-lived connection sitting open.
        """
        self.connect()
        mcp = self._config["mcp"]
        claude = self._config["claude"]
        win = self._config["windows"]
        screen_index = self._config.get("capture", {}).get("screen_index", 0)

        extractor = Extractor(api_key=self._api_key, model=claude["vision_model"])

        async with HorizonMCPClient(mcp["server_path"], mcp["command"]) as client:
            # Identify the window purely to label the note; capture is on demand only.
            title = ""
            try:
                for w in await client.list_windows():
                    if any(t.lower() in w.title.lower() for t in win["monitor_titles"]):
                        title = w.title
                        break
            except Exception:
                pass
            png = await client.screenshot(screen=screen_index)

        if not png:
            print("Capture: empty screenshot — check screen_index in config.toml", flush=True)
            return [], False

        events, is_locked = await extractor.extract(
            png, window_title=title, session_id=self.session_id
        )
        if is_locked:
            return [], True

        new = self._store.ingest(events)   # dedup gate
        if new:
            self._rag.ingest(new)
        return new, False

    def ask(
        self,
        question: str,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """Answer a question over captured notes (session-scoped if session-only)."""
        self.connect()
        agent = QueryAgent(
            api_key=self._api_key,
            model=self._config["claude"]["query_model"],
            rag=self._rag,
            max_tokens=self._config["claude"]["max_tokens"],
        )
        scope = self.session_id if self.session_only else None
        return agent.query(question, on_chunk=on_chunk, session_id=scope)

    # ------------------------------------------------------------- clearing
    def end(self) -> int:
        """End the session. With session-only retention, delete this session's data.

        Returns the number of notes removed (0 if retention keeps them).
        """
        self.connect()
        removed = 0
        if self.session_only:
            removed = self._store.purge_session(self.session_id)
            self._rag.delete_session(self.session_id)
        self.close()
        return removed

    def purge_all(self) -> int:
        """One-click 'purge all stored data' — wipes every note in both stores."""
        self.connect()
        removed = self._store.purge_all()
        self._rag.purge_all()
        return removed

    def close(self) -> None:
        self._store.close()
        self._connected = False
