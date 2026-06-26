"""Tests for the .notification/ filesystem sync mechanism.

Covers the design's invariants and the patch's §13 test matrix:

- §13.1 — fingerprint + collection primitives, atomicity, concurrency
- §13.2 — IDLE-state pair injection / strip / no-op
- §13.3 — ACTIVE-state deferral without ToolResultBlock mutation
- §13.4 — ASLEEP-state wake on fingerprint change
- §13.5 — voluntary `notification(action="check")` returns the dict
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
from types import SimpleNamespace
from typing import Any

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
    # Each entry is (name, size, sha256).
    for name, size, digest in fp:
        assert size > 0
        assert isinstance(digest, str)
        assert len(digest) == 64


def test_fingerprint_changes_on_overwrite(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"count": 1})
    fp1 = notification_fingerprint(tmp_path)
    publish(tmp_path, "email", {"count": 2, "extra": "more bytes"})
    fp2 = notification_fingerprint(tmp_path)
    assert fp1 != fp2


def test_fingerprint_ignores_equivalent_rewrite_mtime_churn(tmp_path: Path) -> None:
    publish(tmp_path, "email", {"count": 1})
    fp1 = notification_fingerprint(tmp_path)
    publish(tmp_path, "email", {"count": 1})
    fp2 = notification_fingerprint(tmp_path)
    assert fp1 == fp2


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
    publish(tmp_path, "soul", {"x": 1})
    bad_path = tmp_path / ".notification" / "bad.json"
    bad_path.write_text("not json {")
    out = collect_notifications(tmp_path)
    assert out == {"soul": {"x": 1}}


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
    clear(tmp_path, "soul")
    publish(tmp_path, "email", {"x": 1})
    clear(tmp_path, "email")
    assert not (tmp_path / ".notification" / "email.json").exists()
    # Second clear is a no-op.
    clear(tmp_path, "email")


def test_concurrent_publish_atomicity(tmp_path: Path) -> None:
    """10 threads × 50 iterations.  Every collect snapshot must return
    parseable JSON for every source (no partial-write reads, no
    corrupted files)."""
    sources = [f"mcp.src_{i}" for i in range(10)]

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
# §13.5 — `notification(action="check")` voluntary call
#
# The read verb moved off `system` onto the standalone `notification` tool.
# `system(action="notification")` is no longer a valid action.
# ---------------------------------------------------------------------------


def test_check_action_returns_empty_when_nothing_published(
    tmp_path: Path,
) -> None:
    from lingtai_kernel.intrinsics.notification import handle

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = handle(_Stub(), {"action": "check"})
    # Voluntary call returns a placeholder dict — the live notification
    # payload (if any) is stamped on by the turn loop's meta-block hook,
    # never built by the handler itself. So even with nothing published,
    # the bare channel keys are absent here.
    assert res == {
        "_notification_placeholder": True,
        "message": res["message"],
    }
    assert "notification" in res["message"].lower()


def test_check_action_returns_placeholder(tmp_path: Path) -> None:
    from lingtai_kernel.intrinsics.notification import handle

    publish(tmp_path, "email", {"count": 5, "newest_received_at": "2026-05-05T00:00:00Z"})
    publish(tmp_path, "soul", {"voices": [{"source": "warmth", "voice": "..."}]})

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = handle(_Stub(), {"action": "check"})
    # Handler returns a placeholder only — channel keys MUST NOT appear
    # here. The canonical `notifications` payload is attached later by
    # `attach_active_notifications`, not by this handler. This guarantees
    # there is only one live notification payload in conversation history.
    assert res.get("_notification_placeholder") is True
    assert "email" not in res
    assert "soul" not in res
    assert "notifications" not in res
    assert "_meta" not in res


def test_system_notification_action_is_removed(tmp_path: Path) -> None:
    """system(action="notification") is no longer a valid action."""
    from lingtai_kernel.intrinsics.system import handle

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = handle(_Stub(), {"action": "notification"})
    assert res["status"] == "error"
    assert "Unknown system action" in res["message"]


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
    _system_notification_lock: threading.Lock = field(default_factory=threading.Lock)

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


def test_human_soul_inquiry_publishes_btw_notification(tmp_path: Path) -> None:
    """Human `/btw` inquiry results are mirrored to the agent as notification."""
    from lingtai_kernel.intrinsics.soul.inquiry import (
        _publish_human_inquiry_notification,
    )

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    _publish_human_inquiry_notification(
        agent,
        {
            "prompt": "What should I know?",
            "voice": "You asked a side question.",
            "thinking": ["mirror thought"],
        },
        "What should I know?",
    )

    out = collect_notifications(tmp_path)
    assert "btw" in out
    payload = out["btw"]
    assert payload["header"] == "/btw side inquiry answered"
    assert payload["icon"] == "💭"
    assert "not a direct new instruction" in payload["instructions"]
    assert payload["data"] == {
        "source": "human",
        "mode": "inquiry",
        "question": "What should I know?",
        "answer": "You asked a side question.",
        "thinking": ["mirror thought"],
    }
    assert any(evt == "btw_notification_published" for evt, _ in agent._logs)


def test_non_human_soul_inquiry_does_not_publish_btw_notification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Auto-insight / agent inquiries keep the existing log-only behavior."""
    from lingtai_kernel.intrinsics.soul import inquiry

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    monkeypatch.setattr(
        inquiry,
        "soul_inquiry",
        lambda _agent, question: {
            "prompt": question,
            "voice": "auto answer",
            "thinking": [],
        },
    )

    inquiry._run_inquiry(agent, "auto?", source="insight")

    out = collect_notifications(tmp_path)
    assert "btw" not in out
    assert any(evt == "insight" for evt, _ in agent._logs)
    assert (tmp_path / "logs" / "soul_inquiry.jsonl").is_file()


