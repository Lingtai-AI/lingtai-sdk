"""Mock tests for the experimental Codex Responses-over-WebSocket session path.

No network: a fake websocket transport is injected so the tests assert only the
*request shapes* the session produces. They mirror the official Codex CLI source
(repo openai/codex, tag ``rust-v0.130.0``):

  * first WS request: full input, no ``previous_response_id`` (``client.rs:1003``)
  * second same-turn request: delta input + ``previous_response_id``
    (``client.rs:998-1024``)
  * ``store`` is always ``false`` — the ChatGPT Codex backend rejects
    ``store=true`` (``client.rs:722`` builds ``store=false`` for ChatGPT)
  * fallback to HTTP full replay on handshake 426 / connect error / delta
    mismatch (``client.rs:1361-1364`` FallbackToHttp)
  * ``x-codex-turn-state`` captured from the handshake and replayed within the
    turn (``client.rs:227-240``)
  * ``response.processed`` sent after a completed response
    (``responses_websocket.rs:208-240``)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from lingtai.llm.openai.codex_ws import SyncCodexWebsocketTransport

from lingtai.llm.openai.adapter import (
    CodexResponsesSession,
    _CodexWsFallback,
)


@dataclass
class Event:
    type: str
    delta: str | None = None
    item: object | None = None
    response: object | None = None
    item_id: str | None = None
    text: str | None = None


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed(resp_id: str = "resp_ws_1") -> Event:
    return Event(
        "response.completed",
        response=SimpleNamespace(id=resp_id, usage=_usage()),
    )


class FakeWsTransport:
    """Records frames + handshake; yields a single completed response per stream.

    One transport instance models one connection that may carry multiple
    ``response.create`` frames within a turn (the official model reuses the
    connection). ``turn_state`` is the value returned from the handshake.
    """

    def __init__(self, *, turn_state="ts-server-1", fallback_on_connect=False):
        self._turn_state = turn_state
        self._fallback_on_connect = fallback_on_connect
        self.connect_calls = 0
        self.sent_frames: list[dict] = []
        self.processed: list[str] = []
        self.handshake_headers: list[dict] = []
        self._resp_counter = 0

    def connect(self, *, headers):
        self.connect_calls += 1
        self.handshake_headers.append(dict(headers))
        if self._fallback_on_connect:
            raise _CodexWsFallback("handshake 426 UPGRADE_REQUIRED")
        return self._turn_state

    def stream(self, frame):
        self.sent_frames.append(frame)
        self._resp_counter += 1
        yield _completed(f"resp_ws_{self._resp_counter}")

    def send_response_processed(self, response_id):
        self.processed.append(response_id)

    def close(self):
        pass


def _make_session(transport: FakeWsTransport, **kwargs):
    """A Codex session wired to use the injected transport, gate forced on."""
    kwargs.setdefault("ws_enabled", True)
    return CodexResponsesSession(
        client=_HttpFallbackClient(),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        ws_transport_factory=lambda url, headers: transport,
        **kwargs,
    )


class _HttpResponses:
    def __init__(self):
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        yield _completed("resp_http_fallback")


class _HttpFallbackClient:
    """Stands in for the OpenAI SDK client; records the HTTP fallback path."""

    def __init__(self):
        self.responses = _HttpResponses()


def test_first_ws_request_sends_full_input_and_no_previous_id():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("hello")

    assert len(t.sent_frames) == 1
    frame = t.sent_frames[0]
    assert frame["type"] == "response.create"
    assert frame["store"] is False
    assert "previous_response_id" not in frame
    # Full input: the single user turn.
    assert frame["input"] and frame["input"][-1]["role"] == "user"
    # No HTTP fallback happened.
    assert session._client.responses.kwargs == []




def test_ws_handshake_includes_bearer_without_body_or_usage_leak():
    t = FakeWsTransport()
    session = _make_session(t, api_key="test-secret-token")

    response = session.send("hello")

    assert t.handshake_headers[0]["Authorization"] == "Bearer test-secret-token"
    assert "Authorization" not in t.sent_frames[0]
    assert "test-secret-token" not in str(response.usage.extra)


def test_second_ws_request_sends_delta_and_previous_response_id():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("hello")  # establishes last_request + last_response(resp_ws_1)
    session.send("again")  # should be a strict extension -> delta

    assert len(t.sent_frames) == 2
    assert t.connect_calls == 1
    second = t.sent_frames[1]
    assert second["type"] == "response.create"
    assert second["store"] is False
    assert second["previous_response_id"] == "resp_ws_1"
    # Delta input must NOT replay the first user turn; it carries only the new
    # items appended since the previous request + its response output.
    flat = [str(i) for i in second["input"]]
    assert not any("hello" in s for s in flat), f"delta leaked prior turn: {second['input']}"


def test_store_is_never_true_on_ws_frames():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("a")
    session.send("b")

    assert all(f.get("store") is False for f in t.sent_frames)




def test_reset_provider_turn_state_clears_replayed_turn_state_on_reconnect():
    t = FakeWsTransport(turn_state="ts-77")
    session = _make_session(t)

    session.send("first")
    session.reset_provider_turn_state()
    session._close_ws_transport()
    t.handshake_headers.clear()

    session.send("second")

    assert t.connect_calls == 2
    assert _CodexTurnStateHeader not in t.handshake_headers[0]


def test_turn_state_uses_persistent_connection_and_replays_on_reconnect():
    t = FakeWsTransport(turn_state="ts-77")
    session = _make_session(t)

    session.send("a")
    session.send("b")

    assert t.connect_calls == 1
    assert len(t.handshake_headers) == 1
    assert _CodexTurnStateHeader not in t.handshake_headers[0]

    session._close_ws_transport()
    session.send("c")

    assert t.connect_calls == 2
    assert t.handshake_headers[1][_CodexTurnStateHeader] == "ts-77"


def test_response_processed_sent_after_completed():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("a")

    assert t.processed == ["resp_ws_1"]


def test_fallback_to_http_full_replay_on_handshake_426():
    t = FakeWsTransport(fallback_on_connect=True)
    session = _make_session(t)

    session.send("hello")

    # No frames sent over WS; the HTTP fallback path was used with full input
    # and store=false.
    assert t.sent_frames == []
    assert len(session._client.responses.kwargs) == 1
    http = session._client.responses.kwargs[0]
    assert http["store"] is False
    assert "previous_response_id" not in http
    assert http["input"][-1]["role"] == "user"


class _FailingSecondStreamTransport(FakeWsTransport):
    def stream(self, frame):
        if self.sent_frames:
            self.sent_frames.append(frame)
            raise _CodexWsFallback("stream failed")
        yield from super().stream(frame)


def test_ws_stream_failure_restores_delta_baseline_and_closes_transport():
    t = _FailingSecondStreamTransport()
    session = _make_session(t)

    session.send("first")
    previous_request = session._ws_session.last_request
    previous_response = session._ws_session.last_response

    with pytest.raises(_CodexWsFallback):
        session.send("second")

    assert session._ws_session.last_request == previous_request
    assert session._ws_session.last_response == previous_response
    assert session._ws_transport is None


def test_fallback_to_http_when_delta_mismatch():
    """If the new request is not a strict extension (non-input field changed),
    the session must NOT send a bad delta — it falls back to a full WS request
    (no previous id), exactly like the official prepare_websocket_request which
    returns ResponseCreate(payload) with full input on mismatch."""
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("hello")
    # Mutate a non-input field to force a mismatch on the second request.
    session._tools = [{"type": "function", "name": "newtool", "parameters": {}}]
    session.send("again")

    second = t.sent_frames[1]
    assert "previous_response_id" not in second
    # Full input replayed (both user turns present somewhere).
    flat = "".join(str(i) for i in second["input"])
    assert "hello" in flat and "again" in flat


def test_ws_disabled_by_default_uses_http():
    """Without the gate, the session uses the existing HTTP path (no transport)."""
    session = CodexResponsesSession(
        client=_HttpFallbackClient(),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        # ws_enabled defaults to False
    )

    session.send("hello")

    assert len(session._client.responses.kwargs) == 1
    assert session._client.responses.kwargs[0]["store"] is False


class _ErrorWsConnection:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def __iter__(self):
        yield json.dumps(self.payload)


def test_sync_ws_transport_top_level_error_falls_back_without_payload_leak():
    transport = SyncCodexWebsocketTransport(url="wss://example.invalid", headers={})
    transport._conn = _ErrorWsConnection(
        {
            "type": "error",
            "status": 400,
            "error": {
                "type": "invalid_request_error",
                "message": "boom with prompt/header-like details",
            },
        }
    )

    with pytest.raises(_CodexWsFallback) as excinfo:
        list(transport.stream({"type": "response.create"}))

    text = str(excinfo.value)
    assert "error" in text
    assert "invalid_request_error" in text
    assert "status=400" in text
    assert "prompt/header-like" not in text


def test_default_factory_falls_back_when_websockets_missing(monkeypatch):
    """The real transport factory raises _CodexWsFallback (caught -> HTTP) when
    the optional ``websockets`` dependency is unavailable — modeling an
    unsupported runtime (official disables WS for the session on such failures)."""
    import builtins

    from lingtai.llm.openai.adapter import _default_codex_ws_transport_factory

    real_import = builtins.__import__

    def _no_websockets(name, *args, **kwargs):
        if name == "websockets" or name.startswith("websockets."):
            raise ImportError("simulated missing websockets")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_websockets)
    with pytest.raises(_CodexWsFallback):
        _default_codex_ws_transport_factory("wss://x/responses", {})


def test_unsupported_runtime_falls_back_to_http_end_to_end(monkeypatch):
    """With the gate on but no usable transport, the session still completes via
    the HTTP full-replay path (store=false), never raising to the caller."""
    session = CodexResponsesSession(
        client=_HttpFallbackClient(),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        ws_enabled=True,
        ws_transport_factory=lambda url, headers: (_ for _ in ()).throw(
            _CodexWsFallback("no runtime")
        ),
    )

    session.send("hello")

    assert len(session._client.responses.kwargs) == 1
    assert session._client.responses.kwargs[0]["store"] is False


def test_ws_url_builder_maps_https_to_wss():
    from lingtai.llm.openai.adapter import _codex_ws_url

    assert (
        _codex_ws_url("https://chatgpt.com/backend-api/codex")
        == "wss://chatgpt.com/backend-api/codex/responses"
    )
    # Trailing slash tolerated; default base used when None.
    assert _codex_ws_url("https://host/base/").endswith("/base/responses")
    assert _codex_ws_url(None).startswith("wss://")


# ---------------------------------------------------------------------------
# Converter-stable delta baseline (#471 ws_full root-cause fix).
#
# The server streams output items in the Responses *output* schema, but this
# session re-derives its input next turn via ``to_responses_input`` in the
# *input* schema. Using the raw output items as the delta baseline can never
# strict-prefix-match the next full input, so every real agent turn collapsed to
# ``ws_full``. The fix records the baseline from the converter after the
# assistant turn lands in the interface. These tests pin that behavior end to
# end with a transport that streams realistic output items.
# ---------------------------------------------------------------------------


class _MessageItem:
    """A streamed ``response.output_item.done`` message in the OUTPUT schema."""

    def __init__(self, text: str):
        self.type = "message"
        self.id = "msg_out_1"
        self.role = "assistant"
        self.status = "completed"
        self._text = text

    def model_dump(self, exclude_none=True):
        return {
            "type": "message",
            "id": self.id,
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": self._text}],
        }


class _ToolCallItem:
    def __init__(self, call_id="call_a", name="do_x"):
        self.type = "function_call"
        self.call_id = call_id
        self.name = name


class RealisticWsTransport(FakeWsTransport):
    """Streams an assistant *message* output item then completes.

    Mirrors the live wire: a text delta, an ``output_item.done`` carrying the
    message in the OUTPUT schema, then ``response.completed``. The point is that
    the raw output item must NOT be what the next-turn baseline compares against.
    """

    def __init__(self, *, text="hi there", **kw):
        super().__init__(**kw)
        self._text = text

    def stream(self, frame):
        self.sent_frames.append(frame)
        self._resp_counter += 1
        rid = f"resp_ws_{self._resp_counter}"
        yield Event("response.output_text.delta", delta=self._text)
        yield Event("response.output_item.done", item=_MessageItem(self._text))
        yield _completed(rid)


class ToolCallWsTransport(FakeWsTransport):
    """First stream returns an assistant tool call; later streams return text."""

    def stream(self, frame):
        self.sent_frames.append(frame)
        self._resp_counter += 1
        rid = f"resp_ws_{self._resp_counter}"
        if self._resp_counter == 1:
            yield Event("response.output_item.added", item=_ToolCallItem())
            yield Event("response.function_call_arguments.delta", delta='{"a": 1}')
            yield Event("response.output_item.done", item=_ToolCallItem())
        else:
            yield Event("response.output_text.delta", delta="done")
        yield _completed(rid)


def test_second_turn_is_incremental_with_realistic_output_items():
    """Root-cause regression: a normal text turn followed by a new user turn
    sends a clean delta + ``previous_response_id`` even though the server streamed
    the assistant message in the OUTPUT schema. Before the fix this was
    ``ws_full`` because the output item never prefix-matched the re-derived input.
    """
    t = RealisticWsTransport()
    session = _make_session(t)

    first = session.send("hello")
    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert first.usage.extra["codex_ws_delta_reason"] == "no_baseline"

    second = session.send("again")

    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert second.usage.extra["codex_ws_delta_reason"] == "ok"
    frame = t.sent_frames[1]
    assert frame["previous_response_id"] == "resp_ws_1"
    # Only the new user turn is sent; the prior turn is NOT replayed.
    flat = "".join(str(i) for i in frame["input"])
    assert "hello" not in flat and "hi there" not in flat
    assert "again" in flat
    assert int(second.usage.extra["codex_ws_delta_len"]) == 1


def test_tool_result_continuation_stays_incremental():
    """A tool-result continuation (the assistant ended on an unanswered
    ``function_call``) must NOT reset to ``ws_full``. The converter injects an
    orphan-output placeholder into the baseline; the fix trims it so the real
    tool result strictly extends the baseline and rides as a delta."""
    from lingtai_kernel.llm.interface import ToolResultBlock

    t = ToolCallWsTransport()
    session = _make_session(t)

    session.send("call the tool")  # turn 1: assistant emits a tool call
    result = session.send([ToolResultBlock(id="call_a", name="do_x", content={"ok": True})])

    assert result.usage.extra["codex_request_mode"] == "ws_incremental"
    assert result.usage.extra["codex_ws_delta_reason"] == "ok"
    frame = t.sent_frames[1]
    assert frame["previous_response_id"] == "resp_ws_1"
    # The delta carries the REAL function_call_output, not the synthesized
    # placeholder, and does not replay the prior user turn or the tool call.
    assert len(frame["input"]) == 1
    only = frame["input"][0]
    assert only["type"] == "function_call_output"
    assert only["call_id"] == "call_a"
    assert "synthesized placeholder" not in str(only)


class _PerTurnToolCallWsTransport(FakeWsTransport):
    """Emits a distinct tool call (``call_1``, ``call_2``, …) on every turn.

    This drives a multi-step tool loop so the kernel-side resident-meta movement
    (latest-only ``_meta`` blocks hopping from an older tool result onto the
    freshest one) can be simulated between turns.
    """

    def stream(self, frame):
        self.sent_frames.append(frame)
        self._resp_counter += 1
        rid = f"resp_ws_{self._resp_counter}"
        call_id = f"call_{self._resp_counter}"
        yield Event("response.output_item.added", item=_ToolCallItem(call_id=call_id))
        yield Event("response.function_call_arguments.delta", delta='{"a": 1}')
        yield Event("response.output_item.done", item=_ToolCallItem(call_id=call_id))
        yield _completed(rid)


def _strip_resident_meta_from_oldest_tool_result(session) -> bool:
    """Mimic ``attach_active_runtime``: strip the latest-only ``_meta`` blocks
    from the OLDEST tool result's content in place (the kernel moves them onto
    the freshest result each turn). Returns True if a result was mutated."""
    from lingtai_kernel.llm.interface import ToolResultBlock

    for entry in session._interface.entries:
        for block in getattr(entry, "content", []) or []:
            if isinstance(block, ToolResultBlock) and isinstance(block.content, dict):
                meta = block.content.get("_meta")
                if isinstance(meta, dict) and (
                    "agent_meta" in meta or "guidance" in meta or "notifications" in meta
                ):
                    meta.pop("agent_meta", None)
                    meta.pop("guidance", None)
                    meta.pop("notifications", None)
                    if not meta:
                        block.content.pop("_meta", None)
                    return True
    return False


def test_resident_meta_movement_does_not_break_incremental_delta():
    """Core regression for resident-meta canonicalization.

    The kernel moves latest-only ``_meta`` blocks off an older tool result onto
    the freshest one each turn, mutating the older ``ToolResultBlock.content`` in
    place. Without per-session output freezing that older
    ``function_call_output.output`` changes between turns, breaking the strict
    prefix and forcing ``ws_full`` (``prefix_mismatch``). With freezing the older
    output replays byte-identically and the delta stays ``ws_incremental``.
    """
    from lingtai_kernel.llm.interface import ToolResultBlock

    t = _PerTurnToolCallWsTransport()
    session = _make_session(t)

    session.send("start the tool loop")  # turn 1 -> assistant emits call_1
    # Turn 2: answer call_1. As the freshest result it carries resident meta.
    session.send([
        ToolResultBlock(
            id="call_1",
            name="do_x",
            content={"ok": True, "_meta": {"agent_meta": {"stamina": 9}, "tool_meta": {"id": "call_1"}}},
        )
    ])  # assistant emits call_2

    # Kernel boundary: the meta hops off call_1's result onto the newer one.
    # Simulate that in-place mutation BEFORE the next send re-converts history.
    assert _strip_resident_meta_from_oldest_tool_result(session)

    # Turn 3: answer call_2. The re-converted history now contains call_1 with
    # its resident meta stripped — the exact prefix-mismatch trigger.
    third = session.send([
        ToolResultBlock(
            id="call_2",
            name="do_x",
            content={"ok": True, "_meta": {"agent_meta": {"stamina": 8}, "tool_meta": {"id": "call_2"}}},
        )
    ])

    # The frozen call_1 output keeps the prefix byte-identical: stays incremental.
    assert third.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_ws_delta_reason"] == "ok"
    # No prefix divergence was recorded (mismatch_index stays the -1 sentinel).
    assert str(third.usage.extra.get("codex_ws_mismatch_index", "-1")) == "-1"
    # The delta carries only the new call_2 result, not a full replay.
    assert t.sent_frames[2]["previous_response_id"] == "resp_ws_2"
    delta = t.sent_frames[2]["input"]
    assert [i.get("call_id") for i in delta if i.get("type") == "function_call_output"] == ["call_2"]


def test_mismatch_falls_back_to_ws_full_with_safe_reason():
    """A non-input field change (tools) forces ``ws_full`` with a safe diagnostic
    naming only the changed KEY, never any value."""
    t = RealisticWsTransport()
    session = _make_session(t)

    session.send("hello")
    session._tools = [{"type": "function", "name": "newtool", "parameters": {}}]
    result = session.send("again")

    assert result.usage.extra["codex_request_mode"] == "ws_full"
    assert result.usage.extra["codex_ws_delta_reason"] == "non_input_fields_changed"
    assert "tools" in result.usage.extra["codex_ws_changed_fields"]
    # The diagnostic carries only key names + counts — no prompt/tool/secret body.
    blob = json.dumps(result.usage.extra, default=str)
    assert "hello" not in blob and "again" not in blob and "newtool" not in blob
    # Full input replayed over WS, no previous id.
    assert "previous_response_id" not in t.sent_frames[1]


def test_baseline_updates_after_each_successful_response():
    """After a successful turn the baseline advances so the NEXT turn chains off
    the latest response id, not the first."""
    t = RealisticWsTransport()
    session = _make_session(t)

    session.send("a")
    session.send("b")
    third = session.send("c")

    assert third.usage.extra["codex_request_mode"] == "ws_incremental"
    assert t.sent_frames[2]["previous_response_id"] == "resp_ws_2"


def test_failed_stream_restores_prior_baseline_and_next_turn_chains_off_it():
    """If a turn's stream fails mid-flight, the prior baseline is restored, so a
    retry after a fresh successful turn still chains off the last GOOD response —
    the failed turn never poisons the delta chain."""
    class _FailSecond(RealisticWsTransport):
        def stream(self, frame):
            self.sent_frames.append(frame)
            self._resp_counter += 1
            if self._resp_counter == 2:
                raise _CodexWsFallback("stream blew up after first")
            rid = f"resp_ws_{self._resp_counter}"
            yield Event("response.output_text.delta", delta="hi")
            yield Event("response.output_item.done", item=_MessageItem("hi"))
            yield _completed(rid)

    t = _FailSecond()
    session = _make_session(t)

    session.send("first")
    good_request = session._ws_session.last_request
    good_response = session._ws_session.last_response

    with pytest.raises(_CodexWsFallback):
        session.send("second")

    # Baseline restored to the last good turn; transport closed.
    assert session._ws_session.last_request == good_request
    assert session._ws_session.last_response == good_response
    assert session._ws_transport is None


def test_new_user_turn_after_tool_loop_keeps_incremental_chain():
    """Boundary: tool call -> tool result -> new user text all stay incremental
    within the same websocket session (no spurious reset breaks the delta)."""
    from lingtai_kernel.llm.interface import ToolResultBlock

    t = ToolCallWsTransport()
    session = _make_session(t)

    session.send("call the tool")  # ws_full (first)
    r2 = session.send([ToolResultBlock(id="call_a", name="do_x", content={"ok": True})])
    r3 = session.send("now continue in plain text")

    assert r2.usage.extra["codex_request_mode"] == "ws_incremental"
    assert r3.usage.extra["codex_request_mode"] == "ws_incremental"
    assert t.sent_frames[2]["previous_response_id"] == "resp_ws_2"
    flat3 = "".join(str(i) for i in t.sent_frames[2]["input"])
    assert "now continue in plain text" in flat3
    # Earlier turns are not replayed in the third delta.
    assert "call the tool" not in flat3


def test_codex_adapter_comment_explains_epoch_reset_and_summarize_delay():
    # WS-enabled path: the comment describes the previous_response_id epoch
    # reset and the ws_full/ws_incremental cache boundary that actually exists.
    session = _make_session(FakeWsTransport(), ws_enabled=True)

    comment = session.adapter_comment()

    assert comment["adapter"] == "codex"
    assert comment["ws_enabled"] is True
    assert comment["feature"] == "responses_websocket_epoch_reset"
    assert comment["epoch_reset_turns"] == 20
    assert comment["turns_since_epoch_reset"] == 0
    note = comment["summarize_ws_full_note"]
    assert "ws_full" in note
    assert "ws_incremental" in note
    assert "five turns" in note
    assert "Codex-specific" in note
    assert "summarize them together" in note
    # The note must also warn that notification dismiss/cleanup breaks the
    # incremental/cache chain and forces a fresh ws_full epoch, just like
    # summarize — so the agent does not assume only summarize is sensitive.
    assert "notification" in note
    assert "dismiss" in note


def test_codex_adapter_comment_is_honest_when_ws_disabled():
    # Stateless HTTP path (the default runtime): there is NO previous_response_id
    # chain and NO ws_full/ws_incremental boundary, so the comment must not claim
    # one. Every request is already a full stateless replay rebuilt from local
    # chat_history; summarize/dismiss take effect by shrinking that next full
    # request, not by toggling an incremental chain.
    session = _make_session(FakeWsTransport(), ws_enabled=False)

    comment = session.adapter_comment()

    assert comment["adapter"] == "codex"
    assert comment["ws_enabled"] is False
    # The inert WS epoch counter must not masquerade as a live signal.
    assert comment["feature"] != "responses_websocket_epoch_reset"
    assert "turns_since_epoch_reset" not in comment
    assert "next_reset_in" not in comment
    note = comment["summarize_ws_full_note"]
    # Stateless honesty: no incremental chain to preserve, every request is a
    # full replay; summarize and notification dismiss/cleanup both take effect
    # by shrinking the next full request from mutated local history.
    assert "stateless" in note
    # Must not claim a live ws_full/ws_incremental boundary; if it mentions the
    # term at all it is to say there is NO such chain.
    assert "ws_full epoch" not in note
    assert "no ws_incremental" in note
    assert "full replay" in note
    assert "summarize" in note
    assert "notification" in note
    assert "dismiss" in note


def test_history_summarized_forces_next_ws_full_epoch_reset():
    t = FakeWsTransport()
    session = _make_session(t)

    first = session.send("one")
    second = session.send("two")
    session.on_history_summarized(["call_old"])
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_request_mode"] == "ws_full"
    assert third.usage.extra["codex_ws_delta_reason"] == "epoch_reset"
    assert third.usage.extra["codex_ws_epoch_reset_reason"] == "summarize"
    assert "previous_response_id" not in t.sent_frames[2]


def test_notification_dismissed_forces_next_ws_full_epoch_reset():
    t = FakeWsTransport()
    session = _make_session(t)

    first = session.send("one")
    second = session.send("two")
    # A notification dismiss/cleanup rewrites the resident-meta off older
    # tool results, so — exactly like summarize — it must force the next
    # request to start a fresh ws_full epoch instead of ws_incremental.
    session.on_notification_dismissed("system")
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_request_mode"] == "ws_full"
    assert third.usage.extra["codex_ws_delta_reason"] == "epoch_reset"
    assert third.usage.extra["codex_ws_epoch_reset_reason"] == "notification_dismiss"
    assert "previous_response_id" not in t.sent_frames[2]


def test_ws_epoch_reset_default_limit_refreshes_for_existing_sessions(monkeypatch):
    t = FakeWsTransport()
    session = _make_session(t)
    # Simulate a live session object created before the default changed.
    session._ws_epoch_reset_turn_limit = 10
    session._ws_epoch_reset_turns_explicit = False
    session._ws_turns_since_epoch_reset = 5
    monkeypatch.setenv("LINGTAI_CODEX_WS_EPOCH_RESET_TURNS", "5")

    session.send("refresh default")

    assert session._ws_last_diag["reason"] == "epoch_reset"
    assert session._ws_last_diag["epoch_reset_turns"] == 5


def test_ws_epoch_reset_explicit_limit_is_not_refreshed(monkeypatch):
    t = FakeWsTransport()
    session = _make_session(t, ws_epoch_reset_turns=10)
    session._ws_turns_since_epoch_reset = 5
    monkeypatch.delenv("LINGTAI_CODEX_WS_EPOCH_RESET_TURNS", raising=False)

    session.send("keep explicit")

    assert session._ws_last_diag["reason"] == "no_baseline"
    assert session._ws_epoch_reset_turn_limit == 10


def test_ws_epoch_reset_forces_full_after_configured_successes():
    """Periodic epoch reset breaks the response-id chain after N WS successes.

    The reset intentionally pays one ``ws_full`` request, clears the frozen output
    map/baseline/transport, and then starts a fresh incremental chain.
    """

    t = FakeWsTransport()
    session = _make_session(t, ws_epoch_reset_turns=2)

    first = session.send("one")
    second = session.send("two")
    third = session.send("three")
    fourth = session.send("four")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert first.usage.extra["codex_ws_delta_reason"] == "no_baseline"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"

    assert third.usage.extra["codex_request_mode"] == "ws_full"
    assert third.usage.extra["codex_ws_delta_reason"] == "epoch_reset"
    assert third.usage.extra["codex_ws_epoch_reset_reason"] == "turn_count"
    assert third.usage.extra["codex_ws_epoch_reset_turns"] == "2"
    assert "previous_response_id" not in t.sent_frames[2]

    assert fourth.usage.extra["codex_request_mode"] == "ws_incremental"
    assert t.sent_frames[3]["previous_response_id"] == "resp_ws_3"


def test_ws_epoch_reset_clears_frozen_outputs_before_full_replay():
    """Reset clears PR #474's frozen map so stale meta is not replayed forever."""

    session = _make_session(FakeWsTransport(), ws_epoch_reset_turns=1)
    session._ws_frozen_outputs["call_old"] = "old meta payload"
    session._ws_turns_since_epoch_reset = 1

    session.send("after reset")

    assert "call_old" not in session._ws_frozen_outputs
    assert session._ws_turns_since_epoch_reset == 1
    assert session._ws_last_diag["reason"] == "epoch_reset"


# Imported here (not at top) so a missing symbol fails the import test loudly.
from lingtai.llm.openai.adapter import _CODEX_TURN_STATE_HEADER as _CodexTurnStateHeader  # noqa: E402
