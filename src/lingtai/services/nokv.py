"""NoKV integration helpers.

This module owns the safe first boundary for NoKV support: URI parsing,
optional config shape, and selected LingTai subtree classification. It does
not import a NoKV SDK at module import time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

DEFAULT_NOKV_URI_PREFIXES: tuple[str, ...] = ("nokv://",)
DEFAULT_NOKV_SELECTED_SUBTREES: tuple[str, ...] = (
    "artifacts",
    "reports",
    "checkpoints",
    "knowledge",
)

_LOCAL_RUNTIME_PARTS: frozenset[str] = frozenset({
    ".agent.lock",
    ".agent.heartbeat",
    ".status.json",
    ".sleep",
    ".suspend",
    ".interrupt",
    ".prompt",
    ".refresh",
    "mailbox",
    "logs",
    "history",
    "tmp",
    "daemons",
    ".notification",
})


class NoKVUnsupportedError(RuntimeError):
    """Raised when a NoKV path is requested without a configured backend."""


@dataclass(frozen=True)
class NoKVConfig:
    """Resolved optional NoKV config.

    Hosts may pass this structure explicitly. Runtime defaults stay local:
    ``enabled=False`` means no NoKV reads or writes are attempted.
    """

    enabled: bool = False
    endpoint: str | None = None
    default_namespace: str | None = None
    uri_prefixes: tuple[str, ...] = DEFAULT_NOKV_URI_PREFIXES
    selected_subtrees: tuple[str, ...] = DEFAULT_NOKV_SELECTED_SUBTREES
    client: Any | None = field(default=None, compare=False, repr=False)


def parse_nokv_config(raw: Mapping[str, Any] | None) -> NoKVConfig:
    """Return a typed NoKVConfig from an already-schema-validated mapping."""
    if not raw:
        return NoKVConfig()
    return NoKVConfig(
        enabled=bool(raw.get("enabled", False)),
        endpoint=raw.get("endpoint"),
        default_namespace=raw.get("default_namespace"),
        uri_prefixes=tuple(raw.get("uri_prefixes") or DEFAULT_NOKV_URI_PREFIXES),
        selected_subtrees=tuple(
            raw.get("selected_subtrees") or DEFAULT_NOKV_SELECTED_SUBTREES
        ),
        client=raw.get("client"),
    )


def is_nokv_uri(path: str, uri_prefixes: Sequence[str] = DEFAULT_NOKV_URI_PREFIXES) -> bool:
    return any(path.startswith(prefix) for prefix in uri_prefixes)


def normalize_nokv_path(
    path: str,
    uri_prefixes: Sequence[str] = DEFAULT_NOKV_URI_PREFIXES,
) -> str:
    """Normalize a NoKV URI or object path to an absolute NoKV object path."""
    raw = path
    for prefix in uri_prefixes:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip()
    if not raw:
        return "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    return "/" + "/".join(part for part in raw.split("/") if part)


def format_nokv_uri(path: str, prefix: str = DEFAULT_NOKV_URI_PREFIXES[0]) -> str:
    object_path = normalize_nokv_path(path)
    return prefix + object_path.lstrip("/")


def classify_lingtai_subtree(
    path: str,
    *,
    selected_subtrees: Sequence[str] = DEFAULT_NOKV_SELECTED_SUBTREES,
) -> str:
    """Classify a LingTai path for the selected-subtree smoke boundary.

    Returns:
        ``"nokv-candidate"`` for output-like subtrees that may be mapped to
        NoKV, ``"local-runtime"`` for locks/mail/logs/signals that must stay
        local, and ``"outside"`` for paths outside this first boundary.
    """
    normalized = str(path).replace("\\", "/")
    parts = tuple(
        part for part in PurePosixPath(normalized).parts
        if part not in {"", "/"}
    )
    if any(part in _LOCAL_RUNTIME_PARTS for part in parts):
        return "local-runtime"
    if any(part in selected_subtrees for part in parts):
        return "nokv-candidate"
    return "outside"