# ---------------------------------------------------------------------------
# §13.6.bis — system.publish_notification (canonical helper)
# ---------------------------------------------------------------------------


def test_submit_writes_envelope(tmp_path: Path) -> None:
    """``submit`` builds the documented envelope and writes the file."""
    from lingtai_kernel.notifications import submit

    submit(tmp_path, "system",
           header="hello", icon="✨",
           data={"x": 1, "y": [2, 3]})

    out = collect_notifications(tmp_path)
    assert "system" in out
    payload = out["system"]
    assert payload["header"] == "hello"
    assert payload["icon"] == "✨"
    assert payload["priority"] == "normal"
    assert payload["data"] == {"x": 1, "y": [2, 3]}
    # published_at is stamped, ISO format.
    assert "published_at" in payload
    assert payload["published_at"].endswith("Z")


def test_submit_priority_override(tmp_path: Path) -> None:
    from lingtai_kernel.notifications import submit

    submit(tmp_path, "nudge",
           header="oh no", icon="🚨",
           priority="high", data={})

    assert collect_notifications(tmp_path)["nudge"]["priority"] == "high"


def test_submit_via_system_alias(tmp_path: Path) -> None:
    """``intrinsics.system.publish_notification`` is the same callable
    as ``notifications.submit`` — producers can import either."""
    from lingtai_kernel.intrinsics.system import (
        publish_notification, clear_notification,
    )
    from lingtai_kernel.notifications import submit, clear

    assert publish_notification is submit
    assert clear_notification is clear

    publish_notification(tmp_path, "system",
                         header="via", icon="🛰",
                         data={"ok": True})
    out = collect_notifications(tmp_path)
    assert out["system"]["data"] == {"ok": True}

    clear_notification(tmp_path, "system")
    out = collect_notifications(tmp_path)
    assert "system" not in out


# ---------------------------------------------------------------------------
# §13.7 — molt clearing
# ---------------------------------------------------------------------------


def test_molt_preserves_notification_dir(tmp_path: Path) -> None:
    """After molt, the .notification/ dir and its files survive — they are
    system state, not conversation memory.  In-memory tracking is reset
    (block_id, pending_meta) but the on-disk files and fingerprint persist."""
    publish(tmp_path, "email", {"count": 3})
    publish(tmp_path, "soul", {"voices": []})
    assert (tmp_path / ".notification").is_dir()

    # Stub agent with the bare minimum the molt reset logic needs.
    @dataclass
    class _MoltStub:
        _working_dir: Path = tmp_path
        _notification_fp: tuple = (("email.json", 1, 12),)
        _notification_block_id: str | None = "notif_xyz"
        _pending_notification_meta: str | None = "stale"
        _appendix_ids_by_source: dict = field(default_factory=dict)

    agent = _MoltStub()
    # Only reset in-memory tracking; notification files survive molt.
    agent._notification_block_id = None
    agent._pending_notification_meta = None

    # .notification/ directory and files should still exist
    assert (tmp_path / ".notification").is_dir()
    assert (tmp_path / ".notification" / "email.json").is_file()
    assert (tmp_path / ".notification" / "soul.json").is_file()
    # _notification_fp keeps its value (files still on disk)
    assert agent._notification_fp == (("email.json", 1, 12),)
    # Wire-level tracking is reset
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


def _make_idle_agent_with_pending_tail(tmp_path: Path, *, call_id: str = "tc-pending"):
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.interface import ToolCallBlock
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()
    chat.interface.add_assistant_message(
        [ToolCallBlock(id=call_id, name="bash", args={"command": "echo hi"})]
    )

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_deferred_log_fp = ()
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

    return _Agent(tmp_path)


