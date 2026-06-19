"""MCP capability — per-agent registry of MCP servers (pure presentation).

Symmetric to the ``library`` capability:

- Per-agent registry lives at ``<agent>/mcp_registry.jsonl`` (sibling to
  ``init.json``). One JSON record per line.
- Capability scans the registry on setup, validates each line, renders the
  registry as XML into the system prompt's ``mcp`` section.
- Boot-time decompression: any name in ``init.json``'s ``addons: [...]`` list
  that isn't already in the registry gets appended from the kernel-shipped
  catalog (``lingtai/mcp_catalog.json``). Append-only, idempotent.
- All registry mutations (register, deregister, update) happen via file
  operations from the agent (``write``, ``edit``). The capability provides
  guidance via the umbrella SKILL.md and a single ``show`` action that
  re-renders the prompt section.

Tool surface: a single ``show`` action that returns the umbrella manual body,
the current registry, and a runtime health snapshot.

Usage: ``Agent(capabilities=["mcp"])`` or via init.json.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

PROVIDERS = {"providers": [], "default": "builtin"}

REGISTRY_FILENAME = "mcp_registry.jsonl"
CATALOG_FILENAME = "mcp_catalog.json"

# Match library's name convention: lowercase, dash-separated, bounded length.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_VALID_TRANSPORTS = {"stdio", "http"}
_MAX_SUMMARY_LEN = 200


# ---------------------------------------------------------------------------
# Catalog (kernel-shipped) — read once, cached on first access.
# ---------------------------------------------------------------------------

_CATALOG_CACHE: dict[str, dict] | None = None


def _load_catalog() -> dict[str, dict]:
    """Read the kernel-shipped MCP catalog. Cached after first call.

    Returns a dict mapping name → record. Entries with leading underscore
    (e.g. ``_comment``) are skipped.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    catalog_path = Path(__file__).parent.parent.parent / CATALOG_FILENAME
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("mcp: failed to load catalog at %s: %s", catalog_path, e)
        _CATALOG_CACHE = {}
        return _CATALOG_CACHE

    _CATALOG_CACHE = {
        name: record for name, record in raw.items()
        if not name.startswith("_") and isinstance(record, dict)
    }
    return _CATALOG_CACHE


# ---------------------------------------------------------------------------
# Validator — single source of truth for registry record schema.
# ---------------------------------------------------------------------------

def validate_record(record: dict) -> tuple[bool, str | None]:
    """Validate a single MCP registry record.

    Returns (is_valid, error_message). On success, error_message is None.
    """
    if not isinstance(record, dict):
        return False, "record must be a JSON object"

    name = record.get("name")
    if not isinstance(name, str):
        return False, "missing or non-string field: name"
    if not _NAME_RE.match(name):
        return False, f"invalid name {name!r}: must match {_NAME_RE.pattern}"

    summary = record.get("summary")
    if not isinstance(summary, str) or not summary:
        return False, "missing or empty field: summary"
    if len(summary) > _MAX_SUMMARY_LEN:
        return False, f"summary too long ({len(summary)} > {_MAX_SUMMARY_LEN} chars)"

    transport = record.get("transport")
    if transport not in _VALID_TRANSPORTS:
        return False, f"invalid transport {transport!r}: must be one of {sorted(_VALID_TRANSPORTS)}"

    if transport == "stdio":
        if not isinstance(record.get("command"), str):
            return False, "stdio transport requires field 'command' (string)"
        args = record.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return False, "stdio transport requires field 'args' (list of strings)"
    else:  # http
        if not isinstance(record.get("url"), str):
            return False, "http transport requires field 'url' (string)"

    source = record.get("source")
    if not isinstance(source, str) or not source:
        return False, "missing or empty field: source"

    # Optional: homepage must be a string when present.
    homepage = record.get("homepage")
    if homepage is not None and (not isinstance(homepage, str) or not homepage):
        return False, "homepage must be a non-empty string when present"

    return True, None


def validate_registry_line(line: str) -> tuple[bool, str | None, dict | None]:
    """Validate a single JSONL line. Returns (is_valid, error, parsed_record)."""
    line = line.strip()
    if not line:
        return False, "empty line", None
    try:
        record = json.loads(line)
    except json.JSONDecodeError as e:
        return False, f"invalid JSON: {e}", None
    valid, err = validate_record(record)
    return valid, err, record if valid else None


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------

def _registry_path(working_dir: Path) -> Path:
    return working_dir / REGISTRY_FILENAME


def read_registry(working_dir: Path) -> tuple[list[dict], list[dict]]:
    """Read and validate the registry file.

    Returns (valid_records, problems). Problems is a list of
    {line: int, error: str, raw: str} dicts.
    """
    path = _registry_path(working_dir)
    if not path.is_file():
        return [], []

    valid: list[dict] = []
    problems: list[dict] = []
    seen_names: set[str] = set()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [], [{"line": 0, "error": f"cannot read registry: {e}", "raw": ""}]

    for i, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        ok, err, record = validate_registry_line(raw)
        if not ok:
            problems.append({"line": i, "error": err or "unknown", "raw": raw})
            continue
        assert record is not None
        if record["name"] in seen_names:
            problems.append({
                "line": i,
                "error": f"duplicate name {record['name']!r}",
                "raw": raw,
            })
            continue
        seen_names.add(record["name"])
        valid.append(record)

    return valid, problems


def _append_record(working_dir: Path, record: dict) -> None:
    """Append a validated record as a JSONL line. Caller must validate first."""
    path = _registry_path(working_dir)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Boot-time decompression: addons:[...] → registry
