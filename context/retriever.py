"""Offline lexical retrieval used to enrich generation context.

The retrieval layer is deliberately pluggable: :class:`Retriever` is a small
protocol so a future vector database implementation can replace
:class:`LexicalRetriever` without touching the context builder.  The shipped
implementation is pure standard library (TF-IDF cosine over CJK bigrams and
ASCII tokens) and needs neither network access nor external services.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from novel_ai.context.models import RetrievalHit
from novel_ai.domain.models import ProjectSnapshot, Summary

_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+(?:['’_\-][a-z0-9]+)*")

#: Source label used when rendering retrieval results in prompts.
SOURCE_LABELS: dict[str, str] = {
    "chapter": "章节",
    "character": "角色",
    "memory": "记忆",
    "outline": "大纲",
    "summary": "摘要",
}


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text into CJK bigrams + ASCII tokens."""

    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_ASCII_TOKEN_RE.findall(lowered))
    return tokens


@dataclass(frozen=True)
class _Document:
    source_type: str
    source_id: str
    title: str
    text: str


@runtime_checkable
class Retriever(Protocol):
    """Pluggable retrieval backend contract used by the context builder."""

    def index(
        self,
        snapshot: ProjectSnapshot,
        *,
        exclude_chapter_id: str | None = None,
    ) -> None: ...

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        exclude_chapter_id: str | None = None,
    ) -> list[RetrievalHit]: ...


