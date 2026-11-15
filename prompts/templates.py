"""Prompt templates and the editable prompt store.

Templates use ``{{placeholder}}`` double-brace markers.  Only known placeholders
listed in :data:`PLACEHOLDERS` are substituted; unknown markers are left intact
so a malformed custom template is visible instead of silently mangled.

The default template is written for Chinese long-form fiction and asks for the
chapter title plus body of *only the current chapter*; the context builder
appends an additional, unremovable guard to the system message.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: Every placeholder the context builder can fill.
PLACEHOLDERS: list[str] = [
    "project_title",
    "genre",
    "chapter_number",
    "chapter_title",
    "worldbuilding",
    "style",
    "taboos",
    "global_summary",
    "volume_summary",
    "chapter_summaries",
    "characters",
    "memories",
    "outline",
    "recent_chapters",
    "retrieval",
    "requirements",
    "target_words",
    "pov",
    "tone",
    "pacing",
    "dialogue_ratio",
    "output_format",
]

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass
class PromptTemplate:
    """One editable prompt template (system + user message)."""

    name: str
    description: str
    system_template: str
    user_template: str


SYSTEM_TEMPLATE = """你是「墨言」的小说写作引擎，一位经验丰富的中文长篇小说作家，正在为作品《{{project_title}}》（题材：{{genre}}）撰写第 {{chapter_number}} 章。

你必须遵守以下创作铁律：
1. 严格服从已给出的设定、人物状态与本章大纲，不擅自改设定、不改人物关系、不提前揭露尚未安排的真相。
2. 只写当前这一章：以本章大纲为边界推进情节，不跳过关键情节，也不写下一章的内容。
3. 与前文自然衔接：延续既有叙事视角、语气和时间线，不复述、不重复前文已经写过的情节。
4. 与「长期记忆锚点」中的伏笔、规则、道具和时间线保持一致，不无铺垫地回收伏笔，不凭空新增与既有设定冲突的设定。
5. 人物言行必须符合其性格与当前状态，不同角色要有区分度，避免所有角色一个腔调。
6. 用具体动作、感官细节和有信息量的对话推进剧情，少用空洞抒情、总结性叙述和口水话，避免重复用词。
7. 风格要求：{{style}}
8. 风格禁忌：{{taboos}}
9. 只输出本章的章节标题与正文，不要输出任何解释、点评、修改说明、创作思路或下一章预告。"""

USER_TEMPLATE = """【作品设定】
{{worldbuilding}}

【前情摘要】
- 全局主线：{{global_summary}}
- 本卷概要：{{volume_summary}}
- 已写章节摘要：
{{chapter_summaries}}

【角色当前状态】
{{characters}}

【长期记忆锚点】（伏笔 / 规则 / 道具 / 时间线，必须遵守）
{{memories}}

【本章大纲】
{{outline}}

【最近原文节选】（仅用于衔接语气与情节，禁止复述、禁止照抄）
{{recent_chapters}}

【相关检索片段】（可能相关，仅作参考，禁止照抄）
{{retrieval}}

【本章额外要求】
{{requirements}}

【本章写作任务】
- 章节：第 {{chapter_number}} 章《{{chapter_title}}》
- 目标字数：约 {{target_words}} 字
- 叙事视角：{{pov}}
- 语气：{{tone}}
- 节奏：{{pacing}}
- 对话比例：约 {{dialogue_ratio}}
- 输出格式：
{{output_format}}

现在开始写作：只输出第 {{chapter_number}} 章《{{chapter_title}}》的章节标题与完整正文。"""

DEFAULT_PROMPT_TEMPLATE = PromptTemplate(
    name="默认中文长篇小说模板",
    description=(
        "面向中文长篇小说的章节生成模板：包含系统角色、作品设定、风格与禁忌、"
        "前情摘要、角色状态、记忆锚点、本章大纲、最近原文、检索片段、"
        "字数/视角/语气/节奏/对话比例与输出格式。"
    ),
    system_template=SYSTEM_TEMPLATE,
    user_template=USER_TEMPLATE,
)


def default_template() -> PromptTemplate:
    """Return a fresh copy of the built-in default template."""

    return PromptTemplate(
        name=DEFAULT_PROMPT_TEMPLATE.name,
        description=DEFAULT_PROMPT_TEMPLATE.description,
        system_template=DEFAULT_PROMPT_TEMPLATE.system_template,
        user_template=DEFAULT_PROMPT_TEMPLATE.user_template,
    )


def find_placeholders(template_text: str) -> list[str]:
    """Return uniquely used placeholder names in first-appearance order."""

    seen: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(template_text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def missing_placeholders(template: PromptTemplate) -> list[str]:
    """Placeholders from :data:`PLACEHOLDERS` not referenced by a template."""

    used = set(find_placeholders(template.system_template)) | set(
        find_placeholders(template.user_template)
    )
    return [name for name in PLACEHOLDERS if name not in used]


def render_template(template_text: str, values: Mapping[str, Any]) -> str:
    """Safely substitute ``{{placeholder}}`` markers.

    Unknown placeholders are kept verbatim so a broken custom template can be
    diagnosed; ``None`` values render as an empty string.
    """

    if not template_text:
        return ""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        value = values[name]
        return "" if value is None else str(value)

    return PLACEHOLDER_PATTERN.sub(_replace, template_text)


class PromptStore:
    """Atomic JSON persistence for a single editable prompt template.

    The stored file is intentionally human readable.  A missing file is seeded
    with the default template; a corrupt or structurally invalid file falls
    back to the default template without raising, so a bad edit can never stop
    the application from generating chapters.
    """

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- public API ---------------------------------------------------------
    def load(self) -> PromptTemplate:
        if not self.path.exists():
            template = default_template()
            try:
                self.save(template)
            except OSError:
                # Read-only location: still return a usable in-memory template.
                pass
            return template
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return default_template()
        template = self._from_payload(raw)
        return template if template is not None else default_template()

    def save(self, template: PromptTemplate) -> None:
        if not isinstance(template, PromptTemplate):
            raise TypeError("template must be a PromptTemplate instance")
        payload = {
            "version": self.VERSION,
            "template": {
                "name": template.name,
                "description": template.description,
                "system_template": template.system_template,
                "user_template": template.user_template,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def reset(self) -> PromptTemplate:
        template = default_template()
        self.save(template)
        return template

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _from_payload(raw: Any) -> PromptTemplate | None:
        if not isinstance(raw, dict):
            return None
        payload = raw.get("template") if isinstance(raw.get("template"), dict) else raw
        fields: dict[str, str] = {}
        for key in ("name", "description", "system_template", "user_template"):
            value = payload.get(key)
            if not isinstance(value, str):
                return None
            fields[key] = value
        if not fields["system_template"].strip() or not fields["user_template"].strip():
            return None
        return PromptTemplate(**fields)


__all__ = [
    "DEFAULT_PROMPT_TEMPLATE",
    "PLACEHOLDERS",
    "PLACEHOLDER_PATTERN",
    "PromptStore",
    "PromptTemplate",
    "default_template",
    "find_placeholders",
    "missing_placeholders",
    "render_template",
]
