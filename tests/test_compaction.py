"""Tests for context compaction in LLMService.check_and_compact().

Exercises the full compaction pipeline:
  1. estimate_context_tokens() detects context > threshold
  2. find_compaction_boundary() splits at the right turn
  3. format_for_summary() produces text for the summarizer
  4. The new ChatInterface has: system + summary + ack + recent turns
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.llm.base import ChatSession, FunctionSchema
from lingtai.llm.service import LLMService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_long_conversation(
    num_turns: int = 10,
    text_size: int = 5000,
) -> ChatInterface:
    """Build a ChatInterface with many turns of large text.

    Each turn = user message + assistant response.
    """
    iface = ChatInterface()
    iface.add_system("You are a helpful assistant.")

    filler = "x" * text_size  # ~text_size/4 tokens with char estimate

    for i in range(num_turns):
        iface.add_user_message(f"Turn {i}: {filler}")
        iface.add_assistant_message(
            [TextBlock(text=f"Response {i}: {filler}")],
        )

    return iface


def _build_conversation_with_tools(
    num_turns: int = 8,
    text_size: int = 5000,
) -> ChatInterface:
    """Build a ChatInterface with user messages, tool calls, and tool results."""
    iface = ChatInterface()
    tools = [{"name": "search", "description": "Search", "parameters": {}}]
    iface.add_system("You are a helpful assistant.", tools=tools)

    filler = "x" * text_size

    for i in range(num_turns):
        iface.add_user_message(f"Turn {i}: Please search for info about topic {i}")
        # Assistant calls a tool
        iface.add_assistant_message(
            [ToolCallBlock(id=f"call_{i}", name="search", args={"q": f"topic {i}"})],
        )
        # Tool result comes back
        iface.add_tool_results(
            [ToolResultBlock(id=f"call_{i}", name="search", content=f"Result {i}: {filler}")]
        )
        # Assistant responds with the result
        iface.add_assistant_message(
            [TextBlock(text=f"Based on the search, here's what I found about topic {i}.")],
        )

    return iface


class FakeChatSession(ChatSession):
    """Minimal ChatSession for testing compaction logic."""

    def __init__(self, interface: ChatInterface, ctx_window: int = 0):
        self._interface = interface
        self._context_window_val = ctx_window
        self._model = "test-model"
        self._agent_type = "test"
        self._tracked = True
        self.session_id = "test-session"

    @property
    def interface(self) -> ChatInterface:
        return self._interface

    def send(self, message):
        raise NotImplementedError

    def context_window(self) -> int:
        return self._context_window_val


# ---------------------------------------------------------------------------
# Tests — ChatInterface compaction primitives
# ---------------------------------------------------------------------------


class TestCompactionBoundary:
    """Tests for find_compaction_boundary()."""

    def test_short_conversation_returns_none(self):
        """Conversations with < 6 non-system entries cannot be compacted."""
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("hello")
        iface.add_assistant_message([TextBlock(text="hi")])
        assert iface.find_compaction_boundary(keep_turns=3) is None

    def test_finds_boundary_with_enough_turns(self):
        """Should find a boundary that keeps the last 3 turns."""
        iface = _build_long_conversation(num_turns=6, text_size=100)
        boundary = iface.find_compaction_boundary(keep_turns=3)
        assert boundary is not None

        # Entries after boundary should contain the last 3 user messages
        conv = [e for e in iface.entries if e.role != "system"]
        kept = [e for e in conv if e.id >= boundary]
        kept_user_texts = [
            e for e in kept
            if e.role == "user"
            and any(isinstance(b, TextBlock) for b in e.content)
        ]
        assert len(kept_user_texts) == 3

    def test_boundary_with_tool_turns(self):
        """Tool call/result exchanges within a turn should not be split."""
        iface = _build_conversation_with_tools(num_turns=6, text_size=100)
        boundary = iface.find_compaction_boundary(keep_turns=3)
        assert boundary is not None

        # The kept portion should have complete tool turns (no orphaned results)
        conv = [e for e in iface.entries if e.role != "system"]
        kept = [e for e in conv if e.id >= boundary]
        # Each kept turn has: user msg, assistant tool_call, tool_result, assistant text = 4 entries
        # 3 turns = 12 entries
        assert len(kept) == 12


class TestFormatForSummary:
    """Tests for format_for_summary()."""

    def test_formats_text_entries(self):
        iface = _build_long_conversation(num_turns=6, text_size=100)
        boundary = iface.find_compaction_boundary(keep_turns=3)
        text = iface.format_for_summary(boundary)
        assert "[user]" in text
        assert "[assistant]" in text
        # Should NOT contain entries from the kept portion
        assert "Turn 5" not in text  # last turn should be kept

    def test_formats_tool_entries(self):
        iface = _build_conversation_with_tools(num_turns=6, text_size=100)
        boundary = iface.find_compaction_boundary(keep_turns=3)
        text = iface.format_for_summary(boundary)
        assert "tool_use: search" in text
        assert "tool_result(search)" in text


class TestEstimateContextTokens:
    """Tests for estimate_context_tokens()."""

    def test_grows_with_conversation(self):
        small = _build_long_conversation(num_turns=2, text_size=100)
        large = _build_long_conversation(num_turns=10, text_size=100)
        assert large.estimate_context_tokens() > small.estimate_context_tokens()

    def test_accounts_for_system_prompt(self):
        iface = ChatInterface()
        iface.add_system("A" * 1000)
        estimate = iface.estimate_context_tokens()
        assert estimate > 100  # 1000 chars / ~8 = ~125 tokens (Gemini) or ~4 = ~250 tokens (tiktoken)


# ---------------------------------------------------------------------------
# Tests — LLMService.check_and_compact() integration
# ---------------------------------------------------------------------------



# TestCheckAndCompact removed — check_and_compact was replaced by molt (psyche intrinsic).
# Compaction is now handled by SessionManager internally, not via LLMService method.



# get_context_limit tests removed — context window is now caller-provided.


# ---------------------------------------------------------------------------
# Tests — Compaction pressure system in BaseAgent._handle_request
# ---------------------------------------------------------------------------


def _make_agent_with_psyche(tmp_path):
    """Create an Agent with psyche capability and mocked LLM service."""
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )


def test_compaction_pressure_not_published_as_notification(tmp_path):
    """Above the molt_pressure threshold, the kernel no longer publishes a
    dismissible ``.notification/molt.json`` warning. Context pressure is
    agent state and is surfaced under ``_meta.agent_meta.context.molt``
    (see ``meta_block.build_molt_context``); ``_check_molt_pressure`` only
    clears any stale legacy ``molt`` notification and never publishes."""
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        # Stamp a real int context_limit so build_meta() doesn't fall through
        # to chat_obj.context_window() (a MagicMock that breaks comparisons).
        agent._config.context_limit = 100_000
        # Mock session to report 85% context pressure (above the default
        # 0.7 threshold).
        agent._session.get_context_pressure = lambda: 0.85

        sent_content = []

        def capture_send(content):
            sent_content.append(content)
            resp = MagicMock()
            resp.text = "ok"
            resp.tool_calls = []
            resp.usage = None
            return resp

        agent._session.send = capture_send

        from lingtai_kernel.message import _make_message, MSG_REQUEST
        msg = _make_message(MSG_REQUEST, sender="test", content="do something")
        agent._handle_request(msg)

        # User content is NOT mutated with an inlined [system] block.
        assert len(sent_content) > 0
        assert not any("[system]" in c for c in sent_content)

        # The legacy molt pressure notification is NOT published anymore.
        notif_path = agent._working_dir / ".notification" / "molt.json"
        assert not notif_path.is_file(), (
            "context pressure must not be published as .notification/molt.json"
        )
    finally:
        agent.stop()
