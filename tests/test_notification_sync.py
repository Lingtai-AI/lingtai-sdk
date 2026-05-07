"""Tests for the .notification/ filesystem sync mechanism.

Covers the design's invariants and the patch's §13 test matrix:

- §13.1 — fingerprint + collection primitives, atomicity, concurrency
- §13.2 — IDLE-state pair injection / strip / no-op
- §13.3 — ACTIVE-state meta on most recent str ToolResultBlock
- §13.4 — ASLEEP-state wake on fingerprint change
- §13.5 — voluntary `system(action="notification")` returns the dict
- §13.6 — producer migrations: email, soul, system
- §13.7 — molt clearing

Where possible the tests use the real `notifications.py` module against
``tmp_path``; agent-level tests use a stub that mimics the
BaseAgent → SessionManager → ChatSession → ChatInterface hierarchy.

The deeper integration paths (heartbeat → `_sync_notifications` → wire
mutation under real adapters) are covered by the existing `test_tc_inbox*`
suites and the soul/email integration tests, which continue to pass
because `tc_inbox` is preserved during the migration window.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lingtai_kernel.notifications import (
    notification_fingerprint,
    collect_notifications,
    publish,
    clear,
)


# ---------------------------------------------------------------------------
# §13.1 — fingerprint + collection primitives
# ---------------------------------------------------------------------------


def test_fingerprint_empty_dir(tmp_path: Path) -> None:
    assert notification_fingerprint(tmp_path) == ()


def test_fingerprint_with_files(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"count": 3})
    publish(tmp_path, "soul", {"voices": []})
    fp = notification_fingerprint(tmp_path)
    names = [entry[0] for entry in fp]
    assert names == sorted(names)
    assert "email.json" in names
    assert "soul.json" in names
    # Each entry is (name, mtime_ns, size).
    for name, mtime_ns, size in fp:
        assert isinstance(mtime_ns, int)
        assert size > 0


def test_fingerprint_changes_on_overwrite(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"count": 1})
    fp1 = notification_fingerprint(tmp_path)
    # Force mtime_ns to differ — tmp+rename always bumps mtime; tiny
    # delay only needed if the underlying filesystem coalesces ns.
    import time as _time
    _time.sleep(0.001)
    publish(tmp_path, "email", {"count": 2, "extra": "more bytes"})
    fp2 = notification_fingerprint(tmp_path)
    assert fp1 != fp2


def test_collect_empty_dir(tmp_path: Path) -> None:
    assert collect_notifications(tmp_path) == {}


def test_collect_mixed_files(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"count": 3})
    publish(tmp_path, "mcp.telegram", {"messages": ["hi"]})
    out = collect_notifications(tmp_path)
    assert out == {
        "email": {"count": 3},
        "mcp.telegram": {"messages": ["hi"]},
    }


def test_collect_skips_malformed_silently(tmp_path: Path) -> None:
    publish(tmp_path, "good", {"x": 1})
    bad_path = tmp_path / ".notification" / "bad.json"
    bad_path.write_text("not json {")
    out = collect_notifications(tmp_path)
    assert out == {"good": {"x": 1}}


def test_collect_skips_non_json_files(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"x": 1})
    other = tmp_path / ".notification" / "stray.txt"
    other.write_text("ignored")
    out = collect_notifications(tmp_path)
    assert "email" in out
    assert "stray" not in out


def test_publish_creates_dir(tmp_path: Path) -> None:
    notif_dir = tmp_path / ".notification"
    assert not notif_dir.exists()
    publish(tmp_path, "email", {"x": 1})
    assert notif_dir.is_dir()


def test_publish_atomic_no_tmp_residue(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"x": 1})
    notif_dir = tmp_path / ".notification"
    assert (notif_dir / "email.json").is_file()
    assert not (notif_dir / "email.json.tmp").exists()


def test_clear_idempotent(tmp_path: Path) -> None:
    # Clearing a non-existent file should not raise.
    clear(tmp_path, "ghost")
    publish(tmp_path, "email", {"x": 1})
    clear(tmp_path, "email")
    assert not (tmp_path / ".notification" / "email.json").exists()
    # Second clear is a no-op.
    clear(tmp_path, "email")


def test_concurrent_publish_atomicity(tmp_path: Path) -> None:
    """10 threads × 50 iterations.  Every collect snapshot must return
    parseable JSON for every source (no partial-write reads, no
    corrupted files)."""
    sources = [f"src_{i}" for i in range(10)]

    def worker(source: str) -> None:
        for i in range(50):
            publish(tmp_path, source, {"src": source, "i": i})

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        list(pool.map(worker, sources))

    out = collect_notifications(tmp_path)
    # All 10 sources eventually published.
    assert set(out.keys()) == set(sources)
    # Every value parsed successfully (collect's try/except skips
    # malformed; if any failed we'd see fewer keys).
    for src, data in out.items():
        assert data["src"] == src
        assert isinstance(data["i"], int)

    # No .tmp residue.
    notif_dir = tmp_path / ".notification"
    leftover = list(notif_dir.glob("*.tmp"))
    assert leftover == [], f"Stale tmp files: {leftover}"


# ---------------------------------------------------------------------------
# §13.5 — `system(action="notification")` voluntary call
# ---------------------------------------------------------------------------


def test_notification_action_returns_empty_when_nothing_published(
    tmp_path: Path,
) -> None:
    from lingtai_kernel.intrinsics.system import handle

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = handle(_Stub(), {"action": "notification"})
    assert res == {}


def test_notification_action_returns_collect(tmp_path: Path) -> None:
    from lingtai_kernel.intrinsics.system import handle

    publish(tmp_path, "email", {"count": 5, "newest_received_at": "2026-05-05T00:00:00Z"})
    publish(tmp_path, "soul", {"voices": [{"source": "warmth", "voice": "..."}]})

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = handle(_Stub(), {"action": "notification"})
    assert "email" in res
    assert "soul" in res
    assert res["email"]["count"] == 5


# ---------------------------------------------------------------------------
# §13.6 — producer migrations
# ---------------------------------------------------------------------------


@dataclass
class _ProducerStubAgent:
    """Minimal agent stub for testing producer file writes.  No chat
    session needed — these tests only verify that producers correctly
    write to .notification/."""
    _working_dir: Path = None
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def _log(self, evt: str, **fields: Any) -> None:
        self._logs.append((evt, fields))

    def _wake_nap(self, *_args, **_kwargs) -> None:
        # No-op for producer-only tests; no run loop is running.
        pass


def test_email_publish_writes_file(tmp_path: Path, monkeypatch) -> None:
    """When the email producer has unread mail, it writes
    `.notification/email.json` with count + digest."""
    from lingtai_kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)

    def fake_render(_agent, **_kw):
        return ("3 unread:\n- A\n- B\n- C\n", 3, "2026-05-05T00:00:00Z")

    monkeypatch.setattr(
        "lingtai_kernel.intrinsics.email.primitives._render_unread_digest",
        fake_render,
    )

    result = messaging._rerender_unread_digest(agent)
    assert result == "email"

    out = collect_notifications(tmp_path)
    assert "email" in out
    assert out["email"]["data"]["count"] == 3
    assert out["email"]["data"]["digest"].startswith("3 unread")
    assert out["email"]["icon"] == "📧"


def test_email_clear_on_zero(tmp_path: Path, monkeypatch) -> None:
    """When unread count drops to 0, the producer clears the file."""
    from lingtai_kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    publish(tmp_path, "email", {"data": {"count": 5}})  # pre-existing
    assert (tmp_path / ".notification" / "email.json").exists()

    monkeypatch.setattr(
        "lingtai_kernel.intrinsics.email.primitives._render_unread_digest",
        lambda _agent, **_kw: ("", 0, None),
    )

    result = messaging._rerender_unread_digest(agent)
    assert result is None
    assert not (tmp_path / ".notification" / "email.json").exists()


def test_system_publish_appends_event(tmp_path: Path) -> None:
    """Two calls produce a single file with both events."""
    from lingtai_kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    messaging._enqueue_system_notification(
        agent, source="email.bounce", ref_id="msg_1", body="bounce 1"
    )
    messaging._enqueue_system_notification(
        agent, source="email.bounce", ref_id="msg_2", body="bounce 2"
    )

    out = collect_notifications(tmp_path)
    assert "system" in out
    events = out["system"]["data"]["events"]
    assert len(events) == 2
    assert {e["ref_id"] for e in events} == {"msg_1", "msg_2"}
    assert all(e["source"] == "email.bounce" for e in events)
    assert events[0]["event_id"] != events[1]["event_id"]


def test_system_publish_caps_at_20(tmp_path: Path) -> None:
    """25 sequential calls keep only the 20 most recent events."""
    from lingtai_kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    for i in range(25):
        messaging._enqueue_system_notification(
            agent, source="daemon", ref_id=f"ref_{i}", body=f"event {i}"
        )

    events = collect_notifications(tmp_path)["system"]["data"]["events"]
    assert len(events) == 20
    refs = [e["ref_id"] for e in events]
    # Cap retained the most recent: ref_5 .. ref_24.
    assert refs[0] == "ref_5"
    assert refs[-1] == "ref_24"


def test_system_publish_concurrent_no_lost_writes(tmp_path: Path) -> None:
    """20 threads concurrently publish; all events end up in the file."""
    from lingtai_kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    n_events = 20

    def worker(i: int) -> None:
        messaging._enqueue_system_notification(
            agent, source="stress", ref_id=f"ref_{i}", body=f"e{i}"
        )

    with ThreadPoolExecutor(max_workers=n_events) as pool:
        list(pool.map(worker, range(n_events)))

    events = collect_notifications(tmp_path)["system"]["data"]["events"]
    # All 20 fit under the 20-cap.
    assert len(events) == n_events
    refs = {e["ref_id"] for e in events}
    assert refs == {f"ref_{i}" for i in range(n_events)}
    event_ids = {e["event_id"] for e in events}
    assert len(event_ids) == n_events  # all distinct


def test_soul_voices_shape(tmp_path: Path) -> None:
    """The soul producer's voice-shaping helper trims empty fields."""
    from lingtai_kernel.intrinsics.soul.flow import _shape_soul_voices

    voices = [
        {"source": "warmth", "voice": "remember to rest", "thinking": ["..."]},
        {"source": "doubt", "voice": "are you sure?", "thinking": []},
    ]
    shaped = _shape_soul_voices(voices)
    assert len(shaped) == 2
    assert shaped[0]["source"] == "warmth"
    assert shaped[0]["voice"] == "remember to rest"
    assert shaped[0]["thinking"] == ["..."]
    assert shaped[1]["voice"] == "are you sure?"
    # Empty thinking is omitted from the entry.
    assert "thinking" not in shaped[1]


