"""Best-effort version resolution for the ``lingtai`` import root.

The whole distribution is published as the ``lingtai`` PyPI package, so the
import root's version tracks that distribution's metadata. Resolving via
``importlib.metadata`` keeps a bare ``import lingtai`` dependency-free; if the
metadata is unavailable (e.g. running straight from a source checkout that was
never installed) we fall back to a sentinel rather than raising at import time.
"""
from __future__ import annotations


def _resolve_version() -> str:
    try:
        from importlib.metadata import version

        return version("lingtai")
    except Exception:  # noqa: BLE001 - never break import over version metadata
        return "0+unknown"


__version__ = _resolve_version()