# ---------------------------------------------------------------------------

def decompress_addons(working_dir: Path, addons: list[str]) -> dict:
    """Append catalog entries for any addon name not already in the registry.

    Non-destructive: never modifies existing records, never reorders.
    Idempotent: running multiple times produces the same registry as once.

    Returns a report dict {appended: [...], skipped: [...], unknown: [...],
    invalid: [...]}.
    """
    catalog = _load_catalog()
    existing, _problems = read_registry(working_dir)
    existing_names = {r["name"] for r in existing}

    appended: list[str] = []
    skipped: list[str] = []
    unknown: list[str] = []
    invalid: list[dict] = []

    import sys
    substitutions = {"{python}": sys.executable}

    def _substitute(value):
        if isinstance(value, str):
            for k, v in substitutions.items():
                if k in value:
                    value = value.replace(k, v)
            return value
        if isinstance(value, list):
            return [_substitute(x) for x in value]
        if isinstance(value, dict):
            return {k: _substitute(v) for k, v in value.items()}
        return value

    for name in addons:
        if name in existing_names:
            skipped.append(name)
            continue
        if name not in catalog:
            unknown.append(name)
            log.warning("mcp: addon %r not found in catalog", name)
            continue
        record = _substitute(dict(catalog[name]))
        ok, err = validate_record(record)
        if not ok:
            invalid.append({"name": name, "error": err})
            log.warning("mcp: catalog entry %r failed validation: %s", name, err)
            continue
        _append_record(working_dir, record)
        appended.append(name)
        existing_names.add(name)

    return {
        "appended": appended,
        "skipped": skipped,
        "unknown": unknown,
        "invalid": invalid,
    }


# ---------------------------------------------------------------------------
# XML registry builder (rendered into system prompt)
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_registry_xml(records: list[dict]) -> str:
    if not records:
        return ""
    lines = [
        "The following MCP servers are registered for this agent. To activate "
        "one, add an entry under `mcp` in your init.json and run "
        "system(action=\"refresh\"). See the mcp-manual skill in "
        ".library/intrinsic/capabilities/mcp/ for the full registration "
        "contract. When you need install or config instructions for a "
        "specific MCP, fetch its <homepage> README via web_read or "
        "bash + curl as your first step (unless you have other guidance).",
        "",
        "<registered_mcp>",
    ]
    for r in records:
        lines.append("  <mcp>")
        lines.append(f"    <name>{_escape_xml(r['name'])}</name>")
        lines.append(f"    <summary>{_escape_xml(r['summary'])}</summary>")
        lines.append(f"    <transport>{_escape_xml(r['transport'])}</transport>")
        lines.append(f"    <source>{_escape_xml(r.get('source', ''))}</source>")
        homepage = r.get("homepage")
        if homepage:
            lines.append(f"    <homepage>{_escape_xml(homepage)}</homepage>")
        lines.append("  </mcp>")
    lines.append("</registered_mcp>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reconciliation (shared by setup and `show` action)
# ---------------------------------------------------------------------------

def _reconcile(agent: "BaseAgent") -> dict:
    """Read registry, render into prompt, return health snapshot."""
    working_dir = agent._working_dir
    records, problems = read_registry(working_dir)

    xml = _build_registry_xml(records)
    agent.update_system_prompt("mcp", xml, protected=True)

    # Health: the umbrella manual must be present.
    intrinsic_dir = working_dir / ".library" / "intrinsic"
    manual_path = intrinsic_dir / "capabilities" / "mcp" / "SKILL.md"
    status = "ok"
    error: str | None = None
    if not manual_path.is_file():
        status = "degraded"
        error = (
            "mcp manual missing — initializer may have failed or "
            "capability not installed correctly"
        )
        manual_body = ""
    else:
        manual_body = manual_path.read_text(encoding="utf-8")

    result = {
        "status": status,
        "mcp_manual": manual_body,
        "registry_path": str(_registry_path(working_dir)),
        "registered_count": len(records),
        "registered": [
            {"name": r["name"], "summary": r["summary"]}
            for r in records
        ],
        "problems": problems,
    }
    if error:
        result["error"] = error
    return result


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "Your per-agent MCP server registry. The <registered_mcp> catalog in your "
    "system prompt lists every MCP server currently registered. Before using "
    "this tool (registering, deregistering, updating, or troubleshooting MCP "
    "servers), read the `mcp-manual` skill — call `show` to fetch its body "
    "(registration contract, file paths, schema) plus a runtime health "
    "snapshot; no exceptions. To register, deregister, or update MCPs, edit "
    "mcp_registry.jsonl directly with write/edit and call "
    "system(action=\"refresh\")."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["show"],
            "description": (
                "show: return the mcp-manual skill body plus a runtime health "
                "snapshot (registry contents, problems, registry path)."
            ),
        },
    },
    "required": ["action"],
}


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def get_schema(lang: str = "en") -> dict:
    return _SCHEMA


def setup(agent: "BaseAgent", **_ignored) -> None:
    """Set up the mcp capability.

    The capability is pure presentation: it reads the registry from disk and
    renders it into the system prompt. Decompression of init.json's addons:
    field happens in the Agent initializer via decompress_addons() before
    setup is called.
    """
    _reconcile(agent)

    def handle_mcp(args: dict) -> dict:
        action = args.get("action", "")
        if action == "show":
            return _reconcile(agent)
        return {
            "status": "error",
            "message": f"unknown action: {action!r}, only 'show' is supported",
        }

    agent.add_tool(
        "mcp",
        schema=get_schema(),
        handler=handle_mcp,
        description=get_description(),
    )
