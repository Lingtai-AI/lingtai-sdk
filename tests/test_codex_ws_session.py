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

from lingtai_kernel.llm.base import UsageMetadata

from lingtai.llm.openai.codex_ws import SyncCodexWebsocketTransport

from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    CodexResponsesSession,
    _CodexWsFallback,
    _CODEX_TRANSPORT_DEFAULT,
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


def test_ws_explicitly_disabled_uses_http():
    """With the gate explicitly off, the session uses the HTTP path (no transport)."""
    session = CodexResponsesSession(
        client=_HttpFallbackClient(),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        ws_enabled=False,
    )

    session.send("hello")

    assert len(session._client.responses.kwargs) == 1
    assert session._client.responses.kwargs[0]["store"] is False


# ---------------------------------------------------------------------------
# Transport axis: REST is hardcoded for normal runtime. There is NO environment
# variable that selects the transport — an inherited ``LINGTAI_CODEX_WS=1`` or
# ``LINGTAI_CODEX_TRANSPORT=websocket`` must NOT flip the adapter to WebSocket.
# WebSocket is reachable only via the explicit ``transport=``/``ws_enabled=``
# constructor kwarg (tests / internal / future).
# ---------------------------------------------------------------------------


def _make_default_session(**kwargs):
    """A Codex session with NO explicit transport kwarg (uses the runtime default)."""
    transport = kwargs.pop("ws_transport", None) or FakeWsTransport()
    session = CodexResponsesSession(
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
    return session, transport


def test_runtime_default_transport_is_rest_constant():
    """The hardcoded normal-runtime transport default is REST."""
    assert _CODEX_TRANSPORT_DEFAULT == "rest"


def test_default_session_is_rest_and_does_not_touch_ws_transport():
    """With no explicit transport kwarg the session is REST and never uses the WS
    transport factory."""
    session, transport = _make_default_session()

    assert session._transport == "rest"
    assert session._ws_enabled is False
    assert session._continuation_enabled is True
    session.send("hello")

    # REST path used: HTTP client saw one full request; WS transport untouched.
    assert transport.sent_frames == []
    assert len(session._client.responses.kwargs) == 1
    first = session._client.responses.kwargs[0]
    assert first["store"] is False
    assert "previous_response_id" not in first


@pytest.mark.parametrize(
    "env",
    [
        {"LINGTAI_CODEX_WS": "1"},
        {"LINGTAI_CODEX_WS": "true"},
        {"LINGTAI_CODEX_TRANSPORT": "websocket"},
        {"LINGTAI_CODEX_TRANSPORT": "ws"},
        {"LINGTAI_CODEX_WS": "1", "LINGTAI_CODEX_TRANSPORT": "websocket"},
    ],
)
def test_env_vars_do_not_flip_runtime_to_websocket(monkeypatch, env):
    """No transport env var switches the runtime: even ``LINGTAI_CODEX_WS=1`` and/or
    ``LINGTAI_CODEX_TRANSPORT=websocket`` leave the default session on REST and never
    touch the WS transport."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    session, transport = _make_default_session()

    assert session._transport == "rest"
    assert session._ws_enabled is False
    session.send("hello")

    # Still REST: WS transport factory never used, HTTP client saw the request.
    assert transport.sent_frames == []
    assert len(session._client.responses.kwargs) == 1


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LINGTAI_CODEX_WS": "1"},
        {"LINGTAI_CODEX_TRANSPORT": "rest"},
        {"LINGTAI_CODEX_TRANSPORT": "websocket"},
    ],
)
def test_explicit_websocket_kwarg_selects_ws_regardless_of_env(monkeypatch, env):
    """WebSocket is still reachable via the explicit ``transport="websocket"`` kwarg
    (tests / internal), independent of any env var."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    session, transport = _make_default_session(transport="websocket")

    assert session._transport == "websocket"
    assert session._ws_enabled is True
    session.send("hello")

    # WS path used: the injected transport saw the frame, HTTP fallback untouched.
    assert transport.sent_frames
    assert len(session._client.responses.kwargs) == 0


def test_explicit_rest_kwarg_selects_rest_regardless_of_env(monkeypatch):
    """An explicit ``transport="rest"`` kwarg stays REST even with WS env set."""
    monkeypatch.setenv("LINGTAI_CODEX_WS", "1")
    monkeypatch.setenv("LINGTAI_CODEX_TRANSPORT", "websocket")

    session, transport = _make_default_session(transport="rest")

    assert session._transport == "rest"
    assert session._ws_enabled is False
    session.send("hello")
    assert transport.sent_frames == []
    assert len(session._client.responses.kwargs) == 1


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


# ---------------------------------------------------------------------------
# REST transport runs the SAME full->incremental planner (the corrected default).
# These mirror the WebSocket realistic-output tests above but drive the request
# through ``client.responses.create`` instead of the websocket wire.
# ---------------------------------------------------------------------------


class RealisticRestResponses:
    """``client.responses.create`` stub that streams a realistic assistant turn.

    Records every request kwargs and yields a text delta + an OUTPUT-schema
    message item + ``response.completed`` with a distinct, incrementing response
    id per call. REST incremental records that id for baseline decisions but does
    not send it on the wire; the REST request remains self-contained.
    """

    def __init__(self, *, text="hi there", fail_incremental=False):
        self.kwargs: list[dict] = []
        self._text = text
        self._counter = 0
        self._fail_incremental = fail_incremental

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        # Optionally model a backend that would reject any accidental REST
        # previous_response_id. Correct REST incremental never sends it.
        if self._fail_incremental and kwargs.get("previous_response_id"):
            raise RuntimeError("Unsupported parameter: previous_response_id")
        self._counter += 1
        rid = f"resp_rest_{self._counter}"

        def _gen():
            yield Event("response.output_text.delta", delta=self._text)
            yield Event("response.output_item.done", item=_MessageItem(self._text))
            yield _completed(rid)

        return _gen()


class RealisticRestClient:
    def __init__(self, **kw):
        self.responses = RealisticRestResponses(**kw)


def _make_rest_session(client, **kwargs):
    """A Codex session pinned to the REST transport with the given fake client."""
    kwargs.setdefault("transport", "rest")
    return CodexResponsesSession(
        client=client,
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="sess-stable",
        thread_id="sess-stable",
        # A WS factory is injected to prove the REST path never touches it.
        ws_transport_factory=lambda url, headers: FakeWsTransport(),
        **kwargs,
    )


def test_rest_first_full_then_second_incremental_replays_full_input():
    """Default REST transport: first turn full; second turn is logically
    incremental because the prefix is unchanged, but REST still sends full input.

    WebSocket incremental is the transport mode that sends delta + previous id.
    REST incremental is a cache/epoch semantic over self-contained requests.
    """
    client = RealisticRestClient()
    session = _make_rest_session(client)

    first = session.send("hello")
    assert first.usage.extra["codex_transport"] == "rest"
    assert first.usage.extra["codex_request_mode"] == "rest_full"
    assert first.usage.extra["codex_transfer_mode"] == "full"
    assert first.usage.extra["codex_ws_delta_reason"] == "no_baseline"
    assert "previous_response_id" not in client.responses.kwargs[0]
    assert client.responses.kwargs[0]["store"] is False

    second = session.send("again")
    assert second.usage.extra["codex_transport"] == "rest"
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert second.usage.extra["codex_transfer_mode"] == "incremental"
    assert second.usage.extra["codex_ws_delta_reason"] == "ok"
    # Second REST request is self-contained/full input, but no previous_response_id.
    second_kwargs = client.responses.kwargs[1]
    assert second_kwargs["store"] is False
    assert "previous_response_id" not in second_kwargs
    flat = "".join(str(i) for i in second_kwargs["input"])
    assert "hello" in flat and "hi there" in flat
    assert "again" in flat
    # The planner still saw a strict additive extension.
    assert int(second.usage.extra["codex_ws_delta_len"]) == 1
    # The WebSocket transport was never used on the REST path.
    assert session._ws_transport is None


def test_rest_prefix_mismatch_falls_back_to_full():
    """A non-input field change (tools differ) on REST forces a full request with a
    safe diagnostic — the same planner decision the WS path makes."""
    client = RealisticRestClient()
    session = _make_rest_session(client)

    session.send("hello")  # rest_full (first)
    # Mutate a non-input field so the strict-extension check fails next turn.
    session._tools = [{"type": "function", "name": "x", "parameters": {}}]
    result = session.send("again")

    assert result.usage.extra["codex_request_mode"] == "rest_full"
    assert result.usage.extra["codex_transfer_mode"] == "full"
    assert result.usage.extra["codex_ws_delta_reason"] == "non_input_fields_changed"
    assert "previous_response_id" not in client.responses.kwargs[1]


def test_rest_incremental_never_sends_previous_response_id():
    """REST incremental remains self-contained and therefore never triggers a
    backend rejection for `previous_response_id`; WS is the only transport that
    carries that field.
    """
    client = RealisticRestClient(fail_incremental=True)
    session = _make_rest_session(client)

    first = session.send("hello")
    assert first.usage.extra["codex_request_mode"] == "rest_full"

    second = session.send("again")
    assert len(client.responses.kwargs) == 2
    assert "previous_response_id" not in client.responses.kwargs[1]
    assert client.responses.kwargs[1]["store"] is False
    flat = "".join(str(i) for i in client.responses.kwargs[1]["input"])
    assert "hello" in flat and "again" in flat  # full replay on the wire
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert second.usage.extra["codex_transfer_mode"] == "incremental"
    assert "codex_fallback_error_type" not in second.usage.extra


def test_rest_summarize_delays_epoch_reset_below_context_threshold(monkeypatch):
    """REST also delays the Codex epoch reset label until local context pressure
    reaches the threshold; summarize still returns normally before then."""
    client = RealisticRestClient()
    session = _make_rest_session(client)
    monkeypatch.setattr(session._interface, "estimate_context_tokens", lambda: 100)
    monkeypatch.setattr(session, "context_window", lambda: 1000)

    first = session.send("one")
    second = session.send("two")
    session.on_history_summarized(["call_old"])
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "rest_full"
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert third.usage.extra["codex_request_mode"] == "rest_incremental"
    assert third.usage.extra["codex_ws_delta_reason"] == "ok"
    assert session.dynamic_adapter_comment()["summarize_effect"]["delayed"] is True


def test_rest_summarize_releases_delayed_epoch_reset_at_context_threshold(monkeypatch):
    client = RealisticRestClient()
    session = _make_rest_session(client)
    monkeypatch.setattr(session._interface, "estimate_context_tokens", lambda: 850)
    monkeypatch.setattr(session, "context_window", lambda: 1000)

    first = session.send("one")
    second = session.send("two")
    session.on_history_summarized(["call_old"])
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "rest_full"
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert third.usage.extra["codex_request_mode"] == "rest_full"
    assert third.usage.extra["codex_ws_delta_reason"] == "epoch_reset"
    assert third.usage.extra["codex_ws_epoch_reset_reason"] == "summarize_delayed"


class _MultiToolCallRestResponses:
    """``responses.create`` stub: turn 1 emits TWO parallel function_calls; later
    turns emit text. Mirrors a model that fans out several tool calls at once."""

    def __init__(self):
        self.kwargs: list[dict] = []
        self._counter = 0

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        self._counter += 1
        rid = f"resp_rest_{self._counter}"

        def _gen():
            if self._counter == 1:
                for cid in ("call_a", "call_b"):
                    yield Event(
                        "response.output_item.added",
                        item=_ToolCallItem(call_id=cid),
                    )
                    yield Event(
                        "response.function_call_arguments.delta", delta='{"a": 1}'
                    )
                    yield Event(
                        "response.output_item.done",
                        item=_ToolCallItem(call_id=cid),
                    )
            else:
                yield Event("response.output_text.delta", delta="done")
            yield _completed(rid)

        return _gen()


class _MultiToolCallRestClient:
    def __init__(self):
        self.responses = _MultiToolCallRestResponses()


def test_rest_parallel_multi_tool_result_stays_incremental():
    """Regression for the ``prefix_mismatch`` ``function_call_output`` vs
    ``function_call`` full-rate: an assistant turn that emits SEVERAL parallel
    ``function_call``s, answered together next turn, must stay ``rest_incremental``.

    The wire-layer guard ``_pair_responses_orphan_function_calls`` injects an
    orphan-output placeholder for each unanswered call. Before the fix it
    interleaved that placeholder right after each ``function_call`` while
    ``to_responses_input`` groups all calls then all real outputs, so the
    placeholder positions drifted relative to where the real outputs land — the
    strict prefix broke and forced ``rest_full`` with the logged signature
    ``mismatch_prev_type=function_call_output`` / ``mismatch_cur_type=function_call``.
    The fix appends placeholders contiguously at the tail and strips them from the
    recorded baseline, so the real tool results strictly extend the baseline.
    """
    from lingtai_kernel.llm.interface import ToolResultBlock

    client = _MultiToolCallRestClient()
    session = _make_rest_session(client)

    first = session.send("call two tools")  # turn 1: emits call_a + call_b
    assert first.usage.extra["codex_request_mode"] == "rest_full"

    # Answer both parallel calls together — a strict additive extension of the
    # baseline. With placeholders interleaved (pre-fix) this collapsed to
    # rest_full; with tail placement + baseline-strip it stays incremental.
    second = session.send([
        ToolResultBlock(id="call_a", name="do_x", content={"ok": "a"}),
        ToolResultBlock(id="call_b", name="do_x", content={"ok": "b"}),
    ])
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert second.usage.extra["codex_ws_delta_reason"] == "ok"


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


def test_codex_adapter_comment_explains_delayed_summarize_without_soft_constraints():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("hello")
    session.send("again")
    session.on_history_summarized(["call_old"])
    comment = session.adapter_comment()
    static = session.static_adapter_comment()

    note = comment["cache_note"]
    assert "Responses API" in note
    assert "fresh full replay/cache epoch effect is delayed" in note
    assert "previous_response_id/cache epoch" in note
    assert "roughly 80%" in note
    assert "additional summarize calls safely" in note
    assert "do not summarize first unless context overflow is imminent" in note

    summarize_effect = comment["summarize_effect"]
    assert summarize_effect["delayed"] is True
    assert summarize_effect["pending_count"] == 1
    assert summarize_effect["threshold_ratio"] == 0.8

    for payload in (comment, static):
        assert "context_budget_note" not in payload
        assert "summarize_economy_note" not in payload
        assert "cache_ledger" not in payload
        assert "cache_ledger_summary" not in payload
        assert "maintenance_hint" not in payload
        assert "last_full_reason" not in payload
        assert "last_ws_full_reason" not in payload
    assert "1:10" not in note
    assert "150k" not in note
    assert "200k" not in note
    assert "full:incremental" not in note
    assert "reduce_summarize_frequency" not in note


def test_codex_adapter_static_comment_available_before_chat_creation():
    adapter = CodexOpenAIAdapter(api_key="test")

    comment = adapter.static_adapter_comment()

    assert comment["adapter"] == "codex"
    assert comment["feature"] == "responses_rest_epoch_reset"
    assert ">=20 API calls" not in comment["summarize_note"]
    assert "Responses API" in comment["summarize_note"]
    assert "fresh full replay/cache epoch effect is delayed" in comment["summarize_note"]
    assert "previous_response_id/cache epoch" in comment["summarize_note"]
    assert "roughly 80%" in comment["summarize_note"]
    assert "do not summarize first unless context overflow is imminent" in comment["summarize_note"]
    assert "context_budget_note" not in comment
    assert "summarize_economy_note" not in comment
    assert "1:10" not in comment["summarize_note"]
    assert "150k" not in comment["summarize_note"]
    assert "200k" not in comment["summarize_note"]
    assert "reduce_summarize_frequency" not in comment["summarize_note"]
    assert "prefer daemon/file-based exploration" in comment["long_context_strategy"]
    assert "main Codex context" in comment["long_context_strategy"]
    assert "does not compact history" in comment["notification_dismiss_note"]
    assert "coalesce dismissals with useful work" in comment["notification_dismiss_note"]
    assert "FROZEN" in comment["system_prompt_update_note"]
    assert "take effect immediately" in comment["system_prompt_update_note"]
    assert "fresh epoch / cache cost" in comment["system_prompt_update_note"]


def test_codex_update_system_prompt_is_no_op_and_keeps_instructions_frozen():
    """em-7: ``update_system_prompt`` is a no-op on the Codex/Responses path, so a
    system-prompt change does NOT mutate ``instructions`` mid-session — it stays
    frozen until a new session is built (molt/refresh/restart/bootstrap). This
    means a pad/system-prompt edit preserves the input-prefix continuation and
    cache rather than forcing a fresh epoch."""
    client = RealisticRestClient()
    session = _make_rest_session(client)

    assert session._instructions == "system prompt"
    first = session.send("hello")
    assert first.usage.extra["codex_request_mode"] == "rest_full"

    # A mid-session system-prompt update must NOT change the wire instructions.
    session.update_system_prompt("a brand new and very different system prompt")
    assert session._instructions == "system prompt"

    second = session.send("again")
    # instructions field is unchanged across turns, so the strict-extension holds
    # and the turn stays incremental (no instruction-driven epoch reset).
    assert client.responses.kwargs[1]["instructions"] == "system prompt"
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"


def test_codex_cache_ledger_stays_internal_not_adapter_comment():
    t = FakeWsTransport()
    session = _make_session(t)

    session.send("one")
    session.send("two")
    session.on_history_summarized(["call_old"])

    ledger = session._ws_cache_ledger_comment()
    assert isinstance(ledger, dict)
    assert ledger["legend"]["sumd"] == "epoch_reset:summarize_delayed"
    assert isinstance(ledger["rows"], list)
    comment = session.adapter_comment()
    assert "cache_ledger" not in comment
    assert "cache_ledger_summary" not in comment
    assert "maintenance_hint" not in comment


def test_codex_adapter_comment_is_rest_continuation_without_soft_constraints():
    client = RealisticRestClient()
    session = _make_rest_session(client)
    comment = session.dynamic_adapter_comment()

    assert comment["transport"] == "rest"
    assert comment["ws_enabled"] is False
    assert comment["continuation_enabled"] is True
    assert "periodic_reset_turns" not in comment
    assert "cache_ledger" not in comment
    assert "cache_ledger_summary" not in comment
    assert "maintenance_hint" not in comment


def test_codex_adapter_comment_stateless_only_when_continuation_off():
    t = FakeWsTransport()
    session = _make_session(t)
    session._continuation_enabled = False

    comment = session.dynamic_adapter_comment()
    static = session.static_adapter_comment()

    assert comment["continuation_enabled"] is False
    assert "cache_ledger" not in comment
    assert "cache_ledger_summary" not in comment
    assert "maintenance_hint" not in comment
    assert "summarize_effect" not in comment
    assert "fresh full replay/cache epoch effect is delayed" not in static["summarize_note"]
    assert "context_budget_note" not in static
    assert "summarize_economy_note" not in static


def test_history_summarized_delays_next_ws_full_below_context_threshold(monkeypatch):
    t = FakeWsTransport()
    session = _make_session(t)
    monkeypatch.setattr(session._interface, "estimate_context_tokens", lambda: 100)
    monkeypatch.setattr(session, "context_window", lambda: 1000)

    first = session.send("one")
    second = session.send("two")
    session.on_history_summarized(["call_old"])

    comment = session.dynamic_adapter_comment()
    summarize_effect = comment["summarize_effect"]
    assert summarize_effect["delayed"] is True
    assert summarize_effect["pending_count"] == 1
    assert summarize_effect["threshold_ratio"] == 0.8
    assert summarize_effect["threshold_context_tokens"] == 800
    assert summarize_effect["current_context_usage"] == 0.1
    assert "additional summarize calls safely" in summarize_effect["message"]

    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_ws_delta_reason"] == "ok"
    assert "codex_ws_epoch_reset_reason" not in third.usage.extra
    assert t.sent_frames[2]["previous_response_id"] == "resp_ws_2"


def test_history_summarized_releases_delayed_ws_full_at_context_threshold(monkeypatch):
    t = FakeWsTransport()
    session = _make_session(t)
    monkeypatch.setattr(session._interface, "estimate_context_tokens", lambda: 850)
    monkeypatch.setattr(session, "context_window", lambda: 1000)

    first = session.send("one")
    second = session.send("two")
    session.on_history_summarized(["call_old"])
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_request_mode"] == "ws_full"
    assert third.usage.extra["codex_ws_delta_reason"] == "epoch_reset"
    assert third.usage.extra["codex_ws_epoch_reset_reason"] == "summarize_delayed"
    assert "previous_response_id" not in t.sent_frames[2]

    summarize_effect = session.dynamic_adapter_comment()["summarize_effect"]
    assert summarize_effect["delayed"] is False
    assert summarize_effect["released_by"] == "summarize_delayed"


def test_notification_dismissed_keeps_incremental_ws_chain():
    t = FakeWsTransport()
    session = _make_session(t)

    first = session.send("one")
    second = session.send("two")
    # Notification dismiss/cleanup is high-frequency housekeeping. It should
    # not reset the Codex previous_response_id chain; only summarize compacts
    # old tool-result payloads enough to require a fresh ws_full epoch.
    session.on_notification_dismissed("system")
    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "ws_full"
    assert second.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_request_mode"] == "ws_incremental"
    assert third.usage.extra["codex_ws_delta_reason"] == "ok"
    assert "codex_ws_epoch_reset_reason" not in third.usage.extra
    assert t.sent_frames[2]["previous_response_id"] == "resp_ws_2"


def test_rest_notification_dismissed_is_noop_and_keeps_incremental():
    """On the production REST transport, a notification dismiss must be a no-op:
    it must NOT open a fresh full epoch, leave the in-memory baseline untouched,
    and the next request must stay ``rest_incremental``. Pins the intended
    behavior (``on_notification_dismissed`` is a deliberate no-op for Codex)
    against accidental re-arming of the dismiss → fresh-epoch footgun.
    """
    client = RealisticRestClient()
    session = _make_rest_session(client)

    first = session.send("one")
    second = session.send("two")
    baseline_before = session._ws_session.last_response

    session.on_notification_dismissed("system")
    # Dismiss must not rotate the baseline or queue an epoch reset.
    assert session._ws_session.last_response is baseline_before
    assert session._ws_epoch_reset_reason_pending is None

    third = session.send("three")

    assert first.usage.extra["codex_request_mode"] == "rest_full"
    assert second.usage.extra["codex_request_mode"] == "rest_incremental"
    assert third.usage.extra["codex_request_mode"] == "rest_incremental"
    assert third.usage.extra["codex_ws_delta_reason"] == "ok"
    assert "codex_ws_epoch_reset_reason" not in third.usage.extra


def test_ws_epoch_reset_disabled_by_default_no_turn_count_full(monkeypatch):
    """The routine turn-count epoch reset is cancelled.

    With no env override and no explicit kwarg, the default limit is 0
    (disabled). No matter how many turns elapse, the continuation chain is never
    broken by turn count alone — only summarize / prefix-mismatch / no-baseline
    open a fresh full epoch. This is Jason's decision: no frequent turn-count
    maintenance full.
    """

    monkeypatch.delenv("LINGTAI_CODEX_WS_EPOCH_RESET_TURNS", raising=False)
    t = FakeWsTransport()
    session = _make_session(t)

    assert session._ws_epoch_reset_turn_limit == 0

    responses = [session.send(f"turn {i}") for i in range(25)]

    # First is the bootstrap full (no baseline); every subsequent turn stays
    # incremental — no turn_count epoch reset is ever forced.
    assert responses[0].usage.extra["codex_request_mode"] == "ws_full"
    assert all(
        r.usage.extra["codex_request_mode"] == "ws_incremental" for r in responses[1:]
    )
    assert all(
        r.usage.extra.get("codex_ws_epoch_reset_reason") != "turn_count"
        for r in responses
    )


def test_ws_epoch_reset_env_override_is_conservative_safety_only(monkeypatch):
    """A turn-count limit can still be opted into via the env escape hatch.

    This is a conservative operator-only safety valve, NOT the routine summarize
    cadence. Existing live sessions pick up an env-set limit on the next send.
    """
    t = FakeWsTransport()
    session = _make_session(t)
    # Simulate a live session object with the default (disabled) limit.
    session._ws_epoch_reset_turn_limit = 0
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
    """An EXPLICITLY opted-in turn-count limit still breaks the chain after N
    WS successes.

    The routine turn-count reset is cancelled (default 0 / disabled). This
    conservative safety valve is reachable only by passing an explicit
    ``ws_epoch_reset_turns`` (or the env override); when set, the reset
    intentionally pays one ``ws_full`` request, clears the frozen output
    map/baseline/transport, and then starts a fresh incremental chain. This is
    NOT the normal summarize cadence.
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


def test_ws_epoch_reset_countdown_fields_reappear_only_when_opted_in():
    """The resident-meta countdown fields are surfaced only for an opted-in limit.

    Disabled (default 0): the fields are omitted so the meta never implies a
    forced reset. Opted in (explicit ``ws_epoch_reset_turns``): they reappear and
    describe the real countdown — the conservative safety valve, not the normal
    cadence.
    """
    disabled = _make_session(FakeWsTransport())
    disabled_comment = disabled.dynamic_adapter_comment()
    assert "epoch_reset_turns" not in disabled_comment
    assert "turns_since_epoch_reset" not in disabled_comment
    assert "next_reset_in" not in disabled_comment

    opted_in = _make_session(FakeWsTransport(), ws_epoch_reset_turns=50)
    opted_in_comment = opted_in.dynamic_adapter_comment()
    assert opted_in_comment["epoch_reset_turns"] == 50
    assert opted_in_comment["turns_since_epoch_reset"] == 0
    assert opted_in_comment["next_reset_in"] == 50


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