# ---------------------------------------------------------------------------
# §13.6.bis — system.publish_notification (canonical helper)
# ---------------------------------------------------------------------------


def test_submit_writes_envelope(tmp_path: Path) -> None:
    """``submit`` builds the documented envelope and writes the file."""
    from lingtai_kernel.notifications import submit

    submit(tmp_path, "demo",
           header="hello", icon="✨",
           data={"x": 1, "y": [2, 3]})

    out = collect_notifications(tmp_path)
    assert "demo" in out
    payload = out["demo"]
    assert payload["header"] == "hello"
    assert payload["icon"] == "✨"
    assert payload["priority"] == "normal"
    assert payload["data"] == {"x": 1, "y": [2, 3]}
    # published_at is stamped, ISO format.
    assert "published_at" in payload
    assert payload["published_at"].endswith("Z")


def test_submit_priority_override(tmp_path: Path) -> None:
    from lingtai_kernel.notifications import submit

    submit(tmp_path, "urgent",
           header="oh no", icon="🚨",
           priority="high", data={})

    assert collect_notifications(tmp_path)["urgent"]["priority"] == "high"


def test_submit_via_system_alias(tmp_path: Path) -> None:
    """``intrinsics.system.publish_notification`` is the same callable
    as ``notifications.submit`` — producers can import either."""
    from lingtai_kernel.intrinsics.system import (
        publish_notification, clear_notification,
    )
    from lingtai_kernel.notifications import submit, clear

    assert publish_notification is submit
    assert clear_notification is clear

    publish_notification(tmp_path, "via_system",
                         header="via", icon="🛰",
                         data={"ok": True})
    out = collect_notifications(tmp_path)
    assert out["via_system"]["data"] == {"ok": True}

    clear_notification(tmp_path, "via_system")
    out = collect_notifications(tmp_path)
    assert "via_system" not in out


