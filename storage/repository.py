"""SQLite repository implementing the persistence contract.

Every public method mirrors ``docs/CONTRACTS.md`` section 5.  All ``save_*``
methods perform an upsert and return the freshly persisted dataclass, so
callers can trust the returned ``updated_at`` values without re-reading rows
themselves.

Semantics worth knowing:

* ``create_project`` / ``update_project`` both upsert by primary key.  The
  original ``created_at`` value is preserved on update.
* ``save_summary`` upserts on ``(project_id, scope, ref_id)`` - the summary's
  ``global`` scope always stores ``ref_id = NULL`` and two partial unique
  indexes enforce uniqueness even though SQLite treats NULLs as distinct.
* ``save_provider_config`` clears every other default in the same transaction
  when ``is_default`` is true; a partial unique index makes the "single
  default" invariant a database guarantee.
* Deleting a project cascades to every child entity through ``ON DELETE
  CASCADE`` foreign keys.
* One connection is opened per operation (see :class:`Database`), so the
  repository is safe to call from GUI and worker threads concurrently.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from novel_ai.domain.models import (
    Chapter,
    ChapterVersion,
    Character,
    GenerationLog,
    MemoryItem,
    OutlineNode,
    Project,
    ProjectSnapshot,
    ProviderConfig,
    Summary,
    count_words,
    utc_now,
)
from novel_ai.storage.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small serialisation / row-mapping helpers
# ---------------------------------------------------------------------------


def _dump_json(value: Any, fallback: str) -> str:
    """Serialise a JSON column, degrading to a safe empty value on error."""

    if value is None:
        return fallback
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        logger.warning("无法序列化 JSON 字段，使用默认值 %s", fallback)
        return fallback


def _load_json_list(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("忽略损坏的 JSON 列表字段")
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _load_json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("忽略损坏的 JSON 对象字段")
        return {}
    return value if isinstance(value, dict) else {}


def _upsert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> None:
    """Insert or update one row keyed by ``id``.

    ``created_at`` is never overwritten by an update; every other supplied
    column is taken from the incoming values.
    """

    columns = list(data)
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [name for name in columns if name not in ("id", "created_at")]
    if update_columns:
        assignments = ", ".join(f"{name} = excluded.{name}" for name in update_columns)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}"
        )
    else:  # pragma: no cover - every table has updatable columns
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING"
        )
    conn.execute(sql, [data[name] for name in columns])


def _normalise_summary_ref(scope: str, ref_id: str | None) -> str | None:
    """``global`` summaries always use ``ref_id = NULL``."""

    return None if scope == "global" else ref_id


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        title=row["title"],
        genre=row["genre"],
        logline=row["logline"],
        synopsis=row["synopsis"],
        worldbuilding=row["worldbuilding"],
        style=row["style"],
        taboos=row["taboos"],
        pov=row["pov"],
        tone=row["tone"],
        pacing=row["pacing"],
        dialogue_ratio=float(row["dialogue_ratio"]),
        target_words=int(row["target_words"]),
        default_provider_id=row["default_provider_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_character(row: sqlite3.Row) -> Character:
    return Character(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        role=row["role"],
        age=row["age"],
        appearance=row["appearance"],
        personality=row["personality"],
        background=row["background"],
        goals=row["goals"],
        arc=row["arc"],
        current_state=row["current_state"],
        relationships=_load_json_list(row["relationships"]),
        notes=row["notes"],
        is_active=bool(row["is_active"]),
        sort_order=int(row["sort_order"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_outline_node(row: sqlite3.Row) -> OutlineNode:
    return OutlineNode(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        kind=row["kind"],
        parent_id=row["parent_id"],
        summary=row["summary"],
        goals=row["goals"],
        conflicts=row["conflicts"],
        order_index=int(row["order_index"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_chapter(row: sqlite3.Row) -> Chapter:
    return Chapter(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        content=row["content"],
        number=int(row["number"]),
        volume_id=row["volume_id"],
        outline_id=row["outline_id"],
        status=row["status"],
        target_words=int(row["target_words"]),
        pov=row["pov"],
        requirements=row["requirements"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_summary(row: sqlite3.Row) -> Summary:
    return Summary(
        id=row["id"],
        project_id=row["project_id"],
        scope=row["scope"],
        content=row["content"],
        ref_id=row["ref_id"],
        token_count=int(row["token_count"]),
        strategy=row["strategy"],
        is_user_edited=bool(row["is_user_edited"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_memory(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        title=row["title"],
        content=row["content"],
        priority=int(row["priority"]),
        status=row["status"],
        source_chapter_id=row["source_chapter_id"],
        is_manual=bool(row["is_manual"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_version(row: sqlite3.Row) -> ChapterVersion:
    return ChapterVersion(
        id=row["id"],
        chapter_id=row["chapter_id"],
        project_id=row["project_id"],
        version_no=int(row["version_no"]),
        title=row["title"],
        content=row["content"],
        note=row["note"],
        word_count=int(row["word_count"]),
        created_at=row["created_at"],
    )


def _row_to_provider_config(row: sqlite3.Row) -> ProviderConfig:
    return ProviderConfig(
        id=row["id"],
        name=row["name"],
        provider_type=row["provider_type"],
        base_url=row["base_url"],
        model=row["model"],
        temperature=float(row["temperature"]),
        max_tokens=int(row["max_tokens"]),
        context_window=int(row["context_window"]),
        timeout=float(row["timeout"]),
        proxy=row["proxy"],
        stream=bool(row["stream"]),
        extra=_load_json_dict(row["extra"]),
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_generation_log(row: sqlite3.Row) -> GenerationLog:
    return GenerationLog(
        id=row["id"],
        project_id=row["project_id"],
        chapter_id=row["chapter_id"],
        provider_id=row["provider_id"],
        provider_name=row["provider_name"],
        model=row["model"],
        status=row["status"],
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        duration_ms=int(row["duration_ms"]),
        error=row["error"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class NovelRepository:
    """Persistence API used by the services and the UI."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    # ------------------------------------------------------------------
    # Project
    # ------------------------------------------------------------------
    def create_project(self, project: Project) -> Project:
        return self._save_project(project)

    def update_project(self, project: Project) -> Project:
        return self._save_project(project)

    def _save_project(self, project: Project) -> Project:
        now = utc_now()
        data = {
            "id": project.id,
            "title": project.title,
            "genre": project.genre,
            "logline": project.logline,
            "synopsis": project.synopsis,
            "worldbuilding": project.worldbuilding,
            "style": project.style,
            "taboos": project.taboos,
            "pov": project.pov,
            "tone": project.tone,
            "pacing": project.pacing,
            "dialogue_ratio": float(project.dialogue_ratio),
            "target_words": int(project.target_words),
            "default_provider_id": project.default_provider_id,
            "created_at": project.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "projects", data)
        saved = self.get_project(project.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("项目保存失败")
        return saved

    def get_project(self, project_id: str) -> Project | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return _row_to_project(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM projects "
                "ORDER BY updated_at DESC, created_at DESC, id ASC"
            ).fetchall()
            return [_row_to_project(row) for row in rows]

    def delete_project(self, project_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ------------------------------------------------------------------
    # Character
    # ------------------------------------------------------------------
    def save_character(self, c: Character) -> Character:
        now = utc_now()
        data = {
            "id": c.id,
            "project_id": c.project_id,
            "name": c.name,
            "role": c.role,
            "age": c.age,
            "appearance": c.appearance,
            "personality": c.personality,
            "background": c.background,
            "goals": c.goals,
            "arc": c.arc,
            "current_state": c.current_state,
            "relationships": _dump_json(c.relationships, "[]"),
            "notes": c.notes,
            "is_active": int(bool(c.is_active)),
            "sort_order": int(c.sort_order),
            "created_at": c.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "characters", data)
        saved = self.get_character(c.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("角色保存失败")
        return saved

    def get_character(self, character_id: str) -> Character | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            return _row_to_character(row) if row is not None else None

    def list_characters(self, project_id: str) -> list[Character]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM characters WHERE project_id = ? "
                "ORDER BY sort_order ASC, created_at ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [_row_to_character(row) for row in rows]

    def delete_character(self, character_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))

    # ------------------------------------------------------------------
    # Outline
    # ------------------------------------------------------------------
    def save_outline_node(self, node: OutlineNode) -> OutlineNode:
        now = utc_now()
        data = {
            "id": node.id,
            "project_id": node.project_id,
            "title": node.title,
            "kind": node.kind,
            "parent_id": node.parent_id,
            "summary": node.summary,
            "goals": node.goals,
            "conflicts": node.conflicts,
            "order_index": int(node.order_index),
            "status": node.status,
            "created_at": node.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "outline_nodes", data)
        saved = self.get_outline_node(node.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("大纲保存失败")
        return saved

    def get_outline_node(self, node_id: str) -> OutlineNode | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM outline_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return _row_to_outline_node(row) if row is not None else None

    def list_outline(self, project_id: str) -> list[OutlineNode]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outline_nodes WHERE project_id = ? "
                "ORDER BY order_index ASC, created_at ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [_row_to_outline_node(row) for row in rows]

    def delete_outline_node(self, node_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM outline_nodes WHERE id = ?", (node_id,))

    # ------------------------------------------------------------------
    # Chapter
    # ------------------------------------------------------------------
    def save_chapter(self, chapter: Chapter) -> Chapter:
        now = utc_now()
        data = {
            "id": chapter.id,
            "project_id": chapter.project_id,
            "title": chapter.title,
            "content": chapter.content,
            "number": int(chapter.number),
            "volume_id": chapter.volume_id,
            "outline_id": chapter.outline_id,
            "status": chapter.status,
            "target_words": int(chapter.target_words),
            "pov": chapter.pov,
            "requirements": chapter.requirements,
            "created_at": chapter.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "chapters", data)
        saved = self.get_chapter(chapter.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("章节保存失败")
        return saved

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
            ).fetchone()
            return _row_to_chapter(row) if row is not None else None

    def list_chapters(self, project_id: str) -> list[Chapter]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chapters WHERE project_id = ? "
                "ORDER BY number ASC, created_at ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [_row_to_chapter(row) for row in rows]

    def delete_chapter(self, chapter_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))

    def next_chapter_number(self, project_id: str) -> int:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS next_number "
                "FROM chapters WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return int(row["next_number"])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def save_summary(self, s: Summary) -> Summary:
        scope = (s.scope or "").strip()
        ref_id = _normalise_summary_ref(scope, s.ref_id)
        now = utc_now()
        base = {
            "project_id": s.project_id,
            "scope": scope,
            "content": s.content,
            "ref_id": ref_id,
            "token_count": int(s.token_count),
            "strategy": s.strategy,
            "is_user_edited": int(bool(s.is_user_edited)),
            "updated_at": now,
        }
        last_error: sqlite3.IntegrityError | None = None
        for _attempt in range(2):
            try:
                with self._db.connection() as conn:
                    row = conn.execute(
                        "SELECT id FROM summaries "
                        "WHERE project_id = ? AND scope = ? AND ref_id IS ?",
                        (s.project_id, scope, ref_id),
                    ).fetchone()
                    if row is not None:
                        data = {
                            "id": row["id"],
                            "created_at": s.created_at or now,
                            **base,
                        }
                    else:
                        data = {
                            "id": s.id,
                            "created_at": s.created_at or now,
                            **base,
                        }
                    _upsert(conn, "summaries", data)
                last_error = None
                break
            except sqlite3.IntegrityError as exc:
                # A concurrent writer may insert the same unique key between
                # our SELECT and INSERT; the retry turns it into an UPDATE.
                # Any other integrity problem is re-raised below.
                last_error = exc
        if last_error is not None:
            raise last_error
        saved = self.get_summary(s.project_id, scope, ref_id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("摘要保存失败")
        return saved

    def get_summary(
        self, project_id: str, scope: str, ref_id: str | None = None
    ) -> Summary | None:
        scope = (scope or "").strip()
        ref_id = _normalise_summary_ref(scope, ref_id)
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM summaries "
                "WHERE project_id = ? AND scope = ? AND ref_id IS ?",
                (project_id, scope, ref_id),
            ).fetchone()
            return _row_to_summary(row) if row is not None else None

    def list_summaries(self, project_id: str) -> list[Summary]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM summaries WHERE project_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [_row_to_summary(row) for row in rows]

    def delete_summary(self, summary_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def save_memory(self, m: MemoryItem) -> MemoryItem:
        now = utc_now()
        data = {
            "id": m.id,
            "project_id": m.project_id,
            "kind": m.kind,
            "title": m.title,
            "content": m.content,
            "priority": int(m.priority),
            "status": m.status,
            "source_chapter_id": m.source_chapter_id,
            "is_manual": int(bool(m.is_manual)),
            "created_at": m.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "memories", data)
        saved = self.get_memory(m.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("记忆保存失败")
        return saved

    def get_memory(self, memory_id: str) -> MemoryItem | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return _row_to_memory(row) if row is not None else None

    def list_memories(
        self, project_id: str, include_resolved: bool = True
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE project_id = ?"
        params: list[Any] = [project_id]
        if not include_resolved:
            sql += " AND status <> 'resolved'"
        sql += " ORDER BY priority DESC, created_at ASC, id ASC"
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_memory(row) for row in rows]

    def delete_memory(self, memory_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    # ------------------------------------------------------------------
    # Chapter versions
    # ------------------------------------------------------------------
    def add_version(self, v: ChapterVersion) -> ChapterVersion:
        now = utc_now()
        data = {
            "id": v.id,
            "chapter_id": v.chapter_id,
            "project_id": v.project_id,
            "version_no": int(v.version_no),
            "title": v.title,
            "content": v.content,
            "note": v.note,
            "word_count": int(v.word_count) or count_words(v.content),
            "created_at": v.created_at or now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "chapter_versions", data)
        saved = self.get_version(v.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("章节版本保存失败")
        return saved

    def get_version(self, version_id: str) -> ChapterVersion | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM chapter_versions WHERE id = ?", (version_id,)
            ).fetchone()
            return _row_to_version(row) if row is not None else None

    def list_versions(self, chapter_id: str) -> list[ChapterVersion]:
        """Return versions oldest-first (``version_no`` ascending).

        The version-history dialog displays ``reversed(versions)`` so the
        newest version appears first, which is why storage keeps ascending
        order here.
        """

        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chapter_versions WHERE chapter_id = ? "
                "ORDER BY version_no ASC, created_at ASC, id ASC",
                (chapter_id,),
            ).fetchall()
            return [_row_to_version(row) for row in rows]

    def count_chapter_versions(self, project_id: str) -> int:
        """Total version rows for every chapter of one project."""

        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM chapter_versions AS v "
                "JOIN chapters AS c ON c.id = v.chapter_id "
                "WHERE c.project_id = ?",
                (project_id,),
            ).fetchone()
            return int(row["total"])

    # ------------------------------------------------------------------
    # Provider configuration (API keys live in SecretStore, never here)
    # ------------------------------------------------------------------
    def list_provider_configs(self) -> list[ProviderConfig]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_configs "
                "ORDER BY is_default DESC, created_at ASC, id ASC"
            ).fetchall()
            return [_row_to_provider_config(row) for row in rows]

    def get_provider_config(self, provider_id: str) -> ProviderConfig | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM provider_configs WHERE id = ?", (provider_id,)
            ).fetchone()
            return _row_to_provider_config(row) if row is not None else None

    def save_provider_config(self, config: ProviderConfig) -> ProviderConfig:
        now = utc_now()
        data = {
            "id": config.id,
            "name": config.name,
            "provider_type": config.provider_type,
            "base_url": config.base_url,
            "model": config.model,
            "temperature": float(config.temperature),
            "max_tokens": int(config.max_tokens),
            "context_window": int(config.context_window),
            "timeout": float(config.timeout),
            "proxy": config.proxy,
            "stream": int(bool(config.stream)),
            "extra": _dump_json(config.extra, "{}"),
            "is_default": int(bool(config.is_default)),
            "created_at": config.created_at or now,
            "updated_at": now,
        }
        with self._db.connection() as conn:
            if config.is_default:
                # Clear the previous default first: the partial unique index
                # allows at most one is_default = 1 row at any moment.
                conn.execute(
                    "UPDATE provider_configs SET is_default = 0 "
                    "WHERE is_default = 1 AND id <> ?",
                    (config.id,),
                )
            _upsert(conn, "provider_configs", data)
        saved = self.get_provider_config(config.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("供应商配置保存失败")
        return saved

    def delete_provider_config(self, provider_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "DELETE FROM provider_configs WHERE id = ?", (provider_id,)
            )

    def set_default_provider(self, provider_id: str) -> None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT id FROM provider_configs WHERE id = ?", (provider_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"供应商配置不存在：{provider_id}")
            conn.execute("UPDATE provider_configs SET is_default = 0 WHERE is_default = 1")
            conn.execute(
                "UPDATE provider_configs SET is_default = 1, updated_at = ? "
                "WHERE id = ?",
                (utc_now(), provider_id),
            )

    def get_default_provider_config(self) -> ProviderConfig | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM provider_configs WHERE is_default = 1 LIMIT 1"
            ).fetchone()
            return _row_to_provider_config(row) if row is not None else None

    # ------------------------------------------------------------------
    # Generation logs
    # ------------------------------------------------------------------
    def add_generation_log(self, log: GenerationLog) -> GenerationLog:
        now = utc_now()
        data = {
            "id": log.id,
            "project_id": log.project_id,
            "chapter_id": log.chapter_id,
            "provider_id": log.provider_id,
            "provider_name": log.provider_name,
            "model": log.model,
            "status": log.status,
            "prompt_tokens": int(log.prompt_tokens),
            "completion_tokens": int(log.completion_tokens),
            "duration_ms": int(log.duration_ms),
            "error": log.error,
            "created_at": log.created_at or now,
        }
        with self._db.connection() as conn:
            _upsert(conn, "generation_logs", data)
        saved = self.get_generation_log(log.id)
        if saved is None:  # pragma: no cover - defensive
            raise RuntimeError("生成日志保存失败")
        return saved

    def get_generation_log(self, log_id: str) -> GenerationLog | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM generation_logs WHERE id = ?", (log_id,)
            ).fetchone()
            return _row_to_generation_log(row) if row is not None else None

    def list_generation_logs(
        self, project_id: str | None = None, limit: int = 100
    ) -> list[GenerationLog]:
        sql = "SELECT * FROM generation_logs"
        params: list[Any] = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        # rowid keeps ordering deterministic when several logs share the same
        # second-resolution created_at timestamp (newest insert first).
        sql += " ORDER BY created_at DESC, rowid DESC"
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_generation_log(row) for row in rows]

    # ------------------------------------------------------------------
    # Aggregate snapshot
    # ------------------------------------------------------------------
    def load_snapshot(self, project_id: str) -> ProjectSnapshot | None:
        project = self.get_project(project_id)
        if project is None:
            return None
        return ProjectSnapshot(
            project=project,
            characters=self.list_characters(project_id),
            outline=self.list_outline(project_id),
            chapters=self.list_chapters(project_id),
            summaries=self.list_summaries(project_id),
            memories=self.list_memories(project_id, include_resolved=True),
        )


__all__ = ["NovelRepository"]
