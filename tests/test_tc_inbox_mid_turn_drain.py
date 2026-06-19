"""Tests for the mid-turn tc_inbox drain hook (option c, see
discussions/tc-inbox-mid-turn-drain-patch.md).

The hook fires inside each adapter's send() after the message has been
committed to the canonical ChatInterface but before the API call. The
kernel installs ``_drain_tc_inbox_for_hook`` as that hook, so any
involuntary tool-call pair enqueued mid-task (mail notifications,
soul.flow voices, future producers) is spliced into the wire chat
within the next tool round rather than waiting for the outer turn to
finish.

These tests verify:

1. Hook attribute exists on ChatSession ABC and defaults to None.
2. Adapter send() calls the hook at the correct safe boundary (after
   commit, before API call).
3. The kernel's _install_drain_hook installs a callable that drains.
4. Mid-turn drain semantics: items enqueued between the entry drain
   and the next adapter send() get spliced.
5. Coalesce + replace_in_history semantics work mid-turn.
6. Double drain (entry + hook) doesn't double-splice items.
7. Hook fires unconditionally per the proposal §8.3 (text + tool sends).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.llm.base import ChatSession
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.tc_inbox import InvoluntaryToolCall, TCInbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mail_pair(notif_id: str, body: str = "test") -> InvoluntaryToolCall:
    """Build a non-coalescing notification pair (mail-shape)."""
    call_id = f"sn_{notif_id}"
    call = ToolCallBlock(
        id=call_id, name="system",
        args={"action": "notification", "notif_id": notif_id, "source": "email"},
    )
    result = ToolResultBlock(id=call_id, name="system", content=body)
    return InvoluntaryToolCall(
        call=call, result=result,
        source=f"system.notification:{notif_id}",
        enqueued_at=time.time(),
        coalesce=False,
        replace_in_history=False,
    )


def _soul_pair(fire_id: str, voice: str = "v") -> InvoluntaryToolCall:
    """Build a coalescing+replace_in_history soul.flow pair."""
    call = ToolCallBlock(id=fire_id, name="soul", args={"action": "flow"})
    result = ToolResultBlock(id=fire_id, name="soul", content={"voice": voice})
    return InvoluntaryToolCall(
        call=call, result=result,
        source="soul.flow",
        enqueued_at=time.time(),
        coalesce=True,
        replace_in_history=True,
    )


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "anthropic"
    svc.model = "claude-test"
    return svc


# ---------------------------------------------------------------------------
# 1. ABC contract
# ---------------------------------------------------------------------------


class TestPreRequestHookABC:
    """The ChatSession ABC declares pre_request_hook with a None default."""

    def test_default_is_none(self):
        # Subclass that satisfies the abstract methods with no-ops.
        class _DummySession(ChatSession):
            @property
            def interface(self):
                return ChatInterface()

            def send(self, message):
                from lingtai_kernel.llm.base import LLMResponse
                return LLMResponse()

        s = _DummySession()
        assert s.pre_request_hook is None

    def test_can_assign_callable(self):
        class _DummySession(ChatSession):
            @property
            def interface(self):
                return ChatInterface()

            def send(self, message):
                from lingtai_kernel.llm.base import LLMResponse
                return LLMResponse()

        s = _DummySession()
        called = []
        s.pre_request_hook = lambda iface: called.append(iface)
        s.pre_request_hook(s.interface)
        assert len(called) == 1


# ---------------------------------------------------------------------------
# 2. Anthropic adapter wiring (canonical-interface case)
# ---------------------------------------------------------------------------


class TestAnthropicAdapterHook:
    """The Anthropic adapter calls the hook at the right place: after
    commit_to_interface but before the API call."""

    def test_hook_fires_after_commit_before_api_call(self):
        """When pre_request_hook is set and send(tool_results) is called,
        the hook must observe the interface AFTER add_tool_results lands
        but BEFORE the API call would mutate state.
        """
        from lingtai.llm.anthropic.adapter import AnthropicChatSession

        # Mock client whose messages.create raises so we can inspect
        # the interface state at hook-fire time without needing a real
        # LLM. The hook fires before this raises.
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api blocked for test")

        iface = ChatInterface()
        # Seed: assistant emitted a tool_call, agent is about to reply
        # with the tool_result. This is mid-tool-loop state.
        call_id = "toolu_test"
        iface.add_user_message("start")
        iface.add_assistant_message(
            [ToolCallBlock(id=call_id, name="bash", args={"cmd": "ls"})],
        )

        session = AnthropicChatSession(
            client=client,
            model="claude-3-5-sonnet",
            system_prompt="",
            interface=iface,
            tools=None,
            tool_choice=None,
            extra_kwargs={},
        )

        observed = {}

        def hook(captured_iface):
            # At hook fire time: tool_results just committed; tail must
            # be user[tool_result] with no pending tool_calls.
            observed["tail_role"] = captured_iface.entries[-1].role
            observed["tail_blocks"] = [type(b).__name__ for b in captured_iface.entries[-1].content]
            observed["has_pending"] = captured_iface.has_pending_tool_calls()

        session.pre_request_hook = hook

        # Send tool_results
        with pytest.raises(RuntimeError, match="api blocked"):
            session.send([
                ToolResultBlock(id=call_id, name="bash", content="result"),
            ])

        assert observed["tail_role"] == "user"
        assert "ToolResultBlock" in observed["tail_blocks"]
        assert observed["has_pending"] is False, (
            "wire must be at safe boundary when hook fires"
        )

    def test_hook_fires_for_str_send(self):
        """Hook fires even on text-message sends, not just tool_results."""
        from lingtai.llm.anthropic.adapter import AnthropicChatSession

        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api blocked")

        session = AnthropicChatSession(
            client=client,
            model="claude-3-5-sonnet",
            system_prompt="",
            interface=ChatInterface(),
            tools=None,
            tool_choice=None,
            extra_kwargs={},
        )
        observed = []
        session.pre_request_hook = lambda iface: observed.append("fired")

        with pytest.raises(RuntimeError):
            session.send("hello")
        assert observed == ["fired"]

    def test_no_hook_means_no_call(self):
        """When pre_request_hook is None (default), nothing is called."""
        from lingtai.llm.anthropic.adapter import AnthropicChatSession

        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api blocked")

        session = AnthropicChatSession(
            client=client,
            model="claude-3-5-sonnet",
            system_prompt="",
            interface=ChatInterface(),
            tools=None,
            tool_choice=None,
            extra_kwargs={},
        )
        # Don't set pre_request_hook — it stays None.

        with pytest.raises(RuntimeError):
            session.send("hello")
        # If we got here without AttributeError or TypeError on the hook
        # path, the None branch worked.


# ---------------------------------------------------------------------------
# 3. _install_drain_hook on BaseAgent
# ---------------------------------------------------------------------------


class TestInstallDrainHook:
    """The kernel-side helper that wires _drain_tc_inbox into the active
    chat session's pre_request_hook attribute."""

    def test_install_on_canonical_session(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="hook-test",
            working_dir=tmp_path / "hook-test",
        )
        # Stub the chat session — anything with a pre_request_hook attribute
        # qualifies.
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = ChatInterface()
        agent._chat = stub_chat

        agent._install_drain_hook()
        assert callable(agent._chat.pre_request_hook)

    def test_no_install_when_chat_none(self, tmp_path):
        """Should be a no-op (no exception) when _chat is None."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="hook-test",
            working_dir=tmp_path / "hook-test",
        )
        agent._chat = None
        agent._install_drain_hook()  # must not raise
        assert agent._chat is None

    def test_no_install_when_session_lacks_attribute(self, tmp_path):
        """Sessions without pre_request_hook (legacy mocks etc.) are skipped."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="hook-test",
            working_dir=tmp_path / "hook-test",
        )
        legacy_chat = object()  # no pre_request_hook
        agent._chat = legacy_chat
        agent._install_drain_hook()  # must not raise
        assert not hasattr(legacy_chat, "pre_request_hook")

    def test_idempotent(self, tmp_path):
        """Calling twice on the same session re-installs cleanly."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="hook-test",
            working_dir=tmp_path / "hook-test",
        )
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = ChatInterface()
        agent._chat = stub_chat

        agent._install_drain_hook()
        first = agent._chat.pre_request_hook
        agent._install_drain_hook()
        second = agent._chat.pre_request_hook
        # Both are functions that drain — they may or may not be the same
        # object identity (lambda creates fresh each time), but both must
        # be callable.
        assert callable(first)
        assert callable(second)


# ---------------------------------------------------------------------------
# 4. Drain hook actually splices when invoked
# ---------------------------------------------------------------------------


class TestDrainHookSplices:
    """The hook callback drains the tc_inbox into the active interface."""

    def test_drain_splices_pending_pair(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="splice-test",
            working_dir=tmp_path / "splice-test",
        )
        iface = ChatInterface()
        iface.add_user_message("hello")
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = iface
        agent._chat = stub_chat

        # Enqueue a mail notification before the hook fires
        agent._tc_inbox.enqueue(_mail_pair("notif1", body="you've got mail"))
        assert len(agent._tc_inbox) == 1

        agent._install_drain_hook()
        # Simulate adapter calling the hook
        agent._chat.pre_request_hook(agent._chat.interface)

        # The pair should now be in the interface
        assert len(agent._tc_inbox) == 0
        # The pair adds an assistant entry (call) and a user entry (result)
        # on top of the seeded user message
        call_block_present = any(
            isinstance(b, ToolCallBlock) and b.name == "system"
            for entry in iface.entries for b in entry.content
        )
        result_block_present = any(
            isinstance(b, ToolResultBlock) and b.name == "system"
            for entry in iface.entries for b in entry.content
        )
        assert call_block_present
        assert result_block_present

    def test_drain_noop_on_empty_queue(self, tmp_path):
        """Hook is cheap when the queue is empty — no interface mutation."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="noop-test",
            working_dir=tmp_path / "noop-test",
        )
        iface = ChatInterface()
        iface.add_user_message("hello")
        before_len = len(iface.entries)
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = iface
        agent._chat = stub_chat

        agent._install_drain_hook()
        agent._chat.pre_request_hook(iface)
        assert len(iface.entries) == before_len


