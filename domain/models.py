"""Domain models for the novel-writing application.

Everything in this module is storage/provider agnostic.  Dataclasses are kept
plain (no Qt, no SQL) so the business layer can be tested without a GUI or a
network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    """Return a compact, sortable UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def count_words(text: str) -> int:
    """Count words for Chinese/Latin mixed text.

    Chinese characters are counted individually; runs of Latin letters and
    digits count as one word.  This matches how most Chinese writing tools
    display 字数.
    """

    import re

    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*", text))
    return cjk + latin


# ---------------------------------------------------------------------------
# Project and story bible
# ---------------------------------------------------------------------------


@dataclass
class Project:
    id: str
    title: str
    genre: str = ""
    logline: str = ""
    synopsis: str = ""
    worldbuilding: str = ""
    style: str = ""
    taboos: str = ""
    pov: str = "第三人称有限视角"
    tone: str = "克制而有张力"
    pacing: str = "张弛有度"
    dialogue_ratio: float = 0.35
    target_words: int = 2500
    default_provider_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, title: str, **kwargs: Any) -> "Project":
        value = cls(id=new_id("proj"), title=title, **kwargs)
        return value

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass
class Character:
    id: str
    project_id: str
    name: str
    role: str = ""
    age: str = ""
    appearance: str = ""
    personality: str = ""
    background: str = ""
    goals: str = ""
    arc: str = ""
    current_state: str = ""
    relationships: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""
    is_active: bool = True
    sort_order: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, project_id: str, name: str, **kwargs: Any) -> "Character":
        return cls(id=new_id("char"), project_id=project_id, name=name, **kwargs)

    @property
    def relationship_text(self) -> str:
        parts = []
        for rel in self.relationships:
            target = rel.get("target", "").strip()
            relation = rel.get("relation", "").strip()
            note = rel.get("note", "").strip()
            if target or relation:
                parts.append(f"{target}（{relation}）" + (f"：{note}" if note else ""))
        return "；".join(parts)


@dataclass
class OutlineNode:
    """A volume, chapter outline or plot beat."""

    id: str
    project_id: str
    title: str
    kind: str = "chapter"  # volume | chapter | beat
    parent_id: str | None = None
    summary: str = ""
    goals: str = ""
    conflicts: str = ""
    order_index: int = 0
    status: str = "planned"  # planned | drafting | done
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        project_id: str,
        title: str,
        kind: str = "chapter",
        **kwargs: Any,
    ) -> "OutlineNode":
        return cls(
            id=new_id("outline"),
            project_id=project_id,
            title=title,
            kind=kind,
            **kwargs,
        )


@dataclass
class Chapter:
    id: str
    project_id: str
    title: str
    content: str = ""
    number: int = 1
    volume_id: str | None = None
    outline_id: str | None = None
    status: str = "draft"  # draft | final
    target_words: int = 2500
    pov: str = ""
    requirements: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        project_id: str,
        title: str,
        number: int = 1,
        **kwargs: Any,
    ) -> "Chapter":
        return cls(
            id=new_id("chap"),
            project_id=project_id,
            title=title,
            number=number,
            **kwargs,
        )

    @property
    def word_count(self) -> int:
        return count_words(self.content)

    @property
    def display_title(self) -> str:
        return f"第{self.number}章 · {self.title}" if self.title else f"第{self.number}章"


@dataclass
class Summary:
    id: str
    project_id: str
    scope: str  # global | volume | chapter
    content: str
    ref_id: str | None = None
    token_count: int = 0
    strategy: str = ""
    is_user_edited: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        project_id: str,
        scope: str,
        content: str,
        ref_id: str | None = None,
        **kwargs: Any,
    ) -> "Summary":
        return cls(
            id=new_id("sum"),
            project_id=project_id,
            scope=scope,
            ref_id=ref_id,
            content=content,
            **kwargs,
        )


@dataclass
class MemoryItem:
    """Durable story fact: foreshadowing, rules, timeline anchor, etc."""

    id: str
    project_id: str
    kind: str  # setting | foreshadow | timeline | item | conflict | relationship | rule
    title: str
    content: str
    priority: int = 3  # 1..5, 5 highest
    status: str = "active"  # active | resolved
    source_chapter_id: str | None = None
    is_manual: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        project_id: str,
        kind: str,
        title: str,
        content: str,
        **kwargs: Any,
    ) -> "MemoryItem":
        return cls(
            id=new_id("mem"),
            project_id=project_id,
            kind=kind,
            title=title,
            content=content,
            **kwargs,
        )


