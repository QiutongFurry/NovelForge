"""Derivation of the prompt token budget from the model context window.

The output reservation must be large enough for the requested chapter and small
enough to leave room for prompt material.  All numbers are deliberately fixed
(no heuristics based on content) so a given configuration always produces the
same budget and therefore deterministic compression decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: Smallest context window we are willing to plan for.
MIN_CONTEXT_WINDOW = 512

#: Never reserve less than this for the model's answer.
MIN_OUTPUT_RESERVE = 256

#: When the provider does not declare max output tokens, reserve this share.
DEFAULT_OUTPUT_RATIO = 0.25

#: Never reserve more than half of the context window for the answer.
MAX_OUTPUT_RATIO = 0.5

#: Keep a safety margin between estimated prompt size and the real window.
INPUT_SAFETY_RATIO = 0.92

#: Smallest input budget we hand to the builder (below this context quality
#: inevitably collapses; the builder still guarantees the hard limit, but the
#: UI should warn the user to pick a larger-context model).
MIN_INPUT_BUDGET = 256

#: Fixed share of the content budget for each story section.  These add up to
#: 1.0; keys absent from a snapshot have their share redistributed.
SECTION_BUDGET_SHARES: dict[str, float] = {
    "worldbuilding": 0.09,
    "style": 0.06,
    "global_summary": 0.06,
    "volume_summary": 0.05,
    "chapter_summaries": 0.10,
    "characters": 0.13,
    "memories": 0.09,
    "outline": 0.14,
    "recent_chapters": 0.14,
    "retrieval": 0.06,
    "requirements": 0.03,
    "output_format": 0.05,
}

#: Minimum soft cap for an optional section (tokens).
MIN_SECTION_TOKENS = 32

#: Below this a mandatory section is considered fully compressed.
MIN_MANDATORY_TOKENS = 24


@dataclass(frozen=True)
class BudgetPlan:
    """Token accounting for one generation request."""

    context_window: int
    reserved_output: int
    input_budget: int

    @property
    def total(self) -> int:
        return self.input_budget + self.reserved_output

    @property
    def output_ratio(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return self.reserved_output / self.context_window


def derive_budget(
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    *,
    safety_ratio: float = INPUT_SAFETY_RATIO,
) -> BudgetPlan:
    """Derive ``input_budget`` from the context window and output reservation.

    Invariants:
    * ``input_budget + reserved_output <= context_window``
    * ``input_budget >= MIN_INPUT_BUDGET`` whenever the window allows it
    """

    window = int(context_window or DEFAULT_CONTEXT_WINDOW)
    window = max(MIN_CONTEXT_WINDOW, window)

    desired_output = (
        DEFAULT_MAX_OUTPUT_TOKENS
        if max_output_tokens is None
        else int(max_output_tokens)
    )
    if desired_output <= 0:
        desired_output = int(window * DEFAULT_OUTPUT_RATIO)
    desired_output = max(desired_output, min(MIN_OUTPUT_RESERVE, window // 2))

    max_reserve = max(1, int(window * MAX_OUTPUT_RATIO))
    reserved = min(desired_output, max_reserve)

    available = max(0, window - reserved)
    ratio = min(max(float(safety_ratio), 0.5), 1.0)
    input_budget = int(available * ratio)
    input_budget = max(min(MIN_INPUT_BUDGET, available), input_budget)
    input_budget = min(input_budget, available)

    return BudgetPlan(
        context_window=window,
        reserved_output=reserved,
        input_budget=input_budget,
    )


__all__ = [
    "BudgetPlan",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_OUTPUT_RATIO",
    "INPUT_SAFETY_RATIO",
    "MAX_OUTPUT_RATIO",
    "MIN_CONTEXT_WINDOW",
    "MIN_INPUT_BUDGET",
    "MIN_MANDATORY_TOKENS",
    "MIN_OUTPUT_RESERVE",
    "MIN_SECTION_TOKENS",
    "SECTION_BUDGET_SHARES",
    "derive_budget",
]