# ---------------------------------------------------------------------------
# 5. Replace-in-history mid-turn (soul.flow semantics)
# ---------------------------------------------------------------------------


class TestReplaceInHistoryMidTurn:
    """soul.flow uses replace_in_history=True to keep at most one
    consultation pair in wire history. The mid-turn drain must honor
    this — splicing a fresh soul.flow pair removes the prior one."""

    def test_replace_removes_prior_pair(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="replace-test",
            working_dir=tmp_path / "replace-test",
        )
        iface = ChatInterface()
        iface.add_user_message("hello")
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = iface
        agent._chat = stub_chat

        agent._install_drain_hook()

        # First soul.flow firing
        first = _soul_pair("fire_1", voice="first voice")
        agent._tc_inbox.enqueue(first)
        agent._chat.pre_request_hook(iface)
        first_call_id = first.call.id

        # Verify the first pair is in the interface and the appendix tracker
        first_call_present = any(
            isinstance(b, ToolCallBlock) and b.id == first_call_id
            for entry in iface.entries for b in entry.content
        )
        assert first_call_present
        assert agent._appendix_ids_by_source.get("soul.flow") == first_call_id

        # Second soul.flow firing (e.g. timer fired again on next round)
        second = _soul_pair("fire_2", voice="second voice")
        agent._tc_inbox.enqueue(second)
        agent._chat.pre_request_hook(iface)
        second_call_id = second.call.id

        # First pair must be GONE; second pair present
        first_call_still_present = any(
            isinstance(b, ToolCallBlock) and b.id == first_call_id
            for entry in iface.entries for b in entry.content
        )
        assert not first_call_still_present, (
            "replace_in_history=True must remove the prior pair from the wire"
        )
        second_call_present = any(
            isinstance(b, ToolCallBlock) and b.id == second_call_id
            for entry in iface.entries for b in entry.content
        )
        assert second_call_present
        assert agent._appendix_ids_by_source.get("soul.flow") == second_call_id


