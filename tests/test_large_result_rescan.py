"""Tests for the live chat-history large-result rescan.

Covers acceptance requirements from the fix/large-result-rescan-20260619 task:
1. Existing large ToolResultBlock triggers notification on rescan (no new tool exec).
2. Already-summarized ToolResultBlock is skipped.
3. Spill manifest with original_char_count > threshold triggers; <= threshold does not.
4. Active notification with same ref_id is not duplicated; after absent/dismissed it re-emits.
5. Threshold 0 disables rescan.
6. Synthesized blocks are skipped.
7. daemon_tool_result blocks are excluded.
8. _rescan_large_tool_results is callable on BaseAgent (boundary-level integration).
9. skip_if_ref_id_exists dedup works in _enqueue_system_notification.
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from lingtai_kernel.llm.base import LLMResponse, ToolCall
from lingtai_kernel.llm.interface import (
    ChatInterface,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.intrinsics.system.summarize import SUMMARIZE_MARKER
from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER
from lingtai_kernel.base_agent.messaging import (
    _rescan_large_tool_results,
    _enqueue_system_notification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_agent(chat_interface: ChatInterface | None = None):
    """Return a minimal stub agent for messaging tests."""
    iface = chat_interface if chat_interface is not None else ChatInterface()

    class _StubChat:
        interface = iface

    agent = MagicMock()
    agent._chat = _StubChat()
    agent._chat.interface = iface
    agent._log = MagicMock()
    agent._summarize_notification_threshold = 5000
    agent._system_notification_lock = threading.Lock()

    published: list[dict] = []

    def _fake_enqueue(*, source, ref_id, body, skip_if_ref_id_exists=False):
        if skip_if_ref_id_exists:
            for ev in published:
                if ev.get("ref_id") == ref_id:
                    return ""
        evt_id = f"evt_{len(published):03d}"
        published.append({"source": source, "ref_id": ref_id, "body": body, "event_id": evt_id})
        return evt_id

    agent._enqueue_system_notification = _fake_enqueue
    agent._published = published
    return agent


def _add_tool_pair(iface: ChatInterface, call_id: str, tool_name: str, result_content):
    """Append a (tool_call, tool_result) pair to the interface."""
    iface.add_assistant_message([ToolCallBlock(id=call_id, name=tool_name, args={})])
    iface.add_tool_results([ToolResultBlock(id=call_id, name=tool_name, content=result_content)])


# Large-result notifications are total-length-gated: they only fire once the
# COMBINED effective length of all pending large-result cases (each above the
# per-result threshold) is strictly greater than 50000 chars
# (LARGE_RESULT_TOTAL_LEN_GATE).  Tests that want the rescan to fire stock
# enough pending large-result text to clear it.
TOTAL_GATE = 50000


def _add_synthesized_tool_pair(iface: ChatInterface, call_id: str, tool_name: str, result_content):
    """Append a synthesized (tool_call, tool_result) pair to the interface."""
    iface.add_assistant_message([ToolCallBlock(id=call_id, name=tool_name, args={})])
    iface.add_tool_results([ToolResultBlock(id=call_id, name=tool_name, content=result_content, synthesized=True)])


# ---------------------------------------------------------------------------
# 1. Existing large ToolResultBlock triggers notification on rescan
# ---------------------------------------------------------------------------


def test_rescan_fires_for_single_block_over_total_gate():
    """A single pending result whose length exceeds 50000 chars triggers by itself."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-large", "bash", "X" * 55_000)  # >50000 by itself
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 1  # one notification per pending case
    ref_ids = {p["ref_id"] for p in agent._published}
    assert "large_tool_result:tc-large" in ref_ids
    pub = next(p for p in agent._published if p["ref_id"] == "large_tool_result:tc-large")
    assert pub["source"] == "large_tool_result"
    assert "tc-large" in pub["body"]
    assert "summarize" in pub["body"]


