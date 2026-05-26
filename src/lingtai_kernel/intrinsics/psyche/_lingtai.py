"""Lingtai (identity/character) management — update and load."""
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
    """Combine system/covenant.md + system/lingtai.md and write to covenant prompt section."""
    system_dir = agent._working_dir / "system"
    covenant_path = system_dir / "covenant.md"
    lingtai_path = system_dir / "lingtai.md"

    covenant = covenant_path.read_text(encoding="utf-8") if covenant_path.is_file() else ""
    character = lingtai_path.read_text(encoding="utf-8") if lingtai_path.is_file() else ""

    parts = [p for p in [covenant, character] if p.strip()]
    combined = "\n\n".join(parts)

    if combined.strip():
        agent._prompt_manager.write_section(
            "covenant", combined, protected=True,
        )
    else:
        agent._prompt_manager.delete_section("covenant")
    agent._token_decomp_dirty = True
    agent._flush_system_prompt()

    agent._log("psyche_lingtai_load", size_bytes=len(combined.encode("utf-8")))

    return {
        "status": "ok",
        "size_bytes": len(combined.encode("utf-8")),
        "content_preview": combined[:200],
    }
