"""Tests for the past-self consultation infrastructure in intrinsics/soul.py.

Covers the mechanical scaffold landed alongside the appendix tool-call-pair
design. Does NOT exercise live LLM calls — those are mocked. Production cue
prompt is deferred and tested separately.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.intrinsics.soul import (
    _DIARY_CUE_TOKEN_CAP,
    _fit_interface_to_window,
    _list_snapshot_paths,
    _load_snapshot_interface,
    _render_current_diary,
    _run_consultation,
    _run_consultation_batch,
    build_consultation_pair,
)
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    language = "en"
    consultation_past_count = 2
    context_limit = 200_000  # consulted by _run_consultation when no live chat is attached
    retry_timeout = 1.0
    model = None
    provider = None


class _FakeAgent:
    """Minimal stand-in for BaseAgent — exposes just the attributes the
    consultation helpers read."""

    def __init__(self, tmp_path: Path, with_chat: bool = True):
        self._working_dir = tmp_path
        self._working_dir.mkdir(parents=True, exist_ok=True)
        self._config = _FakeConfig()
        self.service = MagicMock()
        self.service.model = "test-model"
        self._chat = None
        if with_chat:
            iface = ChatInterface()
            iface.add_system("test sys")
            iface.add_user_message("user said something")
            iface.add_assistant_message([
                ThinkingBlock(text="thinking it through"),
                TextBlock(text="agent reply"),
            ])
            mock_chat = MagicMock()
            mock_chat.interface = iface
            mock_chat.context_window.return_value = 200_000
            self._chat = mock_chat
        self._session = MagicMock()
        self._session._build_tool_schemas_fn.return_value = [
            {"name": "bash", "description": "run shell", "parameters": {"type": "object"}}
        ]
        self.logged: list[tuple[str, dict]] = []

    def _log(self, event: str, **kw) -> None:
        self.logged.append((event, kw))


def _write_snapshot(workdir: Path, *, molt_count: int, unix_ts: int,
                    entries: list[dict] | None = None) -> Path:
    """Write a snapshot file in the same shape as
    psyche._write_molt_snapshot produces."""
    snaps = workdir / "history" / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    if entries is None:
        # Build a minimal valid interface with a system + a user turn.
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("frozen user message")
        iface.add_assistant_message([TextBlock(text="frozen reply")])
        entries = iface.to_dict()
    payload = {
        "schema_version": 1,
        "molt_count": molt_count,
        "created_at": "2026-05-01T00:00:00Z",
        "before_tokens": 12345,
        "agent_name": "test-agent",
        "agent_id": "test-id",
        "molt_summary": "test molt",
        "molt_source": "agent",
        "interface": entries,
    }
    path = snaps / f"snapshot_{molt_count}_{unix_ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _load_snapshot_interface
# ---------------------------------------------------------------------------


class TestLoadSnapshotInterface:

    def test_loads_valid_snapshot(self, tmp_path):
        path = _write_snapshot(tmp_path, molt_count=3, unix_ts=1714567890)
        iface = _load_snapshot_interface(path)
        assert iface is not None
        assert len(iface.entries) > 0

    def test_missing_file_returns_none(self, tmp_path):
        bogus = tmp_path / "nope.json"
        assert _load_snapshot_interface(bogus) is None

    def test_bad_json_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert _load_snapshot_interface(path) is None

    def test_missing_schema_version_returns_none(self, tmp_path):
        path = tmp_path / "noschema.json"
        path.write_text(json.dumps({"interface": []}), encoding="utf-8")
        assert _load_snapshot_interface(path) is None

    def test_non_int_schema_returns_none(self, tmp_path):
        path = tmp_path / "wrongschema.json"
        path.write_text(
            json.dumps({"schema_version": "1", "interface": []}),
            encoding="utf-8",
        )
        assert _load_snapshot_interface(path) is None

    def test_non_list_interface_returns_none(self, tmp_path):
        path = tmp_path / "wrongiface.json"
        path.write_text(
            json.dumps({"schema_version": 1, "interface": {"oops": True}}),
            encoding="utf-8",
        )
        assert _load_snapshot_interface(path) is None

    def test_payload_not_dict_returns_none(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert _load_snapshot_interface(path) is None


# ---------------------------------------------------------------------------
# _fit_interface_to_window
# ---------------------------------------------------------------------------


class TestFitInterfaceToWindow:

    def test_already_fits_returns_clone(self):
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="ok")])
        out = _fit_interface_to_window(iface, 1_000_000)
        # Same content, but distinct object (clone via to_dict round-trip).
        assert len(out.entries) == len(iface.entries)
        assert out is not iface
        # Mutating the clone must not affect the source.
        out._entries.clear()
        assert len(iface.entries) == 3

    def test_zero_target_returns_empty(self):
        iface = ChatInterface()
        iface.add_user_message("hi")
        out = _fit_interface_to_window(iface, 0)
        assert len(out.entries) == 0

    def test_negative_target_returns_empty(self):
        iface = ChatInterface()
        iface.add_user_message("hi")
        out = _fit_interface_to_window(iface, -100)
        assert len(out.entries) == 0

    def test_empty_interface_returns_empty(self):
        iface = ChatInterface()
        out = _fit_interface_to_window(iface, 1000)
        assert len(out.entries) == 0

    def test_preserves_system_at_head(self):
        iface = ChatInterface()
        iface.add_system("frozen sys prompt to preserve")
        # Add a long body that forces trimming.
        for i in range(20):
            iface.add_user_message(f"user {i} " * 200)
            iface.add_assistant_message([TextBlock(text=f"reply {i} " * 200)])
        # Aim at small target so most of body must be dropped.
        out = _fit_interface_to_window(iface, 5_000)
        # System entry preserved at position 0.
        assert out.entries[0].role == "system"

    def test_drops_orphan_tool_results_at_head(self):
        """If the natural cutoff lands on a user{tool_result} whose matching
        assistant{tool_call} got dropped, the orphan is removed too."""
        iface = ChatInterface()
        iface.add_user_message("setup")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_orphan", name="dropped", args={}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_orphan", name="dropped", content="x"),
        ])
        # Add a clean tail entry that fits a small budget by itself.
        iface.add_assistant_message([TextBlock(text="tail thought")])

        # Compute the rough size of the tail-only kept set, then pick a
        # target that keeps tail but excludes the tool_call entry. The
        # function must not return the tool_result entry as the head of
        # the suffix (orphaned).
        tail_only = ChatInterface()
        tail_only.add_assistant_message([TextBlock(text="tail thought")])
        tail_size = tail_only.estimate_context_tokens()

        out = _fit_interface_to_window(iface, tail_size + 5)

        # No entry in `out` should be a user with only ToolResultBlocks
        # whose call_id was excluded from `out`.
        present_call_ids: set[str] = set()
        for e in out.entries:
            for b in e.content:
                if isinstance(b, ToolCallBlock):
                    present_call_ids.add(b.id)
        for e in out.entries:
            if e.role != "user":
                continue
            if not e.content:
                continue
            if all(isinstance(b, ToolResultBlock) for b in e.content):
                for b in e.content:
                    assert b.id in present_call_ids, (
                        "orphan tool_result kept without matching tool_call"
                    )

    def test_heals_trailing_dangling_tool_calls_when_fitted(self):
        """Fitter must close dangling tail tool_calls so the consultation
        path can append the spark via add_user_message.

        Repro for the soul_whisper_error storm: snapshots taken mid-tool-flow
        leave the tail assistant turn carrying tool_calls without matching
        tool_results. The fitter previously returned them as-is and the
        consultation crashed at spark append time.
        """
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("hello")
        iface.add_assistant_message([
            TextBlock(text="thinking..."),
            ToolCallBlock(id="tc_dangling", name="bash", args={"cmd": "x"}),
        ])
        # No tool_result appended — simulates a snapshot frozen mid-flow.
        assert iface.has_pending_tool_calls()

        out = _fit_interface_to_window(iface, 1_000_000)

        # After fitting, tail must be paired so add_user_message succeeds.
        assert not out.has_pending_tool_calls(), (
            "fitter left dangling tool_calls on tail — spark append would crash"
        )
        # Spark append must not raise.
        out.add_user_message("spark")

    def test_heals_trailing_dangling_tool_calls_when_trimmed(self):
        """Same heal must apply when the interface is trimmed, not just when
        it already fits."""
        iface = ChatInterface()
        iface.add_system("sys")
        # Long body to force trimming.
        for i in range(15):
            iface.add_user_message(f"user {i} " * 200)
            iface.add_assistant_message([TextBlock(text=f"reply {i} " * 200)])
        # Trailing dangling tool_call after the body.
        iface.add_user_message("final ask")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_dangling_trim", name="bash", args={}),
        ])
        assert iface.has_pending_tool_calls()

        # Small target forces trimming. The dangling tail should remain in
        # the kept suffix because it's the most recent — and must be healed.
        out = _fit_interface_to_window(iface, 5_000)

        assert not out.has_pending_tool_calls()
        out.add_user_message("spark")


# ---------------------------------------------------------------------------
# _list_snapshot_paths
# ---------------------------------------------------------------------------


class TestListSnapshotPaths:

    def test_no_dir_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        assert _list_snapshot_paths(agent) == []

    def test_lists_snapshots(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        _write_snapshot(tmp_path, molt_count=1, unix_ts=1)
        _write_snapshot(tmp_path, molt_count=2, unix_ts=2)
        _write_snapshot(tmp_path, molt_count=3, unix_ts=3)
        paths = _list_snapshot_paths(agent)
        assert len(paths) == 3

    def test_ignores_non_snapshot_files(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        _write_snapshot(tmp_path, molt_count=1, unix_ts=1)
        # Drop an unrelated file in the snapshots dir.
        snaps = tmp_path / "history" / "snapshots"
        (snaps / "stray.txt").write_text("ignore me")
        paths = _list_snapshot_paths(agent)
        assert len(paths) == 1
        assert paths[0].name == "snapshot_1_1.json"


# ---------------------------------------------------------------------------
# _run_consultation_batch verbatim current-chat clone
# ---------------------------------------------------------------------------


class TestVerbatimCurrentChatForInsights:

    def test_current_chat_clone_preserves_tool_blocks(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        iface = agent._chat.interface
        iface.add_assistant_message([
            ToolCallBlock(id="tc1", name="bash", args={"cmd": "ls"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc1", name="bash", content="files"),
        ])

        captured = {}
        with patch("lingtai_kernel.intrinsics.soul.consultation._run_consultation") as mock_run:
            def fake_run(_agent, passed_iface, source):
                captured["iface"] = passed_iface
                return {"source": source, "blocks": [TextBlock(text="ok")]}
            mock_run.side_effect = fake_run
            voices = _run_consultation_batch(agent)

        assert len(voices) == 1
        cloned = captured["iface"]
        assert cloned is not iface
        all_blocks = [b for e in cloned.entries for b in e.content]
        assert any(isinstance(b, ToolCallBlock) for b in all_blocks)
        assert any(isinstance(b, ToolResultBlock) for b in all_blocks)

    def test_no_chat_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path, with_chat=False)
        voices = _run_consultation_batch(agent)
        assert voices == []


# ---------------------------------------------------------------------------
# _run_consultation_batch
# ---------------------------------------------------------------------------


class TestRunConsultationBatch:

    def test_empty_pool_runs_only_insights(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation"
        ) as mock_run:
            mock_run.return_value = {
                "source": "insights",
                "blocks": [TextBlock(text="the insight voice")],
            }
            voices = _run_consultation_batch(agent)
        assert len(voices) == 1
        # Exactly one consultation call: insights.
        assert mock_run.call_count == 1
        sources = [c.kwargs.get("source") or c.args[2] for c in mock_run.call_args_list]
        assert sources == ["insights"]

    def test_with_snapshots_samples_K(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        # Five snapshots — should sample K=2.
        for i in range(5):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation"
        ) as mock_run:
            def fake_run(_agent, _iface, source):
                return {"source": source, "blocks": [TextBlock(text=f"v from {source}")]}
            mock_run.side_effect = fake_run
            voices = _run_consultation_batch(agent)

        # 1 insights + min(K=2, 5) = 3 work items total.
        assert mock_run.call_count == 3
        # One must be insights; the other two are snapshot:* labels.
        sources = [v["source"] for v in voices]
        assert "insights" in sources
        snapshot_sources = [s for s in sources if s.startswith("snapshot:")]
        assert len(snapshot_sources) == 2

    def test_K_zero_runs_only_insights(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        agent._config.consultation_past_count = 0
        for i in range(3):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation"
        ) as mock_run:
            mock_run.return_value = {"source": "insights", "blocks": [TextBlock(text="v")]}
            voices = _run_consultation_batch(agent)

        assert mock_run.call_count == 1
        assert len(voices) == 1
        assert voices[0]["source"] == "insights"

    def test_failed_consultations_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        for i in range(3):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation"
        ) as mock_run:
            # First call (insights) succeeds, the snapshot calls fail.
            def maybe_fail(_agent, _iface, source):
                if source == "insights":
                    return {"source": "insights", "blocks": [TextBlock(text="ok")]}
                return None
            mock_run.side_effect = maybe_fail
            voices = _run_consultation_batch(agent)

        assert len(voices) == 1
        assert voices[0]["source"] == "insights"

    def test_thread_exception_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        for i in range(2):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation"
        ) as mock_run:
            def maybe_raise(_agent, _iface, source):
                if source == "insights":
                    return {"source": "insights", "blocks": [TextBlock(text="ok")]}
                raise RuntimeError("boom")
            mock_run.side_effect = maybe_raise
            voices = _run_consultation_batch(agent)

        # Insights survives; raising threads logged and filtered.
        assert len(voices) == 1
        events = [e for e, _ in agent.logged]
        assert "consultation_thread_error" in events

    def test_no_chat_no_snapshots_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path, with_chat=False)
        voices = _run_consultation_batch(agent)
        assert voices == []


class TestRunConsultationRedirectLoop:

    def _seed_diary(self, tmp_path: Path, text: str = "DIARY MARKER") -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "diary", "text": text, "ts": 1_700_000_000}) + "\n")

    def test_consultation_redirects_tool_calls(self, tmp_path):
        from lingtai_kernel.llm.base import LLMResponse, ToolCall

        agent = _FakeAgent(tmp_path)
        self._seed_diary(tmp_path)
        sent = []
        captured_session_kwargs = {}

        class _MockSession:
            def __init__(self, interface):
                self.interface = interface
                self.n = 0

            def send(self, content):
                sent.append(content)
                self.n += 1
                if isinstance(content, str):
                    self.interface.add_user_message(content)
                else:
                    self.interface.add_tool_results(content)
                if self.n == 1:
                    block = ToolCallBlock(id="tc_redirect", name="bash", args={"cmd": "ls"})
                    self.interface.add_assistant_message([block])
                    return LLMResponse(tool_calls=[ToolCall(name="bash", args={"cmd": "ls"}, id="tc_redirect")])
                self.interface.add_assistant_message([TextBlock(text="now I speak")])
                return LLMResponse(text="now I speak")

        def _create_session(*, interface, **kw):
            captured_session_kwargs.update(kw)
            return _MockSession(interface)

        agent.service.create_session.side_effect = _create_session
        iface = ChatInterface(); iface.add_user_message("substrate")
        result = _run_consultation(agent, iface, "insights")

        assert result is not None
        blocks = result["blocks"]
        assert isinstance(blocks[0], ToolCallBlock)
        assert isinstance(blocks[1], ToolResultBlock)
        # Refusal content acknowledges receipt (so the model knows the
        # recommendation landed and doesn't retry the same call) and
        # re-grounds with the full system prompt.
        assert "recorded as a recommendation" in blocks[1].content
        assert "soul-flow voice" in captured_session_kwargs["system_prompt"]
        assert captured_session_kwargs["system_prompt"] in blocks[1].content
        assert isinstance(blocks[2], TextBlock)
        assert isinstance(sent[1], list)
        assert isinstance(sent[1][0], ToolResultBlock)

    def test_consultation_max_rounds_exhausted(self, tmp_path):
        from lingtai_kernel.llm.base import LLMResponse, ToolCall
        from lingtai_kernel.intrinsics.soul import _CONSULTATION_MAX_ROUNDS

        agent = _FakeAgent(tmp_path)
        self._seed_diary(tmp_path)

        class _MockSession:
            def __init__(self, interface):
                self.interface = interface
                self.n = 0

            def send(self, content):
                self.n += 1
                if isinstance(content, str):
                    self.interface.add_user_message(content)
                else:
                    self.interface.add_tool_results(content)
                tc_id = f"tc_{self.n}"
                self.interface.add_assistant_message([
                    ToolCallBlock(id=tc_id, name="bash", args={"round": self.n})
                ])
                return LLMResponse(tool_calls=[ToolCall(name="bash", args={"round": self.n}, id=tc_id)])

        agent.service.create_session.side_effect = lambda *, interface, **kw: _MockSession(interface)
        iface = ChatInterface(); iface.add_user_message("substrate")
        result = _run_consultation(agent, iface, "insights")

        assert result is not None
        assert len([b for b in result["blocks"] if isinstance(b, ToolCallBlock)]) == _CONSULTATION_MAX_ROUNDS
        assert len([b for b in result["blocks"] if isinstance(b, ToolResultBlock)]) == _CONSULTATION_MAX_ROUNDS

    def test_consultation_no_diary_cue_past_bails(self, tmp_path):
        # Past branch needs the diary as the spark — empty diary means
        # nothing to react to, so bail.
        agent = _FakeAgent(tmp_path)
        iface = ChatInterface(); iface.add_user_message("substrate")
        assert _run_consultation(agent, iface, "snapshot:foo") is None
        assert agent.service.create_session.called

    def test_consultation_no_diary_cue_insights_bails(self, tmp_path):
        # After the raw-diary refactor, insights no longer bypasses the
        # diary check.  No spark = no consultation for any source.
        agent = _FakeAgent(tmp_path)
        iface = ChatInterface(); iface.add_user_message("substrate")
        assert _run_consultation(agent, iface, "insights") is None
        assert agent.service.create_session.called

    def test_consultation_spark_is_raw_diary(self, tmp_path):
        """The spark sent to session.send() is the raw diary cue, not a
        wrapped/localized version from _build_consultation_cue."""
        from lingtai_kernel.llm.base import LLMResponse

        agent = _FakeAgent(tmp_path)
        self._seed_diary(tmp_path, text="MEANINGFUL DIARY TEXT")

        sent = []

        class _MockSession:
            def __init__(self, interface):
                self.interface = interface

            def send(self, content):
                sent.append(content)
                self.interface.add_user_message(content)
                self.interface.add_assistant_message([TextBlock(text="done")])
                return LLMResponse(text="done")

        agent.service.create_session.side_effect = lambda *, interface, **kw: _MockSession(interface)
        iface = ChatInterface(); iface.add_user_message("substrate")
        _run_consultation(agent, iface, "insights")

        assert sent  # at least one send happened
        spark = sent[0]
        assert isinstance(spark, str)
        assert "MEANINGFUL DIARY TEXT" in spark
        # The spark starts with the [now: ...] header from _render_current_diary
        assert spark.startswith("[now:")

    def test_consultation_uses_configured_soul_voice_prompt(self, tmp_path):
        """Flow consultations resolve their system prompt through the soul
        voice profile, so soul(action='voice', set='custom') affects flow.
        """
        from lingtai_kernel.llm.base import LLMResponse

        agent = _FakeAgent(tmp_path)
        agent._config.soul_voice = "custom"
        agent._config.soul_voice_prompt = "CUSTOM FLOW PROMPT"
        self._seed_diary(tmp_path, text="diary")
        captured_session_kwargs = {}

        class _MockSession:
            def __init__(self, interface):
                self.interface = interface

            def send(self, content):
                self.interface.add_user_message(content)
                self.interface.add_assistant_message([TextBlock(text="done")])
                return LLMResponse(text="done")

        def _create_session(*, interface, **kw):
            captured_session_kwargs.update(kw)
            return _MockSession(interface)

        agent.service.create_session.side_effect = _create_session
        iface = ChatInterface(); iface.add_user_message("substrate")
        result = _run_consultation(agent, iface, "insights")

        assert result is not None
        assert captured_session_kwargs["system_prompt"] == "CUSTOM FLOW PROMPT"


# ---------------------------------------------------------------------------
# _render_current_diary
# ---------------------------------------------------------------------------


class TestRenderCurrentDiary:

    def _write_events(self, tmp_path: Path, records: list[dict]) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_render_diary_format(self, tmp_path):
        """Test [now: HH:MM:SS] header, per-entry [HH:MM:SS] prefix,
        and chronological order of kept entries."""
        agent = _FakeAgent(tmp_path)
        # Three entries with known Unix timestamps
        self._write_events(tmp_path, [
            {"type": "diary", "text": "first thought", "ts": 1_700_000_100},
            {"type": "diary", "text": "second thought", "ts": 1_700_000_200},
            {"type": "diary", "text": "third thought", "ts": 1_700_000_300},
        ])
        result = _render_current_diary(agent)
        assert result, "should return non-empty"
        lines = result.split("\n")
        # First line: [now: HH:MM:SS]
        assert lines[0].startswith("[now:")
        assert lines[0].endswith("]")
        # Entries in chronological order (oldest kept first)
        idx_first = result.index("first thought")
        idx_second = result.index("second thought")
        idx_third = result.index("third thought")
        assert idx_first < idx_second < idx_third
        # Each entry has a [HH:MM:SS] timestamp prefix on its own line
        # (format: "[HH:MM:SS] diary\n<thought text>")
        import re
        ts_lines = re.findall(r"^\[\d{2}:\d{2}:\d{2}\] diary$", result, re.MULTILINE)
        assert len(ts_lines) == 3

    def test_render_diary_tail_cap(self, tmp_path):
        """Write enough entries to exceed 10K tokens; assert the cue is
        under the cap, most recent entries are preserved, oldest dropped."""
        agent = _FakeAgent(tmp_path)
        # ~30 tokens per entry × 500 entries ≈ 15K tokens → some must be trimmed
        records = []
        for i in range(500):
            records.append({
                "type": "diary",
                "text": f"diary entry number {i} with some filler words to pad the length out a bit",
                "ts": 1_700_000_000 + i,
            })
        self._write_events(tmp_path, records)
        result = _render_current_diary(agent)
        assert result
        from lingtai_kernel.token_counter import count_tokens
        token_count = count_tokens(result)
        assert token_count <= _DIARY_CUE_TOKEN_CAP, (
            f"cue is {token_count} tokens, exceeds cap of {_DIARY_CUE_TOKEN_CAP}"
        )
        # Most recent entries must be present
        assert "diary entry number 499" in result
        assert "diary entry number 498" in result
        # Oldest must be dropped
        assert "diary entry number 0" not in result

    def test_render_diary_single_oversized_entry(self, tmp_path):
        """One entry exceeding 10K tokens; still returned (prefer one
        oversized entry to empty cue)."""
        agent = _FakeAgent(tmp_path)
        giant_text = "word " * 5000  # ~5000 tokens
        self._write_events(tmp_path, [
            {"type": "diary", "text": giant_text, "ts": 1_700_000_999},
        ])
        result = _render_current_diary(agent)
        assert result
        assert giant_text.strip() in result


# ---------------------------------------------------------------------------
# build_consultation_pair
# ---------------------------------------------------------------------------


class TestBuildConsultationPair:

    def test_pair_carries_appendix_note(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "insights", "voice": "first", "thinking": []}]
        call, result = build_consultation_pair(agent, voices)
        assert "appendix_note" in result.content
        assert isinstance(result.content["appendix_note"], str)
        assert result.content["appendix_note"] != ""

    def test_pair_call_and_result_share_id(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call, result = build_consultation_pair(agent, voices)
        assert call.id == result.id

    def test_pair_uses_soul_flow_action(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call, result = build_consultation_pair(agent, voices)
        assert call.name == "soul"
        assert call.args == {"action": "flow"}
        assert result.name == "soul"

    def test_voices_array_strips_thinking(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [
            {"source": "a", "voice": "v1", "thinking": ["lots", "of", "thoughts"]},
            {"source": "b", "voice": "v2", "thinking": []},
        ]
        _, result = build_consultation_pair(agent, voices)
        rendered = result.content["voices"]
        assert len(rendered) == 2
        for entry in rendered:
            assert set(entry.keys()) == {"source", "voice"}
            assert "thinking" not in entry

    def test_empty_voice_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [
            {"source": "a", "voice": "real"},
            {"source": "b", "voice": ""},
            {"source": "c"},  # missing voice
        ]
        _, result = build_consultation_pair(agent, voices)
        sources = [v["source"] for v in result.content["voices"]]
        assert sources == ["a"]

    def test_consecutive_calls_get_distinct_ids(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call1, _ = build_consultation_pair(agent, voices)
        time.sleep(0.001)
        call2, _ = build_consultation_pair(agent, voices)
        assert call1.id != call2.id


# ---------------------------------------------------------------------------
# BaseAgent: _run_consultation_fire,
# _rehydrate_appendix_tracking
# ---------------------------------------------------------------------------


# TestMaybeFireConsultation: removed 2026-05-02 with the turn-count cadence.
# Soul flow now fires exclusively on a wall-clock timer via _set_state
# cancel/restart mechanics. See _set_state and _soul_whisper for the new
# single-trigger design.


class TestRunConsultationFire:

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_empty_voices_is_noop(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[],
        ):
            agent._run_consultation_fire()
        assert len(agent._tc_inbox) == 0

    def test_voices_publish_to_notification_file(self, tmp_path):
        """Under the .notification/ filesystem redesign, soul flow voices
        publish to ``.notification/soul.json`` instead of enqueueing on
        tc_inbox.  The kernel's notification sync mechanism reads the
        file and injects the wire pair (single-slot replace, by
        construction of the filesystem write).
        """
        from lingtai_kernel.notifications import collect_notifications

        agent = self._make_real_agent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[{"source": "insights", "blocks": [TextBlock(text="hello")]}],
        ):
            agent._run_consultation_fire()
        # tc_inbox stays empty under the new path.
        assert len(agent._tc_inbox) == 0
        # The soul notification file is published.
        out = collect_notifications(agent.working_dir)
        assert "soul" in out
        voices = out["soul"]["data"]["voices"]
        assert len(voices) == 1
        assert voices[0]["source"] == "insights"
        assert voices[0]["voice"] == "hello"

    def test_exception_swallowed_and_logged(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent.logged = []
        original_log = agent._log

        def capture_log(event, **kw):
            agent.logged.append((event, kw))
            return original_log(event, **kw)
        agent._log = capture_log

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            side_effect=RuntimeError("boom"),
        ):
            agent._run_consultation_fire()  # should not raise
        events = [e for e, _ in agent.logged]
        assert "consultation_fire_error" in events


# ---------------------------------------------------------------------------
# soul_flow.jsonl schema (schema_version=3) — fire + voice records
# ---------------------------------------------------------------------------


class TestSoulFlowPersistenceSchema:
    """End-to-end: drive _run_consultation_fire with a mocked batch, inspect
    the on-disk records in logs/soul_flow.jsonl."""

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def _read_records(self, agent) -> list[dict]:
        path = agent._working_dir / "logs" / "soul_flow.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _seed_diary(self, agent, *texts: str) -> None:
        log_dir = agent._working_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for i, t in enumerate(texts):
                f.write(json.dumps({"type": "diary", "text": t, "ts": 1_700_000_000 + i}) + "\n")

    def test_writes_fire_and_voice_records_with_linked_fire_id(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "did X", "noticed Y")

        voices = [
            {"source": "insights", "blocks": [
                ThinkingBlock(text="considering"),
                TextBlock(text="step back: Z"),
            ]},
            {"source": "snapshot:snapshot_3_1735", "blocks": [
                TextBlock(text="I tried that once"),
            ]},
        ]
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=voices,
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        assert len(records) == 3, f"expected 1 fire + 2 voices, got {len(records)}"

        fires = [r for r in records if r["kind"] == "fire"]
        voice_recs = [r for r in records if r["kind"] == "voice"]
        assert len(fires) == 1
        assert len(voice_recs) == 2

        fire = fires[0]
        assert fire["schema_version"] == 3
        assert fire["fire_id"].startswith("fire_")
        assert fire["tc_id"] == fire["fire_id"]
        assert fire["outcome"] == "ok"
        assert "did X" in fire["diary"] and "noticed Y" in fire["diary"]
        assert set(fire["sources"]) == {"insights", "snapshot:snapshot_3_1735"}
        assert "ts" in fire and fire["ts"].endswith("Z")

        # All voice records link back to the same fire_id.
        for v in voice_recs:
            assert v["fire_id"] == fire["fire_id"]
            assert v["schema_version"] == 3
            assert "ts" in v and v["ts"].endswith("Z")
            assert "blocks" in v
            assert "consultation_kind" not in v
            assert "voice" not in v
            assert "thinking" not in v

        by_src = {v["source"]: v for v in voice_recs}
        assert by_src["insights"]["blocks"] == [
            {"type": "thinking", "text": "considering"},
            {"type": "text", "text": "step back: Z"},
        ]
        assert by_src["snapshot:snapshot_3_1735"]["blocks"] == [
            {"type": "text", "text": "I tried that once"},
        ]

    def test_empty_fire_still_writes_fire_record_with_empty_outcome(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "stuck thinking")

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        assert len(records) == 1
        fire = records[0]
        assert fire["kind"] == "fire"
        assert fire["outcome"] == "empty"
        assert fire["sources"] == []
        # Diary still captured even when no voices came back.
        assert "stuck thinking" in fire["diary"]
        # No tc_inbox enqueue happens on empty fires.
        assert len(agent._tc_inbox) == 0

    def test_synthetic_pair_call_id_matches_fire_id(self, tmp_path):
        """The notification payload's tc_id and the soul_flow.jsonl
        fire_id are the same string, so cross-referencing between the
        wire and the soul-flow log stays trivial under the
        .notification/ redesign."""
        from lingtai_kernel.notifications import collect_notifications

        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "diary text")

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[{"source": "insights", "blocks": [TextBlock(text="v")]}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        fire = next(r for r in records if r["kind"] == "fire")

        # tc_inbox stays empty — soul writes to the filesystem now.
        assert len(agent._tc_inbox) == 0
        # The published notification carries the same fire_id / tc_id.
        out = collect_notifications(agent.working_dir)
        assert "soul" in out
        assert out["soul"]["data"]["fire_id"] == fire["fire_id"]
        assert out["soul"]["data"]["tc_id"] == fire["fire_id"]

    def test_fire_record_written_even_on_exception(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "before crash")

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            side_effect=RuntimeError("boom from batch"),
        ):
            agent._run_consultation_fire()  # must not raise

        records = self._read_records(agent)
        assert len(records) == 1
        fire = records[0]
        assert fire["kind"] == "fire"
        assert fire["outcome"] == "error"
        assert "boom from batch" in fire["error"]
        # No voices on a hard-crash fire.
        assert "fire_id" in fire

    def test_diary_empty_still_recorded(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        # No events.jsonl — diary will be empty string.

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[{"source": "insights", "blocks": [TextBlock(text="v")]}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        fire = next(r for r in records if r["kind"] == "fire")
        assert fire["diary"] == ""
        # Voice record still produced.
        voices = [r for r in records if r["kind"] == "voice"]
        assert len(voices) == 1

    def test_appends_across_multiple_fires(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "d1")

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[{"source": "insights", "blocks": [TextBlock(text="v1")]}],
        ):
            agent._run_consultation_fire()
            # Drain so the second fire doesn't coalesce in tc_inbox terms
            # (it would still write its own log records regardless).
            agent._tc_inbox.drain()
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=[{"source": "snapshot:s1", "blocks": [TextBlock(text="v2")]}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        # 2 fires + 2 voices.
        assert len(records) == 4
        fires = [r for r in records if r["kind"] == "fire"]
        assert len(fires) == 2
        # Each fire gets a distinct id.
        assert fires[0]["fire_id"] != fires[1]["fire_id"]


class TestPersistSoulEntryUnchanged:
    """Inquiry path still uses the legacy schema — make sure we didn't break it."""

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_inquiry_persistence_writes_legacy_shape(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent._persist_soul_entry(
            {"prompt": "what should I do?", "voice": "rest", "thinking": []},
            mode="inquiry",
            source="agent",
        )
        path = agent._working_dir / "logs" / "soul_inquiry.jsonl"
        assert path.is_file()
        rec = json.loads(path.read_text().strip())
        assert rec["mode"] == "inquiry"
        assert rec["source"] == "agent"
        assert rec["prompt"] == "what should I do?"
        assert rec["voice"] == "rest"
        assert rec["thinking"] == []
        assert "ts" in rec
        # Legacy shape: no kind/schema_version/fire_id fields.
        assert "kind" not in rec
        assert "schema_version" not in rec


class TestRehydrateAppendixTracking:

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_no_chat_is_noop(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent._chat = None
        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source == {}

    def test_finds_existing_pair(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        # Inject a chat history containing a soul.flow pair.
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([TextBlock(text="reply")])
        iface.add_assistant_message([
            ToolCallBlock(id="tc_recover_me", name="soul",
                          args={"action": "flow"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_recover_me", name="soul",
                            content={"voices": []}),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source.get("soul.flow") == "tc_recover_me"

    def test_ignores_non_soul_pairs(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_other", name="bash", args={"cmd": "ls"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_other", name="bash", content="file"),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert "soul.flow" not in agent._appendix_ids_by_source

    def test_ignores_inquiry_action(self, tmp_path):
        """A soul(action='inquiry') pair would only ever appear via the
        synchronous inquiry path which doesn't go through tc_inbox; defensive
        check that we don't track it as a flow appendix."""
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_inq", name="soul",
                          args={"action": "inquiry"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_inq", name="soul", content={"voice": "x"}),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert "soul.flow" not in agent._appendix_ids_by_source

    def test_soul_whisper_delegates_to_consultation_fire(self, tmp_path):
        """The wall-clock soul timer (driven by config.soul_delay) now fires
        past-self consultation instead of the legacy diary+mirror-session
        flow. Verifies _soul_whisper -> _run_consultation_fire wiring."""
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc, agent_name="t", working_dir=tmp_path / "agent",
        )
        with patch.object(agent, "_run_consultation_fire") as mock_fire, \
             patch.object(agent, "_start_soul_timer") as mock_resched:
            agent._soul_whisper()
        assert mock_fire.call_count == 1
        # Current cadence is one-shot per IDLE transition: _soul_whisper does
        # not reschedule itself; the next transition to IDLE starts a new timer.
        assert mock_resched.call_count == 0

    def test_soul_whisper_swallows_consultation_fire_error(self, tmp_path):
        """Errors in the consultation fire must not break the cadence —
        the timer reschedules itself in finally regardless."""
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc, agent_name="t", working_dir=tmp_path / "agent",
        )
        with patch.object(agent, "_run_consultation_fire",
                          side_effect=RuntimeError("boom")), \
             patch.object(agent, "_start_soul_timer") as mock_resched:
            agent._soul_whisper()  # must not raise
        # Errors are swallowed, but cadence is still one-shot; no self-reschedule.
        assert mock_resched.call_count == 0

    def test_tracks_first_match_only(self, tmp_path):
        """Defensive: if somehow the history contains two soul.flow pairs
        (shouldn't happen post-design but tolerate it), only the first
        match is tracked. Caller can clean up subsequent matches manually."""
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        for tc_id in ["tc_first", "tc_second"]:
            iface.add_assistant_message([
                ToolCallBlock(id=tc_id, name="soul",
                              args={"action": "flow"}),
            ])
            iface.add_tool_results([
                ToolResultBlock(id=tc_id, name="soul",
                                content={"voices": []}),
            ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source["soul.flow"] == "tc_first"


# ---------------------------------------------------------------------------
# _render_current_diary — concatenate diary entries from events.jsonl
# ---------------------------------------------------------------------------


class TestRenderCurrentDiary:

    def _write_events(self, workdir: Path, records: list[dict]) -> None:
        log_dir = workdir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "events.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_returns_empty_when_no_log(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        assert _render_current_diary(agent) == ""

    def test_returns_empty_when_log_has_no_diary(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "boot", "ts": 1},
            {"type": "tool_call", "name": "psyche"},
        ])
        assert _render_current_diary(agent) == ""

    def test_concatenates_diary_entries_in_order(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "first turn thoughts", "ts": 1_700_000_000},
            {"type": "boot", "ts": 1},
            {"type": "diary", "text": "second turn thoughts", "ts": 1_700_000_001},
            {"type": "diary", "text": "third turn thoughts", "ts": 1_700_000_002},
        ])
        out = _render_current_diary(agent)
        assert "first turn thoughts" in out
        assert "second turn thoughts" in out
        assert "third turn thoughts" in out
        # Order preserved, with paragraph break separator.
        assert out.index("first") < out.index("second") < out.index("third")
        assert "\n\n" in out

    def test_skips_blank_and_non_string_text(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "valid", "ts": 1_700_000_000},
            {"type": "diary", "text": "   ", "ts": 1_700_000_001},   # whitespace only — skip
            {"type": "diary", "text": None, "ts": 1_700_000_002},     # not a string — skip
            {"type": "diary"},                    # missing text — skip
            {"type": "diary", "text": "second valid", "ts": 1_700_000_003},
        ])
        out = _render_current_diary(agent)
        assert "valid" in out
        assert "second valid" in out
        # Whitespace-only entry should not contribute its blanks
        assert out.count("\n\n") == 2   # header separator + one separator between two entries

    def test_tolerates_malformed_lines(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            f.write('{"type": "diary", "text": "good", "ts": 1700000000}\n')
            f.write("not json at all\n")
            f.write("\n")
            f.write('{"type": "diary", "text": "still good", "ts": 1700000001}\n')
        agent = _FakeAgent(tmp_path, with_chat=False)
        out = _render_current_diary(agent)
        assert "good" in out
        assert "still good" in out

    def test_render_diary_format_has_now_and_entry_timestamps(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "first", "ts": 1_700_000_000},
            {"type": "diary", "text": "second", "ts": 1_700_000_060},
        ])
        out = _render_current_diary(agent)
        assert out.startswith("[now: ")
        assert "\n\n[" in out
        assert "first" in out and "second" in out
        assert out.index("first") < out.index("second")

    def test_render_diary_tail_cap(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary, _DIARY_CUE_TOKEN_CAP
        from lingtai_kernel.token_counter import count_tokens
        agent = _FakeAgent(tmp_path, with_chat=False)
        records = []
        for i in range(240):
            records.append({
                "type": "diary",
                "text": f"entry-{i} " + ("x " * 240),
                "ts": 1_700_000_000 + i,
            })
        self._write_events(tmp_path, records)
        out = _render_current_diary(agent)
        assert count_tokens(out) <= _DIARY_CUE_TOKEN_CAP
        assert "entry-239" in out
        assert "entry-0" not in out

    def test_render_diary_single_oversized_entry(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "HUGE " + ("x " * 50_000), "ts": 1_700_000_000},
        ])
        out = _render_current_diary(agent)
        assert "HUGE" in out
        assert out.startswith("[now: ")

    def test_render_includes_thinking_entries_with_kind_tag(self, tmp_path):
        """Thinking and diary are siblings in events.jsonl. The cue mixes
        both — thinking is the inner monologue that explains the *why*
        behind the diary's externalized declaration. Each entry is tagged
        with its kind so the consultation voice can tell them apart."""
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "thinking", "text": "weighing options", "ts": 1_700_000_000},
            {"type": "diary", "text": "decided to act", "ts": 1_700_000_001},
            {"type": "tool_call", "ts": 1_700_000_002},  # not a cue type
            {"type": "thinking", "text": "no luck, retry", "ts": 1_700_000_003},
        ])
        out = _render_current_diary(agent)
        assert "weighing options" in out
        assert "decided to act" in out
        assert "no luck, retry" in out
        # Type tags appear on each entry's header line
        assert " thinking\nweighing options" in out
        assert " diary\ndecided to act" in out
        assert " thinking\nno luck, retry" in out
        # Chronological ordering preserved across mixed kinds
        assert out.index("weighing") < out.index("decided") < out.index("no luck")

    def test_render_reverse_seek_matches_forward_under_cap(self, tmp_path):
        """Reverse-seek + substring-prefilter should produce the same cue
        a forward scan would. The append-only JSONL invariant + UTF-8
        \\n-safety make the byte-level reverse read correct.

        Build a log with non-ASCII text (Chinese, emoji, ligatures), noise
        events that don't match the cue types, blank lines, and assert the
        rendered output contains every cue entry in chronological order."""
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        records = []
        ts0 = 1_700_000_000
        for i in range(20):
            records.append({"type": "tool_call", "name": "bash" * 20, "ts": ts0 + 3 * i})
            records.append({
                "type": "diary",
                "text": f"日记{i} — 中文与 emoji 🌙 entry-{i}",
                "ts": ts0 + 3 * i + 1,
            })
            records.append({
                "type": "thinking",
                "text": f"reasoning {i} ✱ ligatures ﬁ",
                "ts": ts0 + 3 * i + 2,
            })
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, records)
        out = _render_current_diary(agent)
        for i in range(20):
            assert f"日记{i}" in out
            assert f"reasoning {i}" in out
            assert f"emoji 🌙 entry-{i}" in out
            assert f"ligatures ﬁ" in out
        # noise types must not appear as their own entries
        assert " tool_call\n" not in out
        # ordering across both kinds
        assert out.index("日记0") < out.index("日记1") < out.index("日记19")
        assert out.index("reasoning 0") < out.index("reasoning 19")

    def test_render_reverse_seek_chunk_boundary_safety(self, tmp_path):
        """Force the reverse-seek to cross many chunk boundaries by writing
        a log larger than _REVERSE_READ_CHUNK. Every cue entry should still
        be picked up correctly, with no truncated/missing entries from
        chunk-edge JSON splits."""
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        from lingtai_kernel.intrinsics.soul.consultation import _REVERSE_READ_CHUNK
        # Each cue entry plus padding noise — ensure total bytes > 4 chunks
        records = []
        ts0 = 1_700_000_000
        # 200 cue entries with long padding text so the file grows past
        # multiple chunk boundaries
        padding = "padding-x " * 100
        for i in range(200):
            records.append({
                "type": "diary" if i % 2 == 0 else "thinking",
                "text": f"E{i:03d} {padding}",
                "ts": ts0 + i,
            })
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, records)
        # Sanity: file is multi-chunk
        log_size = (tmp_path / "logs" / "events.jsonl").stat().st_size
        assert log_size > _REVERSE_READ_CHUNK * 2
        out = _render_current_diary(agent)
        # Every entry that fits under the cap must appear; entries are
        # tail-trimmed under _DIARY_CUE_TOKEN_CAP, but no entry that
        # appears should be partial.
        seen_ids = []
        for i in range(200):
            if f"E{i:03d}" in out:
                seen_ids.append(i)
        assert seen_ids, "no entries kept"
        # Kept entries must be a contiguous tail slice (chronological)
        assert seen_ids == list(range(seen_ids[0], 200))


