"""Tests that the removed secondary channel is absent from tool schemas."""
from __future__ import annotations

from unittest.mock import MagicMock

from lingtai_kernel.base_agent import BaseAgent


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _schema_by_name(agent: BaseAgent) -> dict[str, dict]:
    return {schema.name: schema.parameters for schema in agent._build_tool_schemas()}


def test_secondary_schema_not_injected_into_dynamic_or_intrinsic_tools(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.add_tool(
        "long_work",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda args: {"status": "ok"},
        description="long work",
    )

    schemas = _schema_by_name(agent)

    for schema in schemas.values():
        props = schema.get("properties", {})
        assert "secondary" not in props
    assert "reasoning" in schemas["long_work"]["properties"]
    assert "reasoning" in schemas["email"]["properties"]
    agent.stop(timeout=1.0)
