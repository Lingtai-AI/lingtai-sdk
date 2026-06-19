"""Tests for retroactive tool-result compaction (issue #144).

Covers:
- The shared kernel module ``tool_result_artifacts`` — preventive cap,
  retroactive cap, shared manifest shape, ``is_spill_manifest`` detector.
- ``compact_oversized_history`` mutating ``ChatInterface._entries`` in place
  without touching ids / pairing / synthesized / entry ordering.
- Idempotency: already-spilled manifests are not re-spilled across repeated
  AED retries.
- The AED retry path in ``base_agent/turn.py`` invokes the retroactive
  helper through ``_compact_history_before_retry`` on both transient and
  deterministic branches.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.tool_result_artifacts import (
    PREVENTIVE_MAX_CHARS,
    RETROACTIVE_MAX_CHARS,
    compact_oversized_history,
    is_spill_manifest,
    spill_oversized_result,
)


# -- Shared helper: manifest shape & detection ------------------------------

def test_constants_match_spec():
    assert PREVENTIVE_MAX_CHARS == 10_000
    assert RETROACTIVE_MAX_CHARS == 5_000


def test_is_spill_manifest_detects_dict_shape():
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    # Preferred shape: explicit namespaced artifact marker.
    manifest_with_marker = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.json",
        "cap_chars": 10000,
        "original_char_count": 50000,
    }
    assert is_spill_manifest(manifest_with_marker)
    # spill_path may legitimately be None when the workdir write failed —
    # the manifest is still a manifest.
    manifest_with_marker_failed_write = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": None,
        "cap_chars": 10000,
        "original_char_count": 50000,
    }
    assert is_spill_manifest(manifest_with_marker_failed_write)

    # Backward-compat: legacy manifests lacking ``artifact`` are still
    # accepted as long as the structural quadruple is present.
    legacy_manifest = {
        "status": "spilled",
        "spill_path": "tmp/tool-results/x.json",
        "cap_chars": 10000,
        "original_char_count": 50000,
    }
    assert is_spill_manifest(legacy_manifest)

    # Conservative refusals — arbitrary business dicts must NOT match:
    assert not is_spill_manifest({"status": "ok", "spill_path": "x"})  # wrong status
    assert not is_spill_manifest({"status": "spilled"})  # no spill_path key
    # Two-field "spilled" + spill_path business dict — refused without the
    # marker AND without the structural quadruple.
    assert not is_spill_manifest({"status": "spilled", "spill_path": "/x"})
    # Has marker but missing required structural fields — refused so the
    # marker alone can't be forged into a manifest by a misconfigured
    # caller.  Actually the marker is owner-stamped, so we accept it as
    # authoritative; the legacy branch is the strict one.
    # (We test the legacy strictness above.)
    assert not is_spill_manifest("just a string")
    assert not is_spill_manifest(None)
    assert not is_spill_manifest({})


def test_is_spill_manifest_accepts_failed_spill_with_none_path():
    """When the spill write fails the manifest still has spill_path=None
    plus a spill_error field; that's still a manifest."""
    manifest = spill_oversized_result(
        "X" * (PREVENTIVE_MAX_CHARS * 2),
        max_chars=PREVENTIVE_MAX_CHARS,
        tool_name="read",
        tool_call_id="tc1",
        working_dir=None,  # forces spill_path = None
    )
    assert is_spill_manifest(manifest)
    assert manifest["spill_path"] is None
    assert "spill_error" in manifest


def test_shared_helper_artifact_contains_full_payload(tmp_path):
    big = "Z" * (PREVENTIVE_MAX_CHARS * 3)
    out = spill_oversized_result(
        big,
        max_chars=PREVENTIVE_MAX_CHARS,
        tool_name="bash",
        tool_call_id="tc-shared",
        working_dir=tmp_path,
    )
    assert is_spill_manifest(out)
    assert out["source"] == "preventive"  # default
    artifact = tmp_path / out["spill_path"]
    assert artifact.read_text(encoding="utf-8") == big


def test_shared_helper_source_field_records_caller(tmp_path):
    """source must distinguish preventive vs retroactive spills."""
    big = "Q" * 20_000
    preventive = spill_oversized_result(
        big, max_chars=PREVENTIVE_MAX_CHARS, tool_name="read",
        tool_call_id="tc-p", working_dir=tmp_path,
    )
    retro = spill_oversized_result(
        big, max_chars=RETROACTIVE_MAX_CHARS, tool_name="read",
        tool_call_id="tc-r", working_dir=tmp_path, source="retroactive",
    )
    assert preventive["source"] == "preventive"
    assert retro["source"] == "retroactive"