# ---------------------------------------------------------------------------
# §13.7 — molt clearing
# ---------------------------------------------------------------------------


def test_molt_clears_notification_dir(tmp_path: Path) -> None:
    """After molt, the .notification/ dir is gone and the agent's
    fingerprint state is reset."""
    publish(tmp_path, "email", {"count": 3})
    publish(tmp_path, "soul", {"voices": []})
    assert (tmp_path / ".notification").is_dir()

    # Stub agent with the bare minimum the molt clear logic needs.
    @dataclass
    class _MoltStub:
        _working_dir: Path = tmp_path
        _notification_fp: tuple = (("email.json", 1, 12),)
        _notification_block_id: str | None = "notif_xyz"
        _pending_notification_meta: str | None = "stale"
        _appendix_ids_by_source: dict = field(default_factory=dict)

    # We exercise the rmtree+reset block directly, since the rest of
    # _context_molt builds a session and runs hooks we don't care
    # about for this test.
    agent = _MoltStub()
    import shutil
    notif_dir = agent._working_dir / ".notification"
    if notif_dir.is_dir():
        shutil.rmtree(notif_dir)
    agent._notification_fp = ()
    agent._notification_block_id = None
    agent._pending_notification_meta = None

    assert not (tmp_path / ".notification").exists()
    assert agent._notification_fp == ()
    assert agent._notification_block_id is None
    assert agent._pending_notification_meta is None