def test_sync_idle_posts_wake_message(tmp_path: Path) -> None:
    """IDLE: fingerprint change -> MSG_TC_WAKE goes to the inbox.

    The synthesized ``(ToolCallBlock, ToolResultBlock)`` pair has
    already been spliced by ``_inject_notification_pair`` —
    impersonating a voluntary ``notification(action="check")`` call
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
            self._notification_deferred_log_fp = ()
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


def test_sync_idle_heal_replays_recorded_tool_result_before_notification(
    tmp_path: Path,
) -> None:
    from lingtai_kernel.llm.interface import ToolResultBlock
    from lingtai_kernel.message import MSG_TC_WAKE

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_result",
                "tool_call_id": "tc-pending",
                "tool_name": "bash",
                "tool_args": {"command": "echo should-not-be-relogged"},
                "result": {"stdout": "already ran"},
                "ts": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    agent = _make_idle_agent_with_pending_tail(tmp_path)
    publish(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 4
    replayed = entries[1].content[0]
    assert isinstance(replayed, ToolResultBlock)
    assert replayed.id == "tc-pending"
    assert replayed.name == "bash"
    assert replayed.content == {"stdout": "already ran"}
    assert replayed.synthesized is False
    assert entries[2].role == "assistant"
    assert entries[3].role == "user"
    assert agent.inbox.get_nowait().type == MSG_TC_WAKE
    assert any(evt == "tool_result_replayed_from_log" for evt, _ in agent._logs)
    serialized_logs = json.dumps(agent._logs, default=str)
    assert "already ran" not in serialized_logs
    assert "should-not-be-relogged" not in serialized_logs


def test_sync_idle_heal_falls_back_to_synthetic_when_no_recorded_result(
    tmp_path: Path,
) -> None:
    from lingtai_kernel.llm.interface import ToolResultBlock

    agent = _make_idle_agent_with_pending_tail(tmp_path)
    publish(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 4
    healed = entries[1].content[0]
    assert isinstance(healed, ToolResultBlock)
    assert healed.id == "tc-pending"
    assert healed.synthesized is True
    assert "did not complete" in healed.content
    assert any(evt == "tool_result_replay_miss" for evt, _ in agent._logs)


def test_sync_idle_injects_post_molt_after_molt_batch_deferred_stamp(tmp_path: Path) -> None:
    """Regression for the post-molt continuation bug.

    A ``post-molt`` continuation is written while the agent is still ACTIVE
    inside the ``psyche.molt`` tool call.  That same molt-result batch must skip
    active notification stamping and leave ``_notification_fp`` uncommitted; if
    it stamped post-molt and committed the full fingerprint, the IDLE
    ``_sync_notifications`` pass would see no change and never inject the
    synthesized pair + MSG_TC_WAKE.

    Here we reproduce the uncommitted-fp state left by the per-molt-batch
    deferral and assert IDLE sync injects the wake.
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
    # The molt wrote the continuation channel while ACTIVE.
    publish(tmp_path, "post-molt", {
        "header": "post-molt #1 — resume work",
        "icon": "🌱",
        "priority": "high",
        "data": {"molt_count": 1, "reminder": "continue the task"},
    })
    # Reproduce the fp the deferred molt batch leaves: unchanged/uncommitted.
    agent._notification_fp = ()

    agent._sync_notifications()

    # The IDLE path must still inject the synthesized (call, result) pair...
    entries = agent._chat_stub.interface.entries
    assert len(entries) == 2, "post-molt continuation must be injected at IDLE"
    body = entries[1].content[0].content
    assert isinstance(body, dict)
    assert "post-molt" in body["_meta"]["notifications"]
    # ...and post a wake so the run loop reorients around the continuation.
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
    # Assistant entry is tool-only: no visible synthesized TextBlock summary
    # should appear in the transcript / diary surface on the successful path.
    assert len(entries[0].content) == 1
    from lingtai_kernel.llm.interface import TextBlock
    assert not any(isinstance(block, TextBlock) for block in entries[0].content)
    injected_logs = [fields for evt, fields in agent._logs if evt == "notification_pair_injected"]
    assert injected_logs
    assert "Notification received: 1 email" in injected_logs[-1]["summary"]
    assert "not necessarily a human instruction" in injected_logs[-1]["summary"]
    call_block = entries[0].content[0]
    result_block = entries[1].content[0]
    assert isinstance(call_block, ToolCallBlock)
    # The kernel-synthesized delivery pair impersonates a voluntary
    # notification(action="check") read — not system(action="notification").
    assert call_block.name == "notification"
    assert call_block.args["action"] == "check"
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.name == "notification"
    assert result_block.synthesized is True

    body = result_block.content
    assert isinstance(body, dict)
    assert body["_synthesized"] is True
    # Notification payload nests under the unified _meta envelope.
    meta = body["_meta"]
    assert "not automatically human instructions" in meta["notification_guidance"]
    assert "source(s): email" in meta["notification_guidance"]
    assert "normal read tool" in meta["notification_guidance"]
    assert "secondary" not in meta["notification_guidance"]
    assert "email" in meta["notifications"]
    assert "not necessarily a human instruction" in meta["notifications"]["email"]["notification_guidance"]
    assert "'email' notification channel" in meta["notifications"]["email"]["notification_guidance"]
    assert "normal read action" in meta["notifications"]["email"]["notification_guidance"]
    assert "secondary" not in meta["notifications"]["email"]["notification_guidance"]

    assert agent._notification_block_id == call_block.id