def test_shared_helper_idempotent_on_manifest(tmp_path):
    """Calling spill on an already-spilled manifest returns it unchanged."""
    big = "M" * 20_000
    first = spill_oversized_result(
        big, max_chars=PREVENTIVE_MAX_CHARS, tool_name="read",
        tool_call_id="tc1", working_dir=tmp_path,
    )
    assert is_spill_manifest(first)
    second = spill_oversized_result(
        first, max_chars=PREVENTIVE_MAX_CHARS, tool_name="read",
        tool_call_id="tc1", working_dir=tmp_path,
    )
    # No new artifact written — same object returned
    assert second is first


# -- Retroactive history compaction -----------------------------------------

def _build_interface_with_pair(*, tool_id: str, result_content):
    """Build a ChatInterface containing a pre-existing tool_call / tool_result pair."""
    iface = ChatInterface()
    # Pre-seed entries so we can mutate ToolResultBlock.content in place.
    iface._append(
        "assistant",
        [
            TextBlock(text="some assistant prose"),
            ToolCallBlock(id=tool_id, name="bash", args={"command": "ls"}),
        ],
    )
    iface._append(
        "user",
        [ToolResultBlock(id=tool_id, name="bash", content=result_content)],
    )
    return iface


def test_compact_oversized_history_rewrites_only_oversized_content(tmp_path):
    big = "B" * (RETROACTIVE_MAX_CHARS * 2)
    iface = _build_interface_with_pair(tool_id="tc-big", result_content=big)

    stats = compact_oversized_history(iface, working_dir=tmp_path)
    assert stats.compacted_blocks == 1
    assert stats.scanned_blocks == 1
    assert stats.original_chars_total >= len(big)
    assert stats.replacement_chars_total < stats.original_chars_total
    assert len(stats.artifact_paths) == 1

    # The ToolResultBlock.content must now be a manifest, but everything
    # else about the entry must be untouched.
    tool_result_entry = iface._entries[1]
    assert tool_result_entry.role == "user"
    assert len(tool_result_entry.content) == 1
    block = tool_result_entry.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.id == "tc-big"          # id untouched
    assert block.name == "bash"           # name untouched
    assert block.synthesized is False     # flag untouched
    assert is_spill_manifest(block.content)
    assert block.content["source"] == "retroactive"
    assert block.content["tool_call_id"] == "tc-big"
    assert block.content["original_char_count"] == len(big)

    # Artifact contains the full original
    artifact = tmp_path / block.content["spill_path"]
    assert artifact.read_text(encoding="utf-8") == big


def test_compact_oversized_history_preserves_entry_ordering_and_pairing(tmp_path):
    """The whole interface walk must not reorder entries or break pairing."""
    big = "B" * (RETROACTIVE_MAX_CHARS * 2)
    small = "small result"

    iface = ChatInterface()
    iface._append("system", [TextBlock(text="sys")])
    iface._append("user", [TextBlock(text="hello")])
    iface._append("assistant", [ToolCallBlock(id="tc-1", name="bash", args={})])
    iface._append("user", [ToolResultBlock(id="tc-1", name="bash", content=big)])
    iface._append("assistant", [ToolCallBlock(id="tc-2", name="read", args={})])
    iface._append("user", [ToolResultBlock(id="tc-2", name="read", content=small)])
    iface._append("assistant", [TextBlock(text="done")])

    pre_roles = [e.role for e in iface._entries]
    pre_ids = [e.id for e in iface._entries]

    stats = compact_oversized_history(iface, working_dir=tmp_path)
    assert stats.compacted_blocks == 1  # only the big one
    assert stats.scanned_blocks == 2    # both tool results scanned

    # Order, ids, roles untouched
    assert [e.role for e in iface._entries] == pre_roles
    assert [e.id for e in iface._entries] == pre_ids

    # Pairing intact: tc-1 call/result still adjacent, tc-2 call/result intact
    big_result_block = iface._entries[3].content[0]
    small_result_block = iface._entries[5].content[0]
    assert big_result_block.id == "tc-1"
    assert small_result_block.id == "tc-2"
    assert is_spill_manifest(big_result_block.content)
    assert small_result_block.content == small  # unchanged


