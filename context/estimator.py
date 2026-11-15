"""Conservative, dependency-free token estimation.

Vendors do not share one tokenizer, so the context layer never tries to be
exact.  Chinese characters are counted as one token each, ASCII text at roughly
3.5 characters per token, and a safety margin is added on top so that a real
tokenizer is unlikely to produce *more* tokens than the estimate.  Under-
estimating is far more dangerous than slightly over-estimating, because it can
push a request past the model context window.
"""

from __future__ import annotations

import math
import re

from novel_ai.providers.base import ChatMessage

#: CJK ideographs plus common Japanese/Korean ranges; each counts as ~1 token.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

#: Rough number of ASCII characters per token (OpenAI-compatible tokenizers).
ASCII_CHARS_PER_TOKEN = 3.5

#: Multiplier applied to the raw estimate to leave a conservative margin.
SAFETY_RATIO = 1.10

#: Chat-format overhead per message (role markers, separators, ...).
MESSAGE_OVERHEAD_TOKENS = 4

#: Fixed chat-format overhead for the whole conversation (reply priming, ...).
CONVERSATION_OVERHEAD_TOKENS = 8


class TokenEstimator:
    """Deterministic token estimator with a conservative bias."""

    def estimate(self, text: str) -> int:
        """Estimate the number of prompt tokens for ``text``."""

        if not text:
            return 0
        cjk_chars = len(_CJK_RE.findall(text))
        other_chars = max(0, len(text) - cjk_chars)
        raw = cjk_chars + other_chars / ASCII_CHARS_PER_TOKEN
        return max(1, math.ceil(raw * SAFETY_RATIO))

    def estimate_messages(self, messages: list[ChatMessage]) -> int:
        """Estimate the full prompt cost of a chat message list."""

        total = CONVERSATION_OVERHEAD_TOKENS
        for message in messages:
            total += MESSAGE_OVERHEAD_TOKENS + self.estimate(message.content)
        return total


__all__ = [
    "ASCII_CHARS_PER_TOKEN",
    "CONVERSATION_OVERHEAD_TOKENS",
    "MESSAGE_OVERHEAD_TOKENS",
    "SAFETY_RATIO",
    "TokenEstimator",
]
