"""Tests for DeepSeek adapter's reasoning_content round-trip.

DeepSeek V4 thinking mode's actual contract (determined empirically —
the docs understate it):

    Once any assistant turn in the conversation has tool_calls, ALL
    subsequent assistant turns (tool-call AND plain-text) must carry
    reasoning_content on replay. Assistant turns BEFORE the first
    tool_call don't need it.

Real reasoning is now preserved end-to-end: the OpenAI adapter captures
``reasoning_content`` into a ThinkingBlock; ``to_openai`` emits the
ThinkingBlock back as ``reasoning_content`` on replay. The DeepSeek
adapter only injects a per-turn-unique fallback when an assistant turn
has no captured ThinkingBlock (e.g. rehydrated pre-fix history). See
lingtai-kernel issue #9 for the cache-collapse failure mode that drove
this design.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from lingtai.llm.deepseek.adapter import DeepSeekAdapter, DeepSeekChatSession
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _make_raw_response(*, content=None, reasoning_content=None, tool_calls=None):
    """Build a minimal fake OpenAI ChatCompletion-like object."""
    msg = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls or [],
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    )


def _make_tool_call(id_, name, args_json="{}"):
    return SimpleNamespace(
        id=id_,
        function=SimpleNamespace(name=name, arguments=args_json),
    )


def _build_session(client, iface=None):
    """Build a DeepSeekChatSession around a mock openai client."""
    if iface is None:
        iface = ChatInterface()
        iface.add_system("you are a helpful assistant")
    return DeepSeekChatSession(
        client=client,
        model="deepseek-v4-pro",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        client_kwargs={},
    )


class TestRealReasoningPreserved:
    """When the provider returns reasoning_content, it lands in a ThinkingBlock
    and replays as real reasoning_content on the next request — no placeholder."""

    def test_real_reasoning_round_trips_on_tool_call_turn(self):
        client = MagicMock()
        tc = _make_tool_call("call_abc", "email")
        client.chat.completions.create.return_value = _make_raw_response(
            tool_calls=[tc],
            reasoning_content="Let me check the inbox first.",
        )
        session = _build_session(client)
        session.send("hi")

        # Turn 2: send tool result — the prior assistant turn must replay
        # with the REAL reasoning, not a placeholder.
        client.chat.completions.create.return_value = _make_raw_response(
            content="done",
            reasoning_content="Inbox confirmed empty.",
        )
        session.send([ToolResultBlock(id="call_abc", name="email", content="sent")])

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        assistant_tool_turns = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_tool_turns) == 1
        assert assistant_tool_turns[0]["reasoning_content"] == "Let me check the inbox first."

    def test_plain_text_turn_before_any_tool_call_has_no_reasoning(self):
        """Plain-text assistant turns that precede the first tool_call must
        NOT carry reasoning_content — DeepSeek rejects it otherwise."""
        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(
            content="hello there",
        )
        session = _build_session(client)
        session.send("hi")

        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session.send("thanks")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        for m in messages:
            assert "reasoning_content" not in m

    def test_plain_text_turn_after_tool_call_replays_real_reasoning(self):
        """The trailing plain-text reply that closes a tool loop must also
        carry reasoning_content on replay (the contract extends past
        tool-call turns once thinking has been invoked). Real reasoning
        is preserved verbatim."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [
                ThinkingBlock(text="user is asking about inbox"),
                TextBlock(text="checking"),
                ToolCallBlock(id="call_1", name="email", args={"action": "check"}),
            ],
            model="deepseek-v4-pro",
            provider="deepseek",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])
        iface.add_assistant_message(
            [
                ThinkingBlock(text="empty inbox, plain reply"),
                TextBlock(text="no new mail for you"),
            ],
            model="deepseek-v4-pro",
            provider="deepseek",
        )

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("anything else?")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        assistant_turns = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistant_turns) == 2
        assert assistant_turns[0]["reasoning_content"] == "user is asking about inbox"
        assert assistant_turns[1]["reasoning_content"] == "empty inbox, plain reply"


