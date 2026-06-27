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
        "Summarize is an investment, not routine cleanup: keep the "
        "full:incremental ratio at or below 1:10 and defer non-urgent "
        "summarize until the expected savings justify the cache miss. "
        "If you are already planning to molt, do not summarize first unless "
        "context overflow is imminent; molt is the higher-level replacement "
        "for summarize."
    ),
    "context_budget_note": (
        "Can wait until roughly 200k token context before proactive "
        "summarize, but if summarizing still leaves the main context "
        "above roughly 150k tokens, consider molt to avoid repeated "
        "summarize misses and improve token efficiency."
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
    assert "investment, not routine cleanup" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    assert "molt is the higher-level replacement for summarize" in prompt
    assert "1:10" in prompt
    assert "roughly 200k token context" in prompt
    assert "above roughly 150k tokens, consider molt" in prompt


def test_agent_batched_prompt_builder_refreshes_meta_guidance_adapter_rules(tmp_path):
    agent = _agent_with_static_comment(tmp_path)

    prompt = "\n".join(agent._build_system_prompt_batches())

    assert "## meta_guidance" in prompt
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "do not summarize first unless context overflow is imminent" in prompt
    assert "roughly 200k token context" in prompt
    assert "above roughly 150k tokens, consider molt" in prompt
