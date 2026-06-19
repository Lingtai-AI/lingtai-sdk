"""Tests for BaseAgent — true name (immutable) and nickname (mutable), messages."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.message import Message, _make_message, MSG_REQUEST, MSG_USER_INPUT
from lingtai_kernel.state import AgentState
from lingtai_kernel.types import UnknownToolError


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


def test_agent_no_name(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    assert agent.agent_name is None
    assert agent.nickname is None
    assert agent.working_dir == tmp_path / "test"


def test_set_name_once(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    assert agent.agent_name == "悟空"


def test_set_name_twice_fails(tmp_path):
    """True name is immutable — cannot be set twice."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    with pytest.raises(RuntimeError, match="True name already set"):
        agent.set_name("八戒")
    assert agent.agent_name == "悟空"


def test_set_name_empty_fails(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    with pytest.raises(ValueError, match="cannot be empty"):
        agent.set_name("")


def test_agent_with_name_at_construction_is_immutable(tmp_path):
    """Name given at construction is a true name — immutable."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="alice")
    assert agent.agent_name == "alice"
    with pytest.raises(RuntimeError, match="True name already set"):
        agent.set_name("bob")


def test_nickname_mutable(tmp_path):
    """Nickname can be set and changed freely."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="悟空")
    assert agent.nickname is None
    agent.set_nickname("代码探索者")
    assert agent.nickname == "代码探索者"
    agent.set_nickname("bug猎手")
    assert agent.nickname == "bug猎手"


def test_nickname_clear(tmp_path):
    """Empty nickname clears it to None."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_nickname("explorer")
    assert agent.nickname == "explorer"
    agent.set_nickname("")
    assert agent.nickname is None


def test_set_name_updates_manifest(tmp_path):
    import json

    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["agent_name"] == "悟空"


def test_nickname_in_manifest(tmp_path):
    import json

    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="悟空")
    agent.set_nickname("代码探索者")
    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["agent_name"] == "悟空"
    assert manifest["nickname"] == "代码探索者"


# ------------------------------------------------------------------
# _perform_refresh — preserves chat history
# ------------------------------------------------------------------

def test_perform_refresh_saves_chat_history(tmp_path):
    """_perform_refresh saves chat history before self-restart."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")

    save_calls = []
    agent._save_chat_history = lambda: save_calls.append(True)

    with patch.object(agent, "_log"):
        agent._perform_refresh()

    # _save_chat_history was called
    assert len(save_calls) == 1


def test_perform_refresh_no_launch_cmd_is_noop(tmp_path):
    """_perform_refresh with no _build_launch_cmd returns None is a no-op."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    assert agent._build_launch_cmd() is None

    log_calls = []
    original_log = agent._log
    agent._log = lambda event, **kw: log_calls.append(event)

    agent._perform_refresh()

    assert "refresh_no_launch_cmd" in log_calls


# ------------------------------------------------------------------
# Message basics
# ------------------------------------------------------------------

def test_msg_constants():
    assert MSG_REQUEST == "request"
    assert MSG_USER_INPUT == "user_input"


def test_make_message():
    msg = _make_message(MSG_REQUEST, "user", "hello")
    assert msg.type == "request"
    assert msg.sender == "user"
    assert "hello" in msg.content
    assert msg.id.startswith("msg_")


# ------------------------------------------------------------------
# AgentState enum
# ------------------------------------------------------------------

def test_agent_state_values():
    assert AgentState.ACTIVE.value == "active"
    assert AgentState.IDLE.value == "idle"
    assert AgentState.STUCK.value == "stuck"
    assert AgentState.ASLEEP.value == "asleep"
    assert AgentState.SUSPENDED.value == "suspended"


# ------------------------------------------------------------------
# UnknownToolError
# ------------------------------------------------------------------

def test_unknown_tool_error():
    err = UnknownToolError("bad_tool")
    assert "bad_tool" in str(err)


def test_status_context_decomposition(tmp_path):
    """status() exposes a 'context' sub-block with system/tools/history/total
    token counts plus meta-line decomposition (fixed_tokens, growing_tokens)."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.start()
    try:
        st = agent.status()
        ctx = st["tokens"]["context"]
        for k in (
            "system_tokens",
            "tools_tokens",
            "history_tokens",
            "total_tokens",
            "window_size",
            "usage_pct",
            "fixed_tokens",
            "growing_tokens",
        ):
            assert k in ctx, f"missing key: {k}"
    finally:
        agent.stop()
