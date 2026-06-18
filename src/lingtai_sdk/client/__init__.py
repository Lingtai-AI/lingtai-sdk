"""``lingtai_sdk.client`` — the thin public client facade.

The :class:`LingTaiClient` / :class:`LingTaiSession` facade and the convenience
:func:`query` / :func:`open_session` helpers live in
:mod:`lingtai_sdk.client.facade`. This package re-exports them so both the new
path (``from lingtai_sdk.client import query``) and the legacy module path
(``import lingtai_sdk.client``) resolve to the same objects.

This package is import-pure: it never imports the ``lingtai`` wrapper at module
load. The default native runtime — and only then the wrapper ``Agent`` — is
imported lazily when a session is actually started.
"""
from __future__ import annotations

from .facade import (
    LingTaiClient,
    LingTaiSession,
    QueryResult,
    open_session,
    query,
)

__all__ = ["LingTaiClient", "LingTaiSession", "QueryResult", "open_session", "query"]