def test_compact_oversized_history_skips_small_content(tmp_path):
    """Sub-cap content must not be touched at all."""
    iface = _build_interface_with_pair(tool_id="tc-s", result_content="just fine")
    stats = compact_oversized_history(iface, working_dir=tmp_path)
    assert stats.compacted_blocks == 0
    assert stats.scanned_blocks == 1
    assert stats.artifact_paths == []
    assert stats.original_chars_total == 0
    assert stats.replacement_chars_total == 0

    block = iface._entries[1].content[0]
    assert block.content == "just fine"
    # No artifact directory created when nothing spilled
    spill_dir = tmp_path / "tmp" / "tool-results"
    assert not spill_dir.exists() or not list(spill_dir.iterdir())


def test_compact_oversized_history_idempotent_across_retries(tmp_path):
    """Two successive AED retries must not produce two artifacts for the
    same content — the second call sees a manifest and skips it."""
    big = "I" * (RETROACTIVE_MAX_CHARS * 2)
    iface = _build_interface_with_pair(tool_id="tc-idemp", result_content=big)

    stats1 = compact_oversized_history(iface, working_dir=tmp_path)
    artifacts_after_first = list((tmp_path / "tmp" / "tool-results").iterdir())
    stats2 = compact_oversized_history(iface, working_dir=tmp_path)
    artifacts_after_second = list((tmp_path / "tmp" / "tool-results").iterdir())

    assert stats1.compacted_blocks == 1
    assert stats2.compacted_blocks == 0  # idempotent
    assert artifacts_after_first == artifacts_after_second


def test_compact_oversized_history_handles_missing_workdir():
    """No working dir → manifest written without artifact, no crash."""
    big = "N" * (RETROACTIVE_MAX_CHARS * 2)
    iface = _build_interface_with_pair(tool_id="tc-no-wd", result_content=big)
    stats = compact_oversized_history(iface, working_dir=None)
    assert stats.compacted_blocks == 1
    # No artifact path captured when spill_path is None
    assert stats.artifact_paths == []
    block = iface._entries[1].content[0]
    assert is_spill_manifest(block.content)
    assert block.content["spill_path"] is None
    assert "spill_error" in block.content


def test_compact_oversized_history_safe_with_none_interface(tmp_path):
    """The helper must be a no-op for missing interface — the AED path
    invokes it without pre-checking."""
    stats = compact_oversized_history(None, working_dir=tmp_path)
    assert stats.compacted_blocks == 0
    assert stats.scanned_blocks == 0


def test_compact_oversized_history_logger_fn_receives_event(tmp_path):
    big = "L" * (RETROACTIVE_MAX_CHARS * 2)
    iface = _build_interface_with_pair(tool_id="tc-log", result_content=big)

    events = []
    def logger(event, **fields):
        events.append((event, fields))

    stats = compact_oversized_history(iface, working_dir=tmp_path, logger_fn=logger)
    assert stats.compacted_blocks == 1
    compaction_events = [e for e in events if e[0] == "tool_result_compacted_retroactively"]
    assert len(compaction_events) == 1
    fields = compaction_events[0][1]
    assert fields["tool_name"] == "bash"
    assert fields["tool_call_id"] == "tc-log"
    assert fields["original_char_count"] == len(big)


def test_compact_oversized_history_preserves_synthesized_flag(tmp_path):
    """Heal-path synthesized placeholders carry a flag the wire alternation
    relies on; the rewriter must not touch it."""
    big = "S" * (RETROACTIVE_MAX_CHARS * 2)
    iface = ChatInterface()
    iface._append("assistant", [ToolCallBlock(id="tc-syn", name="bash", args={})])
    block = ToolResultBlock(id="tc-syn", name="bash", content=big, synthesized=True)
    iface._append("user", [block])

    compact_oversized_history(iface, working_dir=tmp_path)

    after = iface._entries[1].content[0]
    assert after.synthesized is True  # untouched
    assert is_spill_manifest(after.content)


# -- AED integration: _compact_history_before_retry -------------------------

