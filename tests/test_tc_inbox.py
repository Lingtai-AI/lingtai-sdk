"""Tests for lingtai_kernel.tc_inbox — the involuntary tool-call inbox."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock
from lingtai_kernel.tc_inbox import InvoluntaryToolCall, TCInbox


def _make_item(source: str, voice: str = "v", coalesce: bool = False) -> InvoluntaryToolCall:
    tc_id = f"tc_{int(time.time())}_{source}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})
    result = ToolResultBlock(id=tc_id, name="soul", content={"voice": voice})
    return InvoluntaryToolCall(
        call=call, result=result,
        source=source, enqueued_at=time.time(),
        coalesce=coalesce,
    )


class TestTCInbox:

    def test_enqueue_appends(self):
        inbox = TCInbox()
        inbox.enqueue(_make_item("a"))
        inbox.enqueue(_make_item("b"))
        assert len(inbox) == 2

    def test_enqueue_coalesce_replaces_existing_with_same_source(self):
        inbox = TCInbox()
        inbox.enqueue(_make_item("soul.flow", voice="first", coalesce=True))
        inbox.enqueue(_make_item("soul.flow", voice="second", coalesce=True))
        assert len(inbox) == 1
        items = inbox.drain()
        assert items[0].result.content["voice"] == "second"

    def test_enqueue_no_coalesce_appends_even_with_same_source(self):
        inbox = TCInbox()
        inbox.enqueue(_make_item("x", voice="a", coalesce=False))
        inbox.enqueue(_make_item("x", voice="b", coalesce=False))
        assert len(inbox) == 2

    def test_enqueue_coalesce_only_replaces_same_source(self):
        inbox = TCInbox()
        inbox.enqueue(_make_item("a", coalesce=True))
        inbox.enqueue(_make_item("b", coalesce=True))
        inbox.enqueue(_make_item("a", voice="new", coalesce=True))
        assert len(inbox) == 2
        items = inbox.drain()
        # FIFO — 'a' was first; coalesced 'a' replaces in place; 'b' second.
        assert items[0].source == "a"
        assert items[0].result.content["voice"] == "new"
        assert items[1].source == "b"

    def test_drain_returns_fifo_and_clears(self):
        inbox = TCInbox()
        inbox.enqueue(_make_item("a"))
        inbox.enqueue(_make_item("b"))
        inbox.enqueue(_make_item("c"))
        items = inbox.drain()
        assert [i.source for i in items] == ["a", "b", "c"]
        assert len(inbox) == 0

    def test_drain_empty_returns_empty_list(self):
        inbox = TCInbox()
        assert inbox.drain() == []

    def test_concurrent_enqueue_thread_safe(self):
        inbox = TCInbox()
        N = 200

        def producer(i: int):
            inbox.enqueue(_make_item(f"src_{i}"))

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(inbox) == N
        items = inbox.drain()
        assert len(items) == N
        # All sources unique — no losses
        assert len({i.source for i in items}) == N


class TestDrainTCInbox:
    """Tests for BaseAgent._drain_tc_inbox — the wire-chat splice site."""

    def _make_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock()
        svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="test",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_drain_skips_when_chat_none(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent._chat = None
        agent._tc_inbox.enqueue(_make_item("soul.flow"))
        # Should not raise, should not consume the queue.
        agent._drain_tc_inbox()
        assert len(agent._tc_inbox) == 1

    def test_drain_splices_pair_into_wire_chat(self, tmp_path):
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock
        agent = self._make_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._tc_inbox.enqueue(_make_item("soul.flow", voice="my voice"))
        agent._drain_tc_inbox()

        # The last two entries should be the synthetic pair.
        entries = iface.entries
        assert entries[-2].role == "assistant"
        assert entries[-1].role == "user"
        call_block = entries[-2].content[0]
        result_block = entries[-1].content[0]
        assert call_block.name == "soul"
        assert call_block.args == {"action": "flow"}
        assert result_block.id == call_block.id
        assert result_block.content["voice"] == "my voice"
        # Queue is empty after drain.
        assert len(agent._tc_inbox) == 0

    def test_drain_skips_when_pending_tool_calls(self, tmp_path):
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock
        agent = self._make_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("do thing")
        # Assistant turn with an unanswered tool_call — chat is mid-flight.
        iface.add_assistant_message([
            TextBlock(text="let me do it"),
            ToolCallBlock(id="tc_pending", name="some_tool", args={}),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._tc_inbox.enqueue(_make_item("soul.flow"))
        agent._drain_tc_inbox()
        # Queue preserved — splice deferred to next safe boundary.
        assert len(agent._tc_inbox) == 1

    def test_drain_noop_when_queue_empty(self, tmp_path):
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock
        agent = self._make_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        before_count = len(iface.entries)
        agent._drain_tc_inbox()
        # No change to chat state.
        assert len(iface.entries) == before_count


def _make_replace_item(source: str, voice: str = "v") -> InvoluntaryToolCall:
    tc_id = f"tc_{int(time.time() * 1000)}_{source}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})
    result = ToolResultBlock(id=tc_id, name="soul", content={"voice": voice})
    return InvoluntaryToolCall(
        call=call, result=result,
        source=source, enqueued_at=time.time(),
        coalesce=True, replace_in_history=True,
    )


class TestReplaceInHistory:
    """Drain-side single-slot semantics: replace_in_history items remove any
    prior pair of the same source from ChatInterface.entries before
    appending the new one. Used by soul flow."""

    def _make_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock
        svc = MagicMock()
        svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="test",
            working_dir=tmp_path / "agent",
        )
        iface = ChatInterface()
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        return agent, iface

    def test_first_replace_in_history_appends_and_tracks_id(self, tmp_path):
        agent, iface = self._make_agent(tmp_path)
        item = _make_replace_item("soul.flow", voice="first")
        agent._tc_inbox.enqueue(item)
        agent._drain_tc_inbox()
        # Pair appended; tracker now points at the new call's id.
        assert agent._appendix_ids_by_source.get("soul.flow") == item.call.id
        # Last two entries are the synthetic pair.
        assert iface.entries[-2].role == "assistant"
        assert iface.entries[-1].role == "user"
        assert iface.entries[-2].content[0].id == item.call.id

    def test_second_replace_in_history_evicts_first_pair(self, tmp_path):
        agent, iface = self._make_agent(tmp_path)
        first = _make_replace_item("soul.flow", voice="first")
        agent._tc_inbox.enqueue(first)
        agent._drain_tc_inbox()
        n_after_first = len(iface.entries)

        # Time-separated id so coalesce on the inbox side doesn't kick in.
        time.sleep(0.002)
        second = _make_replace_item("soul.flow", voice="second")
        assert second.call.id != first.call.id
        agent._tc_inbox.enqueue(second)
        agent._drain_tc_inbox()

        # Net entry count is unchanged (one pair removed, one appended).
        assert len(iface.entries) == n_after_first
        # First pair gone, second pair present.
        ids_in_history = [
            e.content[0].id for e in iface.entries
            if e.content and hasattr(e.content[0], "id")
        ]
        assert first.call.id not in ids_in_history
        assert second.call.id in ids_in_history
        assert agent._appendix_ids_by_source["soul.flow"] == second.call.id

    def test_replace_in_history_only_affects_same_source(self, tmp_path):
        agent, iface = self._make_agent(tmp_path)
        flow_item = _make_replace_item("soul.flow", voice="flow")
        agent._tc_inbox.enqueue(flow_item)
        agent._drain_tc_inbox()

        time.sleep(0.002)
        wakeup_item = _make_replace_item("system.wakeup", voice="wakeup")
        agent._tc_inbox.enqueue(wakeup_item)
        agent._drain_tc_inbox()

        # Both pairs survive — different sources.
        assert agent._appendix_ids_by_source["soul.flow"] == flow_item.call.id
        assert agent._appendix_ids_by_source["system.wakeup"] == wakeup_item.call.id
        ids_in_history = [
            e.content[0].id for e in iface.entries
            if e.content and hasattr(e.content[0], "id")
        ]
        assert flow_item.call.id in ids_in_history
        assert wakeup_item.call.id in ids_in_history

    def test_replace_in_history_no_prior_pair_just_appends(self, tmp_path):
        agent, iface = self._make_agent(tmp_path)
        # Pre-seed the tracker with a stale id that doesn't actually exist
        # in entries — drain should clear it without crashing.
        agent._appendix_ids_by_source["soul.flow"] = "tc_stale_id"
        item = _make_replace_item("soul.flow", voice="fresh")
        agent._tc_inbox.enqueue(item)
        agent._drain_tc_inbox()
        # New pair is appended; stale tracker replaced with the new id.
        assert agent._appendix_ids_by_source["soul.flow"] == item.call.id

    def test_non_replace_items_skip_history_eviction(self, tmp_path):
        """Items without replace_in_history go through the original path."""
        agent, iface = self._make_agent(tmp_path)
        # Existing replace item builds a tracked pair.
        prior = _make_replace_item("soul.flow", voice="prior")
        agent._tc_inbox.enqueue(prior)
        agent._drain_tc_inbox()
        n_after_prior = len(iface.entries)

        # Now a plain (non-replace) item — must NOT evict the prior pair.
        time.sleep(0.002)
        tc_id = f"tc_{int(time.time() * 1000)}_x"
        plain = InvoluntaryToolCall(
            call=ToolCallBlock(id=tc_id, name="other", args={}),
            result=ToolResultBlock(id=tc_id, name="other", content={}),
            source="other.event",
            enqueued_at=time.time(),
            coalesce=False,
            replace_in_history=False,
        )
        agent._tc_inbox.enqueue(plain)
        agent._drain_tc_inbox()
        # Prior pair still there + plain pair appended.
        assert len(iface.entries) == n_after_prior + 2
        assert agent._appendix_ids_by_source["soul.flow"] == prior.call.id
        assert "other.event" not in agent._appendix_ids_by_source
