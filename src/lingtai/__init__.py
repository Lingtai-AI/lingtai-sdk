"""lingtai — generic AI agent framework with intrinsic tools, composable capabilities, and pluggable services."""

from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("lingtai")

from lingtai_kernel.types import UnknownToolError
from lingtai_kernel.config import AgentConfig
from lingtai_kernel.base_agent import BaseAgent
from .agent import Agent
from lingtai_kernel.state import AgentState
from lingtai_kernel.message import Message, MSG_REQUEST, MSG_USER_INPUT

# Capabilities
from .capabilities import setup_capability
from .core.bash import BashManager
from .core.avatar import AvatarManager
# EmailManager is now exported by the kernel intrinsic; re-export for backwards compat.
from lingtai_kernel.intrinsics.email import EmailManager

# Services
from .services.file_io import FileIOBackend, FileIOService, GrepMatch, LocalFileIOBackend, LocalFileIOService
from .services.file_io_sidecar import (
    BACKEND_ENV_VAR,
    RustFileIOBackend,
    SidecarAdapter,
    SidecarError,
    default_file_io_service,
    resolve_sidecar_binary,
)
from lingtai_kernel.services.mail import MailService, FilesystemMailService
from lingtai_kernel.services.logging import LoggingService, JSONLLoggingService
from .services.vision import VisionService, create_vision_service
from .services.websearch import SearchService, SearchResult, create_search_service

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
