"""Grep capability — search file contents by regex.

Usage: Agent(capabilities=["grep"]) or capabilities=["file"]
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...i18n import t
from .._file_paths import resolve_workdir_path

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
            "summary": {"type": "boolean", "description": t(lang, "tool.summary_option"), "default": False},
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
        search_path = resolve_workdir_path(agent, search_path)
        max_matches = args.get("max_matches", 200)
        glob_filter = args.get("glob", "*")
        try:
            # Push the glob filter into the service so excluded files are
            # pruned *before* stat / read, instead of scanning every file
            # under the search root and post-filtering the matches. ``"*"``
            # is the schema default and means "no filter".
            service_glob = None if glob_filter in (None, "", "*") else glob_filter
            raw_results = agent._file_io.grep(
                pattern,
                path=search_path,
                max_results=max_matches,
                glob_filter=service_glob,
            )
            matches = [{"file": r.path, "line": r.line_number, "text": r.line} for r in raw_results]
            # truncated: true when the (already glob-pruned) scan hit its
            # cap — there may be more matching files beyond what was
            # scanned.
            truncated = len(raw_results) >= max_matches
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