def test_rescan_no_fire_when_total_at_or_below_gate():
    """Pending long-result total <= 50000 must NOT fire (e.g. 3 x 6000 = 18000)."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-1", "bash", "A" * 6000)
    _add_tool_pair(iface, "tc-2", "bash", "B" * 6000)
    _add_tool_pair(iface, "tc-3", "bash", "C" * 6000)  # total = 18000 <= 50000
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0, "total 18000 <= 50000 must not fire"
    assert agent._published == []


def test_rescan_no_fire_when_total_exactly_at_gate():
    """Pending total EXACTLY 50000 must NOT fire (gate is strictly > 50000)."""
    iface = ChatInterface()
    # Two 15000-char + one 20000-char long results = 50000 exactly.
    _add_tool_pair(iface, "tc-1", "bash", "A" * 15000)
    _add_tool_pair(iface, "tc-2", "bash", "B" * 15000)
    _add_tool_pair(iface, "tc-3", "bash", "C" * 20000)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0, "total exactly 50000 must not fire (strictly > gate)"
    assert agent._published == []


def test_rescan_no_fire_for_many_smaller_long_results_under_total():
    """Many long results (more than the old >5 count) below the total gate must NOT fire.

    Jason's example: 6 x 8001 chars (each a long result at the 3000 default
    threshold) sums to 48006 <= 50000, so it must stay quiet — proving the gate
    is on total length, not count.
    """
    iface = ChatInterface()
    for i in range(6):
        _add_tool_pair(iface, f"tc-{i}", "bash", "X" * 8001)  # total = 48006
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 3000  # default

    count = _rescan_large_tool_results(agent)
    assert count == 0, "6 x 8001 = 48006 <= 50000 must not fire despite 6 cases"
    assert agent._published == []


def test_rescan_fires_once_total_exceeds_gate_across_multiple():
    """Several pending long results fire once their combined total exceeds 50000."""
    iface = ChatInterface()
    # 7 x 8000 = 56000 > 50000 — fires; count (7) is irrelevant to the gate.
    for i in range(7):
        _add_tool_pair(iface, f"tc-{i}", "bash", "X" * 8000)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 7  # one notification per pending case
    assert len(agent._published) == 7


def test_rescan_no_fire_for_small_block():
    """Block under threshold produces no notification."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-small", "bash", "X" * 100)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


def test_rescan_fires_for_multiple_large_blocks():
    """Once the total gate is cleared, multiple large blocks each fire a separate notification."""
    iface = ChatInterface()
    # 4 x 13000 = 52000 > 50000 — clears the total-length gate.
    _add_tool_pair(iface, "tc-a", "bash", "A" * 13000)
    _add_tool_pair(iface, "tc-b", "read", "B" * 13000)
    _add_tool_pair(iface, "tc-c", "bash", "C" * 13000)
    _add_tool_pair(iface, "tc-d", "read", "D" * 13000)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 4
    ref_ids = {p["ref_id"] for p in agent._published}
    assert "large_tool_result:tc-a" in ref_ids
    assert "large_tool_result:tc-b" in ref_ids


# ---------------------------------------------------------------------------
# 2. Already-summarized ToolResultBlock is skipped
# ---------------------------------------------------------------------------


def test_rescan_skips_summarized_block():
    """Block with SUMMARIZE_MARKER artifact is skipped."""
    iface = ChatInterface()
    summarized_content = {
        "artifact": SUMMARIZE_MARKER,
        "agent_summary": "my summary",
        "tool_call_id": "tc-001",
        "original_visible_chars": 9000,
    }
    _add_tool_pair(iface, "tc-001", "bash", summarized_content)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


# ---------------------------------------------------------------------------
# 3. Spill manifest with original_char_count > threshold triggers; <= does not
# ---------------------------------------------------------------------------


def test_rescan_spill_over_threshold_triggers():
    """Spill manifest with original_char_count exceeding threshold fires notification."""
    iface = ChatInterface()
    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.txt",
        "cap_chars": 100_000,
        "original_char_count": 55_000,
    }
    _add_tool_pair(iface, "tc-spill", "bash", spill)  # original 55000 > 50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 1
    body = next(
        p["body"] for p in agent._published
        if p["ref_id"] == "large_tool_result:tc-spill"
    )
    assert "spill" in body.lower() or "sidecar" in body.lower()
    assert "foo.txt" in body


def test_rescan_spill_under_threshold_no_trigger():
    """Spill manifest with original_char_count at or below threshold is skipped."""
    iface = ChatInterface()
    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.txt",
        "cap_chars": 100_000,
        "original_char_count": 3000,
    }
    _add_tool_pair(iface, "tc-spill-small", "bash", spill)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