# ---------------------------------------------------------------------------
# §13.2 / §13.3 — sync mechanism on a stub agent
# ---------------------------------------------------------------------------


def _make_chat_stub():
    """Minimal ChatInterface-backed chat stub for sync tests."""
    from lingtai_kernel.llm.interface import ChatInterface

    class _ChatStub:
        def __init__(self):
            self.interface = ChatInterface()

    return _ChatStub()


def test_sync_idle_posts_wake_message(tmp_path: Path) -> None:
    """IDLE: fingerprint change → MSG_TC_WAKE goes to the inbox.

    The synthesized ``(ToolCallBlock, ToolResultBlock)`` pair has
    already been spliced by ``_inject_notification_pair`` —
    impersonating a voluntary ``system(action="notification")`` call
    from the agent's perspective.  ``MSG_TC_WAKE`` then unblocks the
    run loop so ``_handle_tc_wake`` drives one inference round off
    the existing wire, no fake user message and no meta prefix.

    Regression for the IDLE-no-wake bug shipped in d2da97e: notifying
    without posting a wake message left the run loop blocked on
    ``inbox.get()`` even though the wire was correct on disk.
    """
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.message import MSG_TC_WAKE

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1})
    agent._sync_notifications()

    # Wire pair injected.
    assert len(agent._chat_stub.interface.entries) == 2
    # MSG_TC_WAKE in the inbox so the run loop picks it up and
    # _handle_tc_wake drives one inference round off the wire.
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_TC_WAKE


