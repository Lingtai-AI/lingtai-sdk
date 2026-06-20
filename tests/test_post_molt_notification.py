"""Tests for the post-molt notification surface.

After a molt completes (agent-initiated _context_molt or system-initiated
context_forget), the kernel publishes a `.notification/post-molt.json` so
the fresh agent reads a clear reminder that it just molted and should
resume the work it had in flight. The reminder carries:

- ``molt_count`` — the new molt counter value
- ``initiator`` — ``"agent"`` or ``"system"``
- ``source`` — for system molts, the trigger label (warning_ladder, aed, …)
- ``reasoning`` / ``reminder`` — primary recall hook (agent's molt reasoning
  for agent molts; first line of the system-authored summary otherwise)
- ``summary_path`` — pointer into ``system/summaries/`` when persisted

The post-molt channel is intentionally distinct from the ``molt`` channel
owned by ``base_agent.turn._check_molt_pressure``; pressure clearing must
never sweep the post-molt reminder.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


_VALID_SESSION_JOURNAL = """\
---
name: 2026-06-19-molt-1-test
description: A test session journal entry for the molt gate.
date: 2026-06-19
molt_count: 1
type: session-journal
---

## What this segment was about
Testing.

## Accomplishments
Wrote a valid session journal.
"""


def _write_session_journal(agent, rel="knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md"):
    path = agent._working_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_VALID_SESSION_JOURNAL, encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# Helpers — mirror tests/test_molt_notification_persistence.py for parity
# ---------------------------------------------------------------------------


def _make_agent_with_psyche(tmp_path):
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )


def _setup_mock_chat(agent):
    mock_interface = MagicMock()
    mock_interface.entries = []
    mock_interface.estimate_context_tokens.return_value = 50000

    mock_chat = MagicMock()
    mock_chat.interface = mock_interface

    def patched_ensure():
        if agent._session._chat is None:
            new_interface = MagicMock()
            new_interface.entries = []
            new_interface.estimate_context_tokens.return_value = 5000
            new_chat = MagicMock()
            new_chat.interface = new_interface
            agent._session._chat = new_chat
        return agent._session._chat

    agent._session.ensure_session = patched_ensure
    agent._session._chat = mock_chat
    agent._chat = mock_chat

    manifest_path = agent._working_dir / ".agent.json"
    if not manifest_path.exists():
        manifest_path.write_text("{}")

    return mock_interface


def _build_molt_call_entry(mock_interface, tc_id, summary, reasoning=None):
    from lingtai_kernel.llm.interface import ToolCallBlock

    args = {"object": "context", "action": "molt", "summary": summary}
    if reasoning is not None:
        args["_reasoning"] = reasoning
    tc_block = ToolCallBlock(id=tc_id, name="psyche", args=args)
    mock_entry = MagicMock()
    mock_entry.role = "assistant"
    mock_entry.content = [tc_block]
    mock_interface.entries = [mock_entry]


def _read_post_molt(agent):
    path = agent._working_dir / ".notification" / "post-molt.json"
    assert path.is_file(), "post-molt.json should exist after molt"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Agent-initiated molt
# ---------------------------------------------------------------------------


class TestPostMoltNotificationAgentMolt:
    def test_agent_molt_publishes_post_molt_with_reasoning(self, tmp_path):
        """Agent molt → post-molt notification carries the agent's reasoning."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_postmolt_1"
            _build_molt_call_entry(
                mock_interface,
                tc_id,
                summary="finish the foo feature",
                reasoning="context full; want to resume foo cleanly",
            )

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": "finish the foo feature",
                "_reasoning": "context full; want to resume foo cleanly",
                "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            payload = _read_post_molt(agent)
            # Envelope shape (submit() helper)
            assert "header" in payload
            assert "data" in payload
            assert payload.get("priority") == "high"
            assert payload.get("instructions"), (
                "post-molt notification must carry agent-facing instructions"
            )
            assert "post-molt" in payload["instructions"], (
                "instructions should reference the dismiss channel"
            )

            data = payload["data"]
            assert data.get("initiator") == "agent"
            assert data.get("molt_count") == result["molt_count"]
            assert data.get("reasoning") == \
                "context full; want to resume foo cleanly"
            # Helpful echo of the briefing the agent wrote for itself
            assert data.get("reminder")
            # summary_path may be None if persistence failed, but the key
            # must be present so the agent doesn't have to probe.
            assert "summary_path" in data

        finally:
            agent.stop()

    def test_agent_molt_without_reasoning_falls_back_to_summary(self, tmp_path):
        """Without `_reasoning`, the reminder falls back to the summary head."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_postmolt_2"
            _build_molt_call_entry(
                mock_interface, tc_id,
                summary="first line: keep going on the parser bug\nsecond line",
            )

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": "first line: keep going on the parser bug\nsecond line",
                "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            data = _read_post_molt(agent)["data"]
            assert data.get("initiator") == "agent"
            # No reasoning supplied → reasoning may be absent or None
            assert not data.get("reasoning")
            # reminder must surface the summary's first line so the fresh
            # agent has something concrete to act on.
            reminder = data.get("reminder") or ""
            assert "parser bug" in reminder

        finally:
            agent.stop()

    def test_agent_molt_accepts_plain_reasoning_key(self, tmp_path):
        """ToolExecutor injects `_reasoning`, but accept `reasoning` too
        so direct callers (tests, internal call sites) work consistently."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_postmolt_3"
            _build_molt_call_entry(
                mock_interface, tc_id,
                summary="continue work",
            )

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": "continue work",
                "reasoning": "plain-key reasoning",
                "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            data = _read_post_molt(agent)["data"]
            assert data.get("reasoning") == "plain-key reasoning"

        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# System-initiated molt (context_forget)