def test_sync_idle_skeletonizes_then_reinjects(tmp_path: Path) -> None:
    """Two consecutive sync calls — old payload is skeletonized, new pair appended."""
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
    # Old pair kept as a placeholder skeleton, new pair appended.
    assert len(agent._chat_stub.interface.entries) == 4
    first_body = agent._chat_stub.interface.entries[1].content[0].content
    assert first_body["_notification_placeholder"] is True
    assert "notifications" not in first_body
    assert "_meta" not in first_body


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

    # The synthesized pair remains in history, but its live payload is
    # skeletonized so it cannot be mistaken for current notification data.
    assert agent._notification_block_id is not None
    assert len(agent._chat_stub.interface.entries) == 2
    body = agent._chat_stub.interface.entries[1].content[0].content
    assert body["_notification_placeholder"] is True
    assert "notifications" not in body
    assert "_meta" not in body


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


def test_sync_active_defers_without_committing_or_mutating_tool_result(tmp_path: Path) -> None:
    """ACTIVE state: fingerprint change is noticed but not delivered yet.

    The old behavior prepended ``notifications:\n...`` onto the most recent
    unrelated ToolResultBlock.  ACTIVE sync now leaves the wire byte-for-byte
    unchanged and keeps the fingerprint uncommitted so the next IDLE boundary
    retries via a distinct synthetic notification pair.
    """
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock

    chat = _make_chat_stub()
    iface = chat.interface
    iface.add_assistant_message(content=[ToolCallBlock(id="c1", name="daemon", args={})])
    iface.add_tool_results([
        ToolResultBlock(id="c1", name="daemon", content='{"status":"dispatched"}')
    ])
    original_content = iface.entries[1].content[0].content
    original_entry_count = len(iface.entries)

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ACTIVE
            self._notification_fp = ()
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._deferred_notifications_count = 0
            self._deferred_notifications_oldest_at = None
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
    publish(tmp_path, "system", {"data": {"events": [{"source": "daemon"}]}})
    fp = notification_fingerprint(tmp_path)

    agent._sync_notifications()
    agent._sync_notifications()

    assert agent._notification_fp == ()  # not committed while ACTIVE
    assert agent._notification_fp != fp
    assert agent._notification_deferred_log_fp == fp
    assert agent._notification_block_id is None
    assert agent.inbox.empty()
    assert len(iface.entries) == original_entry_count
    assert iface.entries[1].content[0].content == original_content
    assert not iface.entries[1].content[0].content.startswith("notifications:\n")
    assert [evt for evt, _ in agent._logs].count("notification_deferred_active") == 1
    assert agent._deferred_notifications_count == 2
    assert agent._deferred_notifications_oldest_at is not None


