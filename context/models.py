"""Data structures produced by the layered context engine.

The context layer is intentionally free of Qt, storage and network concerns: it
only knows the plain domain models and the provider-facing ``ChatMessage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novel_ai.providers.base import ChatMessage


@dataclass
class ContextSection:
    """One labelled block of prompt material.

    ``key`` follows the fixed vocabulary from ``docs/CONTRACTS.md`` so the UI
    can render a stable, ordered breakdown of what was sent to the model.
    """

    key: str
    title: str
    content: str
    tokens: int
    priority: int  # 1 = most important
    included: bool = True
    truncated: bool = False
    note: str = ""


@dataclass
class RetrievalHit:
    """One offline retrieval result used as extra context."""

    source_type: str  # chapter/character/memory/outline/summary
    source_id: str
    title: str
    text: str
    score: float
    reason: str = ""


@dataclass
class ContextPlan:
    """Everything needed to display, debug and send one generation request."""

    sections: list[ContextSection]
    messages: list[ChatMessage]
    system_prompt: str
    user_prompt: str
    input_tokens: int
    input_budget: int
    context_window: int
    reserved_output: int
    compression_notes: list[str] = field(default_factory=list)
    retrieval_hits: list[RetrievalHit] = field(default_factory=list)
    truncated_sections: list[str] = field(default_factory=list)

    def section(self, key: str) -> ContextSection | None:
        """Return the section with ``key`` (or ``None`` when it was never built)."""

        return next((section for section in self.sections if section.key == key), None)

    @property
    def included_sections(self) -> list[ContextSection]:
        return [section for section in self.sections if section.included]

    @property
    def usage_ratio(self) -> float:
        """Fraction of the input budget actually consumed (0.0 when unknown)."""

        if self.input_budget <= 0:
            return 0.0
        return self.input_tokens / self.input_budget


__all__ = ["ContextPlan", "ContextSection", "RetrievalHit"]
