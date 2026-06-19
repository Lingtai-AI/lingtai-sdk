"""Knowledge capability — private durable knowledge across molts.

Filesystem-backed catalog. Each agent has its own ``<agent>/knowledge/``
directory; every immediate subdirectory with a ``KNOWLEDGE.md`` file is a
knowledge entry. The capability is pure presentation: it scans the directory,
parses each ``KNOWLEDGE.md``'s YAML frontmatter for ``name`` + ``description``,
and injects a compact YAML catalog into the system prompt's
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

import json
import logging
import os
import re
import tempfile
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
# Legacy JSON migration
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slugify(value: str, fallback: str) -> str:
    """Return a filesystem-safe knowledge entry name."""
    base = value.strip().lower() or fallback
    base = _SLUG_RE.sub("-", base)
    base = base.strip(".-_") or fallback
    return base[:64].strip(".-_") or fallback


def _yaml_quote(value: str) -> str:
    """Render a small frontmatter scalar safely as JSON/YAML-compatible text."""
    return json.dumps(value, ensure_ascii=False)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _unique_entry_dir(root: Path, preferred: str, legacy_id: str) -> tuple[Path, str]:
    slug = _slugify(preferred, fallback=_slugify(legacy_id, "entry"))
    candidate = root / slug
    if not candidate.exists():
        return candidate, slug

    suffix_base = _slugify(legacy_id, "entry")
    candidate = root / f"{slug}-{suffix_base}"
    if not candidate.exists():
        return candidate, candidate.name

    i = 2
    while True:
        candidate = root / f"{slug}-{suffix_base}-{i}"
        if not candidate.exists():
            return candidate, candidate.name
        i += 1


def _format_knowledge_md(*, name: str, title: str, description: str, content: str, supplementary: str, legacy_id: str, created_at: str, origin: str) -> str:
    frontmatter = [
        "---",
        f"name: {_yaml_quote(name)}",
        f"description: {_yaml_quote(description)}",
        "version: \"1.0.0\"",
        f"origin: {_yaml_quote(origin)}",
    ]
    if legacy_id:
        frontmatter.append(f"legacy_id: {_yaml_quote(legacy_id)}")
    if title:
        frontmatter.append(f"title: {_yaml_quote(title)}")
    if created_at:
        frontmatter.append(f"created_at: {_yaml_quote(created_at)}")
    frontmatter.append("---")

    body_parts = ["\n".join(frontmatter), ""]
    if title:
        body_parts.append(f"# {title}")
        body_parts.append("")
    if content:
        body_parts.append(content.rstrip())
        body_parts.append("")
    else:
        body_parts.append(description)
        body_parts.append("")
    if supplementary:
        body_parts.append("## References")
        body_parts.append("")
        body_parts.append("- [Migrated supplementary material](references/supplementary.md)")
        body_parts.append("")
    return "\n".join(body_parts).rstrip() + "\n"


def _migrate_one_legacy_json(knowledge_dir: Path, legacy: Path, *, label: str, origin: str) -> list[dict]:
    if not legacy.is_file():
        return []

    problems: list[dict] = []
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [{"folder": label, "reason": f"cannot migrate legacy {label}: {e}"}]

    raw_entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(raw_entries, list):
        return [{"folder": label, "reason": f"cannot migrate legacy {label}: entries is not a list"}]

    migrated = 0
    for idx, raw in enumerate(raw_entries, 1):
        if not isinstance(raw, dict):
            problems.append({"folder": f"{label}[{idx}]", "reason": "legacy entry is not an object"})
            continue
        legacy_id = str(raw.get("id") or idx)
        title = str(raw.get("title") or raw.get("content") or f"Entry {idx}").strip()
        summary = str(raw.get("summary") or title or raw.get("content") or "Migrated knowledge entry").strip()
        content = str(raw.get("content") or "").strip()
        supplementary = str(raw.get("supplementary") or "").strip()
        created_at = str(raw.get("created_at") or "").strip()

        entry_dir, name = _unique_entry_dir(knowledge_dir, title, legacy_id)
        try:
            md = _format_knowledge_md(
                name=name,
                title=title,
                description=summary,
                content=content,
                supplementary=supplementary,
                legacy_id=legacy_id,
                created_at=created_at,
                origin=origin,
            )
            _atomic_write_text(entry_dir / "KNOWLEDGE.md", md)
            if supplementary:
                _atomic_write_text(entry_dir / "references" / "supplementary.md", supplementary.rstrip() + "\n")
            migrated += 1
        except OSError as e:
            problems.append({"folder": f"{label}[{idx}]", "reason": f"migration write failed: {e}"})

    if migrated:
        backup = legacy.with_name(legacy.name + ".migrated")
        if backup.exists():
            n = 2
            while legacy.with_name(legacy.name + f".migrated.{n}").exists():
                n += 1
            backup = legacy.with_name(legacy.name + f".migrated.{n}")
        try:
            legacy.rename(backup)
        except OSError as e:
            problems.append({"folder": label, "reason": f"migrated entries but could not rename legacy file: {e}"})

    return problems


def _migrate_legacy_json(working_dir: Path, knowledge_dir: Path) -> list[dict]:
    """One-time migration from legacy JSON stores into folders.

    Old entries become ``knowledge/<slug>/KNOWLEDGE.md``. The old
    ``supplementary`` field is written to ``references/supplementary.md`` and
    linked from the main document. Source JSON files are renamed after a
    successful migration so the operation is not repeated on the next scan.
    """
    problems: list[dict] = []
    problems.extend(_migrate_one_legacy_json(
        knowledge_dir,
        knowledge_dir / "knowledge.json",
        label="knowledge/knowledge.json",
        origin="migrated-knowledge-json",
    ))
    problems.extend(_migrate_one_legacy_json(
        knowledge_dir,
        working_dir / "codex" / "codex.json",
        label="codex/codex.json",
        origin="migrated-codex-json",
    ))
    return problems


# ---------------------------------------------------------------------------
# YAML catalog builder
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    """Escape only the characters XML actually requires in element text.

    `"` and `'` only need escaping inside attribute values; escaping them in
    element bodies just adds `&quot;`/`&apos;` noise to the rendered prompt
    without making the catalog any more parseable.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _indent_block(text: str, indent: str) -> str:
    """Indent every line of ``text`` by ``indent``. Empty input → empty string."""
    if not text:
        return ""
    return "\n".join(indent + line for line in text.splitlines())


def _build_catalog_yaml(entries: list[dict], lang: str) -> str:
    if not entries:
        return ""

    lines: list[str] = [
        t(lang, "knowledge.preamble"),
        "",
    ]
    for e in entries:
        lines.append(f"- name: {e['name']}")
        lines.append(f"  location: {e['path']}")
        lines.append("  description: |")
        for dl in e["description"].splitlines():
            lines.append(f"    {dl}" if dl else "    ")
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

    migration_problems = _migrate_legacy_json(working_dir, knowledge_dir)
    entries, problems = _scan(knowledge_dir)
    problems = migration_problems + problems

    lang = agent._config.language
    catalog_yaml = _build_catalog_yaml(entries, lang)
    if catalog_yaml:
        agent.update_system_prompt("knowledge", catalog_yaml, protected=True)
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