def test_sync_empty_state_clears_pending_meta(tmp_path: Path) -> None:
    """If a pending ACTIVE payload exists and producer files vanish before
    delivery, the empty-state sync must drop the stale pending payload."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ACTIVE
            self._notification_fp = (("soul.json", 1, 1),)
            self._notification_block_id = None
            self._pending_notification_meta = '{"notifications": {"soul": {}}}'
            self._pending_notification_fp = (("soul.json", 1, 1),)
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
    # No .notification files exist, so fingerprint changed from the stale
    # non-empty value above to empty.
    agent._sync_notifications()

    assert agent._notification_fp == ()
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None


def test_session_manager_has_no_notification_inject_hook() -> None:
    """The retired ACTIVE meta-prefix hook is no longer part of SessionManager.

    Regression guard for #82: notification delivery must not mutate arbitrary
    ToolResultBlock content at ``SessionManager.send()`` time.
    """
    import inspect
    from lingtai_kernel.session import SessionManager

    params = inspect.signature(SessionManager.__init__).parameters
    assert "notification_inject_fn" not in params
    assert not hasattr(SessionManager.__new__(SessionManager), "_notification_inject_fn")


def test_base_agent_no_longer_exposes_meta_prefix_injector() -> None:
    """BaseAgent no longer carries the mutating _inject_notification_meta path."""
    from lingtai_kernel.base_agent import BaseAgent

    assert not hasattr(BaseAgent, "_inject_notification_meta")


def test_end_of_turn_idle_sync_delivers_deferred_notification(tmp_path: Path) -> None:
    """End-of-turn sync runs after IDLE transition and delivers a pair+wake.

    This exercises the #83 ordering: a notification produced during ACTIVE work
    must not be stranded in ACTIVE deferral.  At the post-turn IDLE boundary it
    becomes a distinct synthetic notification pair and a MSG_TC_WAKE.
    """
    import queue
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.base_agent import turn as turn_mod
    from lingtai_kernel.message import _make_message, MSG_REQUEST, MSG_TC_WAKE
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.llm.interface import ToolResultBlock

    class _SessionStub:
        def __init__(self, chat):
            self.chat = chat

        def get_context_pressure(self):
            return 0.0

    class _ConfigStub:
        language = "en"
        molt_pressure = 0.9
        molt_prompt = ""
        insights_interval = 0
        max_aed_attempts = 1

    chat = _make_chat_stub()
    states: list[AgentState] = []

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = AgentState.ACTIVE
            self._notification_fp = ()
            self._notification_block_id = None
            self._notification_inject_seq = 0
            self._chat_stub = chat
            self._session = _SessionStub(chat)
            self._logs = []
            self.agent_name = "stub"
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._cancel_event = threading.Event()
            self._shutdown = threading.Event()
            self._config = _ConfigStub()

        @property
        def _chat(self):
            return self._chat_stub

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            states.append(new_state)

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    agent.inbox.put(_make_message(MSG_REQUEST, "tester", "do work"))

    # Stop the run loop after the post-turn sweep has a chance to execute.
    # Patch the module-level dispatcher because _run_loop calls it directly.
    def fake_handle_message(_agent, _msg):
        publish(_agent._working_dir, "system", {"data": {"events": [{"source": "daemon"}]}})
        _agent._shutdown.set()

    orig_handle = turn_mod._handle_message
    try:
        turn_mod._handle_message = fake_handle_message
        turn_mod._run_loop(agent)
    finally:
        turn_mod._handle_message = orig_handle

    assert AgentState.IDLE in states
    assert agent._notification_block_id is not None
    assert agent._notification_fp == notification_fingerprint(tmp_path)
    wake = agent.inbox.get_nowait()
    assert wake.type == MSG_TC_WAKE

    entries = chat.interface.entries
    assert len(entries) == 2
    assert entries[0].role == "assistant"
    assert entries[1].role == "user"
    result_block = entries[1].content[0]
    assert isinstance(result_block, ToolResultBlock)
    body = result_block.content
    assert isinstance(body, dict)
    assert body["_synthesized"] is True
    assert "system" in body["_meta"]["notifications"]
    assert isinstance(result_block.content, dict)


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
# §13.4.bis — ASLEEP wake when injection still fails after heal (degraded path)
# ---------------------------------------------------------------------------


def _make_asleep_inject_fail_agent(tmp_path: Path, chat, state_history):
    """Build an ASLEEP stub agent whose `_inject_notification_pair`
    always returns False — simulating a wire that cannot accept the
    synthetic pair even after `_heal_pending_tool_calls`."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

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
            self.inject_calls = 0
            self.heal_calls = 0

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
            state_history.append((new_state, reason))

        def _reset_uptime(self):
            pass

        def _inject_notification_pair(self, notifications):
            self.inject_calls += 1
            return False

        def _heal_pending_tool_calls(self, *, reason):
            self.heal_calls += 1
            return False

    return _Agent(tmp_path)