class TestFallbackForRehydratedHistory:
    """Pre-fix chat_history.jsonl entries have no ThinkingBlock. The adapter
    must still satisfy DeepSeek's field-presence requirement on replay,
    using a per-turn-unique stub (NOT a constant placeholder — that
    triggered the cache-collapse failure)."""

    def test_rehydrated_tool_call_turn_gets_fallback(self):
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        # Rehydrated turn: TextBlock + ToolCallBlock, no ThinkingBlock.
        iface.add_assistant_message(
            [
                TextBlock(text="let me check"),
                ToolCallBlock(id="restored_call", name="email", args={"action": "check"}),
            ],
            model="deepseek-v4-pro",
            provider="deepseek",
        )
        iface.add_tool_results([
            ToolResultBlock(id="restored_call", name="email", content="no mail"),
        ])

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("anything else?")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        assistant_tool_turns = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_tool_turns) == 1
        assert assistant_tool_turns[0]["reasoning_content"]
        # Fallback must inline call ids — keeps the stub byte-different per turn.
        assert "restored_call" in assistant_tool_turns[0]["reasoning_content"]

    def test_fallbacks_are_unique_across_turns(self):
        """Two consecutive rehydrated tool-call turns must produce
        byte-different reasoning_content (the cache-collapse trigger
        was a constant placeholder repeated across turns)."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [ToolCallBlock(id="call_1", name="email", args={"action": "check"})],
            model="deepseek-v4-pro",
            provider="deepseek",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])
        iface.add_assistant_message(
            [ToolCallBlock(id="call_2", name="search", args={"q": "x"})],
            model="deepseek-v4-pro",
            provider="deepseek",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_2", name="search", content="no results"),
        ])

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("anything else?")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        rc_values = [
            m["reasoning_content"]
            for m in messages
            if m.get("role") == "assistant" and m.get("reasoning_content")
        ]
        assert len(rc_values) == 2
        assert rc_values[0] != rc_values[1], (
            "Fallback reasoning must be byte-different per turn to defeat "
            "DeepSeek's cache fast-path (issue #9)."
        )

    def test_real_reasoning_preferred_over_fallback(self):
        """When a ThinkingBlock IS present, the adapter must not overwrite
        it with the fallback stub."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [
                ThinkingBlock(text="real reasoning here"),
                ToolCallBlock(id="call_1", name="email", args={"action": "check"}),
            ],
            model="deepseek-v4-pro",
            provider="deepseek",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("anything else?")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        assistant_tool_turn = next(
            m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert assistant_tool_turn["reasoning_content"] == "real reasoning here"


class TestThinkingBlockCaptured:
    """The OpenAI adapter must persist captured reasoning_content as a
    ThinkingBlock on the assistant interface entry — that's the upstream
    half of the round-trip."""

    def test_reasoning_content_lands_in_thinking_block(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(
            content="hi",
            reasoning_content="I should greet the user.",
        )
        session = _build_session(client)
        session.send("hello")

        last_entry = session._interface.entries[-1]
        assert last_entry.role == "assistant"
        thinking_blocks = [b for b in last_entry.content if isinstance(b, ThinkingBlock)]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0].text == "I should greet the user."

    def test_no_reasoning_no_thinking_block(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="hi")
        session = _build_session(client)
        session.send("hello")

        last_entry = session._interface.entries[-1]
        thinking_blocks = [b for b in last_entry.content if isinstance(b, ThinkingBlock)]
        assert thinking_blocks == []


class TestDeepSeekAdapterWiring:
    def test_session_class_override(self):
        assert DeepSeekAdapter._session_class is DeepSeekChatSession

    def test_default_base_url(self):
        adapter = DeepSeekAdapter(api_key="stub")
        assert adapter.base_url == "https://api.deepseek.com"

    def test_base_url_override(self):
        adapter = DeepSeekAdapter(api_key="stub", base_url="https://alt.example/v1")
        assert adapter.base_url == "https://alt.example/v1"
