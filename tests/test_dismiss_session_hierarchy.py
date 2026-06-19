"""Sanity check that the dismiss deprecation shim is robust to agent shape.

Pre-redesign, ``_dismiss`` reached into ``agent._tc_inbox`` AND
``agent._session.chat.interface.remove_pair_by_notif_id`` to remove
notification pairs.  The original tests in this file defended against
a regression where code accessed ``chat.X`` instead of
``chat.interface.X``.

Under the .notification/ filesystem redesign, ``_dismiss`` is a
deprecation no-op — it does not touch the queue or the wire chat.
The hierarchy concern is moot.  This file is reduced to a single
sanity test confirming the shim handles production-like agent shapes
without crashing.

Phase 3 deletes ``_dismiss`` and this file entirely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.llm.interface import ChatInterface
from lingtai_kernel.tc_inbox import TCInbox


class _ProductionLikeChatSession:
    """Mirrors the real ChatSession surface: has ``.interface`` but no
    ``remove_pair_by_notif_id`` directly.  The shim must not depend on
    the absent direct method."""

    def __init__(self, interface: ChatInterface):
        self.interface = interface


@dataclass
class _ProductionLikeSession:
    chat: _ProductionLikeChatSession


@dataclass
class _ProductionLikeAgent:
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _session: _ProductionLikeSession = field(default=None)
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._session is None:
            self._session = _ProductionLikeSession(
                chat=_ProductionLikeChatSession(ChatInterface())
            )

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def test_dismiss_shim_is_safe_against_production_agent_shape():
    """The shim must return ok without raising AttributeError on a
    production-like agent (where ``chat`` is a ChatSession without a
    direct ``remove_pair_by_notif_id`` method).  Since the shim doesn't
    actually call the chat-side helper anymore, this is trivially true,
    but the test guards against future code that re-introduces a chat
    access path without thinking through the access pattern."""
    agent = _ProductionLikeAgent()
    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_anything"]})
    assert res["status"] == "ok"
    assert "legacy ids ignored" in res["note"]
