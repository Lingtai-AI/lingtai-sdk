"""Tests for the Claude Agent SDK completion provider.

CI must not require a Claude CLI login or network access. The real
``claude_agent_sdk`` package is therefore *faked* in ``sys.modules`` for the
tests that exercise the call path. One test deliberately removes any module so
the missing-SDK error path is covered.

What's covered:
- Missing SDK raises a clear RuntimeError (not an ImportError at lingtai
  import time).
- Basic assistant text is gathered into LLMResponse.text.
- ResultMessage.usage is parsed into UsageMetadata (with cache normalization).
- The canonical ChatInterface accumulates the turn and the prompt is built
  role-labeled from the conversation.
- The provider is registered (both hyphen and underscore aliases).
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from lingtai.llm.claude_agent_sdk.adapter import (
    ClaudeAgentSDKAdapter,
    ClaudeAgentSDKChatSession,
    _build_prompt,
)
from lingtai_kernel.llm.interface import ChatInterface, TextBlock


# ---------------------------------------------------------------------------
# Fake claude_agent_sdk
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class _FakeResultMessage:
    def __init__(self, usage=None):
        self.usage = usage


class _FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_fake_sdk(scripted_messages, *, record=None):
    """Build a fake claude_agent_sdk module.

    ``scripted_messages`` is the list of message objects the fake ``query()``
    yields. ``record`` (if given) receives the (prompt, options) of each call.
    """
    mod = ModuleType("claude_agent_sdk")
    mod.TextBlock = _FakeTextBlock
    mod.AssistantMessage = _FakeAssistantMessage
    mod.ResultMessage = _FakeResultMessage
    mod.ClaudeAgentOptions = _FakeOptions

    async def _query(*, prompt, options):
        if record is not None:
            record.append((prompt, options))
        for msg in scripted_messages:
            yield msg

    mod.query = _query
    return mod


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a fake claude_agent_sdk; yield a setter for the scripted output."""
    state = {"record": []}

    def _install(messages):
        mod = _make_fake_sdk(messages, record=state["record"])
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
        return mod

    state["install"] = _install
    yield state


# ---------------------------------------------------------------------------
# Missing-SDK error path
# ---------------------------------------------------------------------------


def test_missing_sdk_raises_runtime_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    with pytest.raises(RuntimeError, match="claude-agent-sdk"):
        session.send("hello")
    # Missing optional SDK is a call-path failure; the staged user turn should
    # be reverted so retrying after installation does not duplicate it.
    assert session.interface.conversation_entries() == []


def test_missing_sdk_does_not_break_import():
    """Importing the adapter module must not require the SDK."""
    # If we got here, the module-level import already succeeded without the SDK.
    import importlib

    mod = importlib.import_module("lingtai.llm.claude_agent_sdk.adapter")
    assert hasattr(mod, "ClaudeAgentSDKAdapter")


# ---------------------------------------------------------------------------
# Basic response text
# ---------------------------------------------------------------------------


def test_basic_response_text(fake_sdk):
    fake_sdk["install"]([
        _FakeAssistantMessage([_FakeTextBlock("Hello "), _FakeTextBlock("world")]),
        _FakeResultMessage(),
    ])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    resp = session.send("hi")
    assert resp.text == "Hello world"
    assert resp.tool_calls == []


def test_assistant_text_appended_to_interface(fake_sdk):
    fake_sdk["install"]([
        _FakeAssistantMessage([_FakeTextBlock("answer")]),
    ])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    session.send("question")
    entries = session.interface.conversation_entries()
    # user(question) + assistant(answer)
    assert len(entries) == 2
    assert entries[0].role == "user"
    assert entries[1].role == "assistant"
    assert entries[1].content[0].text == "answer"
    assert entries[1].provider == "claude-agent-sdk"


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------


def test_usage_extraction(fake_sdk):
    fake_sdk["install"]([
        _FakeAssistantMessage([_FakeTextBlock("ok")]),
        _FakeResultMessage(usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
        }),
    ])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    resp = session.send("hi")
    # input normalized to raw + cache_read + cache_write
    assert resp.usage.input_tokens == 108
    assert resp.usage.output_tokens == 20
    assert resp.usage.cached_tokens == 5


def test_usage_absent_is_zero(fake_sdk):
    fake_sdk["install"]([
        _FakeAssistantMessage([_FakeTextBlock("ok")]),
    ])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    resp = session.send("hi")
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0


# ---------------------------------------------------------------------------
# Canonical history / transcript behavior
# ---------------------------------------------------------------------------