@dataclass
class ChapterVersion:
    id: str
    chapter_id: str
    project_id: str
    version_no: int
    title: str
    content: str
    note: str = ""
    word_count: int = 0
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        chapter_id: str,
        project_id: str,
        version_no: int,
        title: str,
        content: str,
        note: str = "",
    ) -> "ChapterVersion":
        return cls(
            id=new_id("ver"),
            chapter_id=chapter_id,
            project_id=project_id,
            version_no=version_no,
            title=title,
            content=content,
            note=note,
            word_count=count_words(content),
        )


# ---------------------------------------------------------------------------
# Providers and logs
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    id: str
    name: str
    provider_type: str
    base_url: str
    model: str
    temperature: float = 0.8
    max_tokens: int = 4096
    context_window: int = 128_000
    timeout: float = 120.0
    proxy: str = ""
    stream: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
    is_default: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        name: str,
        provider_type: str,
        base_url: str,
        model: str,
        **kwargs: Any,
    ) -> "ProviderConfig":
        return cls(
            id=new_id("prov"),
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            model=model,
            **kwargs,
        )


@dataclass
class GenerationLog:
    id: str
    project_id: str
    chapter_id: str | None
    provider_id: str | None
    provider_name: str
    model: str
    status: str  # success | error | cancelled
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    error: str = ""
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        project_id: str,
        provider_name: str,
        model: str,
        status: str,
        **kwargs: Any,
    ) -> "GenerationLog":
        return cls(
            id=new_id("log"),
            project_id=project_id,
            chapter_id=kwargs.pop("chapter_id", None),
            provider_id=kwargs.pop("provider_id", None),
            provider_name=provider_name,
            model=model,
            status=status,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Aggregate snapshot used by the context engine / generation pipeline
# ---------------------------------------------------------------------------


@dataclass
class ProjectSnapshot:
    project: Project
    characters: list[Character] = field(default_factory=list)
    outline: list[OutlineNode] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    summaries: list[Summary] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)

    def summary_for(self, scope: str, ref_id: str | None = None) -> Summary | None:
        for item in self.summaries:
            if item.scope == scope and (item.ref_id or None) == (ref_id or None):
                return item
        return None

    def chapter_summary(self, chapter_id: str) -> str:
        summary = self.summary_for("chapter", chapter_id)
        return summary.content if summary else ""

    def volume_summary(self, volume_id: str | None) -> str:
        if not volume_id:
            return ""
        summary = self.summary_for("volume", volume_id)
        return summary.content if summary else ""

    def global_summary(self) -> str:
        summary = self.summary_for("global", None)
        return summary.content if summary else ""

    def chapter_by_id(self, chapter_id: str) -> Chapter | None:
        return next((c for c in self.chapters if c.id == chapter_id), None)

    def chapter_for_outline(self, outline_id: str | None) -> Chapter | None:
        if not outline_id:
            return None
        return next((c for c in self.chapters if c.outline_id == outline_id), None)

    def outline_by_id(self, outline_id: str | None) -> OutlineNode | None:
        if not outline_id:
            return None
        return next((n for n in self.outline if n.id == outline_id), None)

    def ordered_outline(self) -> list[OutlineNode]:
        return sorted(self.outline, key=lambda n: (n.order_index, n.created_at))

    def ordered_chapters(self) -> list[Chapter]:
        return sorted(self.chapters, key=lambda c: (c.number, c.created_at))

    def previous_chapters(self, chapter: Chapter, limit: int | None = None) -> list[Chapter]:
        previous = [c for c in self.ordered_chapters() if c.number < chapter.number]
        if limit is not None:
            previous = previous[-limit:]
        return previous

    def active_characters(self) -> list[Character]:
        return [c for c in self.characters if c.is_active]

    def active_memories(self) -> list[MemoryItem]:
        return sorted(self.memories, key=lambda m: (-m.priority, m.created_at))

    def volume_chapters(self, volume_id: str | None) -> list[Chapter]:
        if not volume_id:
            return []
        return [c for c in self.ordered_chapters() if c.volume_id == volume_id]


__all__ = [
    "Chapter",
    "ChapterVersion",
    "Character",
    "GenerationLog",
    "MemoryItem",
    "OutlineNode",
    "Project",
    "ProjectSnapshot",
    "ProviderConfig",
    "Summary",
    "count_words",
    "new_id",
    "utc_now",
]
