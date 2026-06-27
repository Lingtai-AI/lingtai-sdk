"""NoKV capability — explicit read-first NoKV namespace inspection."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ...services.file_io import NoKVFileIOBackend
from ...services.nokv import (
    DEFAULT_NOKV_URI_PREFIXES,
    NoKVUnsupportedError,
    format_nokv_uri,
)

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}


def get_description(lang: str = "en") -> str:
    return (
        "Inspect an explicitly configured NoKV namespace. Use for NoKV ls/stat/"
        "catalog/find/read/grep/snapshot operations; ordinary local file paths "
        "remain handled by read/write/edit/glob/grep."
    )


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ls", "stat", "catalog", "find", "read", "grep", "snapshot"],
                "description": "NoKV action to perform.",
            },
            "path": {
                "type": "string",
                "description": "NoKV URI or object path, for example nokv://lingtai/projects/p.",
            },
            "query": {
                "type": "string",
                "description": "Optional substring query for find/catalog filtering.",
            },
            "pattern": {
                "type": "string",
                "description": "Regex pattern for grep.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum grep/find results.",
                "default": 50,
            },
        },
        "required": ["action"],
    }


class NoKVManager:
    def __init__(
        self,
        *,
        client: Any | None = None,
        uri_prefixes: tuple[str, ...] = DEFAULT_NOKV_URI_PREFIXES,
        default_namespace: str | None = None,
    ):
        self.default_namespace = default_namespace
        self.backend = NoKVFileIOBackend(client, uri_prefixes=uri_prefixes)

    def _path(self, args: dict) -> str:
        path = args.get("path") or self.default_namespace
        if not path:
            return "nokv://"
        return str(path)

    def _uri_entry(self, entry: dict) -> dict:
        out = dict(entry)
        out["path"] = format_nokv_uri(str(entry.get("path", "")))
        return out

    def _grep(self, path: str, pattern: str, max_results: int) -> dict:
        regex = re.compile(pattern)
        matches: list[dict] = []
        for entry in self.backend.list(path):
            content = self.backend.read(str(entry["path"]))
            for line_number, line in enumerate(content.splitlines(), 1):
                if not regex.search(line):
                    continue
                matches.append({
                    "path": format_nokv_uri(str(entry["path"])),
                    "line": line_number,
                    "text": line,
                    "generation": entry.get("generation"),
                    "metadata": entry.get("metadata") or {},
                })
                if len(matches) >= max_results:
                    return {"status": "ok", "action": "grep", "matches": matches, "truncated": True}
        return {"status": "ok", "action": "grep", "matches": matches, "truncated": False}

    def handle(self, args: dict) -> dict:
        action = args.get("action", "")
        path = self._path(args)
        try:
            if action in {"ls", "catalog"}:
                return {
                    "status": "ok",
                    "action": action,
                    "entries": [self._uri_entry(entry) for entry in self.backend.list(path)],
                }
            if action == "find":
                query = str(args.get("query") or "")
                max_results = int(args.get("max_results") or 50)
                entries = [
                    self._uri_entry(entry)
                    for entry in self.backend.list(path)
                    if not query or query in str(entry.get("path", ""))
                ][:max_results]
                return {"status": "ok", "action": "find", "entries": entries}
            if action == "stat":
                return {"status": "ok", "action": "stat", "stat": self._uri_entry(self.backend.stat(path))}
            if action == "read":
                stat = self.backend.stat(path)
                return {
                    "status": "ok",
                    "action": "read",
                    "path": format_nokv_uri(stat["path"]),
                    "content": self.backend.read(path),
                    "generation": stat.get("generation"),
                    "metadata": stat.get("metadata") or {},
                }
            if action == "grep":
                pattern = args.get("pattern")
                if not pattern:
                    return {"status": "error", "message": "pattern is required for grep"}
                return self._grep(path, str(pattern), int(args.get("max_results") or 50))
            if action == "snapshot":
                snapshot = self.backend.snapshot(path)
                snapshot["path"] = format_nokv_uri(str(snapshot.get("path", path)))
                return {"status": "ok", "action": "snapshot", "snapshot": snapshot}
            return {
                "status": "error",
                "message": (
                    "unknown action: "
                    f"{action!r}; expected ls/stat/catalog/find/read/grep/snapshot"
                ),
            }
        except NoKVUnsupportedError as e:
            return {"status": "error", "message": str(e)}
        except FileNotFoundError as e:
            return {"status": "error", "message": str(e)}
        except KeyError as e:
            return {"status": "error", "message": f"NoKV object not found: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"NoKV {action or 'operation'} failed: {e}"}


def setup(
    agent: "BaseAgent",
    *,
    client: Any | None = None,
    default_namespace: str | None = None,
    uri_prefixes: list[str] | tuple[str, ...] | None = None,
    **_ignored: Any,
) -> NoKVManager:
    manager = NoKVManager(
        client=client,
        uri_prefixes=tuple(uri_prefixes or DEFAULT_NOKV_URI_PREFIXES),
        default_namespace=default_namespace,
    )
    agent.add_tool(
        "nokv",
        schema=get_schema(agent._config.language),
        handler=manager.handle,
        description=get_description(agent._config.language),
    )
    return manager
