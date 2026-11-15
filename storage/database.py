"""SQLite database bootstrap for the novel-writing application.

The database stores every domain entity of the story bible.  The public
surface is intentionally tiny: construct a :class:`Database` with the file
path owned by ``ConfigPaths`` and use :meth:`Database.connection` for each
unit of work.  A connection is created per operation, so worker threads and
the GUI thread never share a connection object and can safely read/write in
parallel (WAL journaling + a generous busy timeout handle write contention).

Schema creation is idempotent and forward-only.  ``PRAGMA user_version``
records the schema version; the version-1 tables are created with
``IF NOT EXISTS`` so opening an existing database is a no-op.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

#: Current schema version written to ``PRAGMA user_version``.
SCHEMA_VERSION = 1

#: SQLite has no dedicated boolean type; 0/1 INTEGER columns are used instead.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '',
    logline TEXT NOT NULL DEFAULT '',
    synopsis TEXT NOT NULL DEFAULT '',
    worldbuilding TEXT NOT NULL DEFAULT '',
    style TEXT NOT NULL DEFAULT '',
    taboos TEXT NOT NULL DEFAULT '',
    pov TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    pacing TEXT NOT NULL DEFAULT '',
    dialogue_ratio REAL NOT NULL DEFAULT 0.35,
    target_words INTEGER NOT NULL DEFAULT 2500,
    default_provider_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    age TEXT NOT NULL DEFAULT '',
    appearance TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    background TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    arc TEXT NOT NULL DEFAULT '',
    current_state TEXT NOT NULL DEFAULT '',
    relationships TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_characters_project
    ON characters(project_id, sort_order);

CREATE TABLE IF NOT EXISTS outline_nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chapter',
    parent_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    conflicts TEXT NOT NULL DEFAULT '',
    order_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outline_project
    ON outline_nodes(project_id, parent_id, order_index);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    number INTEGER NOT NULL DEFAULT 1,
    volume_id TEXT,
    outline_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    target_words INTEGER NOT NULL DEFAULT 2500,
    pov TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_project
    ON chapters(project_id, number);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    ref_id TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT '',
    is_user_edited INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_project ON summaries(project_id);
-- SQLite treats NULLs as distinct inside a plain UNIQUE constraint, so two
-- partial indexes are used to enforce one row per (project, scope, ref_id)
-- while still allowing the ``global`` scope to store ref_id = NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_ref_unique
    ON summaries(project_id, scope, ref_id) WHERE ref_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_global_unique
    ON summaries(project_id, scope) WHERE ref_id IS NULL;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active',
    source_chapter_id TEXT,
    is_manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories(project_id, status);

CREATE TABLE IF NOT EXISTS chapter_versions (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(chapter_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_versions_chapter
    ON chapter_versions(chapter_id, version_no);

CREATE TABLE IF NOT EXISTS provider_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.8,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    context_window INTEGER NOT NULL DEFAULT 128000,
    timeout REAL NOT NULL DEFAULT 120.0,
    proxy TEXT NOT NULL DEFAULT '',
    stream INTEGER NOT NULL DEFAULT 1,
    extra TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- At most one default provider row, enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_default_unique
    ON provider_configs(is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS generation_logs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter_id TEXT,
    provider_id TEXT,
    provider_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_project
    ON generation_logs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_created
    ON generation_logs(created_at);
"""


class Database:
    """Thread-friendly wrapper around one SQLite database file.

    Parameters
    ----------
    path:
        Database file path.  ``":memory:"`` is supported for tests; it keeps a
        single shared connection because a per-operation connection would see
        an empty database each time.
    timeout:
        ``sqlite3`` connection timeout in seconds.
    busy_timeout_ms:
        ``PRAGMA busy_timeout`` value; how long a connection waits for a lock
        before raising ``database is locked`` (worker thread + GUI thread).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 15.0,
        busy_timeout_ms: int = 8000,
    ) -> None:
        self._is_memory = str(path) == ":memory:"
        self._timeout = float(timeout)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None

        if self._is_memory:
            self.path: Path | str = ":memory:"
            self._memory_connection = self._open()
        else:
            self.path = Path(path).expanduser().resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.initialize()

    # -- public API ---------------------------------------------------------
    @property
    def is_memory(self) -> bool:
        return self._is_memory

    def initialize(self) -> None:
        """Create the schema when missing and apply forward migrations.

        Safe to call multiple times; opening an existing database is a no-op.
        """

        with self.connection() as conn:
            conn.executescript(_SCHEMA_SQL)
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前程序支持的版本 {SCHEMA_VERSION}，"
                    "请升级应用后再打开。"
                )
            if version < SCHEMA_VERSION:
                self._migrate(conn, version)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                logger.debug("数据库结构已初始化：版本 %s -> %s", version, SCHEMA_VERSION)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection; commit on success, rollback on error.

        One connection is used per operation and closed afterwards.  Multiple
        statements inside the ``with`` block share a transaction, which is what
        the repository relies on for default-provider and summary upserts.
        """

        if self._memory_connection is not None:
            # Tests share one in-process connection so writes survive between
            # operations; the lock keeps it serialized across threads.
            with self._lock:
                conn = self._memory_connection
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return

        conn = self._open()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- internals ----------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        target = ":memory:" if self._is_memory else str(self.path)
        conn = sqlite3.connect(
            target,
            timeout=self._timeout,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply forward-only migrations from ``from_version``.

        Version 0 -> 1 is handled by the idempotent ``CREATE ... IF NOT
        EXISTS`` statements in :data:`_SCHEMA_SQL`; future versions append
        ``ALTER TABLE`` steps here before bumping :data:`SCHEMA_VERSION`.
        """

        if from_version >= SCHEMA_VERSION:
            return
        # Placeholder for future ALTER TABLE migrations.
        logger.debug("Migrating database schema from version %s", from_version)


__all__ = ["Database", "SCHEMA_VERSION"]