def test_compact_history_before_retry_invokes_helper(tmp_path):
    """The turn-engine helper threads agent state into the kernel module."""
    from lingtai_kernel.base_agent.turn import _compact_history_before_retry

    big = "A" * (RETROACTIVE_MAX_CHARS * 2)
    iface = _build_interface_with_pair(tool_id="tc-aed", result_content=big)

    # Build a minimal agent stub matching the attributes the helper reads.
    agent = MagicMock()
    agent._working_dir = tmp_path
    agent._session.chat.interface = iface
    agent._log = MagicMock()
    agent._save_chat_history = MagicMock()

    stats = _compact_history_before_retry(agent, source="aed_deterministic")

    block = iface._entries[1].content[0]
    assert is_spill_manifest(block.content)
    # Helper returns the stats so callers can branch on them
    assert stats is not None
    assert stats.compacted_blocks == 1

    # Helper logs the bounded summary event under the dedicated name.
    log_calls = [c for c in agent._log.call_args_list
                 if c.args and c.args[0] == "aed_history_compacted"]
    assert len(log_calls) == 1
    fields = log_calls[0].kwargs
    assert fields["source"] == "aed_deterministic"
    assert fields["compacted_blocks"] == 1
    assert fields["scanned_blocks"] == 1
    assert fields["original_chars_total"] >= len(big)
    assert fields["replacement_chars_total"] < fields["original_chars_total"]
    assert len(fields["artifact_paths"]) == 1

    # Refinement 3: persisted history must be saved exactly once when at
    # least one block was rewritten, tagged so the ledger is auditable.
    agent._save_chat_history.assert_called_once_with(ledger_source="retroactive_compaction")


def test_compact_history_before_retry_does_not_save_on_noop(tmp_path):
    """When nothing is compacted, _save_chat_history must NOT be called —
    we don't want to churn the chat-history ledger on every AED firing."""
    from lingtai_kernel.base_agent.turn import _compact_history_before_retry

    iface = _build_interface_with_pair(tool_id="tc-tiny", result_content="tiny")
    agent = MagicMock()
    agent._working_dir = tmp_path
    agent._session.chat.interface = iface
    agent._log = MagicMock()
    agent._save_chat_history = MagicMock()

    stats = _compact_history_before_retry(agent, source="aed_deterministic")
    assert stats is not None
    assert stats.compacted_blocks == 0
    agent._save_chat_history.assert_not_called()

    # The summary log fires even on noop so operators can see AED firings
    # that did not result in any compaction.
    log_calls = [c for c in agent._log.call_args_list
                 if c.args and c.args[0] == "aed_history_compacted"]
    assert len(log_calls) == 1
    assert log_calls[0].kwargs["compacted_blocks"] == 0


def test_compact_history_before_retry_safe_when_chat_is_none(tmp_path):
    """If session.chat is None (boot interrupted), the helper must noop."""
    from lingtai_kernel.base_agent.turn import _compact_history_before_retry

    agent = MagicMock()
    agent._working_dir = tmp_path
    agent._session.chat = None
    agent._log = MagicMock()

    # Must not raise
    _compact_history_before_retry(agent, source="aed_transient")


def test_compact_history_before_retry_swallows_helper_exception(tmp_path):
    """Any exception from the helper must be logged but never re-raised —
    AED recovery must never crash the recovery loop."""
    from lingtai_kernel.base_agent.turn import _compact_history_before_retry

    agent = MagicMock()
    agent._working_dir = tmp_path
    # Interface attribute access raises
    type(agent._session.chat).interface = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    agent._log = MagicMock()

    _compact_history_before_retry(agent, source="aed_transient")

    failed_logs = [c for c in agent._log.call_args_list
                   if c.args and c.args[0] == "tool_result_compaction_failed"]
    assert len(failed_logs) == 1
    assert failed_logs[0].kwargs["source"] == "aed_transient"
    assert "RuntimeError" in failed_logs[0].kwargs["error"]


# -- AED loop integration: _run_loop calls retroactive compaction ----------

