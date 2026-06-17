"""lingtai_sdk — the public SDK doorway for building and embedding LingTai agents.

A single curated import path with a stable, typed public API that re-exports
from the two implementation packages underneath it:

- ``lingtai_kernel`` — the minimal standalone runtime (zero hard deps), and
- ``lingtai``        — the batteries-included wrapper (adapters, capabilities, CLI).

Layering and the lazy boundary
------------------------------
``lingtai_sdk`` imports only the **kernel** at module load. The kernel has no
hard heavy third-party dependencies, so ``import lingtai_sdk`` is as cheap and
side-effect-free as ``import lingtai_kernel`` — safe in tooling and in
environments where the wrapper's provider SDKs are not installed.

Wrapper-backed names (``Agent`` and the service classes) resolve lazily via
:pep:`562` ``__getattr__``. Touching ``lingtai_sdk.Agent`` imports ``lingtai``
on first access; if the wrapper (or its deps) is absent you get a clear
``ModuleNotFoundError`` naming ``lingtai`` rather than an import-time crash of
the whole SDK. This makes the one-directional dependency rule visible at the
package boundary: kernel names are eager, wrapper names are lazy.

This package ships **contracts and the doorway**, not a live runtime. The
runtime contract (:mod:`lingtai_sdk.runtime`) and capability-bundle manifest
(:mod:`lingtai_sdk.capabilities`) are seed DTOs; live runtimes and core-bundle
migrations land in later PRs. See ``docs/sdk/architecture-foundation.md``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

# --- Kernel-backed surface (eager; no heavy third-party deps) -------------
from lingtai_kernel.base_agent import BaseAgent
from .types import (
    AgentConfig,
    AgentState,
    ChatSession,
    FunctionSchema,
    LLMResponse,
    LLMService,
    Message,
    MSG_REQUEST,
    MSG_USER_INPUT,
    ToolCall,
)
from .errors import LingTaiSDKError, NativeRuntimeConfigurationError, UnknownToolError

# --- Wrapper-backed surface (lazy; resolved on first attribute access) ----
# Maps SDK attribute name -> (wrapper module, attribute). Each is pulled in only
# when accessed, so the wrapper's heavy provider deps stay optional for
# pure-kernel consumers.
_LAZY_WRAPPER_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": ("lingtai", "Agent"),
    "FileIOService": ("lingtai", "FileIOService"),
    "MailService": ("lingtai", "MailService"),
    "LoggingService": ("lingtai", "LoggingService"),
    "SearchService": ("lingtai", "SearchService"),
    "VisionService": ("lingtai", "VisionService"),
}

# Lazy SDK-internal exports. Unlike ``_LAZY_WRAPPER_EXPORTS``, the target module
# lives inside ``lingtai_sdk`` and is import-pure: accessing these names does
# NOT import the ``lingtai`` wrapper. ``NativeRuntime`` only imports the wrapper
# when a session is actually started.
_LAZY_SDK_EXPORTS: dict[str, tuple[str, str]] = {
    "NativeRuntime": (".native", "NativeRuntime"),
    "NativeRuntimeSession": (".native", "NativeRuntimeSession"),
    "LingTaiClient": (".client", "LingTaiClient"),
    "QueryResult": (".client", "QueryResult"),
    "query": (".client", "query"),
}

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lingtai import (  # noqa: F401
        Agent,
        FileIOService,
        LoggingService,
        MailService,
        SearchService,
        VisionService,
    )
    from .native import NativeRuntime, NativeRuntimeSession  # noqa: F401
    from .client import LingTaiClient, QueryResult, query  # noqa: F401


def __getattr__(name: str):  # PEP 562 module-level lazy attributes
    import importlib

    sdk_target = _LAZY_SDK_EXPORTS.get(name)
    if sdk_target is not None:
        module = importlib.import_module(sdk_target[0], __name__)
        value = getattr(module, sdk_target[1])
        globals()[name] = value
        return value

    target = _LAZY_WRAPPER_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__))


__all__ = [
    "__version__",
    # Runtime entrypoints
    "BaseAgent",  # kernel (eager)
    "Agent",  # wrapper (lazy)
    # Native runtime adapter (lazy, SDK-internal; wrapper loads only on start)
    "NativeRuntime",
    "NativeRuntimeSession",
    # Thin public client facade (lazy, SDK-internal)
    "LingTaiClient",
    "QueryResult",
    "query",
    # Configuration / state / messaging
    "AgentConfig",
    "AgentState",
    "Message",
    "MSG_REQUEST",
    "MSG_USER_INPUT",
    # LLM protocol
    "ChatSession",
    "FunctionSchema",
    "LLMResponse",
    "LLMService",
    "ToolCall",
    # Errors
    "LingTaiSDKError",
    "NativeRuntimeConfigurationError",
    "UnknownToolError",
    # Services (wrapper-backed, lazy)
    "FileIOService",
    "MailService",
    "LoggingService",
    "SearchService",
    "VisionService",
]
