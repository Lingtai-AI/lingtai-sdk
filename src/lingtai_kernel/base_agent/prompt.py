"""System prompt building, flushing, and updating.

Builds the system prompt from prompt-manager sections + tool inventory,
persists to system/system.md, and updates the live LLM session.
"""
from __future__ import annotations


def _refresh_meta_guidance_section(agent) -> None:
    """Rebuild the resident ``meta_guidance`` system-prompt section.

    Renders the static, rule-like runtime guidance (kernel guidance sections,
    the ``_meta`` envelope readme, and the active adapter's static runtime
    rules) into the final system-prompt section.  This is the resident home for
    content that used to ride on every tail ``_meta``; the tail now carries only
    a lightweight ref back to this section (see ``meta_block.build_meta_guidance``
    / ``build_meta_guidance_ref``).  Protected so the LLM cannot overwrite it.
    """
    from ..meta_block import build_meta_guidance

    body = build_meta_guidance(agent)
    if body:
        agent._prompt_manager.write_section("meta_guidance", body, protected=True)


def _build_system_prompt(agent) -> str:
    """Build the system prompt from language principle + sections + tool inventory."""
    from .tools import _refresh_tool_inventory_section
    from ..prompt import build_system_prompt

    _refresh_tool_inventory_section(agent)
    _refresh_meta_guidance_section(agent)
    return build_system_prompt(
        prompt_manager=agent._prompt_manager,
        language=agent._config.language,
        activeness=agent._config.activeness,
    )


def _build_system_prompt_batches(agent) -> list[str]:
    """Build the system prompt as mutation-frequency batches.

    Returns the same content as _build_system_prompt but as a list of
    segments so adapters that support per-block caching can place
    cache breakpoints at batch boundaries.
    """
    from .tools import _refresh_tool_inventory_section
    from ..prompt import build_system_prompt_batches

    _refresh_tool_inventory_section(agent)
    _refresh_meta_guidance_section(agent)
    return build_system_prompt_batches(
        prompt_manager=agent._prompt_manager,
        language=agent._config.language,
        activeness=agent._config.activeness,
    )


def _flush_system_prompt(agent) -> None:
    """Rebuild system prompt, persist to system/system.md, update live session."""
    prompt = _build_system_prompt(agent)
    system_md = agent._working_dir / "system" / "system.md"
    system_md.parent.mkdir(exist_ok=True)
    system_md.write_text(prompt)
    if agent._chat is not None:
        agent._chat.update_system_prompt(prompt)


def _update_system_prompt(agent, section: str, content: str, *, protected: bool = False) -> None:
    """Update a named section of the system prompt.

    Args:
        agent: The agent instance.
        section: Section name.
        content: Section content.
        protected: If True, the LLM cannot overwrite this section.
    """
    agent._prompt_manager.write_section(section, content, protected=protected)
    agent._token_decomp_dirty = True
    _flush_system_prompt(agent)
