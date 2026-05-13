"""Tests for MiMo (Xiaomi) adapter's reasoning_content round-trip.

MiMo thinking mode follows a contract analogous to DeepSeek's: once any
assistant turn in the conversation has tool_calls, ALL subsequent assistant
turns (tool-call AND plain-text) must carry ``reasoning_content`` on replay.
Assistant turns BEFORE the first tool_call must NOT carry it.

Real reasoning is preserved end-to-end: the OpenAI adapter captures
``reasoning_content`` into a ``ThinkingBlock``; ``to_openai`` emits it back
as ``reasoning_content`` on replay. The MiMo adapter only injects a
per-turn-unique fallback when an assistant turn has no captured
``ThinkingBlock``. The fallback must be byte-different per turn — a constant
placeholder would re-trigger MiMo's verbatim-thinking loop pathology.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from lingtai.llm.mimo.adapter import MimoAdapter, MimoChatSession
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _make_raw_response(*, content=None, reasoning_content=None, tool_calls=None):
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
    if iface is None:
        iface = ChatInterface()
        iface.add_system("you are a helpful assistant")
    return MimoChatSession(
        client=client,
        model="mimo-v2.5-pro",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        client_kwargs={},
    )


class TestRealReasoningPreserved:
    """Real ThinkingBlock-sourced reasoning_content must round-trip verbatim."""

    def test_real_reasoning_round_trips_on_tool_call_turn(self):
        client = MagicMock()
        tc = _make_tool_call("call_abc", "email")
        client.chat.completions.create.return_value = _make_raw_response(
            tool_calls=[tc],
            reasoning_content="Let me check the inbox first.",
        )
        session = _build_session(client)
        session.send("hi")

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
        NOT carry reasoning_content."""
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

    def test_long_pre_tool_plain_history_gets_no_fallback(self):
        """Rehydrated pre-tool-call plain history (no ThinkingBlocks) must
        stay clean — the fallback injection path activates only after the
        first assistant tool_call."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        for i in range(5):
            iface.add_assistant_message(
                [TextBlock(text=f"reply {i}")],
                model="mimo-v2.5-pro",
                provider="mimo",
            )
            iface.add_user_message(f"follow-up {i}")

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("final")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        for m in messages:
            assert "reasoning_content" not in m

    def test_plain_text_turn_after_tool_call_replays_real_reasoning(self):
        """Trailing plain-text reply that closes a tool loop carries
        reasoning_content on replay (the contract extends past tool-call
        turns once thinking has been invoked). Real reasoning verbatim."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [
                ThinkingBlock(text="user is asking about inbox"),
                TextBlock(text="checking"),
                ToolCallBlock(id="call_1", name="email", args={"action": "check"}),
            ],
            model="mimo-v2.5-pro",
            provider="mimo",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])
        iface.add_assistant_message(
            [
                ThinkingBlock(text="empty inbox, plain reply"),
                TextBlock(text="no new mail for you"),
            ],
            model="mimo-v2.5-pro",
            provider="mimo",
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


class TestFallbackForMissingThinkingBlock:
    """Assistant turns after the first tool_call that lack a ThinkingBlock
    (e.g. rehydrated pre-fix history) get a per-turn-unique fallback —
    NOT a constant placeholder, which would re-trigger the loop pathology."""

    def test_rehydrated_tool_call_turn_gets_fallback(self):
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [
                TextBlock(text="let me check"),
                ToolCallBlock(id="restored_call", name="email", args={"action": "check"}),
            ],
            model="mimo-v2.5-pro",
            provider="mimo",
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
        # Fallback inlines call ids — keeps the stub byte-different per turn.
        assert "restored_call" in assistant_tool_turns[0]["reasoning_content"]

    def test_fallbacks_are_unique_across_turns(self):
        """Two consecutive rehydrated tool-call turns must produce
        byte-different reasoning_content (the loop pathology was triggered
        by a constant placeholder repeated across turns)."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [ToolCallBlock(id="call_1", name="email", args={"action": "check"})],
            model="mimo-v2.5-pro",
            provider="mimo",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])
        iface.add_assistant_message(
            [ToolCallBlock(id="call_2", name="search", args={"q": "x"})],
            model="mimo-v2.5-pro",
            provider="mimo",
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
            "Fallback reasoning must be byte-different per turn to avoid "
            "MiMo's verbatim-thinking loop pathology."
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
            model="mimo-v2.5-pro",
            provider="mimo",
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

    def test_rehydrated_plain_text_turn_after_tool_call_gets_fallback(self):
        """Plain-text assistant reply that closes a tool loop also needs
        reasoning_content once thinking has been invoked. Without a
        ThinkingBlock, a per-turn-unique fallback is injected."""
        iface = ChatInterface()
        iface.add_system("system prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message(
            [ToolCallBlock(id="call_1", name="email", args={"action": "check"})],
            model="mimo-v2.5-pro",
            provider="mimo",
        )
        iface.add_tool_results([
            ToolResultBlock(id="call_1", name="email", content="no mail"),
        ])
        iface.add_assistant_message(
            [TextBlock(text="no new mail for you")],
            model="mimo-v2.5-pro",
            provider="mimo",
        )

        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="ok")
        session = _build_session(client, iface=iface)
        session.send("anything else?")

        messages = client.chat.completions.create.call_args.kwargs["messages"]
        plain_text_turn = next(
            m for m in messages
            if m.get("role") == "assistant"
            and not m.get("tool_calls")
            and m.get("content") == "no new mail for you"
        )
        assert plain_text_turn["reasoning_content"]
        # Snippet of the content is embedded — keeps it per-turn unique.
        assert "no new mail" in plain_text_turn["reasoning_content"]


class TestThinkingBlockCaptured:
    """The OpenAI adapter must persist captured reasoning_content as a
    ThinkingBlock on the assistant interface entry — the upstream half
    of the round-trip."""

    def test_reasoning_content_lands_in_thinking_block(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(
            content="here's my answer",
            reasoning_content="this is my reasoning",
        )
        session = _build_session(client)
        response = session.send("hi")

        # Response carries the thinking text for the turn loop's _log("thinking").
        assert response.thoughts == ["this is my reasoning"]

        assistant_entries = [
            e for e in session.interface.entries if e.role == "assistant"
        ]
        assert len(assistant_entries) == 1
        block_types = [type(b).__name__ for b in assistant_entries[0].content]
        assert "ThinkingBlock" in block_types

    def test_no_reasoning_no_thinking_block(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _make_raw_response(content="hi")
        session = _build_session(client)
        session.send("hello")

        last_entry = session._interface.entries[-1]
        thinking_blocks = [b for b in last_entry.content if isinstance(b, ThinkingBlock)]
        assert thinking_blocks == []


class TestAdapterRegistration:
    """The MiMo adapter must be wired into the LLMService registry."""

    def test_mimo_resolves_to_mimo_adapter(self):
        from lingtai.llm._register import register_all_adapters
        from lingtai.llm.service import LLMService

        register_all_adapters()
        factory = LLMService._adapter_registry.get("mimo")
        assert factory is not None
        adapter = factory(
            model="mimo-v2.5-pro",
            defaults=None,
            api_key="test",
            base_url="https://api.xiaomimimo.com",
        )
        assert isinstance(adapter, MimoAdapter)
        assert adapter._session_class is MimoChatSession
