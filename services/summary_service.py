"""Post-generation memory maintenance.

After a chapter is accepted, this service asks the selected model for a compact
JSON update containing: chapter summary, rolling global summary, current
character states and new continuity anchors (foreshadowing, items, conflicts).
Failures are non-fatal: the chapter is already saved, and a heuristic chapter
summary keeps the memory from becoming empty.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from novel_ai.domain.models import (
    Chapter,
    MemoryItem,
    ProjectSnapshot,
    ProviderConfig,
    Summary,
    count_words,
)
from novel_ai.providers.base import (
    BaseProvider,
    ChatMessage,
    ProviderError,
    ProviderErrorKind,
)
from novel_ai.providers.manager import ProviderManager
from novel_ai.storage.repository import NovelRepository

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = """你是一位严谨的中文长篇小说责任编辑，负责维护故事的连续性记忆。
你的任务是根据最新章节正文，更新故事摘要、角色当前状态和关键伏笔/设定。

必须只输出一个合法 JSON 对象，不要 Markdown 代码围栏，不要任何解释文字。JSON 结构：
{
  "chapter_summary": "本章 150-300 字摘要，包含事件推进、人物变化、伏笔与结尾钩子",
  "global_summary": "覆盖全书至当前章的总摘要，400-800 字，保留主线、关键转折、伏笔、时间线",
  "character_states": [
    {"name": "角色名（必须使用给定角色卡中的名字）", "current_state": "截至本章末的处境、伤势、目标、关系变化，80字内"}
  ],
  "memory_items": [
    {"kind": "foreshadow|timeline|item|conflict|relationship|rule|setting", "title": "简短标题", "content": "需要在后文保持一致的记忆点", "priority": 3}
  ]
}

原则：
- 只记录正文已经发生的事实，不编造未出现的情节。
- 保留未解决的冲突、伏笔、关键道具、人物关系和不可逆事件。
- global_summary 要吸收旧总摘要的有效信息，不能简单丢失去过的情节。
- 若某角色本章未出场，不要更新其状态。
- memory_items 只提取真正影响后续章节的锚点，最多 6 条；没有则为空数组。
- priority 为 1-5 的整数，5 表示绝不能违背的硬设定。"""


@dataclass
class MemoryUpdateResult:
    success: bool
    warning: str = ""
    chapter_summary: str = ""
    global_summary_updated: bool = False
    character_updates: int = 0
    memories_added: int = 0
    raw: str = ""


@dataclass
class _ParsedUpdate:
    chapter_summary: str = ""
    global_summary: str = ""
    character_states: list[dict[str, str]] = field(default_factory=list)
    memory_items: list[dict[str, Any]] = field(default_factory=list)


class SummaryService:
    def __init__(
        self,
        repo: NovelRepository,
        provider_manager: ProviderManager,
    ) -> None:
        self.repo = repo
        self.provider_manager = provider_manager

    # ------------------------------------------------------------------ public
    def update_after_chapter(
        self,
        project_id: str,
        chapter: Chapter,
        full_text: str,
        *,
        provider: BaseProvider | None = None,
        config: ProviderConfig | None = None,
    ) -> MemoryUpdateResult:
        snapshot = self.repo.load_snapshot(project_id)
        if snapshot is None:
            return MemoryUpdateResult(False, "项目不存在，无法更新记忆。")

        cfg = config or self.provider_manager.default_config()
        if cfg is None:
            fallback = self._fallback_chapter_summary(project_id, chapter, full_text)
            return MemoryUpdateResult(
                False,
                "没有可用 AI 供应商，已使用本地启发式章节摘要。",
                chapter_summary=fallback,
            )
        try:
            active_provider = provider or self.provider_manager.build_provider(cfg)
            parsed, raw = self._request_update(active_provider, snapshot, chapter, full_text)
        except ProviderError as exc:
            logger.warning("Memory update provider error: %s", exc)
            fallback = self._fallback_chapter_summary(project_id, chapter, full_text)
            return MemoryUpdateResult(
                False,
                f"摘要更新失败（章节已保存）：{exc.user_message}",
                chapter_summary=fallback,
            )
        except Exception as exc:
            logger.exception("Unexpected memory update failure")
            fallback = self._fallback_chapter_summary(project_id, chapter, full_text)
            return MemoryUpdateResult(
                False,
                f"摘要更新异常（章节已保存）：{exc}",
                chapter_summary=fallback,
            )

        warnings: list[str] = []
        character_updates = 0
        memories_added = 0

        # A user may have manually edited a summary; automatic regeneration
        # should not clobber that work silently.
        existing_chapter = self.repo.get_summary(project_id, "chapter", chapter.id)
        chapter_summary_text = parsed.chapter_summary.strip() or self._fallback_chapter_summary(
            project_id, chapter, full_text
        )
        if existing_chapter and existing_chapter.is_user_edited:
            warnings.append("本章摘要已手动编辑，自动摘要在“上下文与记忆”中可手动合并。")
        else:
            self.repo.save_summary(
                Summary.create(
                    project_id,
                    "chapter",
                    chapter_summary_text,
                    ref_id=chapter.id,
                    token_count=_estimate_tokens(chapter_summary_text),
                    strategy="ai-rolling",
                )
            )

        existing_global = self.repo.get_summary(project_id, "global", None)
        global_updated = False
        if parsed.global_summary.strip():
            if existing_global and existing_global.is_user_edited:
                warnings.append("全局摘要已手动编辑，未自动覆盖。")
            else:
                self.repo.save_summary(
                    Summary.create(
                        project_id,
                        "global",
                        parsed.global_summary.strip(),
                        ref_id=None,
                        token_count=_estimate_tokens(parsed.global_summary),
                        strategy="ai-rolling",
                    )
                )
                global_updated = True

        for update in parsed.character_states:
            name = str(update.get("name", "")).strip()
            state = str(update.get("current_state", "")).strip()
            if not name or not state:
                continue
            character = _match_character(snapshot, name)
            if character is None:
                continue
            character.current_state = state
            from novel_ai.domain.models import utc_now

            character.updated_at = utc_now()
            self.repo.save_character(character)
            character_updates += 1

        existing_titles = {
            m.title.strip().casefold()
            for m in snapshot.memories
            if m.status == "active"
        }
        valid_kinds = {
            "foreshadow",
            "timeline",
            "item",
            "conflict",
            "relationship",
            "rule",
            "setting",
        }
        for item in parsed.memory_items[:8]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "setting")).strip()
            title = str(item.get("title", "")).strip()
            content = str(item.get("content", "")).strip()
            if not title or not content or kind not in valid_kinds:
                continue
            if title.casefold() in existing_titles:
                continue
            try:
                priority = int(item.get("priority", 3))
            except (TypeError, ValueError):
                priority = 3
            self.repo.save_memory(
                MemoryItem.create(
                    project_id,
                    kind,
                    title,
                    content,
                    priority=max(1, min(5, priority)),
                    source_chapter_id=chapter.id,
                    is_manual=False,
                )
            )
            existing_titles.add(title.casefold())
            memories_added += 1

        if not parsed.chapter_summary.strip():
            warnings.append("模型未返回章节摘要，已使用本地启发式摘要。")
        logger.info(
            "Memory updated for %s: chars=%d, memories=%d",
            chapter.id,
            character_updates,
            memories_added,
        )
        return MemoryUpdateResult(
            success=True,
            warning="；".join(warnings),
            chapter_summary=chapter_summary_text,
            global_summary_updated=global_updated,
            character_updates=character_updates,
            memories_added=memories_added,
            raw=raw,
        )

    # ------------------------------------------------------------------ model
    def _request_update(
        self,
        provider: BaseProvider,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        full_text: str,
    ) -> tuple[_ParsedUpdate, str]:
        project = snapshot.project
        previous_summaries = [
            s
            for s in sorted(
                (s for s in snapshot.summaries if s.scope == "chapter"),
                key=lambda s: s.updated_at,
            )
            if s.ref_id != chapter.id
        ][-5:]
        chapter_list = "\n".join(
            f"- {s.ref_id}: {_truncate(s.content, 180)}" for s in previous_summaries
        ) or "（尚未生成前情摘要）"
        characters = "\n".join(
            f"- {c.name}｜{c.role}｜当前状态：{c.current_state or '未记录'}"
            for c in snapshot.active_characters()
        ) or "（暂无角色卡）"
        memory_anchors = "\n".join(
            f"- [{m.kind}] {m.title}：{_truncate(m.content, 160)}"
            for m in snapshot.active_memories()[:12]
        ) or "（暂无关键记忆）"
        text_for_model = _head_tail(full_text, max_chars=20_000)
        user = f"""【作品】{project.title}｜类型：{project.genre or '未设定'}
