"""Tests for the Codex Responses-over-WebSocket incremental delta algorithm.

These mirror the official Codex CLI source (``codex-rs/core/src/client.rs``
``get_incremental_items`` at 949-985 and ``prepare_websocket_request`` at
998-1024, tag ``rust-v0.130.0``). They are pure/mock tests — no network.

The algorithm decides whether the current full request is a strict extension of
(previous request input + the previous response's server-added output items). If
so, only the suffix ("delta") is sent over the websocket with
``previous_response_id``; otherwise the full input is sent with no previous id.
"""

from __future__ import annotations

from lingtai.llm.openai.adapter import (
    _CodexLastResponse,
    _codex_incremental_items,
)


def _req(input_items, **fields):
    """Build a minimal Codex request dict: ``input`` plus non-input fields."""
    base = {"model": "gpt-5.5", "store": False, "stream": True}
    base.update(fields)
    base["input"] = list(input_items)
    return base


def test_incremental_returns_suffix_when_strict_extension():
    prev = _req([{"role": "user", "content": "a"}])
    last = _CodexLastResponse(
        response_id="resp_1",
        items_added=[{"type": "message", "role": "assistant", "content": "b"}],
    )
    cur = _req(
        [
            {"role": "user", "content": "a"},
            {"type": "message", "role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
    )

    delta = _codex_incremental_items(prev, last.items_added, cur, allow_empty_delta=False)

    # Baseline = prev.input + items_added; only the trailing new user turn is sent.
    assert delta == [{"role": "user", "content": "c"}]


def test_incremental_none_when_non_input_field_differs():
    """All non-input request fields must match, else no delta (full replay)."""
    prev = _req([{"role": "user", "content": "a"}], tools=[{"name": "x"}])
    last = _CodexLastResponse(response_id="resp_1", items_added=[])
    cur = _req(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "c"}],
        tools=[{"name": "DIFFERENT"}],
    )

    delta = _codex_incremental_items(prev, last.items_added, cur, allow_empty_delta=False)

    assert delta is None


def test_incremental_none_when_input_not_a_prefix_extension():
    """If the current input diverges from the baseline prefix, no delta."""
    prev = _req([{"role": "user", "content": "a"}])
    last = _CodexLastResponse(response_id="resp_1", items_added=[])
    cur = _req([{"role": "user", "content": "EDITED"}, {"role": "user", "content": "c"}])

    delta = _codex_incremental_items(prev, last.items_added, cur, allow_empty_delta=False)

    assert delta is None


def test_incremental_empty_delta_rejected_unless_allowed():
    """A request equal to the baseline yields an empty delta only when allowed."""
    prev = _req([{"role": "user", "content": "a"}])
    last = _CodexLastResponse(response_id="resp_1", items_added=[])
    cur = _req([{"role": "user", "content": "a"}])  # identical, nothing new

    assert _codex_incremental_items(prev, [], cur, allow_empty_delta=False) is None
    assert _codex_incremental_items(prev, [], cur, allow_empty_delta=True) == []