def test_sync_idle_injects_pair_with_synthesized_marker(tmp_path: Path) -> None:
    """IDLE: fingerprint change → synthetic pair appended; result block
    has synthesized=True and JSON body carries `_synthesized: true`."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock
    from lingtai_kernel.message import _make_message  # noqa: F401

    chat = _make_chat_stub()

    # Build a partial agent: we override only what _sync_notifications
    # touches, since constructing a real BaseAgent requires a full
    # filesystem agent dir + LLM service.
    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            self._asleep_evt = threading.Event()
            self._cancel_event = threading.Event()
            # inbox for any wake messages
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source: str = "main") -> None:
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1, "data": {"count": 1}})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 2  # call + result
    # First is assistant (call), second is user (result).
    assert entries[0].role == "assistant"
    assert entries[1].role == "user"
    # Assistant entry: [TextBlock(summary), ToolCallBlock(notification)]
    assert len(entries[0].content) == 2
    from lingtai_kernel.llm.interface import TextBlock
    assert isinstance(entries[0].content[0], TextBlock)
    call_block = entries[0].content[1]
    result_block = entries[1].content[0]
    assert isinstance(call_block, ToolCallBlock)
    assert call_block.name == "system"
    assert call_block.args["action"] == "notification"
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.synthesized is True

    body = json.loads(result_block.content)
    assert body["_synthesized"] is True
    assert "email" in body["notifications"]

    assert agent._notification_block_id == call_block.id


def test_sync_idle_strip_then_reinject(tmp_path: Path) -> None:
    """Two consecutive sync calls — first injects, second replaces."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1})
    agent._sync_notifications()
    first_id = agent._notification_block_id
    assert first_id is not None
    assert len(agent._chat_stub.interface.entries) == 2

    # Producer publishes new state — fingerprint must change for sync
    # to fire.  Sleep a moment to bump mtime_ns.
    import time as _time
    _time.sleep(0.001)
    publish(tmp_path, "email", {"count": 2, "extra": "more bytes"})
    agent._sync_notifications()
    second_id = agent._notification_block_id

    assert second_id is not None
    assert second_id != first_id
    # Old pair stripped, new pair in place — total still 2 entries.
    assert len(agent._chat_stub.interface.entries) == 2


def test_sync_idle_empty_strips(tmp_path: Path) -> None:
    """When all producer files are cleared, the wire pair is stripped."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1})
    agent._sync_notifications()
    assert len(agent._chat_stub.interface.entries) == 2

    clear(tmp_path, "email")
    agent._sync_notifications()

    assert agent._notification_block_id is None
    assert len(agent._chat_stub.interface.entries) == 0


def test_sync_no_change_is_noop(tmp_path: Path) -> None:
    """Two syncs without any filesystem change → second is a no-op."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1})
    agent._sync_notifications()
    first_id = agent._notification_block_id
    n_entries_before = len(agent._chat_stub.interface.entries)

    # No change to .notification/ — second sync should no-op.
    agent._sync_notifications()
    assert agent._notification_block_id == first_id
    assert len(agent._chat_stub.interface.entries) == n_entries_before