def test_rescan_spill_no_original_count_skipped():
    """Spill manifest missing original_char_count is skipped (cannot determine size)."""
    iface = ChatInterface()
    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.txt",
        "cap_chars": 100_000,
        # no original_char_count
    }
    _add_tool_pair(iface, "tc-spill-nocount", "bash", spill)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


# ---------------------------------------------------------------------------
# 4. Dedup: existing notification not duplicated; after absent/dismissed, re-emits
# ---------------------------------------------------------------------------


def test_rescan_does_not_duplicate_existing_notification():
    """If notifications for ref_ids already present, rescan does not publish again."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-dup", "bash", "X" * 55_000)  # >50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    # First rescan — publishes one per pending case
    count1 = _rescan_large_tool_results(agent)
    assert count1 == 1
    assert len(agent._published) == 1

    # Second rescan — ref_id already present, must skip
    count2 = _rescan_large_tool_results(agent)
    assert count2 == 0
    assert len(agent._published) == 1, "must not duplicate notifications"


def test_rescan_re_emits_after_notification_dismissed():
    """After dismissal (notifications removed from published list), rescan re-emits."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-dismissed", "bash", "X" * 55_000)  # >50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    # First rescan — publishes one per pending case
    count1 = _rescan_large_tool_results(agent)
    assert count1 == 1
    assert len(agent._published) == 1

    # Simulate dismiss — clear the published list
    agent._published.clear()

    # Second rescan — notification absent and total still > gate, should re-emit
    count2 = _rescan_large_tool_results(agent)
    assert count2 == 1
    assert len(agent._published) == 1


# ---------------------------------------------------------------------------
# 5. Threshold 0 disables rescan
# ---------------------------------------------------------------------------


def test_rescan_threshold_zero_disables():
    """Threshold <= 0 disables all rescan notifications."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-big", "bash", "X" * 999_999)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 0

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


def test_rescan_threshold_negative_disables():
    """Negative threshold also disables rescan."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-neg", "bash", "X" * 999_999)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = -1

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


# ---------------------------------------------------------------------------
# 6. Synthesized blocks are skipped
# ---------------------------------------------------------------------------


def test_rescan_skips_synthesized_blocks():
    """Synthesized ToolResultBlocks (heal/notification placeholders) are excluded."""
    iface = ChatInterface()
    _add_synthesized_tool_pair(iface, "tc-synth", "system", {"_synthesized": True, "data": "X" * 6000})
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


# ---------------------------------------------------------------------------
# 7. daemon_tool_result exclusion
# ---------------------------------------------------------------------------


def test_rescan_excludes_daemon_tool_result():
    """daemon_tool_result blocks are excluded from rescan."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-daemon", "daemon_tool_result", "X" * 10_000)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 0
    assert agent._published == []


def test_rescan_includes_bare_daemon_tool():
    """Bare 'daemon' tool (not daemon_tool_result) is NOT excluded."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-daemon-bare", "daemon", "X" * 55_000)  # >50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    count = _rescan_large_tool_results(agent)
    assert count == 1
    ref_ids = {p["ref_id"] for p in agent._published}
    assert "large_tool_result:tc-daemon-bare" in ref_ids


# ---------------------------------------------------------------------------
# 8. Integration: BaseAgent._rescan_large_tool_results passes through
# ---------------------------------------------------------------------------


def test_base_agent_has_rescan_method(tmp_path):
    """BaseAgent exposes _rescan_large_tool_results as a callable."""
    from lingtai_kernel.base_agent import BaseAgent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="test-rescan", working_dir=tmp_path / "ag")
    assert callable(agent._rescan_large_tool_results)
    # With no chat session built yet, rescan should be a no-op (0 published)
    result = agent._rescan_large_tool_results()
    assert result == 0


