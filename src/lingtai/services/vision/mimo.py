"""Xiaomi MiMo vision service — image understanding via MiMo's OpenAI-compatible chat completions.

MiMo's vision API is plain OpenAI Chat Completions with ``image_url`` content
parts. Among the agent-relevant models (``mimo-v2.5``, ``mimo-v2.5-pro``,
``mimo-v2-flash``), only ``mimo-v2.5`` accepts image input — the pro and
flash variants are text-only and 400 on images. The omni-modal
``mimo-v2-omni`` (audio + video + image, 256K ctx) also exists but is not
exposed by the TUI preset; agents who need it set ``model="mimo-v2-omni"``
explicitly.

Defaults to ``mimo-v2.5`` (1M context, vision-capable, agent default).

The OpenAI Python SDK works against MiMo's endpoint as long as ``base_url``
is pinned to ``https://api.xiaomimimo.com/v1`` — the wire format is
identical. This service wraps that with a sane MiMo-specific default.
"""
from __future__ import annotations

from . import VisionService, _image_url_messages


_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_DEFAULT_MODEL = "mimo-v2.5"


class MiMoVisionService(VisionService):
    """Image understanding via Xiaomi MiMo's OpenAI-compatible chat completions.

    Owns its own ``openai.OpenAI`` client and API key — fully independent
    of any LLM adapter or agent.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        import openai as _openai

        self._client = _openai.OpenAI(
            api_key=api_key,
            base_url=base_url or _MIMO_BASE_URL,
        )
        self._model = model
        self._max_tokens = max_tokens

    def analyze_image(self, image_path: str, prompt: str | None = None) -> str:
        """Analyze an image using MiMo's vision-capable models."""
        raw = self._client.chat.completions.create(
            model=self._model,
            messages=_image_url_messages(image_path, prompt),
            max_completion_tokens=self._max_tokens,
        )
        if raw.choices:
            return raw.choices[0].message.content or ""
        return ""