# ---------------------------------------------------------------------------
# 6. No double-splice (entry drain + hook)
# ---------------------------------------------------------------------------


class TestNoDoubleSplice:
    """If the entry drain at request entry already drained item A, and
    the hook later drains item B, A must appear exactly once and B must
    appear exactly once. The two drains share a queue; drain() atomically
    empties under lock."""

    def test_entry_drain_then_hook_drain(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="double-splice-test",
            working_dir=tmp_path / "double-splice-test",
        )
        iface = ChatInterface()
        iface.add_user_message("hello")
        stub_chat = MagicMock(spec_set=["pre_request_hook", "interface"])
        stub_chat.pre_request_hook = None
        stub_chat.interface = iface
        agent._chat = stub_chat

        # Phase 1: enqueue A, simulate entry drain
        a = _mail_pair("a", body="first mail")
        agent._tc_inbox.enqueue(a)
        agent._drain_tc_inbox()  # entry drain
        # A is now in the interface; queue empty
        assert len(agent._tc_inbox) == 0
        # Hook is installed by the drain
        assert callable(agent._chat.pre_request_hook)

        # Phase 2: enqueue B mid-turn, simulate hook firing
        b = _mail_pair("b", body="second mail")
        agent._tc_inbox.enqueue(b)
        agent._chat.pre_request_hook(iface)
        assert len(agent._tc_inbox) == 0

        # Count occurrences of each call_id
        a_count = sum(
            1
            for entry in iface.entries
            for block in entry.content
            if isinstance(block, ToolCallBlock) and block.id == a.call.id
        )
        b_count = sum(
            1
            for entry in iface.entries
            for block in entry.content
            if isinstance(block, ToolCallBlock) and block.id == b.call.id
        )
        assert a_count == 1, f"item A spliced {a_count} times, want exactly 1"
        assert b_count == 1, f"item B spliced {b_count} times, want exactly 1"
