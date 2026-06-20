"""Tests for the Codex-specific cache-key request header.

Codex API requests continuously send ``codex-cache-key: xyz``, where ``xyz`` is
the first three characters of the current prompt key / prompt-cache-key. This
ties the backend prompt cache to the prompt key, so changing the prompt key
changes the header and forces the old prompt cache to break.

Semantics (per Jason):
  * The header is Codex-specific and unambiguous: ``codex-cache-key``.
  * The header is ALWAYS present on ordinary Codex API requests whenever the
    prompt key / prefix exists (no 8-hit threshold, no streak counter, no
    after-threshold mode, no conditional "after N hits" behavior).
  * The value is the first 3 characters of the current prompt key.
  * Changing the prompt key changes the header.
  * Non-Codex Responses sessions are unaffected.
  * The existing cache-affinity / cache-corruption rotate behavior is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    CodexResponsesSession,
    OpenAIResponsesSession,
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
    def __init__(self, events_per_call):
        self._events_per_call = events_per_call
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.kwargs)
        self.kwargs.append(kwargs)
        yield from self._events_per_call[idx]


class FakeClient:
    def __init__(self, events_per_call):
        self.responses = FakeResponses(events_per_call)


def _usage(cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed(cached: int = 0) -> list[Event]:
    return [
        Event(
            "response.completed",
            response=SimpleNamespace(id="resp_fake", usage=_usage(cached)),
        )
    ]


ANCHOR = "/agents/alice/init.json"
EPOCH0 = 1_700_000_000
HEADER = "codex-cache-key"


def _bare_session(prompt_cache_key=None, *, instructions="system prompt", **kw):
    return CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions=instructions,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key=prompt_cache_key,
        **kw,
    )


def _headers(sent: dict) -> dict[str, str]:
    return sent.get("extra_headers") or {}


# ---------------------------------------------------------------------------
# Header is present whenever a prompt key exists, on ordinary requests.
# ---------------------------------------------------------------------------


def test_lone_prompt_cache_key_sends_codex_cache_key_header():
    """A body-only prompt cache key still sends the Codex cache-key header."""
    session = _bare_session(prompt_cache_key="custom-key:v2")

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    headers = _headers(sent)
    assert headers[HEADER] == "cus"
    # The lone-key carve-out still holds for per-agent slot routers.
    assert "session-id" not in headers
    assert "thread-id" not in headers
    assert sent["instructions"] == "system prompt"


def test_header_value_is_first_three_chars_of_prompt_key():
    session = _bare_session(prompt_cache_key="abcdef123456")

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert _headers(sent)[HEADER] == "abc"


def test_header_present_on_anchored_session_alongside_affinity_headers():
    current = _codex_affinity_id(ANCHOR, EPOCH0)
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
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

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert _headers(sent)[HEADER] == current[:3]
    # Existing affinity headers still ride along unchanged.
    assert sent["extra_headers"]["session-id"] == current
    assert sent["extra_headers"]["thread-id"] == current


def test_header_present_on_every_ordinary_request_no_threshold():
    """No streak / threshold: the header rides on the FIRST request and every one."""
    session = CodexResponsesSession(
        client=FakeClient([_completed(), _completed(), _completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key="streakkey",
    )

    for _ in range(3):
        session.send("hi")

    for sent in session._client.responses.kwargs:
        assert _headers(sent)[HEADER] == "str"


# ---------------------------------------------------------------------------
# Changing the prompt key changes the header.
# ---------------------------------------------------------------------------


def test_different_prompt_key_changes_header():
    a = _bare_session(prompt_cache_key="alpha-key")
    b = _bare_session(prompt_cache_key="bravo-key")

    a.send("hi")
    b.send("hi")

    ha = _headers(a._client.responses.kwargs[0])[HEADER]
    hb = _headers(b._client.responses.kwargs[0])[HEADER]
    assert ha == "alp"
    assert hb == "bra"
    assert ha != hb


def test_header_tracks_prompt_key_after_stalled_cache_rotate():
    """When the affinity id rotates, the header follows the new prompt key."""
    clock = lambda: EPOCH0 + 500  # noqa: E731
    start_id = _codex_affinity_id(ANCHOR, EPOCH0)
    session = CodexResponsesSession(
        client=FakeClient([_completed(7) for _ in range(11)]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key=start_id,
        session_id=start_id,
        thread_id=start_id,
        affinity_anchor=ANCHOR,
        time_fn=clock,
    )

    for _ in range(11):
        session.send("hi")

    rotated = _codex_affinity_id(ANCHOR, EPOCH0 + 500)
    assert rotated != start_id
    # The first ten requests carry the start-id prefix.
    for sent in session._client.responses.kwargs[:10]:
        assert _headers(sent)[HEADER] == start_id[:3]
    # The eleventh (post-rotate) request carries the rotated-id prefix.
    assert _headers(session._client.responses.kwargs[10])[HEADER] == rotated[:3]


# ---------------------------------------------------------------------------
# No prompt key -> no header. Non-Codex unaffected.
# ---------------------------------------------------------------------------


def test_no_prompt_key_no_header():
    """A bare session with no prompt key sends no cache-key header."""
    session = _bare_session(prompt_cache_key=None)

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert HEADER not in _headers(sent)
    assert sent["instructions"] == "system prompt"


def test_non_codex_responses_session_unaffected_by_prompt_cache_key():
    session = OpenAIResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key="custom-key:v2",
    )

    session.send_stream("hi")

    sent = session._client.responses.kwargs[0]
    assert sent["instructions"] == "system prompt"
    assert "extra_headers" not in sent


# ---------------------------------------------------------------------------
# Adapter-created Codex sessions use the same semantics.
# ---------------------------------------------------------------------------


def test_codex_adapter_default_path_sends_codex_cache_key_header():
    epoch = 1_700_000_100
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
        codex_epoch=epoch,
    )
    adapter._client = FakeClient([_completed()])
    session = adapter.create_chat("gpt-5.5", "system prompt", tools=None)

    session.send("hi")

    expected = "lingtai-codex:gpt-5.5:v1"
    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == expected
    assert sent["extra_headers"][HEADER] == expected[:3]