def _make_run_loop_agent_with_oversized_history(tmp_path, big_payload):
    """Build a fake agent matching `test_aed_recovery._make_run_loop_agent`
    shape, but with a real ChatInterface holding an oversized tool result.

    Returns (agent, interface) — the interface is the ground-truth thing the
    AED retry path should compact.
    """
    import queue
    import threading
    from dataclasses import dataclass, field
    from types import SimpleNamespace

    from lingtai_kernel.message import _make_message, MSG_REQUEST
    from lingtai_kernel.state import AgentState

    iface = _build_interface_with_pair(tool_id="tc-aed-int", result_content=big_payload)
    # Add close_pending_tool_calls / has_pending_tool_calls to satisfy turn.py.
    iface.has_pending_tool_calls = lambda: False  # type: ignore[method-assign]
    iface.close_pending_tool_calls = lambda *, reason, tool_completed=False: None  # type: ignore[method-assign]

    @dataclass
    class _Agent:
        _working_dir: object
        agent_name: str = "test"
        _state: AgentState = AgentState.ACTIVE
        _asleep: threading.Event = field(default_factory=threading.Event)
        _logs: list = field(default_factory=list)
        _states: list = field(default_factory=list)
        _chat: object = None

        def _log(self, event_type, **fields):
            self._logs.append((event_type, fields))

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            self._states.append(new_state)
            self._log("agent_state", new=new_state.value, reason=reason)

    agent = _Agent(tmp_path)
    agent._shutdown = threading.Event()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent._reset_uptime = lambda: None
    agent.save_history_calls = []  # list of ledger_source values
    def _record_save(*a, ledger_source: str = "main", **kw):
        agent.save_history_calls.append(ledger_source)
    agent._save_chat_history = _record_save
    agent._config = SimpleNamespace(
        insights_interval=0,
        max_aed_attempts=10,
        language="en",
        time_awareness=True,
        timezone_awareness=True,
    )
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(interface=iface),
        _rebuild_session=lambda interface: None,
    )
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "go"))
    agent._preset_fallback_attempted = False
    agent._can_fallback_preset = lambda: False
    return agent, iface


def test_aed_deterministic_retry_compacts_history_before_rebuild(tmp_path, monkeypatch):
    """When AED's deterministic branch fires, the oversized tool result in
    history must be compacted to a manifest BEFORE _rebuild_session is
    called — so the rebuilt session sees the smaller wire."""
    from lingtai_kernel.base_agent import turn

    big = "D" * (RETROACTIVE_MAX_CHARS * 2)
    agent, iface = _make_run_loop_agent_with_oversized_history(tmp_path, big)
    agent._config.max_aed_attempts = 2

    state_at_rebuild = {}

    def fake_handle(_agent, _msg):
        # First call → structural failure that won't be classified as transient.
        # Second call (after compaction + rebuild) → shutdown to terminate the loop.
        nonlocal_calls["n"] += 1
        if nonlocal_calls["n"] == 1:
            raise RuntimeError("structural model failure")
        _agent._shutdown.set()

    nonlocal_calls = {"n": 0}

    def fake_rebuild(interface):
        # Snapshot whether the oversized result was already manifest-shaped
        # at the moment _rebuild_session is invoked.
        block = interface._entries[1].content[0]
        state_at_rebuild["is_manifest"] = is_spill_manifest(block.content)
        state_at_rebuild["preserved_id"] = block.id
        state_at_rebuild["preserved_name"] = block.name

    agent._session._rebuild_session = fake_rebuild

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    # At the moment _rebuild_session ran, the oversized history must have
    # already been compacted to a manifest.
    assert state_at_rebuild["is_manifest"] is True
    assert state_at_rebuild["preserved_id"] == "tc-aed-int"
    assert state_at_rebuild["preserved_name"] == "bash"

    # The bounded summary event was logged with rich stats.
    applied = [e for e in agent._logs
               if e[0] == "aed_history_compacted"
               and e[1].get("source") == "aed_deterministic"]
    assert len(applied) == 1
    fields = applied[0][1]
    assert fields["compacted_blocks"] == 1
    assert fields["scanned_blocks"] == 1
    assert fields["original_chars_total"] >= len(big)
    assert fields["replacement_chars_total"] < fields["original_chars_total"]
    assert isinstance(fields["artifact_paths"], list)
    assert len(fields["artifact_paths"]) == 1

    # Refinement 3: persisted history saved with the dedicated ledger tag
    # after the rewrite.
    assert "retroactive_compaction" in agent.save_history_calls


