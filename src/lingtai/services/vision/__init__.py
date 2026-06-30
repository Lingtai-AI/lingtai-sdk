"""VisionService — abstract image understanding backing the vision capability.

Provides standalone vision implementations for each provider that take their
own API key and handle file reading, base64 encoding, and SDK calls directly.

Usage:
    from lingtai.services.vision import VisionService, create_vision_service

    svc = create_vision_service("anthropic", api_key="sk-...")
    result = svc.analyze_image("/path/to/image.png", prompt="describe this")
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod


class VisionService(ABC):
    """Abstract vision service.

    Backs the vision capability. Implementations provide image understanding
    via provider-specific APIs.
    """

    @abstractmethod
    def analyze_image(self, image_path: str, prompt: str | None = None) -> str:
        """Analyze an image and return a text description.

        Args:
            image_path: Path to the image file.
            prompt: Optional prompt to guide the analysis (e.g., "describe the chart").

        Returns:
            Text description/analysis of the image.
        """
        ...


_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _read_image(image_path: str) -> tuple[bytes, str]:
    """Read an image file and determine its MIME type.

    Returns:
        Tuple of (image_bytes, mime_type).
    """
    from pathlib import Path

    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Image file not found: {image_path}")
    image_bytes = path.read_bytes()
    mime_type = _MIME_BY_EXT.get(path.suffix.lower(), "image/png")
    return image_bytes, mime_type


def _image_url_messages(image_path: str, prompt: str | None = None) -> list[dict[str, object]]:
    """Build OpenAI-compatible chat messages for image_url vision providers."""
    image_bytes, mime_type = _read_image(image_path)
    question = prompt or "Describe this image."

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"
    return [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": question},
            ],
        }
    ]


def create_vision_service(provider: str, *, api_key: str | None = None, **kwargs) -> VisionService:
    """Factory — create a VisionService for the given provider.

    Args:
        provider: Provider name ("anthropic", "openai", "gemini", "minimax", "codex", "local").
        api_key: API key for the provider (not required for "codex" or "local").
        **kwargs: Additional provider-specific kwargs (e.g., model, base_url).

    Returns:
        A VisionService instance.

    Raises:
        ValueError: If the provider is not supported or api_key missing for API providers.
    """
    if provider == "local":
        from .local import LocalVisionService
        return LocalVisionService(**kwargs)
    elif provider == "codex":
        from .codex import CodexVisionService
        return CodexVisionService(**kwargs)

    if api_key is None:
        raise ValueError(
            f"api_key is required for provider {provider!r}. "
            f"Use provider='local' for on-device vision without an API key."
        )

    if provider == "anthropic":
        from .anthropic import AnthropicVisionService
        return AnthropicVisionService(api_key=api_key, **kwargs)
    elif provider == "openai":
        from .openai import OpenAIVisionService
        return OpenAIVisionService(api_key=api_key, **kwargs)
    elif provider == "gemini":
        from .gemini import GeminiVisionService
        return GeminiVisionService(api_key=api_key, **kwargs)
    elif provider == "minimax":
        from .minimax import MiniMaxVisionService
        return MiniMaxVisionService(api_key=api_key, **kwargs)
    elif provider == "zhipu":
        from .zhipu import ZhipuVisionService
        return ZhipuVisionService(api_key=api_key, **kwargs)
    elif provider == "mimo":
        from .mimo import MiMoVisionService
        return MiMoVisionService(api_key=api_key, **kwargs)
    else:
        raise ValueError(
            f"Unsupported vision provider: {provider!r}. "
            f"Supported: anthropic, openai, gemini, minimax, zhipu, mimo, codex, local."
        )
