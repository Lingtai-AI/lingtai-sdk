"""``lingtai_sdk.runtime`` — the runtime contract surface.

The provider-agnostic runtime DTOs and the :class:`Runtime` / :class:`RuntimeSession`
protocols live in :mod:`lingtai_sdk.runtime.contracts`. This package re-exports
them so both the new path (``from lingtai_sdk.runtime import Runtime``) and the
legacy module path (``import lingtai_sdk.runtime`` as a flat module) resolve to
the same objects.

This package is import-pure: it pulls in only the dependency-light contracts
module and never the ``lingtai`` wrapper or any heavy provider SDK.
"""
from __future__ import annotations

from .contracts import (
    EventKind,
    Runtime,
    RuntimeEvent,
    RuntimeMessage,
    RuntimeOptions,
    RuntimeSession,
    RuntimeState,
)

__all__ = [
    "RuntimeState",
    "EventKind",
    "RuntimeOptions",
    "RuntimeMessage",
    "RuntimeEvent",
    "RuntimeSession",
    "Runtime",
]
