"""Shared config resolution helpers — env vars, capabilities, paths."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


# Module-level: pre-compiled regex for JSONC string literals (matches "..."
# with escape sequences). Used by load_jsonc to skip over string contents
# when stripping // comments.
_JSONC_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def load_jsonc(path: str | Path) -> dict:
    """Load a JSON or JSONC file (strips // comments and trailing commas).

    Comment stripping is string-aware: // inside a quoted JSON string value
    is never treated as a comment (preserving URLs like "https://host/...").
    """
    text = Path(path).read_text(encoding="utf-8")
    # Strip // comments only when not inside a JSON string.
    # Strategy: tokenise by alternating between string spans and non-string
    # spans; within non-string spans, replace //...EOL with nothing.
    parts: list[str] = []
    pos = 0
    for m in _JSONC_STRING_RE.finditer(text):
        # Non-string chunk before this string: strip comments
        chunk = re.sub(r'//[^\n]*', '', text[pos:m.start()])
        parts.append(chunk)
        # String literal: preserve verbatim
        parts.append(m.group())
        pos = m.end()
    # Trailing non-string chunk after last string
    parts.append(re.sub(r'//[^\n]*', '', text[pos:]))
    text = ''.join(parts)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


def resolve_env(value: str | None, env_name: str | None) -> str | None:
    """Resolve a value from env var name, falling back to raw value."""
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
    return value


def load_env_file(path: str | Path, *, overwrite: bool = False) -> None:
    """Load a .env file into os.environ.

    By default, existing process environment variables are preserved so a
    caller's explicit shell environment wins at initial boot. Pass
    ``overwrite=True`` for deliberate config reloads (notably
    ``system(action="refresh")``) so edits to the agent's env_file replace
    stale values inherited by the relaunched process.
    """
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        if not _:
            continue
        key = key.strip()
        val = val.strip().strip("'\"")
        if overwrite or key not in os.environ:
            os.environ[key] = val


def resolve_file(value: str | None, file_path: str | None) -> str | None:
    """Resolve a value from a file path, falling back to raw value."""
    if file_path:
        p = Path(file_path).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return value


def _resolve_env_fields(d: dict) -> dict:
    """Resolve ``*_env`` keys in a dict using ``resolve_env``."""
    result = dict(d)
    env_keys = [k for k in result if k.endswith("_env")]
    for env_key in env_keys:
        base_key = env_key[: -len("_env")]
        result[base_key] = resolve_env(result.get(base_key), result.pop(env_key))
    return result


def _resolve_file_fields(d: dict) -> dict:
    """Resolve ``*_file`` keys in a dict using ``resolve_file``."""
    result = dict(d)
    file_keys = [k for k in result if k.endswith("_file")]
    for file_key in file_keys:
        base_key = file_key[: -len("_file")]
        result[base_key] = resolve_file(result.get(base_key), result.pop(file_key))
    return result


def resolve_paths(data: dict, working_dir: str | Path) -> None:
    """Make every path field in init.json absolute, resolved against working_dir.

    Mutates *data* in place. Handles top-level: env_file, venv_path, and
    *_file (covenant_file, principle_file, etc.).

    MCP-related paths (init.json's `mcp.<name>.env.LINGTAI_*_CONFIG`) are
    intentionally left relative — each MCP server resolves its own config
    path against LINGTAI_AGENT_DIR at startup, which the kernel injects.
    """
    wd = Path(working_dir)

    for key in ("env_file", "venv_path",
                "covenant_file", "principle_file", "procedures_file",
                "substrate_file",
                "brief_file", "pad_file",
                "prompt_file", "comment_file"):
        if key in data and isinstance(data[key], str) and data[key]:
            p = Path(data[key]).expanduser()
            if not p.is_absolute():
                p = wd / p
            data[key] = str(p)


def _resolve_capabilities(capabilities: dict) -> dict:
    """Resolve ``*_env`` fields in each capability's kwargs."""
    resolved = {}
    for name, kwargs in capabilities.items():
        if isinstance(kwargs, dict) and kwargs:
            resolved[name] = _resolve_env_fields(kwargs)
        else:
            resolved[name] = kwargs
    return resolved


