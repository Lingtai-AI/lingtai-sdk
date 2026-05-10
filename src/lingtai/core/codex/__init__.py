"""Codex capability — durable self-memory across molts.

A journal-shaped knowledge store persisted in codex/codex.json.
Each entry's id + title + summary is always visible in the system prompt;
content and supplementary load on demand via view().

Usage:
    agent = Agent(capabilities=["codex"])
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}


def get_description(lang: str = "en") -> str:
    return t(lang, "codex.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["submit", "view", "consolidate", "delete"],
                "description": t(lang, "codex.action"),
            },
            "title": {
                "type": "string",
                "description": t(lang, "codex.title"),
            },
            "summary": {
                "type": "string",
                "description": t(lang, "codex.summary"),
            },
            "content": {
                "type": "string",
                "description": t(lang, "codex.content"),
            },
            "supplementary": {
                "type": "string",
                "description": t(lang, "codex.supplementary"),
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "codex.ids"),
            },
            "include_supplementary": {
                "type": "boolean",
                "description": t(lang, "codex.include_supplementary"),
            },
        },
        "required": ["action"],
    }



class CodexManager:
    """Durable self-memory — submit, view, consolidate, delete."""

    DEFAULT_MAX_ENTRIES = 50

    def __init__(self, agent: "BaseAgent", *, codex_limit: int | None = None):
        self._agent = agent
        self._working_dir = agent._working_dir
        self._max_entries = codex_limit if codex_limit is not None else self.DEFAULT_MAX_ENTRIES

        self._codex_json = self._working_dir / "codex" / "codex.json"
        self._entries: list[dict] = self._load_entries()

    # ------------------------------------------------------------------
    # System prompt catalog
    # ------------------------------------------------------------------

    def _inject_catalog(self) -> None:
        """Inject codex entry index (id + title + summary) into system prompt."""
        if not self._entries:
            self._agent.update_system_prompt("codex", "", protected=True)
            return

        lines = [
            f"Your codex has {len(self._entries)}/{self._max_entries} entries:",
            "",
        ]
        for e in self._entries:
            lines.append(f"- [{e['id']}] {e['title']}: {e['summary']}")
        lines.append("")
        lines.append(
            "Use codex(view, ids=[...]) to read full content. "
            "Pass include_supplementary=true for backing material."
        )

        self._agent.update_system_prompt("codex", "\n".join(lines), protected=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_entries(self) -> list[dict]:
        if not self._codex_json.is_file():
            return []
        try:
            data = json.loads(self._codex_json.read_text())
            entries = data.get("entries", [])
            for e in entries:
                if "title" not in e:
                    e["title"] = e.get("content", "")[:50] or "Untitled"
                    e["summary"] = e.get("content", "")[:200]
                    e["supplementary"] = ""
            return entries
        except (json.JSONDecodeError, OSError):
            return []

    def _save_entries(self) -> None:
        data = {"version": 1, "entries": self._entries}
        self._codex_json.parent.mkdir(exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._codex_json.parent), suffix=".tmp",
        )
        try:
            os.write(fd, json.dumps(data, indent=2, ensure_ascii=False).encode())
            os.close(fd)
            os.replace(tmp, str(self._codex_json))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def _make_id(content: str, created_at: str) -> str:
        return hashlib.sha256(
            (content + created_at).encode()
        ).hexdigest()[:8]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    _VALID_ACTIONS = {"submit", "view", "consolidate", "delete"}

    def handle(self, args: dict) -> dict:
        action = args.get("action", "")
        if action not in self._VALID_ACTIONS:
            return {
                "error": f"Unknown action: {action!r}. "
                f"Valid: {', '.join(sorted(self._VALID_ACTIONS))}.",
            }
        method = getattr(self, f"_{action}")
        return method(args)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _submit(self, args: dict) -> dict:
        title = args.get("title", "").strip()
        summary = args.get("summary", "").strip()
        content = args.get("content", "").strip()
        supplementary = args.get("supplementary", "").strip()
        if not title:
            return {"error": "title is required for submit."}
        if not summary:
            return {"error": "summary is required for submit."}
        if len(self._entries) >= self._max_entries:
            return {
                "error": f"Codex is full ({self._max_entries} entries). "
                "Consolidate related entries first, "
                "delete obsolete ones, or use supplementary "
                "to pack more detail into existing entries.",
                "entries": len(self._entries),
                "max": self._max_entries,
            }
        now = datetime.now(timezone.utc).isoformat()
        # Seed for id: title + (content or summary) — preserves uniqueness
        # when content is omitted.
        entry_id = self._make_id(title + (content or summary), now)
        self._entries.append({
            "id": entry_id,
            "title": title,
            "summary": summary,
            "content": content,
            "supplementary": supplementary,
            "created_at": now,
        })
        self._save_entries()
        self._inject_catalog()
        return {
            "status": "ok",
            "id": entry_id,
            "entries": len(self._entries),
            "max": self._max_entries,
        }

    def _view(self, args: dict) -> dict:
        ids = args.get("ids")
        if not ids:
            return {"error": "ids is required for view."}
        include_supp = bool(args.get("include_supplementary", False))

        entries_by_id = {e["id"]: e for e in self._entries}
        invalid = [i for i in ids if i not in entries_by_id]
        if invalid:
            return {"error": f"Unknown codex IDs: {', '.join(invalid)}"}

        result_entries = []
        for entry_id in ids:
            e = entries_by_id[entry_id]
            item = {
                "id": e["id"],
                "title": e["title"],
                "summary": e["summary"],
                "content": e.get("content", ""),
            }
            if include_supp:
                item["supplementary"] = e.get("supplementary", "")
            result_entries.append(item)

        return {"status": "ok", "entries": result_entries}

    def _consolidate(self, args: dict) -> dict:
        ids = args.get("ids")
        title = args.get("title", "").strip()
        summary = args.get("summary", "").strip()
        content = args.get("content", "").strip()
        supplementary = args.get("supplementary", "").strip()
        if not ids:
            return {"error": "ids is required for consolidate."}
        if not title:
            return {"error": "title is required for consolidate."}
        if not summary:
            return {"error": "summary is required for consolidate."}

        existing_ids = {e["id"] for e in self._entries}
        invalid = [i for i in ids if i not in existing_ids]
        if invalid:
            return {"error": f"Unknown codex IDs: {', '.join(invalid)}"}

        ids_set = set(ids)
        self._entries = [e for e in self._entries if e["id"] not in ids_set]

        now = datetime.now(timezone.utc).isoformat()
        new_id = self._make_id(title + (content or summary), now)
        self._entries.append({
            "id": new_id,
            "title": title,
            "summary": summary,
            "content": content,
            "supplementary": supplementary,
            "created_at": now,
        })

        self._save_entries()
        self._inject_catalog()
        return {"status": "ok", "id": new_id, "removed": len(ids)}

    def _delete(self, args: dict) -> dict:
        ids = args.get("ids")
        if not ids:
            return {"error": "ids is required for delete."}

        existing_ids = {e["id"] for e in self._entries}
        invalid = [i for i in ids if i not in existing_ids]
        if invalid:
            return {"error": f"Unknown codex IDs: {', '.join(invalid)}"}

        ids_set = set(ids)
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["id"] not in ids_set]
        removed = before - len(self._entries)

        self._save_entries()
        self._inject_catalog()
        return {"status": "ok", "removed": removed}


def setup(agent: "BaseAgent", *, codex_limit: int | None = None) -> CodexManager:
    """Set up codex capability — durable self-memory across molts."""
    lang = agent._config.language

    mgr = CodexManager(agent, codex_limit=codex_limit)

    agent.add_tool(
        "codex", schema=get_schema(lang), handler=mgr.handle, description=get_description(lang),
    )

    # Inject codex catalog into system prompt at boot
    mgr._inject_catalog()

    return mgr
