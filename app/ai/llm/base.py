"""
Provider abstraction for the LLM layer.

Every provider (OpenAI today; Anthropic/Ollama/Groq tomorrow) implements the
same minimal interface, so the rest of the application never imports a vendor
SDK directly. Application code talks to the LLMRouter only.
"""

from abc import ABC, abstractmethod
from typing import Sequence


class LLMError(Exception):
    """Raised by providers on any completion failure (after their own handling)."""


class LLMProvider(ABC):
    """Minimal contract every LLM provider must fulfill."""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        json_mode: bool = False,
        images: Sequence[bytes] | None = None,
    ) -> str:
        """
        Runs a single completion and returns the raw text content.
        Must raise LLMError on failure — never a vendor-specific exception.

        `images` is an ordered sequence of raw PNG bytes to send alongside
        `prompt` (vendor-neutral on purpose — each provider does its own
        base64/data-URL encoding). Used by
        `automation/forms/vision_fallback.py`, which sends one cropped
        screenshot per form field automation couldn't fill. A provider whose
        model can't accept images must raise `LLMError` rather than silently
        dropping them: an answer produced without the screenshot it was
        supposed to read is worse than no answer, because it gets typed into a
        real application. `LLMRouter` only passes this argument when a caller
        actually supplies images, so a text-only provider is never handed it.
        """
        raise NotImplementedError
