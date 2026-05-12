"""Knowledge capability — private durable knowledge across molts.

Filesystem-backed catalog. Each agent has its own ``<agent>/knowledge/``
directory; every immediate subdirectory with a ``KNOWLEDGE.md`` file is a
knowledge entry. The capability is pure presentation: it scans the directory,
parses each ``KNOWLEDGE.md``'s YAML frontmatter for ``name`` + ``description``,
and injects a compact ``<knowledge>`` XML catalog into the system prompt's
``knowledge`` section. Bodies, supporting files, scripts, and assets live next
to ``KNOWLEDGE.md`` and are loaded on demand through the regular ``read`` tool.

Knowledge is structurally isomorphic to skills but physically separate:

- Skills live under ``<agent>/.library/{intrinsic,custom}/<name>/SKILL.md`` and
  are portable / shareable across agents.
- Knowledge lives under ``<agent>/knowledge/<name>/KNOWLEDGE.md`` and is
  private, agent-owned, and may reference agent-local paths, mail ids, and
  logs that skills must not depend on.

Tool surface is a single ``info`` action that returns a runtime health
snapshot (catalog size, problems). Bodies are read via the ``read`` tool, the
same way the agent opens a ``SKILL.md``.

Usage: ``Agent(capabilities={"knowledge": {}})`` or via init.json.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

PROVIDERS = {"providers": [], "default": "builtin"}


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        loaded = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): (" ".join(str(v).split()) if v is not None else "") for k, v in loaded.items()}


# ---------------------------------------------------------------------------
# Entry scanner
# ---------------------------------------------------------------------------

def _parse_entry_file(entry_file: Path, label: str) -> tuple[dict | None, dict | None]:
    try:
        text = entry_file.read_text(encoding="utf-8")
    except OSError as e:
        return None, {"folder": label, "reason": f"cannot read KNOWLEDGE.md: {e}"}

    fm = _parse_frontmatter(text)
    name = fm.get("name", "")
    description = fm.get("description", "")
    if not name:
        return None, {"folder": label, "reason": "KNOWLEDGE.md missing required frontmatter field: name"}
    if not description:
        return None, {"folder": label, "reason": "KNOWLEDGE.md missing required frontmatter field: description"}

    return {
        "name": name,
        "description": description,
        "version": fm.get("version", ""),
        "path": str(entry_file),
    }, None


def _scan_recursive(
    directory: Path,
    valid: list[dict],
    problems: list[dict],
    prefix: str = "",
) -> None:
    if not directory.is_dir():
        return

    try:
        children = sorted(directory.iterdir())
    except OSError:
        return

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue

        label = f"{prefix}{child.name}" if prefix else child.name
        entry_file = child / "KNOWLEDGE.md"

        if entry_file.is_file():
            entry, prob = _parse_entry_file(entry_file, label)
            if entry:
                valid.append(entry)
            if prob:
                problems.append(prob)
            continue

        # No KNOWLEDGE.md — classify.
        try:
            grandchildren = list(child.iterdir())
        except OSError:
            continue
        has_loose_files = any(
            not c.is_dir() and not c.name.startswith(".")
            for c in grandchildren
        )
        if has_loose_files:
            problems.append({
                "folder": label,
                "reason": "not a knowledge entry (no KNOWLEDGE.md) and has loose files — corrupted",
            })
            continue

        _scan_recursive(child, valid, problems, prefix=f"{label}/")


def _scan(directory: Path) -> tuple[list[dict], list[dict]]:
    valid: list[dict] = []
    problems: list[dict] = []
    _scan_recursive(directory, valid, problems)
    return valid, problems


# ---------------------------------------------------------------------------
# XML catalog builder
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_catalog_xml(entries: list[dict], lang: str) -> str:
    if not entries:
        return ""

    lines = [
        t(lang, "knowledge.preamble"),
        "",
        "<knowledge>",
    ]
    for e in entries:
        lines.append("  <entry>")
        lines.append(f"    <name>{_escape_xml(e['name'])}</name>")
        lines.append(f"    <description>{_escape_xml(e['description'])}</description>")
        lines.append(f"    <location>{_escape_xml(e['path'])}</location>")
        lines.append("  </entry>")
    lines.append("</knowledge>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core reconciliation (shared by setup and `info`)
# ---------------------------------------------------------------------------

def _reconcile(agent: "BaseAgent") -> dict:
    """Scan ``<agent>/knowledge/``, inject catalog, report status.

    Pure presentation: never writes inside ``knowledge/``. The agent is the
    sole author of its knowledge entries; the capability only renders them.
    """
    working_dir = agent._working_dir
    knowledge_dir = working_dir / "knowledge"

    entries, problems = _scan(knowledge_dir)

    lang = agent._config.language
    catalog_xml = _build_catalog_xml(entries, lang)
    if catalog_xml:
        agent.update_system_prompt("knowledge", catalog_xml, protected=True)
    else:
        agent.update_system_prompt("knowledge", "", protected=True)

    return {
        "status": "ok",
        "knowledge_dir": str(knowledge_dir),
        "catalog_size": len(entries),
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def get_description(lang: str = "en") -> str:
    return t(lang, "knowledge.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["info"],
                "description": t(lang, "knowledge.action_info"),
            },
        },
        "required": ["action"],
    }


def setup(agent: "BaseAgent", **_ignored) -> None:
    """Set up the knowledge capability.

    Scans ``<agent>/knowledge/`` for ``<name>/KNOWLEDGE.md`` entries and
    injects the catalog into the system prompt. Registers a single ``info``
    action that re-scans and returns a runtime health snapshot.

    Unknown kwargs (e.g. the historical ``knowledge_limit``) are accepted and
    ignored — the file-backed catalog has no fixed-size limit.
    """
    lang = agent._config.language

    _reconcile(agent)

    def handle_knowledge(args: dict) -> dict:
        action = args.get("action", "")
        if action == "info":
            return _reconcile(agent)
        return {
            "status": "error",
            "message": f"unknown action: {action!r}, only 'info' is supported",
        }

    agent.add_tool(
        "knowledge",
        schema=get_schema(lang),
        handler=handle_knowledge,
        description=get_description(lang),
    )
