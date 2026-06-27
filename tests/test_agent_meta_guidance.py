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
        "Summarize normally when useful. For Codex continuation over the "
        "Responses API, summarize calls are accepted and recorded immediately, "
        "but their fresh full replay/cache epoch effect is delayed until local "
        "context reaches roughly 80% of the context window. The delay exists "
        "because Codex keeps a previous_response_id/cache epoch; resetting "
        "that epoch for every summarize would discard continuation/cache "
        "benefit. If you are already planning to molt, do not summarize first "
        "unless context overflow is imminent; molt is the higher-level "
        "replacement for summarize."
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
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "Summarize normally when useful" in prompt
    assert "Responses API" in prompt
    assert "fresh full replay/cache epoch effect is delayed" in prompt
    assert "previous_response_id/cache epoch" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    assert "molt is the higher-level replacement for summarize" in prompt
    codex_note = agent.service.static_adapter_comment()["summarize_note"]
    assert "1:10" not in codex_note
    assert "roughly 200k token context" not in codex_note
    assert "above roughly 150k tokens" not in codex_note


def test_agent_batched_prompt_builder_refreshes_meta_guidance_adapter_rules(tmp_path):
    agent = _agent_with_static_comment(tmp_path)

    prompt = "\n".join(agent._build_system_prompt_batches())

    assert "## meta_guidance" in prompt
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "Responses API" in prompt
    assert "fresh full replay/cache epoch effect is delayed" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    codex_note = agent.service.static_adapter_comment()["summarize_note"]
    assert "roughly 200k token context" not in codex_note
    assert "above roughly 150k tokens" not in codex_note