def test_base_agent_rescan_with_chat_session(tmp_path):
    """With a real BaseAgent and chat session, rescan finds large blocks."""
    from lingtai_kernel.base_agent import BaseAgent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="test-rescan-chat", working_dir=tmp_path / "ag2")
    agent._summarize_notification_threshold = 100

    # Build a chat session with a large tool result whose length alone exceeds
    # the 50000-char total-length gate.
    from lingtai_kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
    iface = ChatInterface()
    iface.add_assistant_message([ToolCallBlock(id="tc-real-001", name="bash", args={})])
    iface.add_tool_results([ToolResultBlock(id="tc-real-001", name="bash", content="X" * 55_000)])

    class _FakeChat:
        interface = iface

    agent._chat = _FakeChat()

    published: list[dict] = []
    original_enqueue = agent._enqueue_system_notification

    def _capture(**kw):
        published.append(kw)
        return original_enqueue(**kw)

    agent._enqueue_system_notification = _capture

    count = agent._rescan_large_tool_results()
    assert count == 1
    assert len(published) == 1
    ref_ids = {p["ref_id"] for p in published}
    assert "large_tool_result:tc-real-001" in ref_ids


# ---------------------------------------------------------------------------
# 9. _enqueue_system_notification skip_if_ref_id_exists dedup
# ---------------------------------------------------------------------------


def test_enqueue_skip_if_ref_id_exists(tmp_path):
    """skip_if_ref_id_exists=True skips publishing when ref_id already in events."""
    from lingtai_kernel.base_agent import BaseAgent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="test-dedup", working_dir=tmp_path / "ag")

    # First publish — should succeed
    ev1 = _enqueue_system_notification(
        agent,
        source="large_tool_result",
        ref_id="large_tool_result:tc-test-001",
        body="first notification",
        skip_if_ref_id_exists=False,
    )
    assert ev1 != ""

    # Second publish with same ref_id and skip_if_ref_id_exists=True — must skip
    ev2 = _enqueue_system_notification(
        agent,
        source="large_tool_result",
        ref_id="large_tool_result:tc-test-001",
        body="second notification — same ref_id",
        skip_if_ref_id_exists=True,
    )
    assert ev2 == "", "must return empty string when skipped"

    # Verify only one event in system.json
    from lingtai_kernel.notifications import collect_notifications
    notifs = collect_notifications(agent._working_dir)
    events = notifs.get("system", {}).get("data", {}).get("events", [])
    ref_ids = [ev.get("ref_id") for ev in events]
    assert ref_ids.count("large_tool_result:tc-test-001") == 1


def test_enqueue_no_skip_publishes_twice(tmp_path):
    """Without skip_if_ref_id_exists, same ref_id is published twice (normal behavior)."""
    from lingtai_kernel.base_agent import BaseAgent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="test-nodedup", working_dir=tmp_path / "ag")

    ev1 = _enqueue_system_notification(
        agent,
        source="large_tool_result",
        ref_id="large_tool_result:tc-dup-001",
        body="first",
        skip_if_ref_id_exists=False,
    )
    ev2 = _enqueue_system_notification(
        agent,
        source="large_tool_result",
        ref_id="large_tool_result:tc-dup-001",
        body="second",
        skip_if_ref_id_exists=False,
    )
    assert ev1 != ""
    assert ev2 != ""
    assert ev1 != ev2

    from lingtai_kernel.notifications import collect_notifications
    notifs = collect_notifications(agent._working_dir)
    events = notifs.get("system", {}).get("data", {}).get("events", [])
    ref_ids = [ev.get("ref_id") for ev in events]
    assert ref_ids.count("large_tool_result:tc-dup-001") == 2


# ---------------------------------------------------------------------------
# 10. Rescan with no chat session is safe no-op
# ---------------------------------------------------------------------------


def test_rescan_no_chat_session_is_noop():
    """If agent has no chat session, rescan returns 0 and logs nothing."""
    agent = MagicMock()
    agent._chat = None
    agent._summarize_notification_threshold = 5000
    agent._log = MagicMock()

    count = _rescan_large_tool_results(agent)
    assert count == 0


# ---------------------------------------------------------------------------
# 11. Rescan body content is bounded (no raw oversized payloads)
# ---------------------------------------------------------------------------


def test_rescan_body_preview_is_bounded():
    """Notification body for non-spill results includes only first 200 chars as preview."""
    iface = ChatInterface()
    large_content = "Z" * 55_000  # >50000 alone, clears the total-length gate
    _add_tool_pair(iface, "tc-preview", "read", large_content)
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 100

    _rescan_large_tool_results(agent)
    body = next(
        p["body"] for p in agent._published
        if p["ref_id"] == "large_tool_result:tc-preview"
    )
    # The preview in the body should not contain more than 200 chars of raw content
    # (the body itself is longer due to formatting, but raw content is capped at 200)
    # The body is bounded — it should NOT contain the full 10k string
    assert "Z" * 201 not in body, "raw content in body should be capped at 200 chars"


