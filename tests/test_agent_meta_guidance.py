"""Regression tests for app-level Agent prompt meta_guidance refresh."""
from __future__ import annotations

from types import SimpleNamespace

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service


STATIC_CODEX_COMMENT = {
    "adapter": "codex",
    "feature": "responses_rest_epoch_reset",
    "summary": "Codex plans turns as full or incremental.",
    "summarize_note": (
        "Summarize normally when useful. Codex Responses sessions may keep "
        "request-side continuation/cache epochs, but summarize/reconstruction "
        "timing is the generic runtime behavior documented in "
        "substrate/procedures, not a Codex-only policy. Summary content is "
        "recorded in runtime history now; provider-side context reconstruction "
        "may be delayed until context reaches 0.95 of the window. Below "
        "the threshold, keep working normally. At or above the threshold, the "
        "runtime automatically reconstructs context on the next request with "
        "the compacted history — no manual action is needed. Refresh is an "
        "emergency reconstruction path for broken/stale context, not a routine "
        "knob for the normal summarize flow. If you are already planning to "
        "molt, do not summarize first unless context overflow is imminent; "
        "molt is the higher-level replacement for summarize."
    ),
    "long_context_strategy": (
        "When local context reaches 0.95 of the context window, "
        "summarize/batch the noisy history; if that summarize pass cannot "
        "bring local context back below that threshold, molt instead of "
        "repeatedly paying fresh full replays."
    ),
}


def _agent_with_static_comment(tmp_path):
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "agent",
        capabilities=[],
    )
    agent.service.static_adapter_comment = lambda: STATIC_CODEX_COMMENT
    return agent


def test_agent_prompt_builder_refreshes_meta_guidance_adapter_rules(tmp_path):
    agent = _agent_with_static_comment(tmp_path)

    prompt = agent._build_system_prompt()

    assert "## meta_guidance" in prompt
    assert "Delayed summarization reconstruction threshold" in prompt
    assert "Do not call `refresh` just to apply a summarize" in prompt
    assert "does not mean the active provider-side context" in prompt
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "Summarize normally when useful" in prompt
    assert "Codex Responses sessions may keep" in prompt
    assert "generic runtime behavior documented in" in prompt
    assert "substrate/procedures" in prompt
    assert "not a Codex-only policy" in prompt
    assert "provider-side context reconstruction" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    assert "molt is the higher-level replacement for summarize" in prompt
    assert "Below the threshold, keep working normally" in prompt
    assert "the runtime automatically reconstructs context on the next" in prompt
    assert "no manual action is needed" in prompt
    assert "Refresh is an emergency reconstruction path" in prompt
    assert "if that summarize pass cannot bring local context back below that threshold" in prompt
    assert "molt instead of repeatedly paying fresh full replays" in prompt
    codex_note = agent.service.static_adapter_comment()["summarize_note"]
    assert "1:10" not in codex_note
    assert "roughly 200k token context" not in codex_note
    assert "above roughly 150k tokens" not in codex_note
    assert "previous_response_id/cache epoch" not in codex_note
    assert "fresh full replay/cache epoch effect" not in codex_note