def test_aed_transient_retry_compacts_history_before_backoff(tmp_path, monkeypatch):
    """The transient AED branch must also fire retroactive compaction."""
    from lingtai_kernel.base_agent import turn

    big = "T" * (RETROACTIVE_MAX_CHARS * 2)
    agent, iface = _make_run_loop_agent_with_oversized_history(tmp_path, big)

    state_at_sleep = {}
    real_sleep = turn.time.sleep

    def watched_sleep(seconds):
        # When the first sleep fires for transient backoff, compaction must
        # have already run.
        block = iface._entries[1].content[0]
        state_at_sleep.setdefault("first_sleep_is_manifest",
                                  is_spill_manifest(block.content))
        # Don't actually sleep.

    nonlocal_calls = {"n": 0}
    def fake_handle(_agent, _msg):
        nonlocal_calls["n"] += 1
        if nonlocal_calls["n"] == 1:
            # Transient classifier matches "an error occurred while processing your request"
            raise RuntimeError("An error occurred while processing your request")
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", watched_sleep)
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert state_at_sleep.get("first_sleep_is_manifest") is True
    transient_compactions = [e for e in agent._logs
                             if e[0] == "aed_history_compacted"
                             and e[1].get("source") == "aed_transient"]
    assert len(transient_compactions) == 1
    assert transient_compactions[0][1]["compacted_blocks"] == 1


# -- ToolExecutor preventive path still works after refactor ---------------

