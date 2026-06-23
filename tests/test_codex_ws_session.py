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
    return CodexResponsesSession(
        client=_HttpFallbackClient(),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        ws_enabled=True,
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


# Imported here (not at top) so a missing symbol fails the import test loudly.
from lingtai.llm.openai.adapter import _CODEX_TURN_STATE_HEADER as _CodexTurnStateHeader  # noqa: E402