def test_prompt_is_role_labeled():
    iface = ChatInterface()
    iface.add_system("system text")
    iface.add_user_message("first")
    iface.add_assistant_message([TextBlock(text="reply")])
    iface.add_user_message("second")
    prompt = _build_prompt(iface)
    assert "User: first" in prompt
    assert "Assistant: reply" in prompt
    assert "User: second" in prompt
    # System text excluded from the transcript prompt
    assert "system text" not in prompt
    # Trailing nudge for the next assistant turn
    assert prompt.rstrip().endswith("Assistant:")


def test_options_disable_tools_single_turn(fake_sdk):
    record = fake_sdk["record"]
    fake_sdk["install"]([_FakeAssistantMessage([_FakeTextBlock("ok")])])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    session.send("hi")
    assert len(record) == 1
    _, options = record[0]
    assert options.kwargs["allowed_tools"] == []
    assert options.kwargs["max_turns"] == 1
    assert options.kwargs["setting_sources"] == []
    assert options.kwargs["model"] == "sonnet"
    assert options.kwargs["system_prompt"] == "be helpful"


def test_multi_turn_accumulates(fake_sdk):
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")

    fake_sdk["install"]([_FakeAssistantMessage([_FakeTextBlock("one")])])
    session.send("q1")
    fake_sdk["install"]([_FakeAssistantMessage([_FakeTextBlock("two")])])
    resp = session.send("q2")

    assert resp.text == "two"
    # The second prompt should contain the full prior transcript.
    last_prompt = fake_sdk["record"][-1][0]
    assert "User: q1" in last_prompt
    assert "Assistant: one" in last_prompt
    assert "User: q2" in last_prompt


def test_send_error_reverts_trailing_user(fake_sdk, monkeypatch):
    fake_sdk["install"]([_FakeAssistantMessage([_FakeTextBlock("ok")])])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")

    # Make the query path blow up after the user message is committed.
    import lingtai.llm.claude_agent_sdk.adapter as mod

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(mod, "_run_query", _boom)
    with pytest.raises(RuntimeError, match="network down"):
        session.send("doomed")
    # The trailing user entry must have been reverted.
    assert session.interface.conversation_entries() == []


def test_send_stream_delivers_final_chunk(fake_sdk):
    fake_sdk["install"]([_FakeAssistantMessage([_FakeTextBlock("streamed")])])
    adapter = ClaudeAgentSDKAdapter()
    session = adapter.create_chat("sonnet", "be helpful")
    chunks = []
    resp = session.send_stream("hi", on_chunk=chunks.append)
    assert resp.text == "streamed"
    assert chunks == ["streamed"]


# ---------------------------------------------------------------------------
# generate() one-shot
# ---------------------------------------------------------------------------


def test_generate_one_shot(fake_sdk):
    fake_sdk["install"]([
        _FakeAssistantMessage([_FakeTextBlock("oneshot")]),
        _FakeResultMessage(usage={"input_tokens": 10, "output_tokens": 4}),
    ])
    adapter = ClaudeAgentSDKAdapter()
    resp = adapter.generate("sonnet", "do a thing", system_prompt="sys")
    assert resp.text == "oneshot"
    assert resp.usage.input_tokens == 10
    prompt, options = fake_sdk["record"][0]
    assert prompt == "do a thing"
    assert options.kwargs["system_prompt"] == "sys"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_provider_registered():
    from lingtai.llm.service import LLMService
    from lingtai.llm._register import register_all_adapters

    saved = dict(LLMService._adapter_registry)
    LLMService._adapter_registry.clear()
    try:
        register_all_adapters()
        keys = set(LLMService._adapter_registry.keys())
        assert "claude-agent-sdk" in keys
        assert "claude_agent_sdk" in keys
    finally:
        LLMService._adapter_registry.clear()
        LLMService._adapter_registry.update(saved)


def test_make_tool_result_message():
    adapter = ClaudeAgentSDKAdapter()
    block = adapter.make_tool_result_message("read", {"ok": True}, tool_call_id="toolu_x")
    assert block.id == "toolu_x"
    assert block.name == "read"
    assert block.content == {"ok": True}


def test_is_quota_error():
    adapter = ClaudeAgentSDKAdapter()
    assert adapter.is_quota_error(RuntimeError("rate limit exceeded"))
    assert adapter.is_quota_error(RuntimeError("HTTP 429"))
    assert not adapter.is_quota_error(RuntimeError("boom"))