def test_executor_preventive_spill_still_works_through_refactor(tmp_path):
    """Smoke test: the existing preventive cap path via ToolExecutor is
    unchanged after moving the spill logic into tool_result_artifacts."""
    from lingtai_kernel.llm.base import ToolCall
    from lingtai_kernel.loop_guard import LoopGuard
    from lingtai_kernel.tool_executor import ToolExecutor

    big = "P" * (PREVENTIVE_MAX_CHARS * 2)
    captured = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    executor = ToolExecutor(
        dispatch_fn=lambda tc: big,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        working_dir=tmp_path,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-prev")])

    name, payload = captured.call_args.args
    assert name == "read"
    assert is_spill_manifest(payload)
    assert payload["source"] == "preventive"
    artifact = tmp_path / payload["spill_path"]
    assert artifact.read_text(encoding="utf-8") == big


# -- Refinement 1: conservative manifest detection --------------------------

def test_manifest_carries_namespaced_artifact_marker(tmp_path):
    """Every freshly produced manifest stamps the namespaced marker so
    consumers don't have to rely on the structural quadruple."""
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    big = "A" * 20_000
    out = spill_oversized_result(
        big, max_chars=PREVENTIVE_MAX_CHARS, tool_name="read",
        tool_call_id="tc-mark", working_dir=tmp_path,
    )
    assert out["artifact"] == ARTIFACT_MARKER


def test_is_spill_manifest_refuses_arbitrary_business_dict():
    """A business dict that uses status='spilled' and spill_path keys for
    its own unrelated purpose must NOT be classified as a manifest."""
    # No artifact marker, no cap_chars, no original_char_count — looks
    # superficially similar but is not a real manifest.
    business_dict = {
        "status": "spilled",
        "spill_path": "/data/business/spilled-2026-05-23.csv",
        "rows": 1234,
        "notes": "user dumped overflow to disk during ETL",
    }
    assert not is_spill_manifest(business_dict)


# -- Refinement 4: over-window classifier + AED integration -----------------

@pytest.mark.parametrize("phrase", [
    "context window exceeded",
    "context_window_exceeded",
    "context length exceeded",
    "context_length_exceeded",
    "maximum context length is 200000 tokens",
    "the input exceeds the maximum context length",
    "prompt is too long for this model",
    "prompt too long",
    "input is too long",
    "input token count of 250000 exceeds",
    "tokens in the input are above the limit",
    "request too large",
    "too many tokens",
])
def test_is_over_window_error_matches_provider_phrasing(phrase):
    from lingtai_kernel.base_agent.turn import _is_over_window_error
    assert _is_over_window_error(RuntimeError(phrase))


def test_is_over_window_error_does_not_match_unrelated_errors():
    from lingtai_kernel.base_agent.turn import _is_over_window_error
    assert not _is_over_window_error(RuntimeError("connection reset"))
    assert not _is_over_window_error(RuntimeError("rate limit hit"))
    assert not _is_over_window_error(RuntimeError("auth failed"))
    assert not _is_over_window_error(RuntimeError(""))


def test_aed_over_window_takes_deterministic_branch_not_transient(tmp_path, monkeypatch):
    """An over-window error must NOT take the transient retry loop —
    retrying on the same wire would just refire the same error.  It must
    fall through to the deterministic branch, where compaction runs and
    the session is rebuilt around the shrunk wire.

    This test crucially asserts: no ``aed_transient_retry`` events are
    logged, and the ``aed_over_window_detected`` event is.
    """
    from lingtai_kernel.base_agent import turn

    big = "OW" * (RETROACTIVE_MAX_CHARS)
    agent, iface = _make_run_loop_agent_with_oversized_history(tmp_path, big)

    nonlocal_calls = {"n": 0}

    def fake_handle(_agent, _msg):
        nonlocal_calls["n"] += 1
        if nonlocal_calls["n"] == 1:
            # Phrase matches both transient ("timeout") absent and the
            # over-window matcher (definitive).
            raise RuntimeError("context length exceeded: prompt is too long")
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    # The over-window error was detected and labeled.
    detected = [e for e in agent._logs if e[0] == "aed_over_window_detected"]
    assert len(detected) == 1
    assert "context length exceeded" in detected[0][1]["error"].lower()

    # No transient retries fired — over-window short-circuits the
    # transient branch even though "timeout"-class phrasing might trip
    # the matcher in other contexts.
    transient_logs = [e for e in agent._logs if e[0] == "aed_transient_retry"]
    assert len(transient_logs) == 0

    # The deterministic branch ran with the over-window source tag.
    compactions = [e for e in agent._logs
                   if e[0] == "aed_history_compacted"
                   and e[1].get("source") == "aed_over_window"]
    assert len(compactions) == 1
    assert compactions[0][1]["compacted_blocks"] == 1

    # Persisted-history save happened (refinement 3) — tagged correctly.
    assert "retroactive_compaction" in agent.save_history_calls


def test_aed_over_window_compacts_before_rebuild_session(tmp_path, monkeypatch):
    """Over-window recovery: history must already be shrunk by the time
    _rebuild_session sees the interface, otherwise the rebuilt session
    will replay the same too-big wire."""
    from lingtai_kernel.base_agent import turn

    big = "W" * (RETROACTIVE_MAX_CHARS * 3)
    agent, iface = _make_run_loop_agent_with_oversized_history(tmp_path, big)

    state_at_rebuild = {}

    def fake_handle(_agent, _msg):
        state_at_rebuild.setdefault("handle_n", 0)
        state_at_rebuild["handle_n"] += 1
        if state_at_rebuild["handle_n"] == 1:
            raise RuntimeError("Anthropic: prompt is too long for the context window")
        _agent._shutdown.set()

    def watching_rebuild(interface):
        block = interface._entries[1].content[0]
        state_at_rebuild["is_manifest"] = is_spill_manifest(block.content)

    agent._session._rebuild_session = watching_rebuild

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert state_at_rebuild["is_manifest"] is True


# -- Refinement 5: WorkerStillRunningError branch must NOT compact ----------

def test_worker_still_running_does_not_invoke_compaction(tmp_path, monkeypatch):
    """When _handle_message raises WorkerStillRunningError the AED loop
    must put the agent ASLEEP without touching ChatInterface — and
    therefore without firing the retroactive compaction helper.  The
    worker future may still be mutating the interface from another
    thread; compaction would race."""
    from lingtai_kernel.base_agent import turn
    from lingtai_kernel.llm_utils import WorkerStillRunningError

    big = "S" * (RETROACTIVE_MAX_CHARS * 2)
    agent, iface = _make_run_loop_agent_with_oversized_history(tmp_path, big)

    def fake_handle(_agent, _msg):
        raise WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer",
                        lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    # The agent went ASLEEP via the worker-still-running branch.
    assert any(e[0] == "llm_worker_still_running" for e in agent._logs)
    # CRUCIALLY: no compaction events of any kind were emitted, and the
    # oversized history was NOT mutated.  Compaction on this branch
    # would race the still-alive worker future on the same ChatInterface.
    assert not any(e[0] == "aed_history_compacted" for e in agent._logs)
    assert not any(e[0] == "tool_result_compacted_retroactively" for e in agent._logs)
    block = iface._entries[1].content[0]
    assert block.content == big  # untouched
    assert not is_spill_manifest(block.content)
