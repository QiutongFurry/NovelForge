"""Layered context engine: budget, compression, retrieval and prompt building."""

from novel_ai.context.budget import (
    BudgetPlan,
    SECTION_BUDGET_SHARES,
    derive_budget,
)
from novel_ai.context.builder import (
    BuildOptions,
    ContextBuilder,
    HARD_GUARD_HEADER,
    MANDATORY_SECTIONS,
    SECTION_ORDER,
    SECTION_PRIORITIES,
    SECTION_TITLES,
    current_chapter_guard,
)
from novel_ai.context.compressor import (
    CompressResult,
    fit_to_tokens,
    format_memory_anchors,
    summary_first,
    truncate_head_tail,
)
from novel_ai.context.estimator import TokenEstimator
from novel_ai.context.models import ContextPlan, ContextSection, RetrievalHit
from novel_ai.context.retriever import LexicalRetriever, Retriever, tokenize

__all__ = [
    "BudgetPlan",
    "BuildOptions",
    "CompressResult",
    "ContextBuilder",
    "ContextPlan",
    "ContextSection",
    "HARD_GUARD_HEADER",
    "LexicalRetriever",
    "MANDATORY_SECTIONS",
    "RetrievalHit",
    "Retriever",
    "SECTION_BUDGET_SHARES",
    "SECTION_ORDER",
    "SECTION_PRIORITIES",
    "SECTION_TITLES",
    "TokenEstimator",
    "current_chapter_guard",
    "derive_budget",
    "fit_to_tokens",
    "format_memory_anchors",
    "summary_first",
    "tokenize",
    "truncate_head_tail",
]
