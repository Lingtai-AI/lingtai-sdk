"""Shared Markdown-catalog scanning for the ``knowledge`` and ``skills`` caps.

Both capabilities scan a directory tree for per-folder catalog files
(``KNOWLEDGE.md`` / ``SKILL.md``), parse their YAML frontmatter for
``name`` + ``description``, and render a compact YAML catalog injected into the
system prompt. The scan/parse/render logic was duplicated byte-for-byte; this
module is the single source of truth.

Everything here is model-visible: the catalog YAML goes straight into the
prompt and the problem strings surface in the tool's ``info`` health snapshot.
Behavior is preserved exactly — only the duplication is removed.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse leading ``---`` YAML frontmatter into a flat str→str dict.

    Non-dict or invalid YAML yields ``{}``. Keys are coerced to ``str``;
    values are whitespace-normalized strings (multi-line ``>``/``|`` scalars
    collapse to clean single-line strings); ``None`` becomes ``""``.
    """
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
# Catalog-file parser + directory scanner
# ---------------------------------------------------------------------------

def parse_markdown_catalog_file(
    path: Path,
    label: str,
    *,
    filename: str,
) -> tuple[dict | None, dict | None]:
    """Parse one catalog file into ``(entry, problem)`` (exactly one non-None).

    ``filename`` (e.g. ``"SKILL.md"``) is woven into the problem messages so
    the wording matches each capability's existing health reports.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, {"folder": label, "reason": f"cannot read {filename}: {e}"}

    fm = parse_frontmatter(text)
    name = fm.get("name", "")
    description = fm.get("description", "")
    if not name:
        return None, {"folder": label, "reason": f"{filename} missing required frontmatter field: name"}
    if not description:
        return None, {"folder": label, "reason": f"{filename} missing required frontmatter field: description"}

    return {
        "name": name,
        "description": description,
        "version": fm.get("version", ""),
        "path": str(path),
    }, None


def _scan_recursive(
    directory: Path,
    valid: list[dict],
    problems: list[dict],
    *,
    filename: str,
    kind: str,
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
        catalog_file = child / filename

        if catalog_file.is_file():
            entry, prob = parse_markdown_catalog_file(catalog_file, label, filename=filename)
            if entry:
                valid.append(entry)
            if prob:
                problems.append(prob)
            continue

        # No catalog file — classify.
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
                "reason": f"not a {kind} (no {filename}) and has loose files — corrupted",
            })
            continue

        _scan_recursive(
            child, valid, problems,
            filename=filename, kind=kind, prefix=f"{label}/",
        )


def scan_markdown_catalog(
    directory: Path,
    *,
    filename: str,
    kind: str,
) -> tuple[list[dict], list[dict]]:
    """Scan ``directory`` for ``<name>/<filename>`` catalog entries.

    Returns ``(valid, problems)``. Traversal: sorted ``iterdir()``, skip
    non-directories and dot-prefixed directories, recurse into subtrees that
    have only subdirectories, and flag directories with loose files but no
    catalog file as corrupted. ``kind`` (e.g. ``"skill"``) names the entry
    type in the corruption message.
    """
    valid: list[dict] = []
    problems: list[dict] = []
    _scan_recursive(directory, valid, problems, filename=filename, kind=kind)
    return valid, problems


# ---------------------------------------------------------------------------
# YAML catalog builder
# ---------------------------------------------------------------------------

def build_catalog_yaml(entries: list[dict], preamble: str) -> str:
    """Render a catalog of ``{name, path, description}`` entries as prompt YAML.

    Empty input yields ``""``. Each entry becomes a ``- name:``/``location:``
    pair plus a ``description: |`` block scalar; blank description lines render
    as four spaces. No trailing newline.
    """
    if not entries:
        return ""

    lines: list[str] = [
        preamble,
        "",
    ]
    for e in entries:
        lines.append(f"- name: {e['name']}")
        lines.append(f"  location: {e['path']}")
        lines.append("  description: |")
        for dl in e["description"].splitlines():
            lines.append(f"    {dl}" if dl else "    ")
    return "\n".join(lines)
