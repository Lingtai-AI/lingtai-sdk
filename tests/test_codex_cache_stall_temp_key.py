"""Tests for the Codex affinity-id model (Jason's final #406 semantics).

The Codex Responses adapter uses one *current* affinity id, used byte-identically
for ``prompt_cache_key`` / ``session-id`` / ``thread-id``. That id has exactly
TWO rotation triggers:

  1. start/refresh — the adapter is (re)built, which stamps a fresh epoch; the
     current id is ``hash(anchor + epoch)`` and stays fixed for the life of that
     adapter/session instance (every request inside it uses the same id).
  2. 10-call cache corruption — when the backend returns the SAME positive
     ``cached_tokens`` for ten requests in a row, the cache slot has stalled, so
     the session ROTATES its current id (a persistent replacement, derived via
     the same ``hash(anchor + epoch)`` helper with a fresh epoch), clears the
     queue, and keeps using the new id for ALL subsequent requests until the next
     start/refresh or the next 10-call corruption.

There is no one-shot "temporary id then revert" concept: once rotated, the new
id stays in force. On a rotate the session emits a ``codex_cache_affinity_rotated``
event to ``logs/events.jsonl`` carrying only safe metadata (no token values
beyond the recent cached-hit list, no prompt body, no secrets, and not the
anchor path).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    CodexResponsesSession,
    _codex_affinity_id,
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
    def __init__(self, events_per_call: list[list[Event]]):
        self._events_per_call = events_per_call
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.kwargs)
        self.kwargs.append(kwargs)
        yield from self._events_per_call[idx]


class FakeClient:
    def __init__(self, events_per_call: list[list[Event]]):
        self.responses = FakeResponses(events_per_call)


def _usage(cached: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed(cached: int) -> list[Event]:
    return [
        Event(
            "response.completed",
            response=SimpleNamespace(id="resp_fake", usage=_usage(cached)),
        )
    ]


ANCHOR = "/agents/alice/init.json"
EPOCH0 = 1_700_000_000  # adapter-build epoch -> the start/refresh current id


def _make_session(cached_per_call, *, events=None, clock=None, epoch=EPOCH0):
    """Build a CodexResponsesSession with an epoch-stamped current id.

    ``cached_per_call`` is a list of cached_tokens numbers, one per send().
    ``events`` is an optional list to capture emitted events.
    ``clock`` is an optional zero-arg callable returning epoch seconds (used for
    the rotate epoch and event ts). ``epoch`` is the build epoch baked into the
    start/refresh current id.
    """
    client = FakeClient([_completed(c) for c in cached_per_call])
    current = _codex_affinity_id(ANCHOR, epoch)
    kw = dict(
        client=client,
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key=current,
        session_id=current,
        thread_id=current,
        affinity_anchor=ANCHOR,
    )
    if events is not None:
        kw["event_sink"] = events.append
    if clock is not None:
        kw["time_fn"] = clock
    return CodexResponsesSession(**kw), current


# ---------------------------------------------------------------------------
# Helper: deterministic, epoch-sensitive, reused everywhere.
# ---------------------------------------------------------------------------


def test_affinity_id_helper_is_deterministic():
    """Same (anchor, epoch) -> same 8-char lowercase-hex id."""
    a = _codex_affinity_id(ANCHOR, EPOCH0)
    b = _codex_affinity_id(ANCHOR, EPOCH0)
    assert a == b
    assert len(a) == 8
    assert a == a.lower()
    assert all(c in "0123456789abcdef" for c in a)


def test_affinity_id_helper_changes_with_epoch():
    """A different epoch (a new adapter build / a rotate) -> a different id."""
    a = _codex_affinity_id(ANCHOR, EPOCH0)
    b = _codex_affinity_id(ANCHOR, EPOCH0 + 1)
    assert a != b


def test_affinity_id_helper_changes_with_anchor():
    """A different agent anchor -> a different id (same epoch)."""
    a = _codex_affinity_id("/agents/alice/init.json", EPOCH0)
    b = _codex_affinity_id("/agents/bob/init.json", EPOCH0)
    assert a != b


def test_affinity_id_truncates_epoch_to_whole_seconds():
    """Sub-second epoch jitter does not change the id."""
    assert _codex_affinity_id(ANCHOR, EPOCH0) == _codex_affinity_id(ANCHOR, EPOCH0 + 0.4)


# ---------------------------------------------------------------------------
# start/refresh: two builds (different epoch) -> different current id; stable
# within one session across many requests.
# ---------------------------------------------------------------------------


def test_two_builds_different_epoch_yield_different_current_id_same_anchor():
    """Same anchor, different build epoch -> different current id (refresh rotate)."""
    id_a = _codex_affinity_id(ANCHOR, EPOCH0)
    id_b = _codex_affinity_id(ANCHOR, EPOCH0 + 7)
    assert id_a != id_b


def test_current_id_stable_within_one_session_across_requests():
    """Every request in one session (no corruption) uses the SAME current id."""
    session, current = _make_session([10, 20, 30, 40, 50, 60])

    for _ in range(6):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        assert sent["prompt_cache_key"] == current
        assert sent["extra_headers"]["session-id"] == current
        assert sent["extra_headers"]["thread-id"] == current


def test_adapter_build_epoch_drives_current_id_and_changes_on_rebuild():
    """CodexOpenAIAdapter stamps an injected epoch into the current id.

    Two adapters over the SAME anchor but different build epochs (a refresh)
    derive different current ids; both use them byte-identically for all three.
    """
    def _adapter(epoch):
        a = CodexOpenAIAdapter(
            api_key="fake",
            base_url="http://fake",
            use_responses=True,
            force_responses=True,
            codex_session_anchor=ANCHOR,
            codex_epoch=epoch,
        )
        a._client = FakeClient([_completed(0)])
        return a

    a0 = _adapter(EPOCH0)
    a1 = _adapter(EPOCH0 + 99)
    s0 = a0.create_chat("gpt-5.5", "system prompt")
    s1 = a1.create_chat("gpt-5.5", "system prompt")
    s0.send("hi")
    s1.send("hi")

    sent0 = a0._client.responses.kwargs[0]
    sent1 = a1._client.responses.kwargs[0]
    id0 = _codex_affinity_id(ANCHOR, EPOCH0)
    id1 = _codex_affinity_id(ANCHOR, EPOCH0 + 99)
    assert id0 != id1
    assert sent0["prompt_cache_key"] == sent0["extra_headers"]["session-id"] == id0
    assert sent0["extra_headers"]["thread-id"] == id0
    assert sent1["prompt_cache_key"] == sent1["extra_headers"]["session-id"] == id1


# ---------------------------------------------------------------------------
# 10-call cache corruption: persistent rotate (no one-shot, no revert).
# ---------------------------------------------------------------------------


def test_no_rotate_before_ten_identical_hits():
    """Nine identical hits is not enough — the ninth send still uses the start id."""
    session, current = _make_session([5, 5, 5, 5, 5, 5, 5, 5, 5])

    for _ in range(9):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        assert sent["prompt_cache_key"] == current
        assert sent["extra_headers"]["session-id"] == current


def test_no_rotate_when_cached_values_vary():
    """Varying cache hits never rotate the current id."""
    session, current = _make_session([10, 20, 30, 40, 50, 60])

    for _ in range(6):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        assert sent["prompt_cache_key"] == current


def test_zero_cached_hits_do_not_count_toward_corruption():
    """cached_tokens == 0 are misses, not hits, and never rotate."""
    session, current = _make_session([0, 0, 0, 0, 0, 0, 0, 0, 0])

    for _ in range(9):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        assert sent["prompt_cache_key"] == current


def test_rotate_after_ten_identical_hits_persists():
    """Ten identical positive hits -> the 11th request uses a NEW current id.

    The rotate is decided AFTER the 10th response completes (its cached value
    fills the window), so the 11th request is the first to carry the rotated id.
    """
    clock = lambda: EPOCH0 + 500  # noqa: E731  rotate epoch
    session, start_id = _make_session([7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 99], clock=clock)

    for _ in range(11):
        session.send("hi")

    rotated = _codex_affinity_id(ANCHOR, EPOCH0 + 500)
    assert rotated != start_id

    # First threshold requests used the start id.
    for sent in session._client.responses.kwargs[:10]:
        assert sent["prompt_cache_key"] == start_id

    # The 11th request uses the rotated id for all three levers.
    sent11 = session._client.responses.kwargs[10]
    assert sent11["prompt_cache_key"] == rotated
    assert sent11["extra_headers"]["session-id"] == rotated
    assert sent11["extra_headers"]["thread-id"] == rotated


def test_rotated_id_persists_and_does_not_revert():
    """After a rotate the NEW id is used for every later request — no revert."""
    clock = lambda: EPOCH0 + 500  # noqa: E731
    # 10 identical -> rotate; 11th send records cached=99 (breaks run); 12th + 13th
    # must continue on the SAME rotated id, never reverting to the start id.
    session, start_id = _make_session(
        [7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 99, 0, 3], clock=clock
    )

    for _ in range(13):
        session.send("hi")

    rotated = _codex_affinity_id(ANCHOR, EPOCH0 + 500)
    assert session._client.responses.kwargs[10]["prompt_cache_key"] == rotated
    assert session._client.responses.kwargs[11]["prompt_cache_key"] == rotated
    assert session._client.responses.kwargs[12]["prompt_cache_key"] == rotated
    assert session._client.responses.kwargs[11]["extra_headers"]["session-id"] == rotated
    # None of the post-rotate requests fell back to the start id.
    for sent in session._client.responses.kwargs[10:]:
        assert sent["prompt_cache_key"] != start_id


def test_second_corruption_rotates_again():
    """A second run of ten identical hits rotates to a THIRD distinct id."""
    # Two rotate epochs via a stateful clock.
    ticks = iter([EPOCH0 + 100, EPOCH0 + 200])
    last = {"v": EPOCH0 + 200}

    def clock():
        try:
            last["v"] = next(ticks)
        except StopIteration:
            pass
        return last["v"]

    # First threshold value=7 -> rotate. Then threshold value=8 -> rotate again.
    # Sequence of cached values recorded by requests 1..21:
    #   7*10 (rotate after #10) | 8*10 (rotate after #20) | 0
    session, start_id = _make_session(
        [7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 0], clock=clock
    )

    for _ in range(21):
        session.send("hi")

    first_rotate = _codex_affinity_id(ANCHOR, EPOCH0 + 100)
    second_rotate = _codex_affinity_id(ANCHOR, EPOCH0 + 200)
    assert len({start_id, first_rotate, second_rotate}) == 3

    keys = [kw["prompt_cache_key"] for kw in session._client.responses.kwargs]
    assert keys[:10] == [start_id] * 10
    assert keys[10:20] == [first_rotate] * 10
    assert keys[20] == second_rotate


# ---------------------------------------------------------------------------
# Three-field invariant holds on every request, including across a rotate.
# ---------------------------------------------------------------------------


def test_three_fields_always_equal_including_across_rotate():
    """prompt_cache_key == session-id == thread-id on every request."""
    clock = lambda: EPOCH0 + 500  # noqa: E731
    session, _ = _make_session([7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 99, 0], clock=clock)

    for _ in range(12):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        key = sent["prompt_cache_key"]
        assert sent["extra_headers"]["session-id"] == key
        assert sent["extra_headers"]["thread-id"] == key


# ---------------------------------------------------------------------------
# Event emission + field safety.
# ---------------------------------------------------------------------------


def test_rotate_emits_event_to_sink():
    """A rotate emits one safe event with the documented fields and no secrets."""
    events: list[dict] = []
    clock = lambda: EPOCH0 + 500  # noqa: E731
    session, start_id = _make_session(
        [7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 99], events=events, clock=clock
    )

    for _ in range(11):
        session.send("hi")

    rotated = _codex_affinity_id(ANCHOR, EPOCH0 + 500)
    rot_events = [e for e in events if e.get("type") == "codex_cache_affinity_rotated"]
    assert len(rot_events) == 1
    ev = rot_events[0]
    assert ev["new_id_hash"] == rotated
    assert ev["recent_cached_values"] == [7, 7, 7, 7, 7, 7, 7, 7, 7, 7]
    assert ev["reason"] == "stalled_cache_hits"
    assert ev["had_stable_id"] is True
    assert ev["provider"] == "codex"
    assert ev["model"] == "gpt-5.5"

    # No secrets / no prompt body / no anchor path / no token-cost leakage
    # beyond the cached list, and NOT the previous (start) id.
    blob = json.dumps(ev, default=str)
    assert start_id not in blob  # the prior id is not disclosed
    assert ANCHOR not in blob  # the anchor path is not disclosed
    assert "system prompt" not in blob
    assert "Authorization" not in blob and "Bearer" not in blob


def test_no_event_without_rotate():
    """Varying cache hits never emit a rotate event."""
    events: list[dict] = []
    session, _ = _make_session([10, 20, 30, 40, 50, 60], events=events)

    for _ in range(6):
        session.send("hi")

    assert [e for e in events if e.get("type") == "codex_cache_affinity_rotated"] == []


def test_usage_extra_reflects_current_id_including_after_rotate():
    """UsageMetadata.extra exposes the ACTUAL ids used on each request."""
    clock = lambda: EPOCH0 + 500  # noqa: E731
    session, start_id = _make_session([7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 99], clock=clock)

    results = [session.send("hi") for _ in range(11)]

    rotated = _codex_affinity_id(ANCHOR, EPOCH0 + 500)
    # Pre-rotate requests expose the start id.
    assert results[0].usage.extra["codex_session_id"] == start_id
    assert results[0].usage.extra["codex_prompt_cache_key"] == start_id
    # The rotated (11th) request exposes the new id for all three.
    assert results[10].usage.extra["codex_session_id"] == rotated
    assert results[10].usage.extra["codex_thread_id"] == rotated
    assert results[10].usage.extra["codex_prompt_cache_key"] == rotated


# ---------------------------------------------------------------------------
# Default host wiring — the adapter writes rotate events to logs/events.jsonl.
# ---------------------------------------------------------------------------


def test_adapter_writes_rotate_event_to_logs_events_jsonl(tmp_path):
    """A real Codex adapter emits the rotate event to the agent's events.jsonl."""
    anchor = tmp_path / "init.json"
    anchor.write_text("{}", encoding="utf-8")

    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
        codex_session_anchor=str(anchor),
        codex_epoch=EPOCH0,
    )
    # Eleven requests: ten identical positive hits rotate the id on the eleventh.
    adapter._client = FakeClient([_completed(7) for _ in range(11)])
    session = adapter.create_chat("gpt-5.5", "system prompt")

    for _ in range(11):
        session.send("hi")

    events_path = tmp_path / "logs" / "events.jsonl"
    assert events_path.exists()
    lines = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()]
    rotates = [e for e in lines if e.get("type") == "codex_cache_affinity_rotated"]
    assert len(rotates) == 1
    assert rotates[0]["recent_cached_values"] == [7, 7, 7, 7, 7, 7, 7, 7, 7, 7]
    assert rotates[0]["reason"] == "stalled_cache_hits"
    # The event carries no prompt body, anchor path, or OAuth secret.
    blob = json.dumps(rotates[0], default=str)
    assert "system prompt" not in blob and "Bearer" not in blob
    assert str(anchor) not in blob


def test_bare_adapter_has_no_event_sink():
    """A bare adapter (no anchor) builds no sink, so nothing is written."""
    adapter = CodexOpenAIAdapter(
        api_key="fake", base_url="http://fake", use_responses=True, force_responses=True
    )
    assert adapter._build_codex_event_sink() is None
