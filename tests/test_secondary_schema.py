"""Tests for secondary nested tool-call schema injection."""
from __future__ import annotations

from unittest.mock import MagicMock

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.intrinsics import ALL_INTRINSICS


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _schema_by_name(agent: BaseAgent) -> dict[str, dict]:
    return {schema.name: schema.parameters for schema in agent._build_tool_schemas()}


def test_secondary_schema_injected_into_eligible_dynamic_tool(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.add_tool(
        "long_work",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda args: {"status": "ok"},
        description="long work",
    )

    schemas = _schema_by_name(agent)

    secondary = schemas["long_work"]["properties"]["secondary"]
    assert secondary["properties"]["tool"]["enum"] == ["email", "feishu", "telegram", "wechat", "whatsapp"]
    assert "primary call may take >5s" in secondary["description"]
    assert "before a long bash/daemon/web_search call" in secondary["description"]
    assert "Do not use for routine short calls" in secondary["description"]
    assert "_secondary.result" in secondary["description"]
    # read-only boundary: the secondary channel must not advertise send/reply.
    assert "send" not in secondary["description"]
    assert "reply" not in secondary["description"]
    assert secondary["properties"]["args"]["required"] == ["action"]
    assert secondary["properties"]["args"]["properties"]["action"]["enum"] == ["read"]
    assert "chat_id" in secondary["properties"]["args"]["properties"]
    assert "limit" in secondary["properties"]["args"]["properties"]
    assert "telegram.read needs chat_id" in secondary["properties"]["args"]["description"]
    assert "send" not in secondary["properties"]["args"]["description"]
    assert "reply" not in secondary["properties"]["args"]["description"]
    assert "reasoning" in schemas["long_work"]["properties"]
    agent.stop(timeout=1.0)


def test_secondary_schema_not_injected_into_communication_tools_and_lifecycle_intrinsics(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    for name in ["telegram", "wechat", "feishu", "whatsapp", "imap"]:
        agent.add_tool(
            name,
            schema={"type": "object", "properties": {"action": {"type": "string"}}},
            handler=lambda args: {"status": "ok"},
            description=name,
        )

    schemas = _schema_by_name(agent)

    for name in ["telegram", "wechat", "feishu", "whatsapp", "imap", "system", "psyche", "soul", "email"]:
        assert name in schemas
        assert "secondary" not in schemas[name].get("properties", {})
    agent.stop(timeout=1.0)


def test_secondary_schema_injected_into_eligible_intrinsic(tmp_path, monkeypatch):
    class FakeIntrinsicModule:
        @staticmethod
        def get_schema(lang):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        @staticmethod
        def get_description(lang):
            return "fake intrinsic"

    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent._intrinsics = [*agent._intrinsics, "fake_intrinsic"]
    monkeypatch.setitem(ALL_INTRINSICS, "fake_intrinsic", {"module": FakeIntrinsicModule})

    schemas = _schema_by_name(agent)

    assert "fake_intrinsic" in schemas
    assert "secondary" in schemas["fake_intrinsic"]["properties"]
    assert "reasoning" in schemas["fake_intrinsic"]["properties"]
    agent.stop(timeout=1.0)
