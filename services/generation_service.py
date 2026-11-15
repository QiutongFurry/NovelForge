"""Chapter generation orchestration.

This is the heart of the product:

1. Load a consistent snapshot and the selected provider.
2. Build a layered, token-budgeted context plan (one chapter only).
3. Stream exactly one chapter from the provider.
4. Save a draft + version snapshot, then update rolling memory.

The service is GUI independent and fully testable with a fake provider.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from novel_ai.config.settings import AppSettings
from novel_ai.context.builder import BuildOptions, ContextBuilder
from novel_ai.context.estimator import TokenEstimator
from novel_ai.context.models import ContextPlan
from novel_ai.domain.models import (
    Chapter,
    GenerationLog,
    Project,
    ProjectSnapshot,
    ProviderConfig,
)
from novel_ai.providers.base import (
    BaseProvider,
    ChatMessage,
    ProviderError,
    ProviderErrorKind,
    TokenUsage,
)
from novel_ai.providers.manager import ProviderManager
from novel_ai.prompts.templates import PromptStore
from novel_ai.services.project_service import ProjectService
from novel_ai.services.summary_service import MemoryUpdateResult, SummaryService
from novel_ai.storage.repository import NovelRepository

logger = logging.getLogger(__name__)


@dataclass
class GenerationOptions:
    target_words: int = 2500
    requirements: str = ""
    use_retrieval: bool = True
    auto_update_memory: bool = True
    provider_id: str | None = None


@dataclass
class GenerationUpdate:
    kind: str  # stage | text | usage | plan | done | error | cancelled | memory
    message: str = ""
    text: str = ""
    plan: ContextPlan | None = None
    usage: TokenUsage | None = None
    chapter: Chapter | None = None
    error: str = ""
    retryable: bool = False
    warning: str = ""


@dataclass
class GenerationOutcome:
    ok: bool
    cancelled: bool = False
    chapter: Chapter | None = None
    plan: ContextPlan | None = None
    full_text: str = ""
    error: str = ""
    retryable: bool = False
    memory_updated: bool = False
    warning: str = ""


class GenerationService:
    def __init__(
        self,
        repo: NovelRepository,
        project_service: ProjectService,
        provider_manager: ProviderManager,
        prompt_store: PromptStore,
        summary_service: SummaryService,
        settings: AppSettings | None = None,
    ) -> None:
        self.repo = repo
        self.project_service = project_service
        self.provider_manager = provider_manager
        self.prompt_store = prompt_store
        self.summary_service = summary_service
        self.settings = settings or AppSettings()
        self.context_builder = ContextBuilder()
        self.estimator = TokenEstimator()

    # ------------------------------------------------------------------ plans
    def build_plan(
        self,
        project_id: str,
        chapter_id: str,
        options: GenerationOptions,
    ) -> ContextPlan:
        snapshot = self.project_service.load_snapshot(project_id)
        if snapshot is None:
            raise ProviderError(
                "项目不存在或已被删除。", kind=ProviderErrorKind.CONFIGURATION
            )
        chapter = snapshot.chapter_by_id(chapter_id)
        if chapter is None:
            raise ProviderError(
                "章节不存在或已被删除。", kind=ProviderErrorKind.CONFIGURATION
            )
        config = self._resolve_provider_config(snapshot.project, options)
        if config is None:
            raise ProviderError(
                "尚未配置 AI 供应商。请先在“API 配置”中添加供应商并设为默认。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        template = self.prompt_store.load()
        build_options = self._build_options(snapshot, chapter, config, options)
        return self.context_builder.build(snapshot, chapter, build_options, template)

    # ----------------------------------------------------------------- generate
    def generate(
        self,
        project_id: str,
        chapter_id: str,
        options: GenerationOptions,
        cancel_event: threading.Event,
        on_update: Callable[[GenerationUpdate], None],
    ) -> GenerationOutcome:
        started = time.perf_counter()
        snapshot = self.project_service.load_snapshot(project_id)
        if snapshot is None:
            return self._fail(on_update, "项目不存在或已被删除。", retryable=False)
        chapter = snapshot.chapter_by_id(chapter_id)
        if chapter is None:
            return self._fail(on_update, "章节不存在或已被删除。", retryable=False)
        config = self._resolve_provider_config(snapshot.project, options)
        if config is None:
            return self._fail(
                on_update,
                "尚未配置 AI 供应商。请先在“API 配置”中添加供应商，并测试连接。",
                retryable=False,
            )

        provider_name = config.name or config.provider_type
        usage = TokenUsage()
        plan: ContextPlan | None = None
        try:
            provider = self.provider_manager.build_provider(config)
            self._emit(on_update, "stage", f"正在组装《{snapshot.project.title}》的上下文…")
            if cancel_event.is_set():
                return self._cancelled(on_update, chapter, None, provider_name, config)
            plan = self.build_plan(project_id, chapter_id, options)
            self._emit(on_update, "plan", "上下文已压缩完成。", plan=plan)
            usage.prompt_tokens = plan.input_tokens
            self._emit(
                on_update,
                "usage",
                "已估算上下文 Token。",
                usage=TokenUsage(
                    prompt_tokens=plan.input_tokens,
                    completion_tokens=0,
                    total_tokens=plan.input_tokens,
                ),
            )
            if cancel_event.is_set():
                return self._cancelled(on_update, chapter, plan, provider_name, config)

            self._emit(
                on_update,
                "stage",
                f"正在通过 {provider_name} / {config.model} 流式生成…",
            )
            request = provider.build_request(
                plan.messages,
                max_tokens=config.max_tokens,
                stream=config.stream,
            )
            full_text, finish_reason, usage = self._stream(
                provider=provider,
                request=request,
                cancel_event=cancel_event,
                on_update=on_update,
                base_usage=usage,
                plan=plan,
            )
            if cancel_event.is_set():
                return self._cancelled(on_update, chapter, plan, provider_name, config)
            if not full_text.strip():
                raise ProviderError(
                    "模型没有返回任何内容，请尝试重新生成或更换模型。",
                    kind=ProviderErrorKind.API,
                    retryable=True,
                )

            title, body, cut_note = _parse_chapter_output(full_text, chapter)
            warning = cut_note
            self._emit(on_update, "stage", "正文完成，正在保存草稿与版本…")
            saved = self._save_chapter(
                chapter, title, body, provider_name, config, plan
            )
            usage.prompt_tokens = usage.prompt_tokens or plan.input_tokens
            usage.completion_tokens = usage.completion_tokens or self.estimator.estimate(body)
            usage.total_tokens = usage.total_tokens or (
                usage.prompt_tokens + usage.completion_tokens
            )
            memory_updated = False
            if options.auto_update_memory and self.settings.auto_update_memory:
                self._emit(on_update, "stage", "正文已保存，正在更新摘要与角色状态…")
                memory_result = self._update_memory(
                    provider, config, project_id, saved, body, on_update
                )
                memory_updated = memory_result.success
                if memory_result.warning:
                    warning = _join_warnings(warning, memory_result.warning)
                self._emit(
                    on_update,
                    "memory",
                    "记忆已更新。"
                    if memory_result.success
                    else memory_result.warning or "记忆更新未完成。",
                    warning=memory_result.warning,
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._write_log(
                project_id=project_id,
                chapter_id=chapter.id,
                config=config,
                provider_name=provider_name,
                status="success",
                usage=usage,
                duration_ms=duration_ms,
            )
            final = GenerationOutcome(
                ok=True,
                chapter=saved,
                plan=plan,
                full_text=body,
                memory_updated=memory_updated,
                warning=warning,
            )
            self._emit(
                on_update,
                "done",
                "本章生成完成。",
                text=body,
                plan=plan,
                usage=usage,
                chapter=saved,
                warning=warning,
            )
            logger.info(
                "Generated chapter %s (%s/%s) in %d ms, tokens=%s",
                chapter.id,
                provider_name,
                config.model,
                duration_ms,
                usage,
            )
            return final
        except ProviderError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("Generation failed: %s", exc)
            self._write_log(
                project_id=project_id,
                chapter_id=chapter.id,
                config=config,
                provider_name=provider_name,
                status="error",
                usage=usage,
                duration_ms=duration_ms,
                error=exc.user_message,
            )
            self._emit(
                on_update,
                "error",
                exc.user_message,
                error=exc.user_message,
                retryable=exc.retryable,
                plan=plan,
                usage=usage,
            )
            return GenerationOutcome(
                ok=False,
                chapter=None,
                plan=plan,
                error=exc.user_message,
                retryable=exc.retryable,
            )
        except Exception as exc:  # boundary: never let an exception escape the worker
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception("Unexpected generation failure")
            message = f"生成任务失败：{exc}"
            self._write_log(
                project_id=project_id,
                chapter_id=chapter.id,
                config=config,
                provider_name=provider_name,
                status="error",
                usage=usage,
                duration_ms=duration_ms,
                error=message,
            )
            self._emit(
                on_update,
                "error",
                message,
                error=message,
                retryable=True,
                plan=plan,
                usage=usage,
            )
            return GenerationOutcome(
                ok=False,
                plan=plan,
                error=message,
                retryable=True,
            )

    # ------------------------------------------------------------------ local
    def rewrite_selection(
        self,
        project_id: str,
        chapter_id: str,
        selection: str,
        instruction: str,
        *,
        provider_config: ProviderConfig | None = None,
    ) -> str:
        """Rewrite only a selected passage; caller decides how to apply it."""

        if not selection.strip():
            raise ValueError("请先选择需要重写的正文。")
        snapshot = self.project_service.load_snapshot(project_id)
        if snapshot is None:
            raise ProviderError("项目不存在。", kind=ProviderErrorKind.CONFIGURATION)
        chapter = snapshot.chapter_by_id(chapter_id)
        if chapter is None:
            raise ProviderError("章节不存在。", kind=ProviderErrorKind.CONFIGURATION)
        config = provider_config or self._resolve_provider_config(
            snapshot.project, GenerationOptions()
        )
        if config is None:
            raise ProviderError(
                "尚未配置 AI 供应商，请先在 API 配置中添加。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        provider = self.provider_manager.build_provider(config)
        project = snapshot.project
        characters = "\n".join(
            f"- {c.name}（{c.role}）：{c.current_state or c.personality}"
            for c in snapshot.active_characters()[:12]
        )
        recent = "\n".join(
            f"第{c.number}章 {c.title}：{_short(snapshot.chapter_summary(c.id), 260)}"
            for c in snapshot.previous_chapters(chapter, limit=2)
        )
        system = (
            "你是一位资深中文小说编辑，擅长在不改变剧情事实和人物设定的前提下润色局部文字。"
            "只输出改写后的选段正文，不输出解释、前言、标题或 Markdown 围栏。"
        )
        user = f"""【作品】{project.title}｜{project.genre}