def test_agent_loads_kernel_owned_principle_prompt(tmp_path):
    from lingtai_kernel._frontmatter import split_frontmatter

    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({"principle": "operator supplied principle override"})

    prompt = agent._build_system_prompt()
    principle_file = agent._working_dir / "system" / "principle.md"
    mirrored = principle_file.read_text(encoding="utf-8")

    # The disk mirror is a skill-style artifact: developer-facing YAML
    # frontmatter + the Markdown body. The frontmatter stays on disk (self-
    # explanatory section source) but is stripped before the body is rendered
    # into the LLM prompt / system.md.
    meta, body = split_frontmatter(mirrored)
    assert mirrored.startswith("---"), "mirror should keep its frontmatter"
    assert meta.get("name") == "principle"
    assert "Progressive disclosure principle: each resident prompt layer has one job" in body
    assert "`meta_guidance` is immediate runtime guidance" in body
    assert "`procedures` is how to act" in body
    assert "`substrate` is the working model" in body
    assert "Reference manuals are why" in body
    assert "operator supplied principle override" not in mirrored
    assert "operator supplied principle override" not in prompt
    assert "dynamic kernel " + "preface" not in mirrored
    assert "Agent " + "language:" not in prompt
    assert "Agent " + "activeness:" not in prompt
    assert "Token efficiency principle:" in body
    # The rendered prompt carries the body only — no frontmatter markers.
    assert body in prompt
    assert "---\nname: principle" not in prompt
    assert "kind: prompt-section" not in prompt
    assert prompt.index("Progressive disclosure principle: each resident prompt layer") < prompt.index("## meta_guidance")


def test_init_base_prompt_renders_after_principle_before_covenant(tmp_path):
    """The init-prompt contract's `base_prompt` is the third-party injection
    point. After _reload_prompt_sections threads it into self._base_prompt, the
    builder renders it right after the raw kernel-owned `principle` section and
    before the rest of Batch 1 (here, `covenant`)."""
    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({
        "base_prompt": "Recipe-injected base prompt.",
        "covenant": "The operator contract.",
    })

    prompt = agent._build_system_prompt()
    batched = "\n".join(agent._build_system_prompt_batches())

    assert "Recipe-injected base prompt." in prompt
    assert "Recipe-injected base prompt." in batched
    # principle (raw, kernel-owned) → base_prompt → covenant.
    principle_pos = prompt.index("Progressive disclosure principle: each resident prompt layer")
    base_pos = prompt.index("Recipe-injected base prompt.")
    covenant_pos = prompt.index("The operator contract.")
    assert principle_pos < base_pos < covenant_pos
    # Mirrored to disk so it survives a from-scratch post-molt reload and is
    # inspectable by operators.
    mirror = agent._working_dir / "system" / "base_prompt.md"
    assert mirror.read_text(encoding="utf-8") == "Recipe-injected base prompt."


def test_init_base_prompt_survives_from_scratch_reload(tmp_path):
    """A no-arg reload (post-molt hook re-reads init.json from scratch) keeps the
    base_prompt via the system/base_prompt.md mirror even if the new read has no
    inline value."""
    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({"base_prompt": "Recipe-injected base prompt."})
    assert agent._base_prompt == "Recipe-injected base prompt."

    # Reload with empty data — disk mirror is the fallback.
    agent._reload_prompt_sections({})
    assert agent._base_prompt == "Recipe-injected base prompt."
    assert "Recipe-injected base prompt." in agent._build_system_prompt()


def test_init_substrate_override_is_not_honored(tmp_path):
    """`substrate` is kernel-owned: an init.json substrate value is ignored at the
    builder level; the packaged default renders instead."""
    from importlib.resources import files
    from lingtai_kernel._frontmatter import strip_frontmatter

    packaged = files("lingtai.prompts").joinpath("substrate.md").read_text(encoding="utf-8")

    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({"substrate": "OPERATOR-SUBSTRATE-OVERRIDE"})

    prompt = agent._build_system_prompt()
    assert "OPERATOR-SUBSTRATE-OVERRIDE" not in prompt
    # The packaged source carries developer-facing frontmatter; the rendered
    # section is the body only.
    assert agent._prompt_manager.read_section("substrate") == strip_frontmatter(packaged)