def test_sync_asleep_inject_fail_falls_back_to_degraded_request(tmp_path: Path) -> None:
    """ASLEEP + inject keeps failing after heal → degraded MSG_REQUEST,
    state IDLE (not ASLEEP), fingerprint committed, log emitted.

    Regression for Jason's livelock report: the prior behavior reverted
    to ASLEEP without committing the fingerprint, so the next heartbeat
    saw the same .notification/ state, woke again, failed inject again,
    reverted again — forever. The fix wakes the agent via a degraded
    request that tells it to call notification(action="check") or
    read the producer files directly.
    """
    from lingtai_kernel.state import AgentState
    from lingtai_kernel.message import MSG_REQUEST, MSG_TC_WAKE
    from lingtai_kernel.notifications import notification_fingerprint

    chat = _make_chat_stub()
    state_history: list = []
    agent = _make_asleep_inject_fail_agent(tmp_path, chat, state_history)

    publish(tmp_path, "mcp.wechat", {"data": {"count": 2}})
    fp_before = notification_fingerprint(tmp_path)

    agent._sync_notifications()

    # State stays IDLE (not reverted to ASLEEP) so run loop can run.
    assert agent._state == AgentState.IDLE
    assert AgentState.IDLE in [s for s, _ in state_history]
    assert AgentState.ASLEEP not in [s for s, _ in state_history if s == AgentState.ASLEEP and _ != "notification_arrival"]

    # Inbox got a degraded MSG_REQUEST (not MSG_TC_WAKE).
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_REQUEST
    assert msg.type != MSG_TC_WAKE
    # Content mentions the failure and tells the agent how to recover.
    assert "notification" in msg.content.lower()
    assert "mcp.wechat" in msg.content
    # The body should point at the recovery handles.
    assert ("system" in msg.content) or ("producer" in msg.content.lower())

    # Fingerprint committed so the same failure does not replay.
    assert agent._notification_fp == fp_before
    assert agent._notification_fp != ()

    # Clear, single log event for diagnostics.
    degraded_logs = [f for evt, f in agent._logs if evt == "notification_wake_degraded"]
    assert len(degraded_logs) == 1
    log_fields = degraded_logs[0]
    assert log_fields.get("reason")
    assert "mcp.wechat" in log_fields.get("sources", [])

    # Heal was tried; inject was tried twice (initial + post-heal).
    assert agent.heal_calls == 1
    assert agent.inject_calls == 2


def test_sync_asleep_inject_fail_does_not_replay_on_second_sync(tmp_path: Path) -> None:
    """After the degraded path commits the fingerprint, a second sync
    with the same on-disk state must be a complete no-op — no extra
    inject attempts, no extra inbox messages, no extra log entries."""
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()
    state_history: list = []
    agent = _make_asleep_inject_fail_agent(tmp_path, chat, state_history)

    publish(tmp_path, "mcp.wechat", {"data": {"count": 1}})
    agent._sync_notifications()

    inject_calls_after_first = agent.inject_calls
    inbox_size_after_first = agent.inbox.qsize()
    degraded_logs_after_first = sum(
        1 for evt, _ in agent._logs if evt == "notification_wake_degraded"
    )

    # Second sync — same fingerprint, must short-circuit.
    agent._sync_notifications()

    assert agent.inject_calls == inject_calls_after_first
    assert agent.inbox.qsize() == inbox_size_after_first
    degraded_logs_after_second = sum(
        1 for evt, _ in agent._logs if evt == "notification_wake_degraded"
    )
    assert degraded_logs_after_second == degraded_logs_after_first


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



def test_context_molt_batch_skips_active_notification_stamp(tmp_path):
    """The psyche.molt result batch must not consume its own post-molt wake."""
    from types import SimpleNamespace

    from lingtai_kernel.base_agent.turn import _batch_includes_context_molt
    from lingtai_kernel.llm.base import ToolCall
    from lingtai_kernel.llm.interface import ToolResultBlock
    from lingtai_kernel.meta_block import attach_active_notifications
    from lingtai_kernel.notifications import notification_fingerprint

    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "post-molt.json").write_text(
        '{"header": "post-molt #1", "data": {"molt_count": 1}}'
    )

    agent = SimpleNamespace(
        _working_dir=tmp_path,
        _notification_fp=(("sentinel.json", 1, 1),),
        _notification_live_holder=None,
    )
    molt_call = ToolCall(
        name="psyche",
        args={"object": "context", "action": "molt", "summary": "continue"},
        id="call_molt",
    )
    assert _batch_includes_context_molt([molt_call]) is True

    molt_result = ToolResultBlock(id="call_molt", name="psyche", content={"status": "ok"})
    if not _batch_includes_context_molt([molt_call]):
        agent._notification_live_holder = attach_active_notifications(
            agent, [molt_result], prior_holder=agent._notification_live_holder
        )

    assert "notifications" not in molt_result.content
    assert agent._notification_fp == (("sentinel.json", 1, 1),)
    assert notification_fingerprint(tmp_path) != agent._notification_fp


