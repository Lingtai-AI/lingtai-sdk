"""SDK error surface.

A single SDK base error, runtime-scoped subclasses, plus a re-export of the
kernel's ``UnknownToolError``. Kept in a leaf module with no heavy imports so
``import lingtai_sdk`` stays cheap.
"""
from __future__ import annotations

from lingtai_kernel.types import UnknownToolError


class LingTaiSDKError(Exception):
    """Base class for all SDK-level errors."""


class NativeRuntimeConfigurationError(LingTaiSDKError):
    """Raised when ``NativeRuntime`` cannot build a session from its options.

    The default ``agent_factory`` builds an ``LLMService`` from
    ``RuntimeOptions.provider``/``model`` (with optional ``base_url``/
    ``api_key``); this error is raised — *before* any agent is constructed —
    when that LLM config is partial or absent and no ``agent_factory`` was
    injected to supply a ready service. Its message never echoes ``api_key``.
    """


__all__ = [
    "LingTaiSDKError",
    "NativeRuntimeConfigurationError",
    "UnknownToolError",
]
