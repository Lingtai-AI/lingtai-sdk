"""Read capability — read text file contents.

Usage: Agent(capabilities=["read"]) or capabilities=["file"]
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lingtai_kernel.tool_result_artifacts import PREVENTIVE_MAX_CHARS

from ...i18n import t
from .._file_paths import resolve_workdir_path

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}

# Read defaults to a smaller everyday page budget while the runtime tool-result
# boundary remains a larger non-configurable hard ceiling. Callers may pass
# ``max_chars`` per read call; values above the runtime ceiling are clamped.
DEFAULT_READ_CAP_CHARS: int = 100_000
READ_HARD_CAP_CHARS: int = PREVENTIVE_MAX_CHARS


def _valid_cap(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _runtime_hard_cap(agent: "BaseAgent") -> int:
    """Return the active runtime hard ceiling for provider-visible tool results."""
    executor_cap = _valid_cap(getattr(getattr(agent, "_executor", None), "_max_result_chars", None))
    if executor_cap is not None:
        return min(executor_cap, READ_HARD_CAP_CHARS)
    return READ_HARD_CAP_CHARS


def _resolve_call_cap(agent: "BaseAgent", requested_max_chars: object) -> int:
    """Return the per-call read cap, clamped by the runtime hard ceiling.

    ``max_chars`` lets the caller intentionally ask for smaller or larger chunks
    than the read default while the runtime hard cap remains the ceiling that
    prevents provider-visible tool-result blowups. Invalid per-call values are
    ignored and use the 50k read default.
    """
    runtime_cap = _runtime_hard_cap(agent)
    requested_cap = _valid_cap(requested_max_chars)
    if requested_cap is None:
        return min(DEFAULT_READ_CAP_CHARS, runtime_cap)
    return min(requested_cap, runtime_cap)


def get_description(lang: str = "en") -> str:
    return t(lang, "read.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": t(lang, "read.file_path")},
            "offset": {"type": "integer", "description": t(lang, "read.offset"), "default": 1},
            "limit": {"type": "integer", "description": t(lang, "read.limit"), "default": 2000},
            "max_chars": {"type": "integer", "description": t(lang, "read.max_chars")},
        },
        "required": ["file_path"],
    }


def _apply_cap(
    lines: list[str],
    start: int,
    requested_limit: int,
    cap_chars: int,
) -> tuple[str, dict]:
    """Build numbered content string, capping at *cap_chars* on whole-line boundaries.

    Returns ``(numbered_content, extra_meta)`` where *extra_meta* contains
    continuation fields only when the result was truncated.
    """
    total_lines = len(lines)
    end_exclusive = min(start + requested_limit, total_lines)
    window = lines[start:end_exclusive]

    chars_used = 0
    kept: list[str] = []
    line_truncated = False
    for i, line in enumerate(window):
        numbered_line = f"{start + i + 1}\t{line}"
        if chars_used + len(numbered_line) > cap_chars:
            if not kept:
                # A single line can exceed the cap by itself. Return a bounded
                # prefix, but mark the result as truncated so callers do not
                # mistake the prefix for the whole line.
                kept.append(numbered_line[:cap_chars])
                line_truncated = True
            break
        kept.append(numbered_line)
        chars_used += len(numbered_line)

    numbered = "".join(kept)
    returned_lines = len(kept)
    last_returned_line = start + returned_lines  # 1-based

    truncated = line_truncated or returned_lines < len(window)
    if not truncated:
        meta: dict = {}
    else:
        next_offset = last_returned_line + 1 if returned_lines else start + 1
        remaining = total_lines - last_returned_line
        meta = {
            "truncated": True,
            "cap_chars": cap_chars,
            "returned_chars": len(numbered),
            "requested_offset": start + 1,
            "requested_limit": requested_limit,
            "last_returned_line": last_returned_line if returned_lines else None,
            "next_offset": next_offset,
            "remaining_lines_estimate": max(0, remaining),
        }
        if line_truncated:
            meta["line_truncated"] = True

    return numbered, meta


def setup(agent: "BaseAgent") -> None:
    """Set up the read capability on an agent."""
    lang = agent._config.language

    def handle_read(args: dict) -> dict:
        path = args.get("file_path", "")
        if not path:
            return {"status": "error", "message": "file_path is required"}
        path = resolve_workdir_path(agent, path)
        offset = args.get("offset", 1)
        limit = args.get("limit", 2000)
        max_chars = args.get("max_chars")
        try:
            content = agent._file_io.read(path)
        except FileNotFoundError:
            # Spill-aware messaging: if the missing file is under
            # tmp/tool-results/, it was an ephemeral sidecar artifact
            # that has been cleaned up.  Give a specific hint instead
            # of the generic "File not found".
            # Normalize to collapse ".." components so that e.g.
            # tmp/tool-results/../not-a-spill.txt is NOT misclassified
            # as a spill path.
            try:
                rel = Path(path).resolve().relative_to(
                    Path(agent._working_dir).resolve()
                )
            except (ValueError, OSError):
                rel = Path(path)
            parts = rel.parts
            if len(parts) >= 3 and parts[0] == "tmp" and parts[1] == "tool-results":
                return {
                    "status": "error",
                    "message": (
                        "Spill artifact expired: this tmp/tool-results/ sidecar file "
                        "no longer exists. The original tool result content is "
                        "unavailable. Use the preview from the manifest or rerun the "
                        "source tool."
                    ),
                }
            return {"status": "error", "message": f"File not found: {path}"}
        except Exception as e:
            return {"status": "error", "message": f"Cannot read {path}: {e}"}
        lines = content.splitlines(keepends=True)
        start = max(0, offset - 1)
        numbered, extra = _apply_cap(lines, start, limit, _resolve_call_cap(agent, max_chars))
        result: dict = {
            "content": numbered,
            "total_lines": len(lines),
            "lines_shown": len(numbered.splitlines()) if numbered else 0,
        }
        result.update(extra)
        return result

    agent.add_tool("read", schema=get_schema(lang), handler=handle_read, description=get_description(lang))