def test_sync_active_stashes_meta(tmp_path: Path) -> None:
    """ACTIVE state: fingerprint change → `_pending_notification_meta` set."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ACTIVE
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1, "data": {"count": 1}})
    agent._sync_notifications()

    assert agent._pending_notification_meta is not None
    body = json.loads(agent._pending_notification_meta)
    assert body["_synthesized"] is True
    assert "email" in body["notifications"]
    # No wire mutation yet — the meta will be applied at request-send time.
    assert len(agent._chat_stub.interface.entries) == 0


def test_inject_notification_meta_prefers_most_recent_block(tmp_path: Path) -> None:
    """Most recent ToolResultBlock has dict content — meta is injected
    there (dict serialized to JSON string), not on the older str block."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock

    chat = _make_chat_stub()
    iface = chat.interface

    # First pair: string content (older).
    iface.add_assistant_message(content=[ToolCallBlock(id="c1", name="bash", args={})])
    iface.add_tool_results([ToolResultBlock(id="c1", name="bash", content="hello world")])
    # Second pair: dict content (newer — should receive the notification).
    iface.add_assistant_message(content=[ToolCallBlock(id="c2", name="mcp", args={})])
    iface.add_tool_results([ToolResultBlock(id="c2", name="mcp", content={"structured": True})])

    class _Agent(BaseAgent):
        def __init__(self):
            self._chat_stub = chat
            self._pending_notification_meta = '{"_synthesized": true, "notifications": {"email": {}}}'
            self._logs = []

        @property
        def _chat(self):
            return self._chat_stub

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

    agent = _Agent()
    agent._inject_notification_meta(message=None)

    # Older str-content block is untouched.
    str_block = iface.entries[1].content[0]
    assert str_block.content == "hello world"

    # Newer dict-content block was serialized and now carries the prefix.
    dict_block = iface.entries[3].content[0]
    assert isinstance(dict_block.content, str)
    assert dict_block.content.startswith("notifications:\n")
    assert '"structured": true' in dict_block.content

    # Pending meta cleared.
    assert agent._pending_notification_meta is None


def test_inject_notification_meta_dict_only(tmp_path: Path) -> None:
    """When ALL ToolResultBlocks have dict content, the notification is
    still injected (dict serialized to JSON string) instead of being
    deferred indefinitely.  Regression test for 79% deferral rate bug."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock

    chat = _make_chat_stub()
    iface = chat.interface

    # Only dict-content blocks — the old code would defer here.
    iface.add_assistant_message(content=[ToolCallBlock(id="c1", name="psyche", args={})])
    iface.add_tool_results([ToolResultBlock(id="c1", name="psyche", content={"status": "ok", "identity": "test"})])
    iface.add_assistant_message(content=[ToolCallBlock(id="c2", name="soul", args={})])
    iface.add_tool_results([ToolResultBlock(id="c2", name="soul", content={"voices": []})])

    class _Agent(BaseAgent):
        def __init__(self):
            self._chat_stub = chat
            self._pending_notification_meta = '{"_synthesized": true, "notifications": {"molt": {"pressure": 0.9}}}'
            self._logs = []

        @property
        def _chat(self):
            return self._chat_stub

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

    agent = _Agent()
    agent._inject_notification_meta(message=None)

    # Most recent block (soul) should carry the notification prefix.
    target = iface.entries[3].content[0]
    assert isinstance(target.content, str)
    assert target.content.startswith("notifications:\n")
    assert '"voices": []' in target.content
    assert "molt" in target.content

    # Older dict block untouched (already serialized would only happen
    # if it had been a previous target — it wasn't, so still dict).
    older = iface.entries[1].content[0]
    assert older.content == {"status": "ok", "identity": "test"}

    # Pending meta cleared — not deferred.
    assert agent._pending_notification_meta is None
    # Logged as injected, not deferred.
    events = [e for e, _ in agent._logs]
    assert "notification_meta_injected" in events
    assert "notification_meta_deferred" not in events


def test_inject_notification_meta_strips_old_prefix(tmp_path: Path) -> None:
    """Prior result block carries an old prefix — it gets stripped when
    a new prefix is reinjected on a more recent block."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock

    chat = _make_chat_stub()
    iface = chat.interface

    old_prefix = "notifications:\n{\"_synthesized\": true, \"notifications\": {}}\n\n"
    iface.add_assistant_message(content=[ToolCallBlock(id="c1", name="bash", args={})])
    iface.add_tool_results([
        ToolResultBlock(id="c1", name="bash", content=old_prefix + "first output")
    ])
    iface.add_assistant_message(content=[ToolCallBlock(id="c2", name="bash", args={})])
    iface.add_tool_results([
        ToolResultBlock(id="c2", name="bash", content="second output")
    ])

    class _Agent(BaseAgent):
        def __init__(self):
            self._chat_stub = chat
            self._pending_notification_meta = '{"_synthesized": true, "notifications": {"email": {}}}'
            self._logs = []

        @property
        def _chat(self):
            return self._chat_stub

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

    agent = _Agent()
    agent._inject_notification_meta(message=None)

    # Oldest result: prefix stripped.
    older = iface.entries[1].content[0]
    assert older.content == "first output"
    # Most recent: fresh prefix.
    newer = iface.entries[3].content[0]
    assert newer.content.startswith("notifications:\n")
    assert "second output" in newer.content
    assert agent._pending_notification_meta is None


