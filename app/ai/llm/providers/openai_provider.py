"""OpenAI provider — the only file in the codebase allowed to import the openai SDK."""

import base64
from typing import Sequence

from openai import OpenAI, OpenAIError

from app.ai.llm.base import LLMProvider, LLMError
from app.core.config import OPENAI_API_KEY


#: Sent with every image part. "high" rather than "auto"/"low" because the
#: only images this provider ever receives are cropped screenshots of form
#: fields (see `automation/forms/vision_fallback.py`) — small, text-dense
#: images where a downscaled read is exactly the failure mode that matters:
#: a misread option label becomes a wrong answer typed into a real job
#: application.
_IMAGE_DETAIL = "high"


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        # One client per provider instance; reused across all requests.
        # timeout: without it the SDK waits up to 10 minutes on a stuck
        # connection — the router's retry logic should kick in far sooner.
        # max_retries=0: retrying is the router's job, not the SDK's.
        self._client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0, max_retries=0)

    @staticmethod
    def _user_content(prompt: str, images: Sequence[bytes] | None):
        """A plain string for a text-only call — byte-for-byte the request
        shape this provider always sent — or the multi-part content list the
        Chat Completions API needs when images come along.

        Images go AFTER the text on purpose: the prompt is what tells the
        model what the images are and what order they're in, and a model that
        reads the instructions first interprets N unlabeled screenshots far
        more reliably than one that meets them cold."""
        if not images:
            return prompt
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": _IMAGE_DETAIL},
            })
        return parts

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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": self._user_content(prompt, images)})

        kwargs: dict = {
            "model": model,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "messages": messages,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except OpenAIError as e:
            raise LLMError(f"OpenAI completion failed (model={model}): {e}") from e

        content = response.choices[0].message.content
        if content is None:
            raise LLMError(f"OpenAI returned empty content (model={model})")
        return content