def test_process_response_rescans_after_tool_loop_continuation_round():
    """Tool-loop continuation rounds also rescan live history.

    Request and notification-wake boundaries are not the only LLM rounds: a
    tool result can be sent back to the provider and yield another response in
    the same outer turn.  Jason's requirement was per-round rediscovery, so the
    post-continuation sync_notifications path must invoke the rescan too.
    """
    from lingtai_kernel.base_agent import turn

    agent = MagicMock()
    agent._cancel_event = MagicMock()
    agent._cancel_event.is_set.return_value = False
    guard = MagicMock()
    guard.check_limit.return_value = None
    guard.check_invalid_tool_limit.return_value = None
    agent._executor.guard = guard
    agent._tool_loop_count = 0
    agent._registered_mcp_servers = []
    agent._intercept_config = {"enabled": False}
    agent._chat = MagicMock()
    agent._chat.commit_tool_results = MagicMock()
    agent._session = MagicMock()
    agent._session.send.return_value = LLMResponse(text="done", tool_calls=[])
    agent._last_usage = None

    first = LLMResponse(
        text="",
        tool_calls=[ToolCall(name="system", args={"action": "presets"}, id="tc-loop")],
    )
    tool_result = ToolResultBlock(id="tc-loop", name="system", content="x" * 200)
    agent._executor.execute.return_value = ([tool_result], False, "")

    with patch.object(turn, "attach_active_notifications", return_value=None), \
         patch.object(turn, "_check_external_send"), \
         patch.object(turn, "_check_poll_backoff", return_value=False), \
         patch.object(turn, "_check_molt_pressure"):
        result = turn._process_response(agent, first)

    assert result == {"text": "done", "failed": False, "errors": []}
    agent._session.send.assert_called_once_with([tool_result])
    agent._sync_notifications.assert_called()
    agent._rescan_large_tool_results.assert_called()


# ---------------------------------------------------------------------------
# 12. Rescan body wording: no raise/disable threshold; batch-digest guidance
# ---------------------------------------------------------------------------


def test_rescan_body_no_raise_disable_threshold_wording():
    """Rescan notification body must NOT instruct agents to raise or disable the threshold."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-wording", "bash", "X" * 55_000)  # >50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    _rescan_large_tool_results(agent)
    body = next(
        p["body"] for p in agent._published
        if p["ref_id"] == "large_tool_result:tc-wording"
    )
    assert "raise or disable the threshold" not in body, (
        "rescan body must not say 'raise or disable the threshold'"
    )


def test_rescan_body_batch_digest_or_tolerate_wording():
    """Rescan notification body must mention batch-digest all pending or tolerate reminders."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-batch", "bash", "Y" * 55_000)  # >50000 alone
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    _rescan_large_tool_results(agent)
    body = next(
        p["body"] for p in agent._published
        if p["ref_id"] == "large_tool_result:tc-batch"
    )
    # Must mention config/init as the only way to change threshold
    assert "init" in body.lower() or "config" in body.lower() or "refresh" in body.lower(), (
        "rescan body must mention init/config/refresh as the only way to change threshold"
    )
    # Must mention the batched total-length pending gate
    assert "batched" in body.lower() or "pending" in body.lower(), (
        "rescan body must explain the batch / pending-total gate"
    )
    # Must reference the 50000-char total-length gate, not a count gate
    assert "50000" in body, "rescan body must state the 50000-char total-length gate"


def test_rescan_spill_body_no_raise_disable_threshold_wording():
    """Spill rescan notification body must NOT instruct agents to raise or disable threshold."""
    iface = ChatInterface()
    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/wording-test.txt",
        "cap_chars": 100_000,
        "original_char_count": 55_000,
    }
    _add_tool_pair(iface, "tc-spill-wording", "bash", spill)  # original 55000 > 50000
    agent = _make_stub_agent(iface)
    agent._summarize_notification_threshold = 5000

    _rescan_large_tool_results(agent)
    body = next(
        p["body"] for p in agent._published
        if p["ref_id"] == "large_tool_result:tc-spill-wording"
    )
    assert "raise or disable the threshold" not in body, (
        "spill rescan body must not say 'raise or disable the threshold'"
    )