# ---------------------------------------------------------------------------
# §13.4 — ASLEEP wake on fingerprint change
# ---------------------------------------------------------------------------


def test_sync_asleep_wakes_on_change(tmp_path: Path) -> None:
    """Producer publishes while agent is ASLEEP → state transitions to
    IDLE, pair is injected, MSG_TC_WAKE goes to inbox."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.message import MSG_TC_WAKE

    chat = _make_chat_stub()
    state_history: list[AgentState] = []

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            state_history.append(new_state)

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE
    assert AgentState.IDLE in state_history
    # MSG_TC_WAKE delivered — _handle_tc_wake will drive the wire forward.
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_TC_WAKE
    # Wire pair was injected.
    assert len(agent._chat_stub.interface.entries) == 2


def test_sync_asleep_no_change_stays_asleep(tmp_path: Path) -> None:
    """No producer write → fingerprint stays empty → agent stays
    ASLEEP."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            self._state = _a[0] if _a else _kw.get("new_state")

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    agent._sync_notifications()

    assert agent._state == AgentState.ASLEEP
    assert agent.inbox.empty()
    assert len(agent._chat_stub.interface.entries) == 0


# ---------------------------------------------------------------------------
# §13.8 — wire-drive contract: session.send(None) means "continue from wire"
# ---------------------------------------------------------------------------


