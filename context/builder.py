"""Layered context construction for chapter generation.

The builder turns a :class:`~novel_ai.domain.models.ProjectSnapshot` plus the
current :class:`~novel_ai.domain.models.Chapter` into a :class:`ContextPlan`:

* summaries are used by layer (global / volume / previous chapters),
* characters contribute compact "current state" cards,
* active memories become durable anchors (foreshadowing / rules / timeline),
* the current chapter outline (plus volume and beats) is always present,
* recent chapters are included as excerpts for a natural continuation,
* an optional offline lexical retriever adds relevant cross-chapter material,
* the final messages contain exactly one system message and one user message,
* the system message ends with an unremovable "only the current chapter" guard.

The full text of the current chapter is never included (it is what the model is
about to write).  When the estimated prompt exceeds the derived input budget,
sections are compressed or dropped strictly by priority and every decision is
recorded in ``compression_notes`` / ``truncated_sections``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from novel_ai.context.budget import (
    MIN_MANDATORY_TOKENS,
    MIN_SECTION_TOKENS,
    SECTION_BUDGET_SHARES,
    derive_budget,
)
from novel_ai.context.compressor import fit_to_tokens, format_memory_anchors, truncate_head_tail
from novel_ai.context.estimator import (
    CONVERSATION_OVERHEAD_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    TokenEstimator,
)
from novel_ai.context.models import ContextPlan, ContextSection, RetrievalHit
from novel_ai.context.retriever import SOURCE_LABELS, LexicalRetriever, Retriever
from novel_ai.domain.models import Chapter, MemoryItem, ProjectSnapshot
from novel_ai.prompts.templates import (
    DEFAULT_PROMPT_TEMPLATE,
    PLACEHOLDERS,
    PromptTemplate,
    render_template,
)
from novel_ai.providers.base import ChatMessage

logger = logging.getLogger(__name__)

#: Stable display order of context sections (matches docs/CONTRACTS.md §4).
SECTION_ORDER: tuple[str, ...] = (
    "system",
    "worldbuilding",
    "style",
    "global_summary",
    "volume_summary",
    "chapter_summaries",
    "characters",
    "memories",
    "outline",
    "recent_chapters",
    "retrieval",
    "requirements",
    "output_format",
)

SECTION_TITLES: dict[str, str] = {
    "system": "系统角色与硬性规则",
    "worldbuilding": "作品设定",
    "style": "风格与禁忌",
    "global_summary": "全局故事摘要",
    "volume_summary": "本卷摘要",
    "chapter_summaries": "前情章节摘要",
    "characters": "角色当前状态",
    "memories": "长期记忆锚点",
    "outline": "本章大纲",
    "recent_chapters": "最近原文",
    "retrieval": "相关检索片段",
    "requirements": "本章额外要求",
    "output_format": "输出格式",
}

#: 1 = most important.  Used both for budget allocation and for dropping
#: sections when the prompt must be shortened (higher number dropped first).
SECTION_PRIORITIES: dict[str, int] = {
    "system": 1,
    "worldbuilding": 2,
    "outline": 3,
    "characters": 4,
    "output_format": 5,
    "requirements": 6,
    "style": 7,
    "memories": 8,
    "recent_chapters": 9,
    "retrieval": 10,
    "chapter_summaries": 11,
    "global_summary": 12,
    "volume_summary": 13,
}

#: These sections are never dropped wholesale; they are only compressed.
MANDATORY_SECTIONS: frozenset[str] = frozenset(
    {"system", "worldbuilding", "outline", "characters", "output_format"}
)

CONTENT_SECTION_KEYS: tuple[str, ...] = tuple(
    key for key in SECTION_ORDER if key != "system"
)

#: Header of the hard rule appended to the very end of the system message.
HARD_GUARD_HEADER = "【最高优先级硬性规则】"

MAX_CHARACTERS = 16
MAX_CHAPTER_SUMMARIES = 40
MAX_MEMORY_ANCHORS = 40
RETRIEVAL_HIT_CHARS = 420

_CHARACTER_LABELS = (
    ("current_state", "当前状态"),
    ("personality", "性格"),
    ("goals", "目标"),
    ("background", "背景"),
    ("arc", "人物弧光"),
)


@dataclass
class BuildOptions:
    """User- and configuration-controlled options for one context build."""

    target_words: int = 2500
    requirements: str = ""
    pov: str = ""
    tone: str = ""
    pacing: str = ""
    dialogue_ratio: float | None = None
    model_context_window: int = 128_000
    max_output_tokens: int = 4096
    recent_chapters: int = 2
    retrieval_top_k: int = 6
    use_retrieval: bool = True


def current_chapter_guard(chapter: Chapter) -> str:
    """Return the unremovable hard rule that ends the system message."""

    title = (chapter.title or "").strip()
    label = f"第{chapter.number}章"
    if title:
        label += f"《{title}》"
    return (
        f"{HARD_GUARD_HEADER}只写当前这一章：只输出{label}的章节标题与完整正文；"
        "不得写作或预告下一章，不得输出本章之外的任何内容"
        "（包括创作说明、修改建议、总结、作者的话）。"
    )


@dataclass
class _StyleState:
    """Style and taboo texts kept separate for their own placeholders."""

    style: str
    taboos: str

    def __post_init__(self) -> None:
        self.style = (self.style or "").strip()
        self.taboos = (self.taboos or "").strip()

    def content(self) -> str:
        pieces: list[str] = []
        if self.style:
            pieces.append(f"风格基调：{self.style}")
        if self.taboos:
            pieces.append(f"风格禁忌：{self.taboos}")
        return "\n".join(pieces)

    def fit(self, estimator: TokenEstimator, max_tokens: int) -> tuple[str, bool]:
        """Compress style/taboos into ``max_tokens``; return (content, truncated)."""

        content = self.content()
        if estimator.estimate(content) <= max_tokens:
            return content, False
        budget = max(int(max_tokens), MIN_SECTION_TOKENS)
        label_tokens = estimator.estimate("风格基调：\n风格禁忌：")
        available = max(8, budget - label_tokens)
        taboo_budget = (
            min(estimator.estimate(self.taboos), max(6, available // 3))
            if self.taboos
            else 0
        )
        style_budget = max(4, available - taboo_budget)
        if self.style:
            self.style = fit_to_tokens(self.style, estimator, style_budget).text.strip()
        if self.taboos:
            self.taboos = fit_to_tokens(self.taboos, estimator, taboo_budget).text.strip()
        return self.content(), True


class ContextBuilder:
    """Build a deterministic, budget-aware :class:`ContextPlan`."""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or TokenEstimator()
        #: Optional injection point for a future vector retriever.
        self.retriever: Retriever | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build(
        self,
        snapshot: ProjectSnapshot,
        current_chapter: Chapter,
        options: BuildOptions,
        template: PromptTemplate | None = None,
    ) -> ContextPlan:
        estimator = self.estimator
        chapter = current_chapter
        project = snapshot.project
        template = template or DEFAULT_PROMPT_TEMPLATE
        budget = derive_budget(options.model_context_window, options.max_output_tokens)

        compression_notes: list[str] = []
        truncated_sections: list[str] = []

        # -- 1. raw section material -------------------------------------
        raw: dict[str, str] = {}

        worldbuilding = self._worldbuilding_text(project)
        if worldbuilding:
            raw["worldbuilding"] = worldbuilding

        style_state = _StyleState(project.style, project.taboos)
        if style_state.content():
            raw["style"] = style_state.content()

        global_summary = (snapshot.global_summary() or "").strip()
        if global_summary:
            raw["global_summary"] = global_summary

        volume_id = self._volume_id(snapshot, chapter)
        volume_summary = (snapshot.volume_summary(volume_id) or "").strip()
        if volume_summary:
            raw["volume_summary"] = volume_summary

        chapter_summaries = self._chapter_summaries_text(snapshot, chapter)
        if chapter_summaries:
            raw["chapter_summaries"] = chapter_summaries

        characters = self._characters_text(snapshot)
        if characters:
            raw["characters"] = characters

        memories = self._memories_text(snapshot)
        if memories:
            raw["memories"] = memories

        raw["outline"] = self._outline_text(snapshot, chapter, volume_id)

        recent = self._recent_chapters_text(snapshot, chapter, options)
        if recent:
            raw["recent_chapters"] = recent

        requirements = self._requirements_text(chapter, options)
        if requirements:
            raw["requirements"] = requirements

        target_words = self._target_words(options, chapter, project)
        raw["output_format"] = self._output_format_text(chapter, target_words)

        # -- 2. optional offline retrieval --------------------------------
        retrieval_hits: list[RetrievalHit] = []
        if options.use_retrieval and int(options.retrieval_top_k) > 0:
            retrieval_hits = self._retrieve(snapshot, chapter, options)
            retrieval_text = self._retrieval_text(retrieval_hits)
            if retrieval_text:
                raw["retrieval"] = retrieval_text

        metadata = self._metadata_values(project, chapter, options, target_words)
        guard = current_chapter_guard(chapter)
        present = [key for key in CONTENT_SECTION_KEYS if key in raw]

        # -- 3. estimate fixed template overhead --------------------------
        empty_values: dict[str, Any] = {name: "" for name in PLACEHOLDERS}
        empty_values.update(metadata)
        overhead_system = render_template(template.system_template, empty_values).strip()
        overhead_user = render_template(template.user_template, empty_values).strip()
        overhead_tokens = estimator.estimate_messages(
            [
                ChatMessage(
                    role="system",
                    content=(overhead_system + "\n\n" + guard) if overhead_system else guard,
                ),
                ChatMessage(role="user", content=overhead_user),
            ]
        )
        content_budget = max(0, budget.input_budget - overhead_tokens)

        # -- 4. allocate a soft cap per section and compress --------------
        share_total = sum(SECTION_BUDGET_SHARES.get(key, 0.0) for key in present) or 1.0
        sections: dict[str, ContextSection] = {}
        for key in present:
            share = SECTION_BUDGET_SHARES.get(key, 0.0) / share_total
            cap = max(MIN_SECTION_TOKENS, int(content_budget * share))
            source_text = raw[key]
            source_tokens = estimator.estimate(source_text)
            truncated = False
            note = ""
            if key == "style":
                content, truncated = style_state.fit(estimator, cap)
                if truncated:
                    note = "风格与禁忌超出预算分配，已按首尾保留压缩。"
            else:
                result = fit_to_tokens(source_text, estimator, cap)
                content = result.text
                truncated = result.truncated
                note = result.note
            tokens = estimator.estimate(content)
            sections[key] = ContextSection(
                key=key,
                title=SECTION_TITLES.get(key, key),
                content=content,
                tokens=tokens,
                priority=SECTION_PRIORITIES.get(key, 99),
                included=True,
                truncated=truncated,
                note=note,
            )
            if truncated:
                _append_unique(truncated_sections, key)
                compression_notes.append(
                    f"{sections[key].title}：超出预算分配，已压缩"
                    f"（约 {source_tokens} → {tokens} tokens）。"
                )

        # -- 5. render, then enforce the hard input budget ----------------
        def _values() -> dict[str, Any]:
            values: dict[str, Any] = {name: "" for name in PLACEHOLDERS}
            values.update(metadata)
            for key, section in sections.items():
                if key == "style":
                    if section.included:
                        values["style"] = style_state.style or "（未指定，保持与前文一致）"
                        values["taboos"] = style_state.taboos or "（无特别禁忌）"
                    continue
                values[key] = section.content if section.included else ""
            if "style" not in sections:
                values["style"] = style_state.style or "（未指定，保持与前文一致）"
                values["taboos"] = style_state.taboos or "（无特别禁忌）"
            return values

        def _render() -> tuple[str, str]:
            values = _values()
            system_text = render_template(template.system_template, values).strip()
            user_text = render_template(template.user_template, values).strip()
            system_text = (
                f"{system_text}\n\n{guard}" if system_text else guard
            )
            return system_text, user_text

        def _messages(system_text: str, user_text: str) -> list[ChatMessage]:
            return [
                ChatMessage(role="system", content=system_text),
                ChatMessage(role="user", content=user_text),
            ]

        system_prompt, user_prompt = _render()
        messages = _messages(system_prompt, user_prompt)
        input_tokens = estimator.estimate_messages(messages)
        budget_limit = budget.input_budget

        rounds = 0
        maximum_rounds = len(present) + 32
        while input_tokens > budget_limit and rounds < maximum_rounds:
            rounds += 1
            droppable = [
                key
                for key in present
                if sections[key].included and key not in MANDATORY_SECTIONS
            ]
            if droppable:
                victim = max(
                    droppable,
                    key=lambda key: (
                        SECTION_PRIORITIES.get(key, 99),
                        -sections[key].tokens,
                    ),
                )
                sections[victim].included = False
                sections[victim].note = "整体超出上下文预算，已按优先级整节省略。"
                _append_unique(truncated_sections, victim)
                compression_notes.append(
                    f"{sections[victim].title}：超出上下文预算，已按优先级整节省略。"
                )
                system_prompt, user_prompt = _render()
                messages = _messages(system_prompt, user_prompt)
                input_tokens = estimator.estimate_messages(messages)
                continue

            shrinkable = [
                key
                for key in present
                if sections[key].included
                and key in MANDATORY_SECTIONS
                and sections[key].tokens > MIN_MANDATORY_TOKENS
            ]
            if not shrinkable:
                break
            victim = max(shrinkable, key=lambda key: sections[key].tokens)
            target = max(MIN_MANDATORY_TOKENS, int(sections[victim].tokens * 0.7))
            result = fit_to_tokens(sections[victim].content, estimator, target)
            sections[victim].content = result.text
            sections[victim].tokens = estimator.estimate(result.text)
            sections[victim].truncated = True
            sections[victim].note = (
                result.note or "上下文预算不足，已强制压缩。"
            )
            _append_unique(truncated_sections, victim)
            compression_notes.append(
                f"{sections[victim].title}：预算不足，已强制压缩至约 "
                f"{sections[victim].tokens} tokens。"
            )
            system_prompt, user_prompt = _render()
            messages = _messages(system_prompt, user_prompt)
            input_tokens = estimator.estimate_messages(messages)

        # -- 6. last-resort safety net (pathologically small windows) -----
        if input_tokens > budget_limit:
            compression_notes.append(
                "上下文预算过小：已对最终提示做保底截断，以保证不超过模型窗口。"
            )
            guard_tokens = estimator.estimate(guard)
            for _ in range(8):
                if input_tokens <= budget_limit:
                    break
                user_cost = (
                    estimator.estimate(user_prompt)
                    + MESSAGE_OVERHEAD_TOKENS
                    + CONVERSATION_OVERHEAD_TOKENS
                )
                allowed_body = max(8, budget_limit - user_cost - guard_tokens)
                body = (
                    system_prompt[: -len(guard)].rstrip()
                    if guard and system_prompt.endswith(guard)
                    else system_prompt
                )
                if estimator.estimate(body) <= allowed_body:
                    break
                body = fit_to_tokens(body, estimator, allowed_body, head_ratio=0.4).text
                system_prompt = (f"{body}\n\n{guard}" if body else guard).strip()
                messages = _messages(system_prompt, user_prompt)
                input_tokens = estimator.estimate_messages(messages)
            shrink_rounds = 0
            while (
                input_tokens > budget_limit
                and estimator.estimate(user_prompt) > 8
                and shrink_rounds < 40
            ):
                shrink_rounds += 1
                target = max(8, int(estimator.estimate(user_prompt) * 0.75))
                user_prompt = fit_to_tokens(
                    user_prompt, estimator, target, head_ratio=0.4
                ).text
                messages = _messages(system_prompt, user_prompt)
                input_tokens = estimator.estimate_messages(messages)

        # -- 7. assemble the plan -----------------------------------------
        messages = _messages(system_prompt, user_prompt)
        input_tokens = estimator.estimate_messages(messages)
        system_section = ContextSection(
            key="system",
            title=SECTION_TITLES["system"],
            content=system_prompt,
            tokens=estimator.estimate(system_prompt),
            priority=SECTION_PRIORITIES["system"],
            included=True,
            truncated=False,
            note="",
        )
        ordered_sections = [system_section] + [sections[key] for key in present]
        return ContextPlan(
            sections=ordered_sections,
            messages=messages,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            input_tokens=input_tokens,
            input_budget=budget.input_budget,
            context_window=budget.context_window,
            reserved_output=budget.reserved_output,
            compression_notes=compression_notes,
            retrieval_hits=list(retrieval_hits),
            truncated_sections=truncated_sections,
        )

    # ------------------------------------------------------------------
    # Section material
    # ------------------------------------------------------------------
    @staticmethod
    def _worldbuilding_text(project: Any) -> str:
        pieces: list[str] = []
        logline = (project.logline or "").strip()
        synopsis = (project.synopsis or "").strip()
        worldbuilding = (project.worldbuilding or "").strip()
        if logline:
            pieces.append(f"一句话简介：{logline}")
        if synopsis:
            pieces.append(f"故事梗概：{synopsis}")
        if worldbuilding:
            pieces.append(f"世界观与核心设定：\n{worldbuilding}")
        return "\n\n".join(pieces)

    @staticmethod
    def _previous_chapters(snapshot: ProjectSnapshot, chapter: Chapter) -> list[Chapter]:
        return [
            item
            for item in snapshot.previous_chapters(chapter)
            if item.id != chapter.id
        ]

    def _volume_id(self, snapshot: ProjectSnapshot, chapter: Chapter) -> str | None:
        if chapter.volume_id:
            return chapter.volume_id
        outline = snapshot.outline_by_id(chapter.outline_id)
        parent_id = outline.parent_id if outline else None
        while parent_id:
            parent = snapshot.outline_by_id(parent_id)
            if parent is None:
                break
            if parent.kind == "volume":
                return parent.id
            parent_id = parent.parent_id
        previous = self._previous_chapters(snapshot, chapter)
        if previous and previous[-1].volume_id:
            return previous[-1].volume_id
        volumes = [node for node in snapshot.ordered_outline() if node.kind == "volume"]
        return volumes[0].id if volumes else None

    def _chapter_summaries_text(self, snapshot: ProjectSnapshot, chapter: Chapter) -> str:
        lines: list[str] = []
        previous = self._previous_chapters(snapshot, chapter)
        for item in previous[-MAX_CHAPTER_SUMMARIES:]:
            summary = (snapshot.chapter_summary(item.id) or "").strip()
            if not summary:
                continue
            title = (item.title or "").strip()
            label = f"第{item.number}章" + (f"《{title}》" if title else "")
            lines.append(f"- {label}：{summary}")
        return "\n".join(lines)

    def _characters_text(self, snapshot: ProjectSnapshot) -> str:
        characters = sorted(
            snapshot.active_characters(),
            key=lambda item: (item.sort_order, item.name, item.id),
        )[:MAX_CHARACTERS]
        cards: list[str] = []
        for character in characters:
            header = (character.name or "").strip() or "未命名角色"
            role = (character.role or "").strip()
            if role:
                header = f"{header}（{role}）"
            lines = [f"◆ {header}"]
            for field_name, label in _CHARACTER_LABELS:
                value = (getattr(character, field_name, "") or "").strip()
                if value:
                    lines.append(f"{label}：{value}")
            relationships = character.relationship_text.strip()
            if relationships:
                lines.append(f"关系：{relationships}")
            cards.append("\n".join(lines))
        return "\n\n".join(cards)

    def _memories_text(self, snapshot: ProjectSnapshot) -> str:
        memories: list[MemoryItem] = [
            item
            for item in snapshot.active_memories()
            if (item.status or "active") == "active"
        ]
        return format_memory_anchors(memories, limit=MAX_MEMORY_ANCHORS)

    def _outline_text(
        self,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        volume_id: str | None,
    ) -> str:
        lines: list[str] = []
        volume = snapshot.outline_by_id(volume_id)
        if volume is not None and volume.kind == "volume":
            lines.append(f"【本卷】{(volume.title or '未命名卷').strip()}")
            for label, value in (
                ("本卷概要", volume.summary),
                ("本卷目标", volume.goals),
                ("本卷冲突", volume.conflicts),
            ):
                text = (value or "").strip()
                if text:
                    lines.append(f"{label}：{text}")

        outline = snapshot.outline_by_id(chapter.outline_id)
        if outline is not None and outline is not volume:
            lines.append(f"【本章】{(outline.title or chapter.display_title).strip()}")
            for label, value in (
                ("概要", outline.summary),
                ("目标", outline.goals),
                ("冲突", outline.conflicts),
            ):
                text = (value or "").strip()
                if text:
                    lines.append(f"{label}：{text}")
            beats = [
                node
                for node in snapshot.ordered_outline()
                if node.kind == "beat" and node.parent_id == outline.id
            ]
            if beats:
                lines.append("【本章节拍】")
                for index, beat in enumerate(beats, start=1):
                    detail = "；".join(
                        part.strip()
                        for part in (beat.summary, beat.goals, beat.conflicts)
                        if (part or "").strip()
                    )
                    line = f"{index}. {(beat.title or '未命名节拍').strip()}"
                    if detail:
                        line = f"{line}：{detail}"
                    lines.append(line)
        elif outline is None:
            title = (chapter.title or "").strip()
            label = f"第{chapter.number}章" + (f" {title}" if title else "")
            lines.append(f"【本章】{label}")
            lines.append(
                "（未提供本章大纲：请严格依据作品设定、前情与角色状态推进，"
                "不要引入与已写内容矛盾的新设定。）"
            )
        return "\n".join(lines)

    def _recent_chapters_text(
        self,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        options: BuildOptions,
    ) -> str:
        limit = max(0, int(options.recent_chapters))
        if limit <= 0:
            return ""
        previous = self._previous_chapters(snapshot, chapter)[-limit:]
        if not previous:
            return ""
        max_chars = max(800, int(options.target_words or 2500) * 2)
        blocks: list[str] = []
        for item in previous:
            body = (item.content or "").strip()
            if not body:
                continue
            excerpt_text = truncate_head_tail(body, max_chars, head_ratio=0.45)
            title = (item.title or "").strip()
            label = f"第{item.number}章" + (f" {title}" if title else "")
            blocks.append(f"【{label}（原文节选）】\n{excerpt_text}")
        return "\n\n".join(blocks)

    @staticmethod
    def _requirements_text(chapter: Chapter, options: BuildOptions) -> str:
        items: list[str] = []
        option_requirements = (options.requirements or "").strip()
        chapter_requirements = (chapter.requirements or "").strip()
        if option_requirements:
            items.append(option_requirements)
        if chapter_requirements and chapter_requirements not in items:
            items.append(chapter_requirements)
        if not items:
            return ""
        return "\n".join(f"- {item}" for item in items)

    @staticmethod
    def _output_format_text(chapter: Chapter, target_words: int) -> str:
        title = (chapter.title or "").strip() or "未命名"
        return (
            f"1. 第一行输出章节标题：第{chapter.number}章《{title}》。\n"
            "2. 空一行后输出正文，自然分段，不写小标题。\n"
            f"3. 正文约 {target_words} 字，只输出本章，不写下一章、预告或说明。"
        )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    def _retrieve(
        self,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        options: BuildOptions,
    ) -> list[RetrievalHit]:
        retriever = self.retriever if self.retriever is not None else LexicalRetriever()
        query = self._retrieval_query(snapshot, chapter, options)
        if not query.strip():
            return []
        try:
            retriever.index(snapshot, exclude_chapter_id=chapter.id)
            return retriever.search(
                query,
                top_k=max(1, int(options.retrieval_top_k)),
                exclude_chapter_id=chapter.id,
            )
        except Exception as exc:  # retrieval must never break generation
            logger.warning("Offline retrieval failed, continuing without it: %s", exc)
            return []

    def _retrieval_query(
        self,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        options: BuildOptions,
    ) -> str:
        pieces: list[str] = [chapter.title or ""]
        outline = snapshot.outline_by_id(chapter.outline_id)
        if outline is not None:
            pieces.extend([outline.title, outline.summary, outline.goals, outline.conflicts])
        pieces.extend([options.requirements, chapter.requirements])
        previous = self._previous_chapters(snapshot, chapter)
        if previous:
            pieces.append((previous[-1].content or "")[-600:])
        return "\n".join(piece.strip() for piece in pieces if (piece or "").strip())

    @staticmethod
    def _retrieval_text(hits: list[RetrievalHit]) -> str:
        if not hits:
            return ""
        blocks: list[str] = []
        for index, hit in enumerate(hits, start=1):
            label = SOURCE_LABELS.get(hit.source_type, hit.source_type)
            title = (hit.title or "").strip() or "未命名"
            body = truncate_head_tail(
                (hit.text or "").strip(), RETRIEVAL_HIT_CHARS, head_ratio=0.45
            )
            blocks.append(f"{index}. 【{label}·{title}】{hit.reason}\n{body}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _target_words(options: BuildOptions, chapter: Chapter, project: Any) -> int:
        value = int(options.target_words or 0)
        if value <= 0:
            value = int(chapter.target_words or 0)
        if value <= 0:
            value = int(project.target_words or 0)
        if value <= 0:
            value = 2500
        return max(200, value)

    @staticmethod
    def _format_ratio(value: float | int | None) -> str:
        try:
            ratio = float(value) if value is not None else 0.35
        except (TypeError, ValueError):
            ratio = 0.35
        if ratio > 1.0:
            ratio = ratio / 100.0
        ratio = min(max(ratio, 0.0), 1.0)
        return f"{round(ratio * 100)}%"

    def _metadata_values(
        self,
        project: Any,
        chapter: Chapter,
        options: BuildOptions,
        target_words: int,
    ) -> dict[str, str]:
        pov = (options.pov or chapter.pov or project.pov or "").strip()
        tone = (options.tone or project.tone or "").strip()
        pacing = (options.pacing or project.pacing or "").strip()
        dialogue_ratio = (
            options.dialogue_ratio
            if options.dialogue_ratio is not None
            else project.dialogue_ratio
        )
        return {
            "project_title": (project.title or "").strip() or "未命名作品",
            "genre": (project.genre or "").strip() or "未指定",
            "chapter_number": str(chapter.number),
            "chapter_title": (chapter.title or "").strip() or "未命名",
            "target_words": str(target_words),
            "pov": pov or "第三人称有限视角",
            "tone": tone or "克制而有张力",
            "pacing": pacing or "张弛有度",
            "dialogue_ratio": self._format_ratio(dialogue_ratio),
        }


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


__all__ = [
    "BuildOptions",
    "CONTENT_SECTION_KEYS",
    "ContextBuilder",
    "HARD_GUARD_HEADER",
    "MANDATORY_SECTIONS",
    "SECTION_ORDER",
    "SECTION_PRIORITIES",
    "SECTION_TITLES",
    "current_chapter_guard",
]
