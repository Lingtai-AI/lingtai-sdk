"""Vision capability — image understanding via VisionService.

Adds the ability to analyze images. Requires a VisionService instance,
created either explicitly or via the ``provider``/``api_key`` factory.

Usage:
    agent.add_capability("vision", vision_service=my_svc)
    agent.add_capability("vision", provider="anthropic", api_key="sk-...")

Note: a local mlx-vlm provider exists (``provider="local"``) and works
on Apple Silicon, but it is intentionally NOT exposed in ``PROVIDERS``
below so that first-run wizards and check-caps don't advertise it yet.
Users who want it can opt in explicitly via ``add_capability`` with
``provider="local"``; see ``services/vision/local.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...i18n import t
from ...services.vision import VisionService, create_vision_service

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {
    "providers": ["minimax", "zhipu", "mimo", "gemini", "anthropic", "openai", "codex"],
    "default": None,
    "fallback_on_inherit": None,  # no agnostic fallback for vision
}

def get_description(lang: str = "en") -> str:
    return t(lang, "vision.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": t(lang, "vision.image_path")},
            "question": {
                "type": "string",
                "description": t(lang, "vision.question"),
                "default": "Describe this image.",
            },
        },
        "required": ["image_path"],
    }



class VisionManager:
    """Handles vision tool calls via a VisionService."""

    def __init__(
        self,
        agent: "BaseAgent",
        vision_service: VisionService,
    ) -> None:
        self._agent = agent
        self._vision_service = vision_service

    def handle(self, args: dict) -> dict:
        image_path = args.get("image_path", "")
        question = args.get("question", "Describe what you see in this image.")

        if not image_path:
            return {"status": "error", "message": "Provide image_path"}

        path = Path(image_path)
        if not path.is_absolute():
            path = self._agent._working_dir / path

        if not path.is_file():
            return {"status": "error", "message": f"Image file not found: {path}"}

        try:
            analysis = self._vision_service.analyze_image(str(path), prompt=question)
            if not analysis:
                return {
                    "status": "error",
                    "message": "Vision analysis returned no response.",
                }
            return {"status": "ok", "analysis": analysis}
        except Exception as e:
            return {"status": "error", "message": f"Vision analysis failed: {e}"}


def setup(
    agent: "BaseAgent",
    vision_service: VisionService | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> VisionManager:
    """Set up the vision capability on an agent.

    Requires either ``vision_service`` or ``provider`` + ``api_key``.
    Raises ``ValueError`` if neither is provided.
    """
    if vision_service is None and provider is not None:
        if provider not in PROVIDERS["providers"]:
            # No dedicated VisionService for this provider. If the agent's
            # main LLM is OpenAI-compatible (custom relay, OpenRouter,
            # DeepSeek, Kimi, ...), route vision through OpenAIVisionService
            # using the LLM's own base_url. If the relay or model can't
            # actually do vision, the call fails at runtime — no pre-check.
            api_compat = ""
            defaults = getattr(getattr(agent, "service", None), "_provider_defaults", None)
            if isinstance(defaults, dict):
                api_compat = defaults.get("api_compat") or ""
            if api_compat == "openai":
                from ...services.vision.openai import OpenAIVisionService
                llm_base_url = getattr(agent.service, "_base_url", None)
                llm_model = getattr(agent.service, "_model", None) or "gpt-4o"
                vision_service = OpenAIVisionService(
                    api_key=api_key,
                    model=llm_model,
                    base_url=llm_base_url,
                )
            else:
                agent._log(
                    "capability_skipped",
                    capability="vision",
                    requested_provider=provider,
                    reason=f"no vision support for provider {provider!r}",
                )
                return None
        else:
            # Provider-specific kwarg injection. Each branch is opt-in because
            # vision services have heterogeneous constructor signatures —
            # passing api_host to a service that doesn't accept it raises
            # TypeError at construction (silently swallowed by the agent's
            # capability-setup try/except, leaving the agent without vision).
            if provider == "minimax" and "api_host" not in kwargs:
                from .._media_host import resolve_media_host
                kwargs["api_host"] = resolve_media_host(agent)
            if provider == "zhipu" and "z_ai_mode" not in kwargs:
                from .._zhipu_mode import resolve_z_ai_mode
                kwargs["z_ai_mode"] = resolve_z_ai_mode(agent)
            # Dedicated vision services do not consume the LLM adapter's
            # transport selector. ``expand_inherit`` copies it for capability
            # routing, but forwarding it into service constructors breaks
            # providers such as MiniMax that only accept provider-native args.
            kwargs.pop("api_compat", None)
            kwargs.pop("base_url", None)
            vision_service = create_vision_service(provider, api_key=api_key, **kwargs)
    elif vision_service is None:
        raise ValueError(
            "vision capability requires 'vision_service' or 'provider' + 'api_key'. "
            "Example: capabilities={'vision': {'provider': 'gemini', 'api_key': '...'}}"
        )

    lang = agent._config.language
    mgr = VisionManager(agent, vision_service=vision_service)
    agent.add_tool("vision", schema=get_schema(lang), handler=mgr.handle, description=get_description(lang))
    return mgr
