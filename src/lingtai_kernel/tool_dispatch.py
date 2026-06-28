"""Tiny action-router helper shared by single-action tool surfaces.

Several capability and intrinsic tools read an ``action`` field from the tool
arguments and dispatch to a handler. This helper captures only that mechanical
lookup. It deliberately does **not** impose an error schema: each caller passes
its own ``unknown`` factory so the exact, model-visible unknown-action envelope
is preserved per-tool.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

ActionHandler = Callable[[dict], dict]
UnknownFactory = Callable[[Any], dict]


def dispatch_action(
    args: dict,
    handlers: dict[Any, ActionHandler],
    *,
    action_key: str = "action",
    default: Any = "",
    unknown: UnknownFactory,
) -> dict:
    """Look up ``args[action_key]`` in ``handlers`` and call the match.

    When the action is missing, ``default`` is used as the lookup key. When no
    handler matches, ``unknown(action)`` is returned verbatim — the caller owns
    the exact error envelope, so casing, quoting, and key names stay identical
    to the hand-written router each tool used before.

    Invalid JSON can make ``action`` an unhashable value (e.g. ``[]`` or
    ``{}``). The hand-written routers compared the action with ``==`` and so
    fell through to the unknown-action envelope; mirror that here by treating
    an unhashable action as simply not matching any handler instead of letting
    ``dict.get`` raise ``TypeError``.
    """
    action = args.get(action_key, default)
    try:
        handler = handlers.get(action)
    except TypeError:
        handler = None
    if handler is None:
        return unknown(action)
    return handler(args)
