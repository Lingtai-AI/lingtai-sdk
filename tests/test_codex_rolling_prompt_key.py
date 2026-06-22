"""Tests for the Codex rolling prompt-cache-key experiment.

Background (Jason, 2026-06-22): the stable per-agent Codex affinity id pins one
sticky-warm cache slot for the life of the agent. This experiment trades that
for a *rolling* key: the FIRST request of a session still uses the stable
per-agent id (no previous API-call time exists yet), but every subsequent
request derives its key from ``hash(agent_anchor + previous_api_call_time)``.

The official invariant observed in fresh captures still holds within each
request: ``prompt_cache_key == session_id == thread_id`` (byte-identical). Only
``x-client-request-id`` / ``turn_id`` stay fresh-per-request UUIDs, exactly as
the official client and the pre-experiment code already emitted them.

The mode is a single reversible module switch (``_CODEX_PROMPT_KEY_MODE``):

  * ``"rolling_prev_call_time"`` (experiment default) -> roll after request #1.
  * ``"stable"``                                       -> old byte-stable id.

The previous API-call time is recorded per anchor at request START (a module
dict keyed by the stable base id) so request N derives from request N-1's start.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace

import lingtai.llm.openai.adapter as adapter_mod
from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    _codex_rolling_key,
    _codex_session_id,
)


@dataclass
class Event:
    type: str
    delta: str | None = None
    item: object | None = None
    response: object | None = None
    item_id: str | None = None
    text: str | None = None


class FakeResponses:
    def __init__(self, events: list[Event]):
        self.events = events
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        yield from self.events


class FakeClient:
    def __init__(self, events: list[Event]):
        self.responses = FakeResponses(events)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed() -> Event:
    return Event(
        "response.completed",
        response=SimpleNamespace(id="resp_fake", usage=_usage()),
    )


def _function_schema():
    from lingtai_kernel.llm.base import FunctionSchema

    return FunctionSchema(
        name="report_answer",
        description="Report answer",
        parameters={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )


def _create_codex_session_cfg(events, *, model="gpt-5.5", **adapter_kw):
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
        **adapter_kw,
    )
    adapter._client = FakeClient(events)
    return adapter.create_chat(
        model,
        "system prompt",
        tools=[_function_schema()],
        force_tool_call=True,
        thinking="high",
    )


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Module-level switch / helper invariants
# ---------------------------------------------------------------------------


def test_rolling_mode_is_the_experiment_default():
    """The experiment ships with rolling mode on by Jason's request."""
    assert adapter_mod._CODEX_PROMPT_KEY_MODE == "rolling_prev_call_time"


def test_codex_rolling_key_is_deterministic_for_same_inputs():
    """``hash(base + prev_time)`` is a pure function: same inputs -> same id."""
    a = _codex_rolling_key("abcd1234", 1_700_000_000_000)
    b = _codex_rolling_key("abcd1234", 1_700_000_000_000)
    assert a == b
    # Shaped like the stable id (8-char lowercase hex) so it drops into the
    # same session/thread/prompt_cache_key slot byte-identically.
    assert re.fullmatch(r"[0-9a-f]{8}", a)


def test_codex_rolling_key_varies_with_prev_time_and_anchor():
    base = "abcd1234"
    t0 = _codex_rolling_key(base, 1_700_000_000_000)
    t1 = _codex_rolling_key(base, 1_700_000_000_001)
    other = _codex_rolling_key("ffff0000", 1_700_000_000_000)
    assert t0 != t1  # different prev time -> different key
    assert t0 != other  # different anchor -> different key


# ---------------------------------------------------------------------------
# First request falls back to the stable per-agent key
# ---------------------------------------------------------------------------