【全局摘要（旧）】
{snapshot.global_summary() or "（尚未生成）"}

【最近章节摘要】
{chapter_list}

【角色卡与旧状态】
{characters}

【关键记忆锚点】
{memory_anchors}

【待总结章节】第{chapter.number}章 {chapter.title}
【章节正文】
{text_for_model}

请输出 JSON。"""
        request = provider.build_request(
            [
                ChatMessage(role="system", content=_SUMMARY_SYSTEM),
                ChatMessage(role="user", content=user),
            ],
            temperature=0.2,
            max_tokens=2600,
            stream=False,
        )
        result = provider.complete(request)
        raw = result.text.strip()
        return _parse_json_update(raw), raw

    def _fallback_chapter_summary(
        self, project_id: str, chapter: Chapter, full_text: str
    ) -> str:
        text = re.sub(r"\s+", " ", full_text or chapter.content).strip()
        summary = _head_tail(text, max_chars=520)
        if not summary:
            return ""
        existing = self.repo.get_summary(project_id, "chapter", chapter.id)
        if existing and existing.is_user_edited:
            return existing.content
        self.repo.save_summary(
            Summary.create(
                project_id,
                "chapter",
                summary,
                ref_id=chapter.id,
                token_count=_estimate_tokens(summary),
                strategy="heuristic",
            )
        )
        return summary


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_json_update(raw: str) -> _ParsedUpdate:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"模型返回的摘要不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("模型返回的摘要 JSON 顶层不是对象")

    update = _ParsedUpdate()
    update.chapter_summary = str(data.get("chapter_summary") or "").strip()
    update.global_summary = str(data.get("global_summary") or "").strip()
    states = data.get("character_states") or []
    if isinstance(states, list):
        update.character_states = [s for s in states if isinstance(s, dict)]
    items = data.get("memory_items") or []
    if isinstance(items, list):
        update.memory_items = [i for i in items if isinstance(i, dict)]
    return update


def _match_character(snapshot: ProjectSnapshot, name: str):
    target = name.strip().casefold()
    for character in snapshot.characters:
        if character.name.strip().casefold() == target:
            return character
    # Fall back to a unique substring match ("林默" -> "林默（主角）").
    partial = [
        c for c in snapshot.characters if target and target in c.name.strip().casefold()
    ]
    return partial[0] if len(partial) == 1 else None


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _head_tail(text: str, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.65)
    tail = max_chars - head
    return text[:head] + "\n……（中间内容略）……\n" + text[-tail:]


def _estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return max(1, cjk + other // 3)