【风格】{project.style or project.tone}
【禁忌】{project.taboos or '无'}
【前情】{snapshot.global_summary() or snapshot.chapter_summary(chapter.id) or '（无）'}
【最近章节】{recent or '（无）'}
【角色】
{characters or '（无）'}
【本章上下文】
{_short(chapter.content, 5000)}

【需要重写的选段】
{selection}

【改写要求】
{instruction or '保持原有情节与信息，改善文笔、节奏和画面感，字数与原文接近。'}

只输出改写后的选段。"""
        request = provider.build_request(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user),
            ],
            temperature=min(config.temperature, 0.9),
            max_tokens=min(config.max_tokens, 2200),
            stream=False,
        )
        result = provider.complete(request)
        text = result.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\\w*\\s*", "", text)
            text = re.sub(r"\\s*```$", "", text)
        if not text:
            raise ProviderError(
                "模型没有返回改写内容。", kind=ProviderErrorKind.API, retryable=True
            )
        return text.strip()

    # ------------------------------------------------------------------ internals
    def _stream(
        self,
        *,
        provider: BaseProvider,
        request,
        cancel_event: threading.Event,
        on_update: Callable[[GenerationUpdate], None],
        base_usage: TokenUsage,
        plan: ContextPlan,
    ) -> tuple[str, str | None, TokenUsage]:
        chunks: list[str] = []
        usage = TokenUsage(
            prompt_tokens=base_usage.prompt_tokens,
            completion_tokens=0,
            total_tokens=base_usage.prompt_tokens,
        )
        finish_reason: str | None = None
        last_usage_len = 0
        iterator = provider.stream_chat(request)
        try:
            for event in iterator:
                if cancel_event.is_set():
                    break
                if event.type == "text" and event.text:
                    chunks.append(event.text)
                    self._emit(on_update, "text", text=event.text)
                    approx = sum(len(part) for part in chunks)
                    if approx - last_usage_len >= 80:
                        last_usage_len = approx
                        current_text = "".join(chunks)
                        live = TokenUsage(
                            prompt_tokens=usage.prompt_tokens,
                            completion_tokens=self.estimator.estimate(current_text),
                        )
                        live.total_tokens = live.prompt_tokens + live.completion_tokens
                        self._emit(on_update, "usage", usage=live)
                elif event.type == "usage" and event.usage:
                    incoming = event.usage
                    usage = TokenUsage(
                        prompt_tokens=(
                            incoming.prompt_tokens
                            or usage.prompt_tokens
                            or base_usage.prompt_tokens
                        ),
                        completion_tokens=(
                            incoming.completion_tokens or usage.completion_tokens
                        ),
                        total_tokens=(
                            incoming.total_tokens
                            or (
                                (incoming.prompt_tokens or usage.prompt_tokens)
                                + (incoming.completion_tokens or usage.completion_tokens)
                            )
                        ),
                    )
                    self._emit(on_update, "usage", usage=usage)
                elif event.type == "done":
                    finish_reason = event.finish_reason
                elif event.type == "error" and event.error:
                    raise event.error
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.debug("Error closing provider stream", exc_info=True)
        full_text = "".join(chunks)
        if not usage.completion_tokens and full_text:
            usage.completion_tokens = self.estimator.estimate(full_text)
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
            self._emit(on_update, "usage", usage=usage)
        return full_text, finish_reason, usage

    def _save_chapter(
        self,
        chapter: Chapter,
        title: str,
        body: str,
        provider_name: str,
        config: ProviderConfig,
        plan: ContextPlan,
    ) -> Chapter:
        if chapter.content.strip():
            try:
                self.project_service.create_version(
                    chapter.id, note="AI 重新生成前的自动快照"
                )
            except Exception:
                logger.exception("Could not snapshot previous draft for %s", chapter.id)
        chapter.title = title or chapter.title
        chapter.content = body
        chapter.status = "draft"
        note = f"AI 生成 · {provider_name} / {config.model}"
        saved = self.project_service.save_chapter(
            chapter, note=note, create_version=True
        )
        try:
            self.repo.save_chapter(saved)
        except Exception:  # service already persisted; this is only a safety sync
            logger.debug("Chapter resave no-op failed", exc_info=True)
        # Mark the linked outline as drafted, if any.
        if chapter.outline_id:
            node = self.snapshot_or_none(chapter.project_id)
            outline = node.outline_by_id(chapter.outline_id) if node else None
            if outline and outline.status != "done":
                outline.status = "drafting"
                try:
                    self.project_service.save_outline_node(outline)
                except Exception:
                    logger.debug("Could not update outline status", exc_info=True)
        logger.info(
            "Saved chapter %s: %d chars, %d tokens in context",
            chapter.id,
            len(body),
            plan.input_tokens,
        )
        return saved

    def snapshot_or_none(self, project_id: str) -> ProjectSnapshot | None:
        return self.repo.load_snapshot(project_id)

    def _update_memory(
        self,
        provider: BaseProvider,
        config: ProviderConfig,
        project_id: str,
        chapter: Chapter,
        body: str,
        on_update: Callable[[GenerationUpdate], None],
    ) -> MemoryUpdateResult:
        try:
            result = self.summary_service.update_after_chapter(
                project_id,
                chapter,
                body,
                provider=provider,
                config=config,
            )
        except Exception as exc:
            logger.exception("Summary service failed after chapter save")
            result = MemoryUpdateResult(
                False, warning=f"章节已保存，但摘要更新失败：{exc}"
            )
        if not result.success:
            logger.warning("Memory update warning: %s", result.warning)
        return result

    def _build_options(
        self,
        snapshot: ProjectSnapshot,
        chapter: Chapter,
        config: ProviderConfig,
        options: GenerationOptions,
    ) -> BuildOptions:
        project = snapshot.project
        target_words = (
            options.target_words
            or chapter.target_words
            or project.target_words
            or 2500
        )
        requirements = "\n".join(
            part for part in (chapter.requirements.strip(), options.requirements.strip()) if part
        )
        return BuildOptions(
            target_words=target_words,
            requirements=requirements,
            pov=chapter.pov or project.pov,
            tone=project.tone,
            pacing=project.pacing,
            dialogue_ratio=project.dialogue_ratio,
            model_context_window=config.context_window,
            max_output_tokens=config.max_tokens,
            recent_chapters=self.settings.recent_chapter_count,
            retrieval_top_k=self.settings.retrieval_top_k,
            use_retrieval=options.use_retrieval and self.settings.retrieval_enabled,
        )

    def _resolve_provider_config(
        self, project: Project, options: GenerationOptions
    ) -> ProviderConfig | None:
        if options.provider_id:
            config = self.provider_manager.get_config(options.provider_id)
            if config is not None:
                return config
        if project.default_provider_id:
            config = self.provider_manager.get_config(project.default_provider_id)
            if config is not None:
                return config
        return self.provider_manager.default_config()

    def _write_log(
        self,
        *,
        project_id: str,
        chapter_id: str,
        config: ProviderConfig | None,
        provider_name: str,
        status: str,
        usage: TokenUsage,
        duration_ms: int,
        error: str = "",
    ) -> None:
        try:
            self.repo.add_generation_log(
                GenerationLog.create(
                    project_id,
                    provider_name,
                    config.model if config else "",
                    status,
                    chapter_id=chapter_id,
                    provider_id=config.id if config else None,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    duration_ms=duration_ms,
                    error=error,
                )
            )
        except Exception:
            logger.exception("Could not persist generation log")

    def _cancelled(
        self,
        on_update: Callable[[GenerationUpdate], None],
        chapter: Chapter,
        plan: ContextPlan | None,
        provider_name: str,
        config: ProviderConfig,
    ) -> GenerationOutcome:
        self._emit(on_update, "cancelled", "生成已取消，原正文未改动。", plan=plan)
        try:
            self.repo.add_generation_log(
                GenerationLog.create(
                    chapter.project_id,
                    provider_name,
                    config.model,
                    "cancelled",
                    chapter_id=chapter.id,
                    provider_id=config.id,
                    prompt_tokens=plan.input_tokens if plan else 0,
                )
            )
        except Exception:
            logger.exception("Could not persist cancellation log")
        return GenerationOutcome(
            ok=False, cancelled=True, chapter=None, plan=plan, error="生成已取消"
        )

    @staticmethod
    def _fail(
        on_update: Callable[[GenerationUpdate], None],
        message: str,
        retryable: bool = True,
    ) -> GenerationOutcome:
        on_update(
            GenerationUpdate(
                kind="error", message=message, error=message, retryable=retryable
            )
        )
        return GenerationOutcome(ok=False, error=message, retryable=retryable)

    @staticmethod
    def _emit(
        on_update: Callable[[GenerationUpdate], None],
        kind: str,
        message: str = "",
        **kwargs,
    ) -> None:
        on_update(GenerationUpdate(kind=kind, message=message, **kwargs))


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,4}\s*)?(第[0-9一二三四五六七八九十百千万零〇两]+章)"
    r"[\s:：·\-—]*(?P<title>.*?)\s*$"
)
_SIMPLE_TITLE_RE = re.compile(r"^\s{0,3}(?:#{1,3}\s*)?(?:章节?标题|标题)\s*[:：]\s*(.+?)\s*$")
_MARKDOWN_TITLE_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$")
_NEXT_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,4}\s*)?第[0-9一二三四五六七八九十百千万零〇两]+章")


def _parse_chapter_output(text: str, chapter: Chapter) -> tuple[str, str, str]:
    """Extract (title, body, note) from model output.

    The UI displays "第N章 · title", so a leading "第N章" is removed from the
    title to avoid duplication.  Any subsequent chapter heading is treated as
    an overflow attempt and cut off.
    """

    note = ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    lines = cleaned.splitlines()
    title = ""
    body_start = 0
    first_content = next((i for i, line in enumerate(lines) if line.strip()), 0)
    heading_idx: int | None = None

    # Search a small preamble window for the chapter heading.
    for idx in range(first_content, min(len(lines), first_content + 8)):
        line = lines[idx]
        if _HEADING_RE.match(line):
            heading_idx = idx
            match = _HEADING_RE.match(line)
            title = (match.group("title") if match else "").strip(" ·—-:：")
            break
        simple = _SIMPLE_TITLE_RE.match(line)
        if simple:
            heading_idx = idx
            title = simple.group(1).strip().strip("#").strip()
            break
        md = _MARKDOWN_TITLE_RE.match(line)
        if md and len(md.group(1)) <= 60:
            candidate = md.group(1).strip()
            heading_match = _HEADING_RE.match(candidate)
            title = (
                heading_match.group("title").strip(" ·—-:：")
                if heading_match
                else candidate
            )
            heading_idx = idx
            break

    if heading_idx is not None:
        body_start = heading_idx + 1
        # Remove any immediately repeated title line.
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
        if body_start < len(lines) and title and lines[body_start].strip() == title:
            body_start += 1
    else:
        first_line = lines[first_content] if lines else ""
        stripped = first_line.strip()
        if (
            stripped
            and len(stripped) <= 36
            and not any(ch in stripped for ch in "。！？!?；;，,")
            and len(lines) > first_content + 1
            and not stripped.startswith(("第", "序", "——"))
        ):
            title = stripped.strip("# ").strip()
            body_start = first_content + 1
        else:
            body_start = first_content

    body_lines = lines[body_start:] if lines else []
    cut_index: int | None = None
    for idx, line in enumerate(body_lines):
        if _NEXT_HEADING_RE.match(line) and idx > 0:
            cut_index = idx
            break
    if cut_index is not None:
        body_lines = body_lines[:cut_index]
        note = "模型试图输出后续章节，已自动截断，仅保留本章正文。"
    body = "\n".join(body_lines).strip()
    if not body:
        # Defensive fallback: never lose the generated output.
        body = cleaned.strip()
    title = title.strip().strip("《》").strip() or chapter.title
    return title[:120], body, note


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _join_warnings(*values: str) -> str:
    parts = []
    for value in values:
        value = (value or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "；".join(parts)
