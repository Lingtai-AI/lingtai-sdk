"""Grep capability — search file contents by regex.

Usage: Agent(capabilities=["grep"]) or capabilities=["file"]
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent


def get_description(lang: str = "en") -> str:
    return t(lang, "grep.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": t(lang, "grep.pattern")},
            "path": {"type": "string", "description": t(lang, "grep.path")},
            "glob": {"type": "string", "description": t(lang, "grep.glob"), "default": "*"},
            "max_matches": {"type": "integer", "description": t(lang, "grep.max_matches"), "default": 200},
        },
        "required": ["pattern"],
    }



def setup(agent: "BaseAgent") -> None:
    """Set up the grep capability on an agent."""
    lang = agent._config.language

    def handle_grep(args: dict) -> dict:
        pattern = args.get("pattern", "")
        if not pattern:
            return {"status": "error", "message": "pattern is required"}
        search_path = args.get("path", str(agent._working_dir))
        if not Path(search_path).is_absolute():
            search_path = str(agent._working_dir / search_path)
        max_matches = args.get("max_matches", 200)
        glob_filter = args.get("glob", "*")
        try:
            raw_results = agent._file_io.grep(pattern, path=search_path, max_results=max_matches)
            raw_truncated = len(raw_results) >= max_matches
            if glob_filter == "*":
                matches = [{"file": r.path, "line": r.line_number, "text": r.line} for r in raw_results]
            else:
                matches = [
                    {"file": r.path, "line": r.line_number, "text": r.line}
                    for r in raw_results
                    if fnmatch.fnmatch(Path(r.path).name, glob_filter)
                ]
            # truncated: true when the raw scan hit its cap (there may be
            # more matching files beyond what was scanned), OR when glob
            # filtering was active and we got fewer results than the cap
            # (meaning the glob may have discarded results that masked
            # additional matches).
            truncated = raw_truncated
            result: dict[str, Any] = {
                "matches": matches,
                "count": len(matches),
                "truncated": truncated,
            }
            # Issue #164: surface traversal budget / exclusion info so the
            # LLM can react to partial results instead of treating them
            # as definitive ("no matches found anywhere").
            stats = getattr(agent._file_io, "last_traversal", None)
            if stats is not None and stats.truncated_reason is not None:
                result["truncated"] = True
                result["truncated_reason"] = stats.truncated_reason
                result["traversal"] = {
                    "visited": stats.visited,
                    "elapsed_ms": stats.elapsed_ms,
                    "dirs_pruned": stats.dirs_pruned,
                    "files_skipped_size": stats.files_skipped_size,
                    "files_skipped_binary": stats.files_skipped_binary,
                }
            return result
        except Exception as e:
            return {"status": "error", "message": f"Grep failed: {e}"}

    agent.add_tool("grep", schema=get_schema(lang), handler=handle_grep, description=get_description(lang))
