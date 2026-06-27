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
        "may be delayed until context reaches 0.75 of the window. Below "
        "the threshold, keep working normally. At or above the threshold, the "
        "runtime automatically reconstructs context on the next request with "
        "the compacted history — no manual action is needed. Refresh is an "
        "emergency reconstruction path for broken/stale context, not a routine "
        "knob for the normal summarize flow. If you are already planning to "
        "molt, do not summarize first unless context overflow is imminent; "
        "molt is the higher-level replacement for summarize."
    ),
    "long_context_strategy": (
        "When local context reaches 0.75 of the context window, "
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