# ---------------------------------------------------------------------------


class TestPostMoltNotificationSystemForget:
    def test_context_forget_publishes_post_molt(self, tmp_path):
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            _setup_mock_chat(agent)

            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="warning_ladder")
            assert result.get("status") == "ok"

            payload = _read_post_molt(agent)
            assert payload.get("priority") == "high"
            data = payload["data"]
            assert data.get("initiator") == "system"
            assert data.get("source") == "warning_ladder"
            assert data.get("molt_count") == result["molt_count"]
            # reminder = system-authored summary's first line
            reminder = data.get("reminder") or ""
            assert reminder, "system molt must surface a reminder string"
            assert "summary_path" in data

        finally:
            agent.stop()

    def test_context_forget_aed_source_propagates(self, tmp_path):
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            _setup_mock_chat(agent)
            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="aed", attempts=2)
            assert result.get("status") == "ok"

            data = _read_post_molt(agent)["data"]
            assert data.get("initiator") == "system"
            assert data.get("source") == "aed"

        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# Channel isolation — pressure clear must not sweep post-molt
# ---------------------------------------------------------------------------


class TestPostMoltContinuationSignal:
    """Issue #184 — the post-molt notification is an actionable continuation
    signal carrying molt_id / molt_at / source_agent and an ack taxonomy
    (continue / defer / obsolete), durable until dismissed with a reason.

    Per PR #190 feedback there is intentionally **no** heuristic next-action
    extraction: the agent reconstructs context itself from pad / summary /
    human-channel messages — the kernel never excerpts or parses the summary.
    """

    def test_agent_molt_carries_continuation_fields(self, tmp_path):
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_cont_1"
            summary = (
                "Did the foundation work for the doctor skill.\n"
                "Next action: open the PR and run focused tests."
            )
            _build_molt_call_entry(mock_interface, tc_id, summary=summary)

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": summary, "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            payload = _read_post_molt(agent)
            data = payload["data"]
            # Continuation identity fields are present.
            assert data.get("ack_options") == ["continue", "defer", "obsolete"]
            assert data.get("molt_id"), "continuation must carry a molt_id"
            assert data["molt_id"].startswith(f"molt-{result['molt_count']}-")
            assert data.get("molt_at"), "continuation must carry a timestamp"
            # source_agent echoes the agent's true name.
            assert data.get("source_agent") == "test"
            # No heuristic extraction: the kernel must NOT excerpt the summary.
            assert "next_action" not in data
            # summary_path is the pointer the agent reconstructs from instead.
            assert "summary_path" in data

        finally:
            agent.stop()

    def test_instructions_spell_out_reconstruct_then_ack(self, tmp_path):
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_cont_2"
            _build_molt_call_entry(mock_interface, tc_id, summary="keep going")

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": "keep going", "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            instr = (_read_post_molt(agent).get("instructions") or "").lower()
            # Self-reconstruction sources are named explicitly.
            assert "pad" in instr
            assert "summary" in instr
            assert "human-channel" in instr or "human channel" in instr
            # The three ack paths must be discoverable from the instructions.
            assert "continue" in instr
            assert "defer" in instr
            assert "obsolete" in instr
            # Concrete dismiss mechanism + reason-required ack.
            assert "post-molt" in instr
            assert "reason='continue" in instr
            assert "reason='defer" in instr
            assert "reason='obsolete" in instr
            # No-auto-execution must be explicit (non-goal guard).
            assert "not auto-executed" in instr or "not auto" in instr

        finally:
            agent.stop()

    def test_no_next_action_field_even_with_marker_summary(self, tmp_path):
        """A summary that looks like it has a 'next action:' marker must NOT
        produce a next_action field — heuristic extraction is removed."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            mock_interface = _setup_mock_chat(agent)
            tc_id = "toolu_cont_3"
            summary = "Next step: finish wiring the parser; tests red on case 3."
            _build_molt_call_entry(mock_interface, tc_id, summary=summary)

            journal_path = _write_session_journal(agent)
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            result = _context_molt(agent, {
                "summary": summary, "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })
            assert result.get("status") == "ok"

            data = _read_post_molt(agent)["data"]
            assert "next_action" not in data

        finally:
            agent.stop()

    def test_system_forget_also_carries_continuation_fields(self, tmp_path):
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            _setup_mock_chat(agent)
            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="warning_ladder")
            assert result.get("status") == "ok"

            data = _read_post_molt(agent)["data"]
            assert data.get("molt_id", "").startswith(f"molt-{result['molt_count']}-")
            assert data.get("molt_at")
            assert data.get("source_agent") == "test"
            assert data.get("ack_options") == ["continue", "defer", "obsolete"]
            assert "next_action" not in data

        finally:
            agent.stop()


class TestPostMoltChannelIsolation:
    def test_pressure_below_threshold_clears_molt_not_post_molt(self, tmp_path):
        """Falling under molt_pressure clears `.notification/molt.json` but
        leaves `.notification/post-molt.json` intact — they are separate
        producer channels."""
        from lingtai_kernel.notifications import publish, clear

        # Use a bare workdir; this exercises only the file-channel contract.
        workdir = tmp_path / "agent"
        workdir.mkdir()

        publish(workdir, "molt", {
            "header": "context 92% — molt NOW",
            "icon": "🚨",
            "priority": "high",
            "data": {"pressure": 0.92, "urgent": True},
        })
        publish(workdir, "post-molt", {
            "header": "you just molted",
            "icon": "🌱",
            "priority": "high",
            "data": {"initiator": "agent", "molt_count": 1,
                     "reminder": "continue the task"},
        })
        assert (workdir / ".notification" / "molt.json").is_file()
        assert (workdir / ".notification" / "post-molt.json").is_file()

        # Simulate pressure-clear (channel="molt").
        clear(workdir, "molt")

        assert not (workdir / ".notification" / "molt.json").exists(), (
            "pressure clear should remove the molt channel"
        )
        assert (workdir / ".notification" / "post-molt.json").is_file(), (
            "pressure clear must not touch the post-molt channel"
        )
