"""Tests for OpenAIChatSession context-overflow auto-recovery.

When a provider returns 400 with context_length_exceeded, the adapter
trims the oldest ~10% of non-system entries and retries — up to
_OVERFLOW_MAX_ROUNDS times — then injects a [kernel] notice telling the
agent to molt soon.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import openai

from lingtai.llm.openai.adapter import OpenAIChatSession
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _make_raw_response(content="ok"):
    msg = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            completion_tokens_details=None,
            prompt_tokens_details=None,
        ),
    )


def _make_overflow_error(msg="This model's maximum context length is 128000 tokens"):
    """Construct an openai.BadRequestError mimicking context-length overflow."""
    body = {"error": {"message": msg, "code": "context_length_exceeded"}}
    request = MagicMock()
    response = MagicMock(status_code=400)
    return openai.BadRequestError(message=msg, response=response, body=body)


def _make_session(client, interface=None):
    if interface is None:
        interface = ChatInterface()
        interface.add_system("you are helpful")
    return OpenAIChatSession(
        client=client,
        model="gpt-test",
        interface=interface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        client_kwargs={},
    )


def _seed_history(iface: ChatInterface, n_pairs: int = 10) -> None:
    """Add n user/assistant text-only pairs to a fresh interface."""
    for i in range(n_pairs):
        iface.add_user_message(f"q{i}")
        iface.add_assistant_message(
            [TextBlock(text=f"a{i}")],
            model="gpt-test",
            provider="openai",
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detects_canonical_openai_overflow_code():
    err = _make_overflow_error()
    assert OpenAIChatSession._is_context_overflow_error(err) is True


def test_detects_message_only_overflow_compat_provider():
    err = openai.BadRequestError(
        message="prompt is too long for this model's context window",
        response=MagicMock(status_code=400),
        body={"error": {"message": "prompt is too long"}},
    )
    assert OpenAIChatSession._is_context_overflow_error(err) is True


def test_does_not_detect_unrelated_400():
    err = openai.BadRequestError(
        message="invalid tool schema",
        response=MagicMock(status_code=400),
        body={"error": {"message": "invalid tool schema"}},
    )
    assert OpenAIChatSession._is_context_overflow_error(err) is False


def test_does_not_detect_non_bad_request():
    err = RuntimeError("network down")
    assert OpenAIChatSession._is_context_overflow_error(err) is False


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------


def test_trim_drops_at_least_one_oldest_entry():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=10)
    session = _make_session(client=MagicMock(), interface=iface)
    pre = len(iface._entries)
    dropped = session._trim_context_one_round()
    assert dropped >= 1
    assert len(iface._entries) == pre - dropped
    assert iface._entries[0].role == "system"  # system preserved


def test_trim_preserves_tool_call_result_pair():
    iface = ChatInterface()
    iface.add_system("sys")
    # Build a layout where the front-half cut would land mid-pair.
    # Many small entries followed by an assistant[tool_call] -> user[tool_result]
    # pair near the cut point.
    for i in range(8):
        iface.add_user_message(f"q{i}")
        iface.add_assistant_message(
            [TextBlock(text=f"a{i}")],
            model="gpt-test",
            provider="openai",
        )
    # Insert a tool_call/result pair early so the 10% cut hits inside it.
    iface.add_assistant_message(
        [ToolCallBlock(id="tc1", name="search", args={})],
        model="gpt-test",
        provider="openai",
    )
    iface.add_tool_results([ToolResultBlock(id="tc1", name="search", content="result")])
    # Tail
    iface.add_user_message("recent")
    iface.add_assistant_message([TextBlock(text="ok")], model="gpt-test", provider="openai")

    session = _make_session(client=MagicMock(), interface=iface)
    session._trim_context_one_round()

    # Verify: every remaining ToolCallBlock id has a matching ToolResultBlock,
    # and every remaining ToolResultBlock id has a matching ToolCallBlock.
    call_ids = set()
    result_ids = set()
    for e in iface._entries:
        for b in e.content:
            if isinstance(b, ToolCallBlock):
                call_ids.add(b.id)
            elif isinstance(b, ToolResultBlock):
                result_ids.add(b.id)
    assert call_ids == result_ids, (
        f"mismatch — calls={call_ids}, results={result_ids}"
    )


def test_trim_returns_zero_when_only_system_present():
    iface = ChatInterface()
    iface.add_system("sys")
    session = _make_session(client=MagicMock(), interface=iface)
    assert session._trim_context_one_round() == 0


def test_trim_returns_zero_when_single_conversation_entry():
    iface = ChatInterface()
    iface.add_system("sys")
    iface.add_user_message("only")
    session = _make_session(client=MagicMock(), interface=iface)
    assert session._trim_context_one_round() == 0


# ---------------------------------------------------------------------------
# Recovery wrapper
# ---------------------------------------------------------------------------


def test_recovery_no_overflow_passes_through():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=5)
    session = _make_session(client=MagicMock(), interface=iface)

    calls = {"n": 0}
    def do_call():
        calls["n"] += 1
        return "result"

    result, dropped, rounds = session._run_with_overflow_recovery(do_call)
    assert result == "result"
    assert dropped == 0
    assert rounds == 0
    assert calls["n"] == 1


def test_recovery_succeeds_after_one_trim():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=20)
    session = _make_session(client=MagicMock(), interface=iface)

    attempts = {"n": 0}
    def do_call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _make_overflow_error()
        return "result"

    result, dropped, rounds = session._run_with_overflow_recovery(do_call)
    assert result == "result"
    assert rounds == 1
    assert dropped >= 1


def test_recovery_gives_up_after_max_rounds():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=200)  # plenty to trim
    session = _make_session(client=MagicMock(), interface=iface)

    def do_call():
        raise _make_overflow_error()

    try:
        session._run_with_overflow_recovery(do_call)
    except openai.BadRequestError:
        pass
    else:
        raise AssertionError("expected BadRequestError after max rounds")


def test_recovery_reraises_non_overflow_400():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=5)
    session = _make_session(client=MagicMock(), interface=iface)

    err = openai.BadRequestError(
        message="bad tool schema",
        response=MagicMock(status_code=400),
        body={"error": {"message": "bad tool schema"}},
    )
    def do_call():
        raise err

    try:
        session._run_with_overflow_recovery(do_call)
    except openai.BadRequestError as e:
        assert e is err
    else:
        raise AssertionError("expected the original BadRequestError")


# ---------------------------------------------------------------------------
# End-to-end via send()
# ---------------------------------------------------------------------------


def test_send_recovers_and_injects_kernel_notice():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=20)

    client = MagicMock()
    raw = _make_raw_response()
    # First call raises overflow, second succeeds.
    client.chat.completions.create.side_effect = [
        _make_overflow_error(),
        raw,
    ]
    session = _make_session(client=client, interface=iface)

    response = session.send("a brand new question")
    assert response.text == "ok"
    assert client.chat.completions.create.call_count == 2

    # Find the kernel notice — should be a user-role TextBlock with the
    # [kernel] prefix and a "molt" recommendation.
    found = False
    for entry in iface._entries:
        for b in entry.content:
            if (isinstance(b, TextBlock)
                and b.text.startswith("[kernel]")
                and "molt" in b.text.lower()):
                found = True
                break
    assert found, "expected a [kernel] molt-recommendation notice in interface"


def test_send_passes_through_when_no_overflow():
    iface = ChatInterface()
    iface.add_system("sys")
    _seed_history(iface, n_pairs=3)

    client = MagicMock()
    client.chat.completions.create.return_value = _make_raw_response()
    session = _make_session(client=client, interface=iface)

    pre_len = len(iface._entries)
    response = session.send("hi")
    assert response.text == "ok"
    assert client.chat.completions.create.call_count == 1
    # No kernel notice should be injected when nothing overflowed.
    for entry in iface._entries:
        for b in entry.content:
            if isinstance(b, TextBlock) and b.text.startswith("[kernel]"):
                raise AssertionError("unexpected [kernel] notice in interface")
    # User message was added, assistant reply was recorded — net +2.
    assert len(iface._entries) == pre_len + 2
