"""Deterministic text compression helpers for the context builder.

Everything in this module is pure, local and network-free.  Compression is a
fixed function of the input text and the token budget, so the same snapshot
always produces the same prompt (which makes prompts testable and debuggable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from novel_ai.context.estimator import TokenEstimator
from novel_ai.domain.models import MemoryItem

#: Marker inserted where the middle of a long text was removed.
TRUNCATION_MARK = "\n……（内容过长，已省略中间部分）……\n"

MEMORY_KIND_LABELS: dict[str, str] = {
    "setting": "设定",
    "foreshadow": "伏笔",
    "timeline": "时间线",
    "item": "道具",
    "conflict": "冲突",
    "relationship": "关系",
    "rule": "规则",
}

#: A detail excerpt shorter than this is not worth appending after a summary.
MIN_DETAIL_TOKENS = 24


@dataclass(frozen=True)
class CompressResult:
    """Outcome of a compression step."""

    text: str
    truncated: bool
    note: str = ""


def truncate_head_tail(
    text: str,
    max_chars: int,
    *,
    head_ratio: float = 0.55,
    marker: str = TRUNCATION_MARK,
) -> str:
    """Keep the beginning and the end of ``text`` within ``max_chars`` chars."""

    if not text:
        return ""
    max_chars = int(max_chars)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return text[:max_chars]
    budget = max_chars - len(marker)
    ratio = min(max(float(head_ratio), 0.0), 1.0)
    head = int(budget * ratio)
    tail = budget - head
    pieces: list[str] = []
    if head > 0:
        pieces.append(text[:head])
    pieces.append(marker)
    if tail > 0:
        pieces.append(text[-tail:])
    return "".join(pieces)


def fit_to_tokens(
    text: str,
    estimator: TokenEstimator,
    max_tokens: int,
    *,
    head_ratio: float = 0.55,
    marker: str = TRUNCATION_MARK,
) -> CompressResult:
    """Trim ``text`` until its estimated tokens fit within ``max_tokens``."""

    if not text:
        return CompressResult("", False)
    budget = int(max_tokens)
    if budget <= 0:
        return CompressResult("", True, "预算为 0，内容已省略。")
    current = estimator.estimate(text)
    if current <= budget:
        return CompressResult(text, False)

    result = ""
    if marker and budget >= estimator.estimate(marker) + 12:
        # Largest head+tail window that still fits after adding the marker.
        best = truncate_head_tail(text, 8, head_ratio=head_ratio, marker=marker)
        low, high = 8, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = truncate_head_tail(text, middle, head_ratio=head_ratio, marker=marker)
            if estimator.estimate(candidate) <= budget:
                low = middle
                best = candidate
            else:
                high = middle - 1
        if estimator.estimate(best) <= budget:
            result = best

    if not result:
        # Marker does not fit (or was disabled): keep the leading part only.
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if estimator.estimate(text[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        result = text[:low]

    note = f"已按预算压缩（约 {current} → {estimator.estimate(result)} tokens）。"
    return CompressResult(result, True, note)


def summary_first(
    summary: str,
    detail: str,
    max_tokens: int,
    estimator: TokenEstimator,
    *,
    separator: str = "\n\n",
) -> CompressResult:
    """Compose ``summary`` first, then add as much ``detail`` as still fits."""

    summary = (summary or "").strip()
    detail = (detail or "").strip()
    if not summary and not detail:
        return CompressResult("", False)
    if not summary:
        return fit_to_tokens(detail, estimator, max_tokens)
    if not detail:
        return fit_to_tokens(summary, estimator, max_tokens)

    combined = f"{summary}{separator}{detail}"
    if estimator.estimate(combined) <= max_tokens:
        return CompressResult(combined, False)

    summary_fit = fit_to_tokens(summary, estimator, max_tokens)
    if summary_fit.truncated:
        return CompressResult(
            summary_fit.text,
            True,
            "摘要优先保留；摘要本身超出预算，已压缩。",
        )

    remaining = (
        max_tokens
        - estimator.estimate(summary)
        - estimator.estimate(separator)
    )
    if remaining <= MIN_DETAIL_TOKENS:
        return CompressResult(summary, True, "摘要优先保留；细节内容超出预算，已省略。")

    detail_fit = fit_to_tokens(detail, estimator, remaining, head_ratio=0.7)
    return CompressResult(
        f"{summary}{separator}{detail_fit.text}",
        True,
        "摘要优先保留；细节仅保留节选。",
    )


def format_memory_anchors(
    memories: Iterable[MemoryItem],
    *,
    limit: int | None = None,
    include_priority: bool = True,
) -> str:
    """Render durable story facts as a deterministic, structured list."""

    items = sorted(memories, key=lambda item: (-item.priority, item.created_at, item.id))
    if limit is not None:
        items = items[: max(0, int(limit))]
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        label = MEMORY_KIND_LABELS.get(item.kind, item.kind or "记忆")
        title = (item.title or "").strip() or "未命名"
        content = (item.content or "").strip()
        line = f"- 【{label}】{title}"
        if include_priority:
            line += f"（优先级 {item.priority}/5）"
        if content:
            line += f"：{content}"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "CompressResult",
    "MEMORY_KIND_LABELS",
    "MIN_DETAIL_TOKENS",
    "TRUNCATION_MARK",
    "fit_to_tokens",
    "format_memory_anchors",
    "summary_first",
    "truncate_head_tail",
]
