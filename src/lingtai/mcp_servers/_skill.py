"""Shared bundled-skill (SKILL.md) helpers for curated MCP servers.

Each curated MCP ships a standard skill file (``SKILL.md``: YAML frontmatter +
markdown body) in its package folder. The ``manual`` tool action reads the full
body on demand (progressive disclosure), while the frontmatter ``name`` +
``description`` are injected into the tool schema's ``manual`` action line as a
catalog entry. This module factors out the tiny frontmatter parser, the loader,
the schema catalog line, and the ``action='manual'`` payload so every MCP can
share one understandable implementation.
"""

from __future__ import annotations

from importlib import resources

# The frontmatter parser is kernel-owned (the kernel must not import from the
# wrapper, but the wrapper may import from the kernel). Re-exported here so the
# curated MCPs keep their historical ``from ._skill import split_frontmatter``
# call site while sharing one implementation with the prompt-section catalog.
from lingtai_kernel._frontmatter import split_frontmatter

SKILL_FILENAME = "SKILL.md"

__all__ = [
    "SKILL_FILENAME",
    "split_frontmatter",
    "load_skill",
    "manual_action_description",
    "manual_payload",
]


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