# ---------------------------------------------------------------------------
# _load_snapshot_interface verbatim substrate
# ---------------------------------------------------------------------------


class TestSnapshotVerbatimLoading:

    def test_preserves_tool_call_and_tool_result_blocks(self, tmp_path):
        # Build a snapshot with mixed content: text, thinking, tool_call,
        # tool_result. After load, every block survives verbatim.
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("a question from user")
        iface.add_assistant_message([
            ThinkingBlock(text="reasoning"),
            TextBlock(text="I'll call a tool"),
            ToolCallBlock(id="tc_1", name="psyche", args={"action": "show"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_1", name="psyche", content="result"),
        ])
        iface.add_assistant_message([TextBlock(text="final answer")])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        all_blocks = [b for entry in loaded.entries for b in entry.content]
        assert any(isinstance(block, ToolCallBlock) for block in all_blocks)
        assert any(isinstance(block, ToolResultBlock) for block in all_blocks)

    def test_preserves_tool_result_only_entries(self, tmp_path):
        # A user entry that is purely a tool_result is legitimate context for
        # the past-self substrate and must be preserved.
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("question")
        iface.add_assistant_message([
            TextBlock(text="calling"),
            ToolCallBlock(id="tc_1", name="psyche", args={}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_1", name="psyche", content="data"),
        ])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        assert any(
            e.role == "user" and all(isinstance(b, ToolResultBlock) for b in e.content)
            for e in loaded.entries
        )

    def test_preserves_system_entry(self, tmp_path):
        iface = ChatInterface()
        iface.add_system("THE FROZEN SYSTEM PROMPT")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello back")])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None
        sys_entries = [e for e in loaded.entries if e.role == "system"]
        assert len(sys_entries) == 1
        assert sys_entries[0].content[0].text == "THE FROZEN SYSTEM PROMPT"

    def test_keeps_thinking_blocks(self, tmp_path):
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("question")
        iface.add_assistant_message([
            ThinkingBlock(text="careful reasoning"),
            TextBlock(text="answer"),
        ])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None
        all_blocks = [b for e in loaded.entries for b in e.content]
        assert any(isinstance(b, ThinkingBlock) for b in all_blocks)

    def test_preserves_tool_schema_list_from_system_entry(self, tmp_path):
        # Past self had a real tool schema list bound to its system entry.
        # After thaw, the complete substrate, including schema metadata,
        # survives verbatim so historic calls/results remain legible.
        frozen_tools = [
            {
                "name": "psyche",
                "description": "molt yourself",
                "input_schema": {"type": "object"},
            },
            {
                "name": "soul",
                "description": "inner voice",
                "input_schema": {"type": "object"},
            },
        ]
        iface = ChatInterface()
        iface.add_system("FROZEN PROMPT WITH TOOL PROSE", tools=frozen_tools)
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])

        # Sanity: the source interface really did carry tools.
        assert iface.current_tools == frozen_tools
        sys_src = next(e for e in iface.entries if e.role == "system")
        assert sys_src._tools == frozen_tools

        path = _write_snapshot(
            tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict()
        )
        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        # System text is preserved verbatim — that's the past self's
        # frozen identity / job description.
        sys_loaded = next(e for e in loaded.entries if e.role == "system")
        assert sys_loaded.content[0].text == "FROZEN PROMPT WITH TOOL PROSE"

        assert sys_loaded._tools == frozen_tools
        assert loaded.current_tools == frozen_tools


# ---------------------------------------------------------------------------
# _kind_for_source / _build_consultation_cue / dispatch
# ---------------------------------------------------------------------------


class TestKindDispatch:

    def test_insights_source_maps_to_insights_kind(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        assert _kind_for_source("insights") == "insights"

    def test_snapshot_source_maps_to_past_kind(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        assert _kind_for_source("snapshot:snapshot_3_1735") == "past"

    def test_other_source_maps_to_past(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        # Defaults to past for unknown labels — past is the more general
        # frame and the safer default.
        assert _kind_for_source("anything else") == "past"


class TestBuildConsultationCue:

    def test_insights_cue_does_not_inject_diary(self, tmp_path):
        # The insights branch consultation runs against the live chat
        # interface, which already contains the diary entries verbatim.
        # Re-injecting them as the spark would be duplicative — the cue
        # is just a step-back nudge.
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "insights", "I built X today.")
        assert "I built X today." not in cue
        # Insights cue should not frame as "your future self"
        assert "future self" not in cue.lower()
        # But it should nudge the model to step back from its own context.
        assert "step back" in cue.lower()

    def test_past_cue_includes_diary_and_future_self_frame(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "past", "I built X today.")
        assert "I built X today." in cue
        assert "future self" in cue.lower()

    def test_empty_diary_uses_placeholder(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "past", "")
        assert "no diary yet" in cue

    def test_zh_cue_renders(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        agent._config.language = "zh"
        cue = _build_consultation_cue(agent, "past", "今日做了 X。")
        assert "今日做了 X。" in cue
        assert "未来" in cue   # zh-localized "future self" framing

    def test_wen_cue_renders(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        agent._config.language = "wen"
        cue = _build_consultation_cue(agent, "past", "今日造 X。")
        assert "今日造 X。" in cue


class TestRunConsultationDispatchesByKind:
    """Confirms _run_consultation uses one consultation prompt and sends a
    kind-appropriate spark: past gets the diary as future-self framing,
    insights gets a step-back nudge without the diary text (substrate
    already contains it)."""

    def _run(self, tmp_path, source: str):
        from lingtai_kernel.intrinsics.soul import _run_consultation

        agent = _FakeAgent(tmp_path)
        # Seed a tiny diary
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w") as f:
            f.write(json.dumps({"type": "diary", "text": "DIARY MARKER", "ts": 1700000000}) + "\n")

        captured = {}

        from lingtai_kernel.llm.base import LLMResponse

        class _MockSession:
            def __init__(self, interface):
                self.interface = interface

            def send(self, content):
                captured["sent_content"] = content
                self.interface.add_user_message(content)
                self.interface.add_assistant_message([TextBlock(text="voice text")])
                return LLMResponse(text="voice text")

        def _create_session(*, system_prompt, interface, **kw):
            captured["system_prompt"] = system_prompt
            captured["tools"] = kw.get("tools")
            return _MockSession(interface)

        agent.service.create_session.side_effect = _create_session

        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("frozen user")
        iface.add_assistant_message([TextBlock(text="frozen reply")])

        result = _run_consultation(agent, iface, source)
        return captured, result

    def test_past_dispatch_uses_past_cue(self, tmp_path):
        captured, result = self._run(tmp_path, "snapshot:snapshot_3_1735")
        assert result is not None
        # After the raw-diary refactor, the spark is the raw diary cue
        # from _render_current_diary — not wrapped by _build_consultation_cue.
        assert "DIARY MARKER" in captured["sent_content"]
        assert captured["sent_content"].startswith("[now:")
        assert "soul-flow voice" in captured["system_prompt"]
        assert captured["tools"] == [{"name": "bash", "description": "run shell", "parameters": {"type": "object"}}]

    def test_insights_dispatch_uses_raw_diary(self, tmp_path):
        captured, result = self._run(tmp_path, "insights")
        assert result is not None
        # After the raw-diary refactor, the spark is the raw diary cue
        # for ALL sources (insights included) — no special "step-back"
        # wrapper. The diary text is verbatim in the spark.
        assert "DIARY MARKER" in captured["sent_content"]
        assert captured["sent_content"].startswith("[now:")
        assert "soul-flow voice" in captured["system_prompt"]
        assert captured["tools"] == [{"name": "bash", "description": "run shell", "parameters": {"type": "object"}}]


# ---------------------------------------------------------------------------
# action='config' — agent-tunable soul cadence (delay + K)
# ---------------------------------------------------------------------------


class _ConfigFakeAgent(_FakeAgent):
    """Extension of _FakeAgent with the attributes _handle_config touches:
    _soul_delay, _config.consultation_past_count,
    _shutdown (Event), _start_soul_timer (callback).
    Tracks whether the timer was restarted."""

    def __init__(self, tmp_path, *, initial_delay=120.0, shutdown=False,
                 initial_past_count=2):
        super().__init__(tmp_path)
        self._soul_delay = float(initial_delay)
        self._config.consultation_past_count = initial_past_count
        import threading
        self._shutdown = threading.Event()
        if shutdown:
            self._shutdown.set()
        self.timer_restart_count = 0

    def _start_soul_timer(self):
        self.timer_restart_count += 1


class TestSoulConfig:

    def test_config_updates_delay_and_restarts_timer(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {"action": "config", "delay_seconds": 600})

        assert result["status"] == "ok"
        assert result["old"]["delay_seconds"] == 120.0
        assert result["new"]["delay_seconds"] == 600.0
        assert agent._soul_delay == 600.0
        assert agent.timer_restart_count == 1
        assert any(ev == "soul_config" for ev, _ in agent.logged)

    def test_config_updates_consultation_past_count(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_past_count=2)

        result = handle(agent, {"action": "config", "consultation_past_count": 4})

        assert result["status"] == "ok"
        assert result["old"]["consultation_past_count"] == 2
        assert result["new"]["consultation_past_count"] == 4
        assert agent._config.consultation_past_count == 4

    def test_config_accepts_multiple_fields_at_once(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0,
                                  initial_past_count=2)

        result = handle(agent, {
            "action": "config",
            "delay_seconds": 300,
            "consultation_past_count": 1,
        })

        assert result["status"] == "ok"
        assert result["new"] == {
            "delay_seconds": 300.0,
            "consultation_past_count": 1,
        }
        assert agent._soul_delay == 300.0
        assert agent._config.consultation_past_count == 1
        assert agent.timer_restart_count == 1  # delay changed

    def test_config_rejects_delay_below_minimum(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import (
            handle,
            SOUL_DELAY_MIN_SECONDS,
        )
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {
            "action": "config",
            "delay_seconds": SOUL_DELAY_MIN_SECONDS - 1,
        })

        assert "error" in result
        assert agent._soul_delay == 120.0
        assert agent.timer_restart_count == 0

    def test_config_accepts_delay_exactly_at_minimum(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import (
            handle,
            SOUL_DELAY_MIN_SECONDS,
        )
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {
            "action": "config",
            "delay_seconds": SOUL_DELAY_MIN_SECONDS,
        })

        assert result["status"] == "ok"
        assert agent._soul_delay == SOUL_DELAY_MIN_SECONDS

    def test_config_requires_at_least_one_field(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {"action": "config"})

        assert "error" in result
        assert agent._soul_delay == 120.0
        assert agent.timer_restart_count == 0

    def test_config_rejects_non_numeric_delay(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {
            "action": "config",
            "delay_seconds": "fast",
        })

        assert "error" in result
        assert agent._soul_delay == 120.0
        assert agent.timer_restart_count == 0

    def test_config_rejects_nan_delay(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {
            "action": "config",
            "delay_seconds": float("nan"),
        })

        assert "error" in result
        assert agent._soul_delay == 120.0

    def test_config_skips_timer_restart_when_shutdown(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0, shutdown=True)

        result = handle(agent, {"action": "config", "delay_seconds": 300})

        assert result["status"] == "ok"
        assert agent._soul_delay == 300.0
        assert agent.timer_restart_count == 0

    def test_config_rejects_past_count_above_max(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import (
            handle, CONSULTATION_PAST_COUNT_MAX,
        )
        agent = _ConfigFakeAgent(tmp_path, initial_past_count=2)

        result = handle(agent, {
            "action": "config",
            "consultation_past_count": CONSULTATION_PAST_COUNT_MAX + 1,
        })
        assert "error" in result
        assert agent._config.consultation_past_count == 2

    def test_config_rejects_past_count_below_min(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_past_count=2)

        result = handle(agent, {
            "action": "config",
            "consultation_past_count": -1,
        })
        assert "error" in result
        assert agent._config.consultation_past_count == 2

    def test_config_in_schema_enum(self):
        from lingtai_kernel.intrinsics.soul import get_schema
        schema = get_schema("en")
        assert "config" in schema["properties"]["action"]["enum"]
        # set_delay removed from enum
        assert "set_delay" not in schema["properties"]["action"]["enum"]
        # Both knobs present in schema
        assert "delay_seconds" in schema["properties"]
        assert schema["properties"]["delay_seconds"]["type"] == "number"
        assert schema["properties"]["delay_seconds"]["minimum"] == 30.0
        assert "consultation_interval" not in schema["properties"]
        assert "consultation_past_count" in schema["properties"]
        assert schema["properties"]["consultation_past_count"]["type"] == "integer"

    def test_unknown_action_still_errors(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path)
        result = handle(agent, {"action": "bogus"})
        assert "error" in result
        assert "bogus" in result["error"]

    def test_set_delay_action_now_unknown(self, tmp_path):
        # Regression guard: set_delay was removed; agents calling it must
        # see an unknown-action error pointing them at config.
        from lingtai_kernel.intrinsics.soul import handle
        agent = _ConfigFakeAgent(tmp_path, initial_delay=120.0)

        result = handle(agent, {"action": "set_delay", "delay_seconds": 600})

        assert "error" in result
        assert "config" in result["error"]
        assert agent._soul_delay == 120.0  # state unchanged


# ---------------------------------------------------------------------------
# Soul notification envelope — instructions field framing
# ---------------------------------------------------------------------------


class TestSoulNotificationInstructions:
    """The soul notification carries an instructions field that frames
    voices as YOUR own inner monologue (not external messages) and
    distinguishes insights (current-self) from snapshots (past-self).
    Regression for the case where an agent mistook a soul voice's
    narration of 'human pasted my diary' as a fact, when the human
    had only sent a short email."""

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def _seed_diary(self, agent, *texts: str) -> None:
        log_dir = agent._working_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for i, t in enumerate(texts):
                f.write(json.dumps({"type": "diary", "text": t, "ts": 1_700_000_000 + i}) + "\n")

    def test_soul_notification_carries_instructions(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "did X")

        voices = [
            {"source": "insights", "blocks": [TextBlock(text="step back")]},
        ]
        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
            return_value=voices,
        ):
            agent._run_consultation_fire()

        notif_path = agent._working_dir / ".notification" / "soul.json"
        assert notif_path.is_file()
        payload = json.loads(notif_path.read_text())
        assert "instructions" in payload
        text = payload["instructions"]
        # Defines insights vs snapshot sources.
        assert "insights" in text
        assert "snapshot" in text
        # Emphasizes voices are the agent's own monologue, not external.
        assert "YOUR OWN" in text or "inner monologue" in text
        # Reaffirms email-as-only-human-channel.
        assert "email" in text
        # Tells the agent to verify before acting on narrated events.
        assert "verify" in text.lower() or "belief" in text.lower()


# ---------------------------------------------------------------------------
# Regression: the consultation prompt must not teach removed tools.
# ---------------------------------------------------------------------------


def test_consultation_prompt_has_no_removed_codex_tool_call(tmp_path):
    """The resolved consultation system prompt (and its refusal echo) must
    not suggest a removed `codex(...)` tool call.
    """
    from lingtai_kernel.intrinsics.soul.config import _build_soul_system_prompt
    from lingtai_kernel.intrinsics.soul.consultation import (
        _build_consultation_tool_refusal,
    )

    agent = _FakeAgent(tmp_path)
    system_prompt = _build_soul_system_prompt(agent, kind="insights")
    refusal = _build_consultation_tool_refusal(system_prompt)

    # No `codex(` anywhere as a suggested tool-probe.
    assert "codex(" not in system_prompt
    # The refusal text re-grounds with the resolved system prompt, so check it too.
    assert "codex(" not in refusal
    assert system_prompt in refusal
