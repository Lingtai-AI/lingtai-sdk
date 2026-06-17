"""Best-effort version resolution for the SDK doorway.

The SDK ships inside the same wheel as ``lingtai`` today (add the package now,
rename the distribution later), so its version tracks the ``lingtai``
distribution metadata. Resolving via ``importlib.metadata`` keeps
``import lingtai_sdk`` dependency-free; if metadata is unavailable (e.g. running
straight from a source checkout that was never installed) we fall back to a
sentinel rather than raising at import time.
"""
from __future__ import annotations


def _resolve_version() -> str:
    try:
        from importlib.metadata import version

        return version("lingtai")
    except Exception:  # noqa: BLE001 - never break import over version metadata
        return "0+unknown"


__version__ = _resolve_version()
