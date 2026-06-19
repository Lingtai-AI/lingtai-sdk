"""lingtai — generic AI agent framework with intrinsic tools, composable capabilities, and pluggable services.

Naming principle
----------------
``lingtai`` is the **Python import root** for the whole distribution. The repo /
PyPI distribution may be named ``lingtai-sdk``; the SDK is the entire package,
not a ``lingtai.sdk`` subpackage. This ``__init__`` is a thin namespace / fuse
— a re-export surface, not a business layer. It owns no runtime logic; every
exported name is resolved from the kernel or the wrapper submodules.

Import-light boundary
---------------------
A bare ``import lingtai`` stays cheap and side-effect-free: this ``__init__``
imports only the dependency-light **kernel** eagerly. It does NOT pull in
``lingtai.agent``, the capabilities layer, the FileIO / vision / search
services, the LLM adapters, MCP, or any addon — and therefore no heavy provider
SDK is loaded at import time.

Wrapper-backed names (``Agent``, ``setup_capability``, the manager and service
classes) resolve lazily via :pep:`562` ``__getattr__``. Touching one of them
imports its wrapper submodule on first access only, then caches it in module
globals so subsequent access skips ``__getattr__``. Kernel-backed names
(``BaseAgent``, ``Message``, ``AgentConfig``, …) stay eager because the kernel
has zero hard heavy dependencies. This mirrors the lazy boundary in
``lingtai_sdk`` and ``lingtai_cli``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

# --- Kernel-backed surface (eager; dependency-light, no heavy provider SDKs) ---
from lingtai_kernel.types import UnknownToolError
from lingtai_kernel.config import AgentConfig
from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.state import AgentState
from lingtai_kernel.message import Message, MSG_REQUEST, MSG_USER_INPUT
# EmailManager is exported by the kernel intrinsic; re-export for backwards compat.
from lingtai_kernel.intrinsics.email import EmailManager
from lingtai_kernel.services.mail import MailService, FilesystemMailService
from lingtai_kernel.services.logging import LoggingService, JSONLLoggingService

# --- Wrapper-backed surface (lazy; resolved on first attribute access) ---------
# Maps exported attribute name -> (wrapper submodule, attribute). Each is pulled
# in only when accessed, so the wrapper's capabilities / services / adapters and
# their heavy deps stay out of a bare ``import lingtai``.
_LAZY_WRAPPER_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": (".agent", "Agent"),
    "setup_capability": (".capabilities", "setup_capability"),
    "BashManager": (".core.bash", "BashManager"),
    "AvatarManager": (".core.avatar", "AvatarManager"),
    # services.file_io
    "FileIOBackend": (".services.file_io", "FileIOBackend"),
    "FileIOService": (".services.file_io", "FileIOService"),
    "GrepMatch": (".services.file_io", "GrepMatch"),
    "LocalFileIOBackend": (".services.file_io", "LocalFileIOBackend"),
    "LocalFileIOService": (".services.file_io", "LocalFileIOService"),
    # services.file_io_sidecar
    "BACKEND_ENV_VAR": (".services.file_io_sidecar", "BACKEND_ENV_VAR"),
    "RustFileIOBackend": (".services.file_io_sidecar", "RustFileIOBackend"),
    "SidecarAdapter": (".services.file_io_sidecar", "SidecarAdapter"),
    "SidecarError": (".services.file_io_sidecar", "SidecarError"),
    "default_file_io_service": (".services.file_io_sidecar", "default_file_io_service"),
    "resolve_sidecar_binary": (".services.file_io_sidecar", "resolve_sidecar_binary"),
    # services.vision
    "VisionService": (".services.vision", "VisionService"),
    "create_vision_service": (".services.vision", "create_vision_service"),
    # services.websearch
    "SearchService": (".services.websearch", "SearchService"),
    "SearchResult": (".services.websearch", "SearchResult"),
    "create_search_service": (".services.websearch", "create_search_service"),
}

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .agent import Agent  # noqa: F401
    from .capabilities import setup_capability  # noqa: F401
    from .core.bash import BashManager  # noqa: F401
    from .core.avatar import AvatarManager  # noqa: F401
    from .services.file_io import (  # noqa: F401
        FileIOBackend,
        FileIOService,
        GrepMatch,
        LocalFileIOBackend,
        LocalFileIOService,
    )
    from .services.file_io_sidecar import (  # noqa: F401
        BACKEND_ENV_VAR,
        RustFileIOBackend,
        SidecarAdapter,
        SidecarError,
        default_file_io_service,
        resolve_sidecar_binary,
    )
    from .services.vision import VisionService, create_vision_service  # noqa: F401
    from .services.websearch import (  # noqa: F401
        SearchResult,
        SearchService,
        create_search_service,
    )


def __getattr__(name: str):  # PEP 562 module-level lazy attributes
    import importlib

    target = _LAZY_WRAPPER_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__))


__all__ = [
    "__version__",
    # Core
    "BaseAgent",
    "Agent",
    "Message",
    "AgentState",
    "MSG_REQUEST",
    "MSG_USER_INPUT",
    "AgentConfig",
    "UnknownToolError",
    # Capabilities
    "setup_capability",
    "BashManager",
    "AvatarManager",
    "EmailManager",
    # Services
    "FileIOService",
    "FileIOBackend",
    "LocalFileIOBackend",
    "LocalFileIOService",
    "RustFileIOBackend",
    "SidecarAdapter",
    "SidecarError",
    "BACKEND_ENV_VAR",
    "default_file_io_service",
    "resolve_sidecar_binary",
    "GrepMatch",
    "MailService",
    "FilesystemMailService",
    "LoggingService",
    "JSONLLoggingService",
    "VisionService",
    "create_vision_service",
    "SearchService",
    "SearchResult",
    "create_search_service",
]
