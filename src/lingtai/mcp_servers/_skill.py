"""Shared bundled-skill (SKILL.md) helpers for curated MCP servers.

Each curated MCP ships a standard skill file (``SKILL.md``: YAML frontmatter +
markdown body) in its package folder. The ``manual`` tool action reads the full
body on demand (progressive disclosure), while the frontmatter ``name`` +
``description`` are injected into the tool schema's ``manual`` action line as a
catalog entry. This module factors out the tiny frontmatter parser, the loader,
the schema catalog line, and the ``action='manual'`` payload so every MCP can
share one understandable implementation instead of copying Telegram's.

The original Telegram MCP keeps its own inline copy of this logic for historical
reasons; new/curated MCPs use this shared helper.
"""

from __future__ import annotations

from importlib import resources

SKILL_FILENAME = "SKILL.md"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body markdown).

    Tiny dependency-free parser for the leading ``---`` YAML block. Handles the
    flat ``key: value`` and ``key: |``/``key: >`` block-scalar forms used by our
    skill frontmatter; it does not attempt to be a general YAML parser. Returns
    an empty mapping and the original text when no frontmatter is present.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening '---'; find the closing fence.
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n").strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fm_lines = [ln.rstrip("\n") for ln in lines[1:end]]
    body = "".join(lines[end + 1:]).lstrip("\n")

    meta: dict[str, str] = {}
    key: str | None = None
    block_parts: list[str] = []

    def _flush() -> None:
        if key is not None:
            meta[key] = " ".join(" ".join(block_parts).split())

    for raw in fm_lines:
        if key is not None and (raw.startswith((" ", "\t")) or not raw.strip()):
            # Continuation line of a block scalar (key: | / key: >).
            block_parts.append(raw.strip())
            continue
        if ":" in raw and not raw.startswith((" ", "\t")):
            _flush()
            k, _, v = raw.partition(":")
            key = k.strip()
            block_parts = []
            v = v.strip()
            if v in ("|", ">", "|-", ">-", "|+", ">+"):
                # Block scalar — value continues on indented lines below.
                continue
            block_parts.append(v.strip("'\""))
    _flush()
    return meta, body


def load_skill(package: str) -> tuple[dict[str, str], str, str]:
    """Load a package's bundled SKILL.md → (frontmatter, body, path)."""
    resource = resources.files(package).joinpath(SKILL_FILENAME)
    text = resource.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    return frontmatter, body, str(resource)


def manual_action_description(frontmatter: dict[str, str], default_name: str) -> str:
    """Build the schema 'manual' action line from the skill frontmatter.

    Injects the skill name + description (the frontmatter/catalog entry) so the
    tool schema advertises the skill while the full body stays
    progressive-disclosure behind ``action='manual'``.
    """
    name = frontmatter.get("name", default_name)
    description = frontmatter.get("description", "")
    return (
        f"manual: progressive-disclosure usage manual (skill '{name}') — "
        "call this (no other args) to pull the full bundled SKILL.md. "
        f"{description}"
    ).strip()


def manual_payload(
    frontmatter: dict[str, str], body: str, path: str, default_name: str
) -> dict:
    """Build the ``action='manual'`` response payload.

    Returns the full skill markdown plus parsed metadata and the resolved
    SKILL.md path, mirroring the Telegram MCP's ``_manual`` result shape.

    Asset/reference discovery deliberately stays in the SKILL.md text. Do not
    add concrete asset/reference lists to this tool payload: the stable minimal
    contract is the main manual body + the absolute SKILL.md path, and callers
    can follow relative paths documented by the skill when they need bundled
    side files. This keeps MCP schemas small and makes SKILL.md the single
    source of truth for sidecar organization.
    """
    return {
        "status": "ok",
        "action": "manual",
        "skill": frontmatter.get("name", default_name),
        "metadata": dict(frontmatter),
        "path": path,
        "manual": body,
    }