def test_first_request_falls_back_to_stable_per_agent_key(monkeypatch):
    """No previous API-call time exists for request #1 -> stable id."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg([_completed()], codex_session_anchor=anchor)
    session.send("first")

    sent = session._client.responses.kwargs[0]
    headers = sent["extra_headers"]
    stable = _codex_session_id(anchor)
    assert sent["prompt_cache_key"] == stable
    assert headers["session_id"] == stable
    assert headers["thread_id"] == stable


# ---------------------------------------------------------------------------
# Second request rolls off the first request's recorded call time
# ---------------------------------------------------------------------------


def test_second_request_uses_hash_of_first_call_time(monkeypatch):
    """Request #2 derives ``hash(base + prev_call_time)`` from request #1's
    recorded start time; request #1 still uses the stable fallback."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")

    # Deterministic clock: request #1 starts at t=1000ms, request #2 at t=2000ms.
    times = iter([1000, 2000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg([_completed(), _completed()], codex_session_anchor=anchor)
    session.send("first")
    session.send("second")

    stable = _codex_session_id(anchor)
    expected_second = _codex_rolling_key(stable, 1000)  # rolls off req #1's start

    s0 = session._client.responses.kwargs[0]
    s1 = session._client.responses.kwargs[1]
    assert s0["prompt_cache_key"] == stable
    assert s1["prompt_cache_key"] == expected_second
    assert s1["prompt_cache_key"] != s0["prompt_cache_key"]


def test_third_request_rolls_off_second_call_time(monkeypatch):
    """Each request rolls off the immediately preceding request's start time."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")
    times = iter([1000, 2000, 3000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg(
        [_completed(), _completed(), _completed()], codex_session_anchor=anchor
    )
    session.send("first")
    session.send("second")
    session.send("third")

    stable = _codex_session_id(anchor)
    s1 = session._client.responses.kwargs[1]
    s2 = session._client.responses.kwargs[2]
    assert s1["prompt_cache_key"] == _codex_rolling_key(stable, 1000)
    assert s2["prompt_cache_key"] == _codex_rolling_key(stable, 2000)


# ---------------------------------------------------------------------------
# Within-request invariant: key == session_id == thread_id (every request)
# ---------------------------------------------------------------------------


def test_within_each_request_key_session_thread_are_byte_identical(monkeypatch):
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")
    times = iter([1000, 2000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg([_completed(), _completed()], codex_session_anchor=anchor)
    session.send("first")
    session.send("second")

    for sent in session._client.responses.kwargs:
        headers = sent["extra_headers"]
        key = sent["prompt_cache_key"]
        assert headers["session_id"] == key
        assert headers["thread_id"] == key
        # The window id and turn-metadata envelope track the same rolled id.
        assert headers["x-codex-window-id"] == f"{key}:0"
        turn = json.loads(headers["x-codex-turn-metadata"])
        assert turn["session_id"] == key
        assert turn["thread_id"] == key


def test_rolling_preserves_fresh_per_request_client_request_id(monkeypatch):
    """``x-client-request-id`` was NOT tied to the cache key before this
    experiment (it is a fresh UUID per request) and stays that way — rolling
    only moves the cache-affinity trio, not the request id."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")
    times = iter([1000, 2000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg([_completed(), _completed()], codex_session_anchor=anchor)
    session.send("first")
    session.send("second")

    h0 = session._client.responses.kwargs[0]["extra_headers"]
    h1 = session._client.responses.kwargs[1]["extra_headers"]
    assert _UUID_RE.match(h0["x-client-request-id"])
    assert _UUID_RE.match(h1["x-client-request-id"])
    assert h0["x-client-request-id"] != h1["x-client-request-id"]


# ---------------------------------------------------------------------------
# Stable mode restores the old byte-stable behavior
# ---------------------------------------------------------------------------


def test_stable_mode_restores_byte_stable_key_across_requests(monkeypatch):
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "stable")
    times = iter([1000, 2000, 3000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg(
        [_completed(), _completed(), _completed()], codex_session_anchor=anchor
    )
    session.send("a")
    session.send("b")
    session.send("c")

    stable = _codex_session_id(anchor)
    keys = [kw["prompt_cache_key"] for kw in session._client.responses.kwargs]
    assert keys == [stable, stable, stable]


# ---------------------------------------------------------------------------
# Independent agents roll independently / explicit override never rolls
# ---------------------------------------------------------------------------


def test_rolling_keys_are_independent_per_agent(monkeypatch):
    """Two agents keep separate previous-call-time state -> separate rolls."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")
    # alice: req1 @1000, req2 @2000 ; bob: req1 @1500, req2 @2500.
    times = iter([1000, 1500, 2000, 2500])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    alice = _create_codex_session_cfg(
        [_completed(), _completed()], codex_session_anchor="/agents/alice/init.json"
    )
    bob = _create_codex_session_cfg(
        [_completed(), _completed()], codex_session_anchor="/agents/bob/init.json"
    )
    alice.send("a1")
    bob.send("b1")
    alice.send("a2")
    bob.send("b2")

    a_stable = _codex_session_id("/agents/alice/init.json")
    b_stable = _codex_session_id("/agents/bob/init.json")
    a2 = alice._client.responses.kwargs[1]["prompt_cache_key"]
    b2 = bob._client.responses.kwargs[1]["prompt_cache_key"]
    assert a2 == _codex_rolling_key(a_stable, 1000)
    assert b2 == _codex_rolling_key(b_stable, 1500)
    assert a2 != b2


def test_bare_session_without_anchor_never_rolls_and_sends_no_headers(monkeypatch):
    """No per-agent identity -> the bare/model-only path is untouched by rolling
    and still emits no session/thread headers."""
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")

    session = _create_codex_session_cfg([_completed(), _completed()])  # no anchor
    session.send("a")
    session.send("b")

    for sent in session._client.responses.kwargs:
        headers = sent.get("extra_headers") or {}
        assert "session_id" not in headers
        assert "thread_id" not in headers
        assert sent["prompt_cache_key"] == "lingtai-codex:gpt-5.5:v1"


# ---------------------------------------------------------------------------
# No legacy codex-cache-key resurrected by this experiment
# ---------------------------------------------------------------------------


def test_rolling_never_emits_legacy_codex_cache_key_header(monkeypatch):
    adapter_mod._reset_codex_prev_call_times()
    monkeypatch.setattr(adapter_mod, "_CODEX_PROMPT_KEY_MODE", "rolling_prev_call_time")
    times = iter([1000, 2000])
    monkeypatch.setattr(adapter_mod, "_codex_now_ms", lambda: next(times))

    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg([_completed(), _completed()], codex_session_anchor=anchor)
    session.send("a")
    session.send("b")

    for sent in session._client.responses.kwargs:
        blob = json.dumps(sent, default=str)
        assert "codex-cache-key" not in blob
