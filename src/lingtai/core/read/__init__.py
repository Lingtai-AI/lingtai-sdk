"""Read capability — read text file contents.

Usage: Agent(capabilities=["read"]) or capabilities=["file"]
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}

# Conservative character budget for a single read result.  Tool-result
# transport caps are typically ~10 k characters; we stay well under that to
# leave room for the JSON envelope and metadata fields.
READ_CAP_CHARS: int = 8_000


def get_description(lang: str = "en") -> str:
    return t(lang, "read.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": t(lang, "read.file_path")},
            "offset": {"type": "integer", "description": t(lang, "read.offset"), "default": 1},
            "limit": {"type": "integer", "description": t(lang, "read.limit"), "default": 2000},
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
        if not Path(path).is_absolute():
            path = str(agent._working_dir / path)
        offset = args.get("offset", 1)
        limit = args.get("limit", 2000)
        try:
            content = agent._file_io.read(path)
        except FileNotFoundError:
            return {"status": "error", "message": f"File not found: {path}"}
        except Exception as e:
            return {"status": "error", "message": f"Cannot read {path}: {e}"}
        lines = content.splitlines(keepends=True)
        start = max(0, offset - 1)
        numbered, extra = _apply_cap(lines, start, limit, READ_CAP_CHARS)
        result: dict = {
            "content": numbered,
            "total_lines": len(lines),
            "lines_shown": len(numbered.splitlines()) if numbered else 0,
        }
        result.update(extra)
        return result

    agent.add_tool("read", schema=get_schema(lang), handler=handle_read, description=get_description(lang))
