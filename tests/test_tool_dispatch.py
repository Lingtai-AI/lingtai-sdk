"""Unit tests for the tiny action-router helper (issue #513)."""
from __future__ import annotations

from lingtai_kernel.tool_dispatch import dispatch_action


def test_dispatch_calls_matching_handler():
    seen = {}

    def info(args):
        seen["args"] = args
        return {"status": "ok"}

    result = dispatch_action(
        {"action": "info", "x": 1},
        {"info": info},
        unknown=lambda action: {"status": "error", "action": action},
    )
    assert result == {"status": "ok"}
    # The matched handler receives the full args dict.
    assert seen["args"] == {"action": "info", "x": 1}


def test_missing_action_uses_default():
    calls = []

    result = dispatch_action(
        {},
        {"": lambda args: {"status": "default-ran"}},
        default="",
        unknown=lambda action: calls.append(action) or {"status": "error"},
    )
    assert result == {"status": "default-ran"}
    assert calls == []  # default matched, unknown never fired


def test_unknown_action_returns_supplied_envelope_verbatim():
    envelope = {"status": "error", "message": "nope: bogus"}
    result = dispatch_action(
        {"action": "bogus"},
        {"info": lambda args: {"status": "ok"}},
        unknown=lambda action: {"status": "error", "message": f"nope: {action}"},
    )
    assert result == envelope


def test_missing_action_with_no_matching_default_is_unknown():
    # When default ("") is not a registered handler, the unknown factory is
    # called with the default value — this is exactly what the capability
    # routers rely on to render `unknown action: '', ...`.
    result = dispatch_action(
        {},
        {"info": lambda args: {"status": "ok"}},
        unknown=lambda action: {"unknown": action},
    )
    assert result == {"unknown": ""}


def test_unhashable_action_falls_through_to_unknown():
    # Invalid JSON can make `action` a list or dict. The hand-written routers
    # compared with `==` and rendered the unknown-action envelope; the helper
    # must not raise `TypeError` from `dict.get` on an unhashable key.
    for bad_action in ([], {}, [1, 2], {"k": "v"}, set()):
        result = dispatch_action(
            {"action": bad_action},
            {"info": lambda args: {"status": "ok"}},
            unknown=lambda action: {"status": "error", "action": action},
        )
        assert result == {"status": "error", "action": bad_action}


def test_non_string_hashable_action_falls_through_to_unknown():
    # Hashable-but-non-string actions (numbers, booleans, None) already worked
    # with `dict.get`; lock the behaviour so they keep rendering as unknown.
    for bad_action in (5, 3.5, True, None):
        result = dispatch_action(
            {"action": bad_action},
            {"info": lambda args: {"status": "ok"}},
            unknown=lambda action: {"status": "error", "action": action},
        )
        assert result == {"status": "error", "action": bad_action}


def test_custom_action_key_and_default():
    result = dispatch_action(
        {"verb": "go"},
        {"go": lambda args: {"status": "went"}},
        action_key="verb",
        default="stay",
        unknown=lambda action: {"status": "error", "action": action},
    )
    assert result == {"status": "went"}

    # Missing custom key falls back to the custom default, then unknown.
    result2 = dispatch_action(
        {},
        {"go": lambda args: {"status": "went"}},
        action_key="verb",
        default="stay",
        unknown=lambda action: {"status": "error", "action": action},
    )
    assert result2 == {"status": "error", "action": "stay"}
