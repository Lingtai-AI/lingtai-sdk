"""Lingtai (identity/character) management — update and load.

`_lingtai_load` is the single canonical writer of the `character` prompt
section, composed from `system/lingtai.md` alone. It does not touch the
`covenant` section (operator contract) — that is owned solely by
`Agent._reload_prompt_sections`.
"""
from __future__ import annotations


def _lingtai_update(agent, args: dict) -> dict:
    """Write content to system/lingtai.md and auto-load into system prompt."""
    content = args.get("content", "")
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    lingtai_path = system_dir / "lingtai.md"
    lingtai_path.write_text(content)

    agent._log("psyche_lingtai_update", length=len(content))

    _lingtai_load(agent, {})
    return {"status": "ok", "path": str(lingtai_path)}


def _lingtai_load(agent, _args: dict) -> dict:
    """Load system/lingtai.md into the protected `character` prompt section.

    This is the single canonical writer of `character` — the agent's
    self-authored identity (灵台). It is deliberately distinct from the
    operator-supplied `covenant` section (covenant.md, written by
    `Agent._reload_prompt_sections`) and from the mechanical `identity`
    section (name/nickname/manifest, written by BaseAgent). An empty or
    missing lingtai.md deletes the section.
    """
    system_dir = agent._working_dir / "system"
    lingtai_path = system_dir / "lingtai.md"

    character = lingtai_path.read_text(encoding="utf-8") if lingtai_path.is_file() else ""

    if character.strip():
        agent._prompt_manager.write_section(
            "character", character, protected=True,
        )
    else:
        agent._prompt_manager.delete_section("character")
    agent._token_decomp_dirty = True
    agent._flush_system_prompt()

    agent._log("psyche_lingtai_load", size_bytes=len(character.encode("utf-8")))

    return {
        "status": "ok",
        "size_bytes": len(character.encode("utf-8")),
        "content_preview": character[:200],
    }