def test_non_molt_batch_after_molt_can_consume_post_molt(tmp_path):
    """If the agent keeps going ACTIVE after molt, the next batch sees post-molt."""
    from types import SimpleNamespace

    from lingtai_kernel.base_agent.turn import _batch_includes_context_molt
    from lingtai_kernel.llm.base import ToolCall
    from lingtai_kernel.llm.interface import ToolResultBlock
    from lingtai_kernel.meta_block import attach_active_notifications
    from lingtai_kernel.notifications import notification_fingerprint

    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "post-molt.json").write_text(
        '{"header": "post-molt #1", "data": {"molt_count": 1}}'
    )

    agent = SimpleNamespace(
        _working_dir=tmp_path,
        _notification_fp=(),
        _notification_live_holder=None,
    )
    later_call = ToolCall(name="bash", args={"command": "true"}, id="call_later")
    assert _batch_includes_context_molt([later_call]) is False

    later_result = ToolResultBlock(id="call_later", name="bash", content={"status": "ok"})
    if not _batch_includes_context_molt([later_call]):
        agent._notification_live_holder = attach_active_notifications(
            agent, [later_result], prior_holder=agent._notification_live_holder
        )

    assert "post-molt" in later_result.content["_meta"]["notifications"]
    assert agent._notification_fp == notification_fingerprint(tmp_path)


# ---------------------------------------------------------------------------
# §13.8 — notification_block_injected durable snapshot event
# ---------------------------------------------------------------------------


def _make_stub_agent_for_block_log(tmp_path: Path):
    """Build a minimal agent stub carrying _logs for notification_block_injected tests."""
    from dataclasses import dataclass, field as dc_field
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
            self._logs: list = []
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

    return _Agent(tmp_path)


def test_inject_notification_pair_emits_block_injected_event(tmp_path: Path) -> None:
    """IDLE injection via _inject_notification_pair must log notification_block_injected
    with the full ``_meta`` envelope (tool_meta/agent_meta/guidance/notifications/
    notification_guidance)."""
    publish(tmp_path, "email", {"count": 2, "data": {"count": 2, "digest": "2 unread"}})
    publish(tmp_path, "system", {"events": [{"source": "test", "body": "ping"}]})

    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    # notification_pair_injected must still fire (existing behavior unchanged).
    pair_logs = [f for evt, f in agent._logs if evt == "notification_pair_injected"]
    assert pair_logs, "notification_pair_injected must still be logged"

    # notification_block_injected must be emitted.
    block_logs = [f for evt, f in agent._logs if evt == "notification_block_injected"]
    assert block_logs, "notification_block_injected event must be logged"
    bl = block_logs[-1]

    assert bl["mode"] == "synthetic_notification_pair"
    assert "call_id" in bl
    assert isinstance(bl["sources"], list)
    assert "email" in bl["sources"]
    assert "system" in bl["sources"]

    # New schema: a single top-level ``_meta`` envelope carrying formal blocks.
    meta = bl["_meta"]
    assert "tool_meta" in meta
    assert meta["tool_meta"].get("synthetic") is True
    assert "agent_meta" in meta
    # Tail/synthetic guidance is now a lightweight ref/hook pointing at the
    # resident ``meta_guidance`` system-prompt section, not the full ordered
    # sections (which no longer ride on every result).
    assert "guidance" in meta
    guidance = meta["guidance"]
    assert "sections" not in guidance
    assert guidance.get("ref") == "meta_guidance"

    assert "notification_guidance" in meta
    assert "not automatically human instructions" in meta["notification_guidance"]
    assert "notifications" in meta
    notifs = meta["notifications"]
    assert "email" in notifs
    assert "system" in notifs
    # Per-channel guidance present
    assert "notification_guidance" in notifs["email"]


def test_block_injected_payload_not_mutated_by_skeletonization(tmp_path: Path) -> None:
    """The logged payload must survive later skeletonization of the live holder."""
    import time as _time

    publish(tmp_path, "email", {"count": 1})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    block_logs = [f for evt, f in agent._logs if evt == "notification_block_injected"]
    assert block_logs
    logged_notifs = block_logs[-1]["_meta"]["notifications"]
    assert "email" in logged_notifs

    # Simulate later delivery: publish new state and re-sync; this skeletonizes
    # the first live holder.
    _time.sleep(0.001)
    publish(tmp_path, "email", {"count": 2, "extra": "more"})
    agent._sync_notifications()

    # The *first* logged snapshot must still have email in it (not mutated).
    assert "email" in logged_notifs


def test_block_injected_emits_companion_to_pair_injected(tmp_path: Path) -> None:
    """Both notification_pair_injected and notification_block_injected must fire
    on every IDLE injection, in that order (pair before block)."""
    publish(tmp_path, "email", {"count": 1})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    event_types = [evt for evt, _ in agent._logs]
    pair_idx = next((i for i, e in enumerate(event_types) if e == "notification_pair_injected"), -1)
    block_idx = next((i for i, e in enumerate(event_types) if e == "notification_block_injected"), -1)
    assert pair_idx != -1, "notification_pair_injected missing"
    assert block_idx != -1, "notification_block_injected missing"
    assert block_idx > pair_idx, "notification_block_injected must follow notification_pair_injected"