def test_section_mirrors_keep_frontmatter_but_prompt_is_body_only(tmp_path):
    """Jason's contract: `system/*.md` section mirrors may carry frontmatter, but
    the rendered LLM prompt and the final `system/system.md` must be body-only.
    """
    from lingtai_kernel._frontmatter import split_frontmatter

    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({})
    prompt = agent._build_system_prompt()

    system_dir = agent._working_dir / "system"
    for name in ("principle", "substrate", "procedures"):
        mirror = (system_dir / f"{name}.md").read_text(encoding="utf-8")
        meta, body = split_frontmatter(mirror)
        # Mirror keeps the developer-facing frontmatter on disk.
        assert mirror.startswith("---"), f"{name} mirror should keep frontmatter"
        assert meta.get("name") == name
        # The body (not the frontmatter) is what renders.
        assert body and body in prompt

    # The rendered prompt carries no frontmatter fence / metadata keys.
    assert "\n---\nname: " not in prompt
    assert not prompt.startswith("---\n")
    assert "kind: prompt-section" not in prompt

    # The final rendered system.md is body-only too.
    from lingtai_kernel.base_agent.prompt import _flush_system_prompt

    _flush_system_prompt(agent)
    system_md = (system_dir / "system.md").read_text(encoding="utf-8")
    assert not system_md.startswith("---")
    assert "kind: prompt-section" not in system_md
    assert "kind: meta-guidance" not in system_md


def test_base_agent_seeds_body_only_from_frontmatter_mirror(tmp_path):
    """T6 — a `system/*.md` mirror that carries frontmatter must seed a body-only
    section when read directly by the lower-level BaseAgent constructor path."""
    from lingtai_kernel.base_agent import BaseAgent
    from tests._service_helpers import make_gemini_mock_service as make_mock_service

    workdir = tmp_path / "ba"
    system_dir = workdir / "system"
    system_dir.mkdir(parents=True)
    # A frontmatter-bearing mirror, body-only section expected.
    (system_dir / "substrate.md").write_text(
        "---\nname: substrate\nkind: prompt-section\n---\nSUBSTRATE-BODY-ONLY\n",
        encoding="utf-8",
    )

    agent = BaseAgent(
        service=make_mock_service(),
        agent_name="ba-test",
        working_dir=workdir,
    )
    section = agent._prompt_manager.read_section("substrate")
    assert section == "SUBSTRATE-BODY-ONLY\n"
    assert "kind: prompt-section" not in (section or "")


def test_init_brief_override_is_not_honored(tmp_path):
    """`brief` is no longer an init.json prompt override; an inline value is
    ignored and the section comes only from system/brief.md on disk."""
    agent = _agent_with_static_comment(tmp_path)
    agent._reload_prompt_sections({"brief": "INIT-BRIEF-OVERRIDE"})

    prompt = agent._build_system_prompt()
    assert "INIT-BRIEF-OVERRIDE" not in prompt
    assert agent._prompt_manager.read_section("brief") is None

    # Disk-sourced brief still renders.
    brief_md = agent._working_dir / "system" / "brief.md"
    brief_md.write_text("DISK-BRIEF-CONTEXT", encoding="utf-8")
    agent._reload_prompt_sections({})
    assert "DISK-BRIEF-CONTEXT" in agent._build_system_prompt()


def test_agent_batched_prompt_builder_refreshes_meta_guidance_adapter_rules(tmp_path):
    agent = _agent_with_static_comment(tmp_path)

    prompt = "\n".join(agent._build_system_prompt_batches())

    assert "## meta_guidance" in prompt
    assert "Delayed summarization reconstruction threshold" in prompt
    assert "Do not call `refresh` just to apply a summarize" in prompt
    assert "does not mean the active provider-side context" in prompt
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "Codex Responses sessions may keep" in prompt
    assert "generic runtime behavior documented in" in prompt
    assert "provider-side context reconstruction" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    assert "if that summarize pass cannot bring local context back below that threshold" in prompt
    codex_note = agent.service.static_adapter_comment()["summarize_note"]
    assert "roughly 200k token context" not in codex_note
    assert "above roughly 150k tokens" not in codex_note
    assert "previous_response_id/cache epoch" not in codex_note
    assert "fresh full replay/cache epoch effect" not in codex_note
