"""Regression tests for app-level Agent prompt meta_guidance refresh."""
from __future__ import annotations

from types import SimpleNamespace

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service


STATIC_CODEX_COMMENT = {
    "adapter": "codex",
    "feature": "responses_rest_epoch_reset",
    "summary": "Codex plans turns as full or incremental.",
    "summarize_note": "wait until >=20 API calls before non-urgent summarize.",
    "context_budget_note": (
        "Can wait until 150k token context to proactively "
        "summarize; if summarize cannot bring context under "
        "150k, consider molt."
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
    assert ">=20 API calls" in prompt
    assert ">=20 API calls" in prompt
    assert "Can wait until 150k token context" in prompt
    assert "if summarize cannot bring context under 150k, consider molt" in prompt


def test_agent_batched_prompt_builder_refreshes_meta_guidance_adapter_rules(tmp_path):
    agent = _agent_with_static_comment(tmp_path)

    prompt = "\n".join(agent._build_system_prompt_batches())

    assert "## meta_guidance" in prompt
    assert "### codex runtime rules" in prompt
    assert "responses_rest_epoch_reset" in prompt
    assert "Can wait until 150k token context" in prompt
    assert "if summarize cannot bring context under 150k, consider molt" in prompt