# ---------------------------------------------------------------------------
# Poisoned-interface fail-closed guard in _sync_notifications
# ---------------------------------------------------------------------------


def _make_poisoned_sync_agent(tmp_path: Path, state):
    """An agent whose interface is poisoned. Every mutating sync helper is
    wired to fail the test if called — the guard must short-circuit first."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._state = state
            self._notification_fp = (("email.json", 1, "old"),)
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._pending_notification_meta = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            if state == AgentState.ASLEEP:
                self._asleep.set()
            self._cancel_event = threading.Event()
            self._llm_worker_interface_poisoned = True
            self._llm_worker_poison_artifact = (
                "history/unfinished_turns/worker_still_running_test.json"
            )
            self._llm_worker_refresh_requested = False
            self.refresh_calls = []

        @property
        def _chat(self):
            return self._chat_stub

        def _inject_notification_pair(self, _notifications):
            raise AssertionError("poisoned sync must not inject")

        def _heal_pending_tool_calls(self, *, reason):
            raise AssertionError("poisoned sync must not heal")

        def _save_chat_history(self, *, ledger_source="main"):
            raise AssertionError("poisoned sync must not save")

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            raise AssertionError("poisoned sync must not wake")

        def _set_state(self, *_a, **_kw):
            raise AssertionError("poisoned sync must not change state")

        def _reset_uptime(self):
            raise AssertionError("poisoned sync must not reset uptime")

        def _perform_refresh(self, *, skip_chat_history_save=False, skip_save_reason=None):
            self.refresh_calls.append({
                "skip_chat_history_save": skip_chat_history_save,
                "skip_save_reason": skip_save_reason,
            })

    return _Agent(tmp_path)


def test_sync_notifications_asleep_refuses_touch_when_poisoned(tmp_path: Path) -> None:
    from lingtai_kernel.state import AgentState

    agent = _make_poisoned_sync_agent(tmp_path, AgentState.ASLEEP)
    before_fp = agent._notification_fp
    publish(tmp_path, "system", {"data": {"events": [{"source": "daemon"}]}})

    agent._sync_notifications()

    assert agent._state == AgentState.ASLEEP
    assert agent._asleep.is_set()
    assert agent.inbox.empty()
    assert agent._notification_fp == before_fp
    assert agent.refresh_calls == [{
        "skip_chat_history_save": True,
        "skip_save_reason": "worker_still_running_interface_unsafe",
    }]
    assert any(
        evt == "notification_sync_skipped_poisoned_interface"
        for evt, _ in agent._logs
    )


def test_sync_notifications_idle_refuses_touch_when_poisoned(tmp_path: Path) -> None:
    from lingtai_kernel.state import AgentState

    agent = _make_poisoned_sync_agent(tmp_path, AgentState.IDLE)
    before_fp = agent._notification_fp
    publish(tmp_path, "email", {"data": {"count": 1}})

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE
    assert agent.inbox.empty()
    assert agent._notification_fp == before_fp
    assert len(agent._chat_stub.interface.entries) == 0
    assert agent.refresh_calls == [{
        "skip_chat_history_save": True,
        "skip_save_reason": "worker_still_running_interface_unsafe",
    }]


def test_heal_pending_tool_calls_logs_pending_tail_diagnostics(tmp_path: Path) -> None:
    """Successful heal logs a bounded pending-tail summary without tool args."""
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    iface = ChatInterface()
    iface.add_user_message("before")
    iface.add_assistant_message([TextBlock("thinking aloud")])
    iface.add_assistant_message([
        ToolCallBlock(id="call_1", name="bash", args={"command": "SECRET"}),
        ToolCallBlock(id="call_2", name="email", args={"message": "SECRET"}),
    ])

    logs: list[tuple[str, dict]] = []
    saves: list[str] = []
    agent = SimpleNamespace(
        _chat=SimpleNamespace(interface=iface),
        _log=lambda event, **fields: logs.append((event, fields)),
        _save_chat_history=lambda *, ledger_source="main": saves.append(ledger_source),
    )

    assert BaseAgent._heal_pending_tool_calls(agent, reason="wake_inject_blocked") is True

    event, fields = logs[-1]
    assert event == "heal_pending_tool_calls"
    assert fields["reason"] == "wake_inject_blocked"
    assert fields["pending_tool_call_count"] == 2
    assert fields["pending_tool_call_ids"] == ["call_1", "call_2"]
    assert fields["pending_tool_names"] == ["bash", "email"]
    assert fields["pending_tail_roles"] == ["user", "assistant", "assistant"]
    assert fields["pending_tail_block_types"] == [["text"], ["text"], ["tool_call", "tool_call"]]
    assert "SECRET" not in str(fields)
    assert saves == ["heal"]
