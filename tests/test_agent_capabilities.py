"""Tests for Agent — capabilities layer."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from lingtai.agent import Agent
from lingtai.services.vision import VisionService
from lingtai.services.websearch import SearchService, SearchResult
from tests._service_helpers import make_gemini_mock_service as make_mock_service




class FakeVisionService(VisionService):
    def analyze_image(self, image_path, prompt=None):
        return "fake"


class FakeSearchService(SearchService):
    def search(self, query, max_results=5):
        return [SearchResult(title="t", url="u", snippet="s")]


def test_agent_no_capabilities_boots_core_floor(tmp_path):
    """Agent with no explicit capabilities still boots the `lingtai.core.*` floor.

    The default-on set covers knowledge/skills/bash/avatar/daemon/mcp + file caps.
    Opt-in capabilities (vision, web_search) stay off until requested.
    """
    from lingtai.capabilities import CORE_DEFAULTS
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    registered = {name for name, _ in agent._capabilities}
    assert registered == set(CORE_DEFAULTS), (
        f"expected exactly the core defaults, got {registered - set(CORE_DEFAULTS)} extra / "
        f"{set(CORE_DEFAULTS) - registered} missing"
    )
    manifest_registered = {
        name for name, _ in agent._build_manifest().get("capabilities", [])
    }
    for name in CORE_DEFAULTS:
        assert agent.has_capability(name) is True
        assert name in manifest_registered
    assert "vision" not in registered
    assert "web_search" not in registered
    agent.stop(timeout=1.0)


def test_agent_unsupported_vision_skip_is_not_registered(tmp_path):
    """A skipped capability must not appear registered or callable."""
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "test",
        capabilities={"vision": {"provider": "not-real"}},
    )
    try:
        manifest_registered = {
            name for name, _ in agent._build_manifest().get("capabilities", [])
        }
        assert agent.has_capability("vision") is False
        assert agent.get_capability("vision") is None
        assert "vision" not in agent._tool_handlers
        assert "vision" not in manifest_registered
    finally:
        agent.stop(timeout=1.0)


def test_agent_disable_strips_core_capability(tmp_path):
    """`disable=[...]` opt-out drops a default-on capability."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        disable=["bash", "avatar"],
    )
    registered = {name for name, _ in agent._capabilities}
    assert "bash" not in registered
    assert "avatar" not in registered
    assert "knowledge" in registered  # other defaults still on
    agent.stop(timeout=1.0)


def test_agent_capabilities_dict_kwarg_overrides_default(tmp_path):
    """Passing kwargs for a default-on capability merges over the defaults.

    `bash` defaults to {"yolo": True}; passing `policy_file` keeps yolo (since
    the override is a merge, not a replace) — hosts wanting strict sandbox
    should set `{"yolo": False, "policy_file": "..."}`.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities={"bash": {"yolo": False}},
    )
    bash_entry = [(n, k) for n, k in agent._capabilities if n == "bash"]
    assert bash_entry and bash_entry[0][1].get("yolo") is False
    agent.stop(timeout=1.0)


def test_agent_capabilities_list(tmp_path):
    """capabilities= as list of strings is honored alongside the core defaults."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["read", "write"],
    )
    registered = {name for name, _ in agent._capabilities}
    assert "read" in registered
    assert "write" in registered
    agent.stop(timeout=1.0)


def test_read_tool_description_points_to_file_manual(tmp_path):
    """The read tool should route hard file cases to the intrinsic file manual."""
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "test",
        capabilities=["read"],
    )
    try:
        read_schema = next(schema for schema in agent._tool_schemas if schema.name == "read")
        assert "file-manual" in read_schema.description
        assert "non-UTF-8" in read_schema.description
    finally:
        agent.stop(timeout=1.0)


def test_agent_capabilities_dict(tmp_path):
    """capabilities= as dict registers user-supplied capabilities with kwargs.

    Core defaults still register alongside; the assertion focuses on the
    opt-in tools getting their handler wired.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities={
            "vision": {"vision_service": FakeVisionService()},
            "web_search": {"search_service": FakeSearchService()},
        },
    )
    registered = {name for name, _ in agent._capabilities}
    assert "vision" in registered
    assert "web_search" in registered
    assert "vision" in agent._tool_handlers
    assert "web_search" in agent._tool_handlers
    agent.stop(timeout=1.0)


def test_agent_get_capability(tmp_path):
    """get_capability() returns the manager instance."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities={"vision": {"vision_service": FakeVisionService()}},
    )
    mgr = agent.get_capability("vision")
    assert mgr is not None
    assert agent.get_capability("nonexistent") is None
    agent.stop(timeout=1.0)


def test_agent_seal_after_start(tmp_path):
    """add_tool() raises after start() on Agent too."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities={"vision": {"vision_service": FakeVisionService()}},
    )
    agent.start()
    try:
        with pytest.raises(RuntimeError, match="Cannot modify tools after start"):
            agent.add_tool("foo", schema={"type": "object", "properties": {}}, handler=lambda a: {}, description="x")
    finally:
        agent.stop(timeout=2.0)


def test_vision_requires_provider(tmp_path):
    """Vision capability is skipped when no provider or service is given.

    setup() raises ValueError, but the agent catches it (capability_skipped)
    and simply doesn't register the tool.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["vision"],
    )
    assert agent.get_capability("vision") is None
    assert "vision" not in {s.name for s in agent._tool_schemas}
    agent.stop(timeout=1.0)


def test_web_search_defaults_to_duckduckgo(tmp_path):
    """Web search capability falls back to duckduckgo when no provider given."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["web_search"],
    )
    mgr = agent.get_capability("web_search")
    assert mgr is not None
    assert "web_search" in {s.name for s in agent._tool_schemas}
    agent.stop(timeout=1.0)
