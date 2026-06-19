"""Mechanical redaction for durable trajectory data.

The live LLM conversation may contain user-provided credentials while a turn is
running. Durable trajectory surfaces written by the kernel (events JSONL, SQLite indexes,
and chat-history JSONL) must not persist those raw values. This module provides
a small, deterministic, fail-open redactor used immediately before writing or
indexing trajectory records.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any


_SECRET_PLACEHOLDER = "<REDACTED:secret>"

# High-confidence token/key shapes. Keep these intentionally conservative: a
# false negative in an unknown provider-specific format is preferable to turning
# ordinary prose into unreadable logs, while common credential families are
# redacted mechanically before trajectory writes.
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<REDACTED:telegram_bot_token>"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{40,}\b"), "<REDACTED:openai_project_key>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "<REDACTED:api_key>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "<REDACTED:github_token>"),
    (re.compile(r"\bgho_[A-Za-z0-9]{30,}\b"), "<REDACTED:github_token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), "<REDACTED:github_token>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), "<REDACTED:slack_token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED:aws_access_key_id>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "<REDACTED:google_api_key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<REDACTED:jwt>"),
    (re.compile(r"(?i)\bBearer\s+(?=[A-Za-z0-9._~+/-]*[._~+/])[A-Za-z0-9._~+/-]{20,}={0,2}\b"), "Bearer <REDACTED:bearer_token>"),
)

# Keyed assignments in JSON/YAML/env/shell-ish text. Group 1 preserves the
# visible key and separators; group 2 is the value to remove. Quoted and
# unquoted values are handled separately to avoid consuming following prose.
_KEY_NAME = (
    r"api[_-]?key|secret|token|password|passwd|pwd|credential|credentials|"
    r"bot[_-]?token|email[_-]?password|app[_-]?secret|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token"
)
_JSON_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(([\"'])\b(?:{_KEY_NAME})\b\2\s*:\s*)([\"'])([^\"']{{8,}})(\3)"
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(\b(?:{_KEY_NAME})\b\s*[=:]\s*)([\"'])([^\"']{{8,}})(\2)"
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)((?:[\"\']?\b(?:{_KEY_NAME})\b[\"\']?)\s*[=:]\s*)([^\s,;}}\"\']{{8,}})"
)

# Mapping keys whose scalar values should always be redacted, even if the value
# does not match a known provider token shape (for example app passwords).
_SECRET_KEY_RE = re.compile(rf"(?i)^(?:{_KEY_NAME})$")


def redact_text(text: str) -> str:
    """Return *text* with high-confidence secret values replaced."""
    redacted = text
    for pattern, replacement in _TOKEN_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    redacted = _JSON_QUOTED_ASSIGNMENT_RE.sub(rf"\1\3{_SECRET_PLACEHOLDER}\5", redacted)
    redacted = _QUOTED_ASSIGNMENT_RE.sub(rf"\1\2{_SECRET_PLACEHOLDER}\4", redacted)
    redacted = _UNQUOTED_ASSIGNMENT_RE.sub(rf"\1{_SECRET_PLACEHOLDER}", redacted)
    return redacted


def _redact_value(value: Any, *, key_hint: str | None = None) -> Any:
    if isinstance(value, str):
        if key_hint and _SECRET_KEY_RE.match(key_hint):
            return _SECRET_PLACEHOLDER if len(value) >= 4 else value
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: _redact_value(item, key_hint=str(key))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    # Avoid treating bytes as a sequence of integers. Durable logs should rarely
    # carry bytes, but if they do, stringify through repr and redact the text.
    if isinstance(value, (bytes, bytearray)):
        return redact_text(repr(value))
    return value


def redact_for_trajectory(value: Any) -> Any:
    """Return a redacted copy suitable for durable trajectory storage.

    The original object is never mutated; failures are intentionally avoided so
    trajectory logging remains fail-open from the caller's perspective.
    """
    try:
        return _redact_value(value)
    except Exception:
        # Last-resort defensive copy: do not let the redactor break runtime
        # logging. Returning a deep copy preserves the "do not mutate caller
        # state" contract for ordinary objects.
        try:
            return copy.deepcopy(value)
        except Exception:
            return value


__all__ = ["redact_for_trajectory", "redact_text"]