class LexicalRetriever:
    """Deterministic TF-IDF cosine retrieval over story material."""

    def __init__(self, *, min_score: float = 0.02) -> None:
        self.min_score = max(0.0, float(min_score))
        self._documents: list[_Document] = []
        self._idf: dict[str, float] = {}
        self._vectors: list[dict[str, float]] = []
        self._norms: list[float] = []

    # -- indexing -----------------------------------------------------------
    def index(
        self,
        snapshot: ProjectSnapshot,
        *,
        exclude_chapter_id: str | None = None,
    ) -> None:
        documents: list[_Document] = []
        current_chapter = (
            snapshot.chapter_by_id(exclude_chapter_id) if exclude_chapter_id else None
        )
        current_outline_id = current_chapter.outline_id if current_chapter else None

        for chapter in snapshot.ordered_chapters():
            if exclude_chapter_id and chapter.id == exclude_chapter_id:
                continue
            content = (chapter.content or "").strip()
            if not content:
                continue
            documents.append(
                _Document(
                    source_type="chapter",
                    source_id=chapter.id,
                    title=chapter.display_title,
                    text=content,
                )
            )

        for character in snapshot.active_characters():
            parts = [
                character.name,
                character.role,
                character.personality,
                character.goals,
                character.current_state,
                character.background,
                character.relationship_text,
            ]
            text = "\n".join(part.strip() for part in parts if (part or "").strip())
            if text:
                documents.append(
                    _Document(
                        source_type="character",
                        source_id=character.id,
                        title=character.name or "未命名角色",
                        text=text,
                    )
                )

        for memory in snapshot.active_memories():
            text = "\n".join(
                part.strip() for part in (memory.title, memory.content) if (part or "").strip()
            )
            if text:
                documents.append(
                    _Document(
                        source_type="memory",
                        source_id=memory.id,
                        title=memory.title or "未命名记忆",
                        text=text,
                    )
                )

        for node in snapshot.ordered_outline():
            if current_outline_id and (
                node.id == current_outline_id
                or (node.kind == "beat" and node.parent_id == current_outline_id)
            ):
                # The current chapter's own outline is a dedicated context
                # section; indexing it again would only duplicate it.
                continue
            parts = [node.title, node.summary, node.goals, node.conflicts]
            text = "\n".join(part.strip() for part in parts if (part or "").strip())
            if text:
                documents.append(
                    _Document(
                        source_type="outline",
                        source_id=node.id,
                        title=node.title or "未命名大纲",
                        text=text,
                    )
                )

        for summary in snapshot.summaries:
            content = (summary.content or "").strip()
            if not content:
                continue
            if (
                current_chapter is not None
                and summary.scope == "chapter"
                and summary.ref_id == current_chapter.id
            ):
                continue
            documents.append(
                _Document(
                    source_type="summary",
                    source_id=summary.id,
                    title=self._summary_title(snapshot, summary),
                    text=content,
                )
            )

        self._build_index(documents)

    @staticmethod
    def _summary_title(snapshot: ProjectSnapshot, summary: Summary) -> str:
        if summary.scope == "chapter":
            chapter = snapshot.chapter_by_id(summary.ref_id)
            if chapter is not None:
                return f"{chapter.display_title} 摘要"
            return "章节摘要"
        if summary.scope == "volume":
            node = snapshot.outline_by_id(summary.ref_id)
            if node is not None and (node.title or "").strip():
                return f"卷摘要·{node.title.strip()}"
            return "卷摘要"
        if summary.scope == "global":
            return "全局摘要"
        return f"{summary.scope or '故事'}摘要"

    def _build_index(self, documents: list[_Document]) -> None:
        self._documents = documents
        counts = [Counter(tokenize(f"{doc.title}\n{doc.text}")) for doc in documents]
        document_frequency: Counter[str] = Counter()
        for count in counts:
            document_frequency.update(count.keys())

        total_documents = len(documents)
        self._idf = {
            term: math.log((total_documents + 1) / (frequency + 1)) + 1.0
            for term, frequency in document_frequency.items()
        }

        vectors: list[dict[str, float]] = []
        norms: list[float] = []
        for count in counts:
            length = sum(count.values())
            if length <= 0:
                vectors.append({})
                norms.append(1.0)
                continue
            vector = {
                term: (frequency / length) * self._idf.get(term, 1.0)
                for term, frequency in count.items()
            }
            norm = math.sqrt(sum(weight * weight for weight in vector.values())) or 1.0
            vectors.append(vector)
            norms.append(norm)
        self._vectors = vectors
        self._norms = norms

    # -- querying -----------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        exclude_chapter_id: str | None = None,
    ) -> list[RetrievalHit]:
        if not self._documents or int(top_k) <= 0:
            return []
        query_counts = Counter(tokenize(query))
        if not query_counts:
            return []
        query_length = sum(query_counts.values())
        query_vector = {
            term: (frequency / query_length) * self._idf[term]
            for term, frequency in query_counts.items()
            if term in self._idf
        }
        query_norm = math.sqrt(sum(weight * weight for weight in query_vector.values()))
        if query_norm <= 0:
            return []

        scored: list[tuple[float, _Document, str]] = []
        for index, document in enumerate(self._documents):
            if (
                exclude_chapter_id
                and document.source_type == "chapter"
                and document.source_id == exclude_chapter_id
            ):
                continue
            vector = self._vectors[index]
            dot = sum(
                weight * vector.get(term, 0.0) for term, weight in query_vector.items()
            )
            if dot <= 0:
                continue
            score = dot / (query_norm * self._norms[index])
            if score < self.min_score:
                continue
            matched = sorted(
                (term for term in query_vector if vector.get(term)),
                key=lambda term: -(query_vector[term] * vector.get(term, 0.0)),
            )[:3]
            reason = f"词面相似度 {score:.2f}"
            if matched:
                reason += "；命中关键词：" + "、".join(matched)
            scored.append((score, document, reason))

        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].source_type,
                item[1].source_id,
            )
        )
        return [
            RetrievalHit(
                source_type=document.source_type,
                source_id=document.source_id,
                title=document.title,
                text=document.text,
                score=round(score, 6),
                reason=reason,
            )
            for score, document, reason in scored[: int(top_k)]
        ]


__all__ = ["LexicalRetriever", "Retriever", "SOURCE_LABELS", "tokenize"]
