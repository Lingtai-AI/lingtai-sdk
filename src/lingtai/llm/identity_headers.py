"""Shared LingTai HTTP identity headers for LLM provider adapters."""

from __future__ import annotations

import importlib.metadata as metadata


_PACKAGE_NAME = "lingtai"
_CLIENT_NAME = "LingTai"


def lingtai_version() -> str | None:
    """Return the installed LingTai package version, if package metadata exists."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return None


def lingtai_user_agent() -> str:
    """Return the honest LingTai User-Agent token."""
    installed_version = lingtai_version()
    if installed_version:
        return f"{_CLIENT_NAME}/{installed_version}"
    return _CLIENT_NAME


def lingtai_identity_headers(*, user_agent: bool = True) -> dict[str, str]:
    """Return default non-secret LingTai identity/version headers."""
    headers = {"X-LingTai-Client": _CLIENT_NAME}
    if user_agent:
        headers["User-Agent"] = lingtai_user_agent()
    installed_version = lingtai_version()
    if installed_version:
        headers["X-LingTai-Version"] = installed_version
    return headers


def merge_lingtai_identity_headers(
    headers: dict | None = None, *, user_agent: bool = True
) -> dict[str, str]:
    """Merge LingTai identity headers under caller-supplied headers.

    Header names are compared case-insensitively, while caller spelling and
    values are preserved. This keeps provider/user headers authoritative.
    """
    caller_headers = dict(headers or {})
    merged = dict(caller_headers)
    caller_names = {str(name).lower() for name in caller_headers}
    for name, value in lingtai_identity_headers(user_agent=user_agent).items():
        if name.lower() not in caller_names:
            merged[name] = value
    return merged
