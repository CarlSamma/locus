"""SQLite database layer — schema DDL, connection, migrations.

The ``probes`` table is the attack tree: every row is a node, and it is the
single source of truth for attack state (no SSOT markdown, no event store).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS properties (
    key TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    prior_entropy REAL NOT NULL DEFAULT 0.0,
    state TEXT NOT NULL DEFAULT 'unknown',
    votes INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS probes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    property_key TEXT NOT NULL,
    frame_alias TEXT DEFAULT '',
    text TEXT NOT NULL,
    tweet_id TEXT,
    posted_at TEXT,
    reply_id TEXT,
    reply_text TEXT,
    replied_at TEXT,
    classification TEXT DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probes_session ON probes(session_id);
CREATE INDEX IF NOT EXISTS idx_probes_property ON probes(property_key);
CREATE INDEX IF NOT EXISTS idx_probes_status ON probes(status);

CREATE TABLE IF NOT EXISTS frames (
    alias TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    prompt_template TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intel (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    entropy_before REAL NOT NULL DEFAULT 0.0,
    entropy_after REAL NOT NULL DEFAULT 0.0,
    note TEXT DEFAULT '',
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_session ON intel(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    probes_total INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    target TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ledger (
    id TEXT PRIMARY KEY,
    property_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    probe_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ledger_property ON ledger(property_key);

CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    embedding BLOB,
    kind TEXT NOT NULL DEFAULT 'probe',
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_kind ON memory_entries(kind);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


class Database:
    """Async SQLite connection manager with schema migration."""

    def __init__(self) -> None:
        self.conn: Optional[aiosqlite.Connection] = None

    async def initialize(self, db_path: str = ":memory:") -> None:
        """Open connection, enable WAL + FK, run schema DDL."""
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = await aiosqlite.connect(db_path)
        self.conn.row_factory = aiosqlite.Row

        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")

        await self._run_migrations()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _run_migrations(self) -> None:
        if self.conn is None:
            raise RuntimeError("Database not initialized")
        await self.conn.executescript(_DDL)

        cursor = await self.conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = await cursor.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        await self.conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        if self.conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params_list) -> None:
        if self.conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self.conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple = ()):
        if self.conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        cursor = await self.conn.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        if self.conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        cursor = await self.conn.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self) -> None:
        if self.conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self.conn.commit()

    async def seed_properties(self, properties: dict) -> int:
        """Insert (or refresh) property rows from a data-driven dict, e.g.
        ``{"word_count": {"weight": 2.0, "prior_entropy": 2.0}}``."""
        rows = [
            (
                key,
                meta.get("weight", 1.0),
                meta.get("prior_entropy", 0.0),
                meta.get("state", "unknown"),
                meta.get("votes", 0),
                meta.get("notes", ""),
            )
            for key, meta in properties.items()
        ]
        if not rows:
            return 0
        await self.executemany(
            """INSERT INTO properties (key, weight, prior_entropy, state, votes, notes)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 weight = excluded.weight,
                 prior_entropy = excluded.prior_entropy,
                 state = excluded.state,
                 votes = excluded.votes,
                 notes = excluded.notes""",
            rows,
        )
        await self.commit()
        return len(rows)
