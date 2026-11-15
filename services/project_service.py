"""High-level project workflows built on :class:`NovelRepository`.

The service adds the behaviours the UI and generation pipeline need but which
do not belong in a pure persistence layer:

* creating a project also creates the first volume outline node;
* chapter numbering, chapter/outline linkage and outline status transitions;
* manual version snapshots and safe version restore (current content is
  snapshotted before being overwritten);
* summary upsert that never silently drops a user edit;
* memory CRUD and aggregated project statistics.

Exception policy: mutating methods raise :class:`ValueError` for unknown ids or
invalid references (instead of leaking ``sqlite3.IntegrityError``); delete
methods are idempotent and never raise for missing rows.
"""

from __future__ import annotations

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
    Summary,
    count_words,
    utc_now,
)
from novel_ai.storage.repository import NovelRepository

logger = logging.getLogger(__name__)

SUMMARY_SCOPES = ("global", "volume", "chapter")

_EMPTY_STATS: dict[str, int] = {
    "chapters": 0,
    "chapter_count": 0,
    "words": 0,
    "total_words": 0,
    "characters": 0,
    "outline_nodes": 0,
    "volumes": 0,
    "memories": 0,
    "active_memories": 0,
    "summaries": 0,
    "versions": 0,
}


class ProjectService:
    """Project-level operations exposed to the UI pages."""

    def __init__(self, repo: NovelRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def list_projects(self) -> list[Project]:
        return self._repo.list_projects()

    def create_project(self, title: str) -> Project:
        """Create a project and its automatically generated first volume."""

        clean_title = (title or "").strip() or "未命名作品"
        project = self._repo.create_project(Project.create(title=clean_title))
        self._repo.save_outline_node(
            OutlineNode.create(
                project.id,
                "第一卷",
                kind="volume",
                order_index=0,
            )
        )
        return project

    def update_project(self, project: Project) -> Project:
        project.touch()
        return self._repo.update_project(project)

    def delete_project(self, project_id: str) -> None:
        self._repo.delete_project(project_id)

    def load_snapshot(self, project_id: str) -> ProjectSnapshot | None:
        return self._repo.load_snapshot(project_id)

    def project_stats(self, project_id: str) -> dict[str, int]:
        """Return dashboard counters for one project.

        Missing projects produce an all-zero mapping so the dashboard can be
        refreshed safely after a project was deleted.  ``words``/
        ``total_words`` and ``chapters``/``chapter_count`` are aliases kept for
        UI compatibility.
        """

        snapshot = self._repo.load_snapshot(project_id)
        if snapshot is None:
            return dict(_EMPTY_STATS)
        words = sum(count_words(chapter.content) for chapter in snapshot.chapters)
        versions = self._repo.count_chapter_versions(project_id)
        stats = {
            "chapters": len(snapshot.chapters),
            "chapter_count": len(snapshot.chapters),
            "words": words,
            "total_words": words,
            "characters": len(snapshot.characters),
            "outline_nodes": len(snapshot.outline),
            "volumes": sum(1 for node in snapshot.outline if node.kind == "volume"),
            "memories": len(snapshot.memories),
            "active_memories": sum(
                1 for memory in snapshot.memories if memory.status != "resolved"
            ),
            "summaries": len(snapshot.summaries),
            "versions": versions,
        }
        return stats

    # ------------------------------------------------------------------
    # Characters
    # ------------------------------------------------------------------
    def create_character(self, project_id: str, name: str = "新角色") -> Character:
        self._require_project(project_id)
        existing = self._repo.list_characters(project_id)
        next_order = max((item.sort_order for item in existing), default=-1) + 1
        character = Character.create(
            project_id,
            (name or "").strip() or "新角色",
            sort_order=next_order,
        )
        return self._repo.save_character(character)

    def save_character(self, character: Character) -> Character:
        self._require_project(character.project_id)
        return self._repo.save_character(character)

    def delete_character(self, character_id: str) -> None:
        self._repo.delete_character(character_id)

    # ------------------------------------------------------------------
    # Outline
    # ------------------------------------------------------------------
    def create_volume(self, project_id: str, title: str = "新卷") -> OutlineNode:
        self._require_project(project_id)
        volumes = [
            node
            for node in self._repo.list_outline(project_id)
            if node.kind == "volume" and node.parent_id is None
        ]
        next_order = max((node.order_index for node in volumes), default=-1) + 1
        volume = OutlineNode.create(
            project_id,
            (title or "").strip() or "新卷",
            kind="volume",
            order_index=next_order,
        )
        return self._repo.save_outline_node(volume)

    def create_chapter_outline(
        self, project_id: str, parent_id: str | None, title: str = "新章节"
    ) -> OutlineNode:
        self._require_project(project_id)
        if parent_id is not None:
            parent = self._repo.get_outline_node(parent_id)
            if parent is None:
                raise ValueError(f"大纲节点不存在：{parent_id}")
            if parent.project_id != project_id:
                raise ValueError("父大纲节点不属于该项目")
        siblings = [
            node
            for node in self._repo.list_outline(project_id)
            if (node.parent_id or None) == (parent_id or None)
        ]
        next_order = max((node.order_index for node in siblings), default=-1) + 1
        node = OutlineNode.create(
            project_id,
            (title or "").strip() or "新章节",
            kind="chapter",
            parent_id=parent_id,
            order_index=next_order,
        )
        return self._repo.save_outline_node(node)

    def save_outline_node(self, node: OutlineNode) -> OutlineNode:
        self._require_project(node.project_id)
        return self._repo.save_outline_node(node)

    def delete_outline_node(self, node_id: str) -> None:
        """Delete an outline node and its descendants.

        Chapters are never deleted here; their ``outline_id`` is cleared so the
        written content survives when the plan is removed.
        """

        node = self._repo.get_outline_node(node_id)
        if node is None:
            return
        nodes = self._repo.list_outline(node.project_id)
        descendants: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in descendants:
                continue
            descendants.add(current)
            stack.extend(
                child.id for child in nodes if (child.parent_id or None) == current
            )
        for chapter in self._repo.list_chapters(node.project_id):
            if chapter.outline_id in descendants:
                chapter.outline_id = None
                chapter.updated_at = utc_now()
                self._repo.save_chapter(chapter)
        for descendant_id in descendants:
            self._repo.delete_outline_node(descendant_id)

    # ------------------------------------------------------------------
    # Chapters
    # ------------------------------------------------------------------
    def create_chapter(
        self,
        project_id: str,
        title: str = "",
        *,
        number: int | None = None,
        volume_id: str | None = None,
        outline_id: str | None = None,
        target_words: int | None = None,
    ) -> Chapter:
        project = self._require_project(project_id)

        outline: OutlineNode | None = None
        if outline_id is not None:
            outline = self._repo.get_outline_node(outline_id)
            if outline is None:
                raise ValueError(f"大纲节点不存在：{outline_id}")
            if outline.project_id != project_id:
                raise ValueError("大纲节点不属于该项目")

        if number is None:
            chapter_number = self._repo.next_chapter_number(project_id)
        else:
            chapter_number = max(1, int(number))

        resolved_title = (title or "").strip()
        requirements = ""
        resolved_volume_id = volume_id
        if outline is not None:
            if not resolved_title:
                resolved_title = outline.title
            requirements = outline.summary or ""
            if resolved_volume_id is None and outline.parent_id:
                parent = self._repo.get_outline_node(outline.parent_id)
                if parent is not None and parent.kind == "volume":
                    resolved_volume_id = parent.id
            if outline.status == "planned":
                outline.status = "drafting"
                self._repo.save_outline_node(outline)

        chapter = Chapter.create(
            project_id,
            resolved_title,
            number=chapter_number,
            volume_id=resolved_volume_id,
            outline_id=outline.id if outline is not None else None,
            target_words=(
                int(target_words) if target_words is not None else project.target_words
            ),
            pov=project.pov,
            requirements=requirements,
        )
        return self._repo.save_chapter(chapter)

    def save_chapter(
        self, chapter: Chapter, note: str = "", create_version: bool = False
    ) -> Chapter:
        """Persist a chapter; optionally record the saved content as a version.

        ``create_version=True`` stores the *newly saved* content as a
        :class:`ChapterVersion`, so AI generations and manual saves become
        restore points with the supplied ``note``.
        """

        self._require_project(chapter.project_id)
        saved = self._repo.save_chapter(chapter)
        if create_version:
            self._add_version(saved, note or "保存版本")
        self._sync_outline_status(saved)
        return saved

    def delete_chapter(self, chapter_id: str) -> None:
        self._repo.delete_chapter(chapter_id)

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        return self._repo.get_chapter(chapter_id)

    def list_chapters(self, project_id: str) -> list[Chapter]:
        return self._repo.list_chapters(project_id)

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------
    def create_version(self, chapter_id: str, note: str = "手动保存") -> ChapterVersion:
        chapter = self._repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError(f"章节不存在：{chapter_id}")
        return self._add_version(chapter, note or "手动保存")

    def list_versions(self, chapter_id: str) -> list[ChapterVersion]:
        return self._repo.list_versions(chapter_id)

    def restore_version(self, chapter_id: str, version_id: str) -> Chapter:
        """Restore an earlier version, backing up the current content first."""

        chapter = self._repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError(f"章节不存在：{chapter_id}")
        version = self._repo.get_version(version_id)
        if version is None:
            raise ValueError(f"版本不存在：{version_id}")
        if version.chapter_id != chapter_id:
            raise ValueError("该版本不属于当前章节")

        # Safety net: never lose the content that is about to be replaced.
        self._add_version(chapter, f"恢复第 {version.version_no} 版前自动备份")

        chapter.title = version.title if version.title else chapter.title
        chapter.content = version.content
        chapter.updated_at = utc_now()
        return self._repo.save_chapter(chapter)

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------
    def save_summary(
        self,
        project_id: str,
        scope: str,
        content: str,
        ref_id: str | None = None,
        *,
        strategy: str = "",
        is_user_edited: bool = False,
    ) -> Summary:
        """Create or update the summary for ``(project, scope, ref_id)``.

        Existing user edits are sticky: once ``is_user_edited`` is true a later
        automatic (re)generation cannot clear the flag.  ``global`` summaries
        always store ``ref_id = NULL``.
        """

        self._require_project(project_id)
        clean_scope = (scope or "").strip()
        if clean_scope not in SUMMARY_SCOPES:
            raise ValueError(f"未知的摘要范围：{scope!r}")
        clean_ref = None if clean_scope == "global" else ref_id
        text = content or ""
        existing = self._repo.get_summary(project_id, clean_scope, clean_ref)

        if existing is not None:
            existing.content = text
            if strategy:
                existing.strategy = strategy
            existing.is_user_edited = bool(existing.is_user_edited or is_user_edited)
            existing.token_count = len(text)
            existing.updated_at = utc_now()
            summary = existing
        else:
            summary = Summary.create(
                project_id,
                clean_scope,
                text,
                ref_id=clean_ref,
                strategy=strategy,
                is_user_edited=bool(is_user_edited),
            )
            summary.token_count = len(text)
        return self._repo.save_summary(summary)

    def list_summaries(self, project_id: str) -> list[Summary]:
        return self._repo.list_summaries(project_id)

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------
    def create_memory(
        self,
        project_id: str,
        kind: str,
        title: str,
        content: str,
        **kwargs: Any,
    ) -> MemoryItem:
        self._require_project(project_id)
        memory = MemoryItem.create(project_id, kind, title, content, **kwargs)
        return self._repo.save_memory(memory)

    def save_memory(self, memory: MemoryItem) -> MemoryItem:
        self._require_project(memory.project_id)
        return self._repo.save_memory(memory)

    def delete_memory(self, memory_id: str) -> None:
        self._repo.delete_memory(memory_id)

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    def list_logs(
        self, project_id: str | None = None, limit: int = 100
    ) -> list[GenerationLog]:
        return self._repo.list_generation_logs(project_id, limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_project(self, project_id: str) -> Project:
        project = self._repo.get_project(project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return project

    def _add_version(self, chapter: Chapter, note: str) -> ChapterVersion:
        """Append a version, retrying if a concurrent writer took the number."""

        last_error: sqlite3.IntegrityError | None = None
        for _attempt in range(3):
            versions = self._repo.list_versions(chapter.id)
            next_no = max((item.version_no for item in versions), default=0) + 1
            version = ChapterVersion.create(
                chapter.id,
                chapter.project_id,
                next_no,
                chapter.title,
                chapter.content,
                note=note,
            )
            try:
                return self._repo.add_version(version)
            except sqlite3.IntegrityError as exc:
                # UNIQUE(chapter_id, version_no) raced with another thread.
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("无法创建章节版本")  # pragma: no cover - defensive

    def _sync_outline_status(self, chapter: Chapter) -> None:
        """Keep the linked outline node's status in step with the chapter."""

        if not chapter.outline_id:
            return
        outline = self._repo.get_outline_node(chapter.outline_id)
        if outline is None:
            return
        target = "done" if chapter.status == "final" else "drafting"
        if outline.status == target:
            return
        if target == "done" or outline.status == "planned":
            outline.status = target
            outline.updated_at = utc_now()
            self._repo.save_outline_node(outline)


__all__ = ["ProjectService", "SUMMARY_SCOPES"]