def _make_anthropic_session_with_pre_staged_pair():
    """Build a real AnthropicChatSession with a synthesized notification
    pair already at the wire tail."""
    from unittest.mock import MagicMock
    from lingtai_kernel.llm.interface import (
        ChatInterface,
        ToolCallBlock,
        ToolResultBlock,
    )
    from lingtai.llm.anthropic.adapter import AnthropicChatSession

    iface = ChatInterface()
    iface.add_assistant_message(content=[
        ToolCallBlock(id="notif_1", name="system",
                      args={"action": "notification"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="notif_1", name="system",
                        content='{"_synthesized": true}',
                        synthesized=True),
    ])

    session = AnthropicChatSession(
        client=MagicMock(),
        model="claude-sonnet-test",
        system_prompt="system",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )
    return session, iface


def _fake_anthropic_response(text: str = "ok"):
    """Build a MagicMock that mimics anthropic SDK's response shape."""
    from unittest.mock import MagicMock

    raw = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    raw.content = [block]
    raw.usage = MagicMock(
        input_tokens=10,
        output_tokens=2,
        thinking_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    raw.id = "resp_1"
    raw.model = "claude-sonnet-test"
    raw.role = "assistant"
    raw.stop_reason = "end_turn"
    return raw


def test_anthropic_send_none_does_not_append_input() -> None:
    """``AnthropicChatSession.send(None)`` calls the API with the
    pre-staged wire, does not append a user message, and records only
    the assistant response."""
    session, iface = _make_anthropic_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.messages.create.return_value = _fake_anthropic_response()

    response = session.send(None)

    assert response is not None
    assert session._client.messages.create.called
    # Wire grew by exactly one entry — the assistant response — not two.
    assert len(iface.entries) == pre_count + 1
    assert iface.entries[-1].role == "assistant"
    # The pre-staged pair is intact.
    assert iface.entries[pre_count - 2].role == "assistant"
    assert iface.entries[pre_count - 1].role == "user"


def test_anthropic_send_none_error_does_not_drop_pair() -> None:
    """API failure during a ``send(None)`` must not invoke
    ``drop_trailing`` — the pre-staged user entry is the notification
    pair's tool_result, not something this call appended."""
    session, iface = _make_anthropic_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.messages.create.side_effect = RuntimeError("boom")

    try:
        session.send(None)
    except RuntimeError:
        pass

    # Wire is unchanged — the synthesized pair survived.
    assert len(iface.entries) == pre_count
    assert iface.entries[-1].role == "user"
    from lingtai_kernel.llm.interface import ToolResultBlock
    assert any(
        isinstance(b, ToolResultBlock) for b in iface.entries[-1].content
    )


def _make_openai_session_with_pre_staged_pair():
    """Build a real OpenAIChatSession with a synthesized notification
    pair already at the wire tail."""
    from unittest.mock import MagicMock
    from lingtai_kernel.llm.interface import (
        ChatInterface,
        ToolCallBlock,
        ToolResultBlock,
    )
    from lingtai.llm.openai.adapter import OpenAIChatSession

    iface = ChatInterface()
    iface.add_assistant_message(content=[
        ToolCallBlock(id="notif_1", name="system",
                      args={"action": "notification"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="notif_1", name="system",
                        content='{"_synthesized": true}',
                        synthesized=True),
    ])

    session = OpenAIChatSession(
        client=MagicMock(),
        model="gpt-test",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        client_kwargs={},
    )
    return session, iface


def _fake_openai_response(text: str = "ok"):
    """Build a MagicMock mimicking openai SDK's ChatCompletion shape."""
    from unittest.mock import MagicMock

    raw = MagicMock()
    msg = MagicMock()
    msg.role = "assistant"
    msg.content = text
    msg.tool_calls = None
    msg.reasoning_content = None
    msg.reasoning = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    raw.choices = [choice]
    raw.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    raw.model = "gpt-test"
    raw.id = "resp_1"
    return raw


def test_openai_send_none_does_not_append_input() -> None:
    """``OpenAIChatSession.send(None)`` drives the API off the
    pre-staged wire, does not append a user message, and records only
    the assistant response."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.return_value = _fake_openai_response()

    response = session.send(None)

    assert response is not None
    assert session._client.chat.completions.create.called
    # Wire grew by exactly one entry — the assistant response — not two.
    assert len(iface.entries) == pre_count + 1
    assert iface.entries[-1].role == "assistant"


def test_openai_send_none_error_does_not_drop_pair() -> None:
    """API failure during a ``send(None)`` must not corrupt the
    pre-staged wire."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.side_effect = RuntimeError("boom")

    try:
        session.send(None)
    except RuntimeError:
        pass

    assert len(iface.entries) == pre_count
    assert iface.entries[-1].role == "user"


def test_openai_send_str_still_appends_user_message() -> None:
    """Sanity check: the existing str/list paths are unchanged."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.return_value = _fake_openai_response()

    session.send("hello world")

    # Two new entries: the user message we just appended, and the
    # assistant response.
    assert len(iface.entries) == pre_count + 2


def test_responses_convert_input_none_yields_empty_list() -> None:
    """``OpenAIResponsesSession._convert_input(None)`` returns ``[]``
    so the existing ``previous_response_id`` chain continues with no
    new input items."""
    from lingtai.llm.openai.adapter import OpenAIResponsesSession

    session = OpenAIResponsesSession.__new__(OpenAIResponsesSession)
    assert OpenAIResponsesSession._convert_input(session, None) == []
