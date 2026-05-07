"""Tests for the subconscious engine (feat/subconscious-redesign).

Tests the shared engine extraction, config hard-gating, JSON parsing,
timer lifecycle, JSONL persistence, and IDLE-gated soul flow.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.state import AgentState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.provider = "test-provider"
    return svc


def _make_agent(tmp_path, **config_kw):
    from lingtai_kernel import BaseAgent
    return BaseAgent(
        service=_make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "test_agent",
        config=AgentConfig(**config_kw),
    )


# ---------------------------------------------------------------------------
# Config hard-gating
# ---------------------------------------------------------------------------


class TestSubconsciousConfigHardGating:
    """enabling requires both provider and model to be explicitly set."""

    def test_enable_without_provider_fails(self, tmp_path):
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config
        result = _handle_config(agent, {
            "subconscious_enabled": True,
            "subconscious_model": "cheap-model",
        })
        assert "error" in result
        assert "subconscious_provider" in result["error"]
        assert not agent._config.subconscious_enabled

    def test_enable_without_model_fails(self, tmp_path):
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config
        result = _handle_config(agent, {
            "subconscious_enabled": True,
            "subconscious_provider": "mimo",
        })
        assert "error" in result
        assert "subconscious_model" in result["error"]
        assert not agent._config.subconscious_enabled

    def test_enable_with_both_succeeds(self, tmp_path):
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config
        result = _handle_config(agent, {
            "subconscious_enabled": True,
            "subconscious_provider": "mimo",
            "subconscious_model": "mimo-2-cheap",
        })
        assert result["status"] == "ok"
        assert result["new"]["subconscious_enabled"] is True
        assert agent._config.subconscious_enabled is True
        assert agent._config.subconscious_provider == "mimo"
        assert agent._config.subconscious_model == "mimo-2-cheap"

    def test_set_provider_model_before_enable(self, tmp_path):
        """Provider and model can be set in separate config calls."""
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config

        # Set provider first.
        r1 = _handle_config(agent, {"subconscious_provider": "mimo"})
        assert r1["status"] == "ok"

        # Set model.
        r2 = _handle_config(agent, {"subconscious_model": "cheap"})
        assert r2["status"] == "ok"

        # Now enable — both are set.
        r3 = _handle_config(agent, {"subconscious_enabled": True})
        assert r3["status"] == "ok"
        assert agent._config.subconscious_enabled is True

    def test_disable_succeeds_without_provider_model(self, tmp_path):
        """Disabling doesn't require provider/model."""
        agent = _make_agent(tmp_path, subconscious_enabled=True)
        agent._config.subconscious_provider = None
        agent._config.subconscious_model = None
        from lingtai_kernel.intrinsics.soul.config import _handle_config
        result = _handle_config(agent, {"subconscious_enabled": False})
        assert result["status"] == "ok"
        assert agent._config.subconscious_enabled is False


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


class TestSubconsciousConfigPersistence:
    """subconscious config round-trips through init.json."""

    def test_persist_subconscious_config(self, tmp_path):
        agent = _make_agent(tmp_path)
        init_path = agent._working_dir / "init.json"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        init_path.write_text(json.dumps({
            "manifest": {"llm": {}}
        }), encoding="utf-8")

        from lingtai_kernel.intrinsics.soul.config import _persist_soul_config
        _persist_soul_config(agent, {
            "subconscious_enabled": True,
            "subconscious_provider": "mimo",
            "subconscious_model": "cheap",
        })

        data = json.loads(init_path.read_text())
        sub = data["manifest"]["soul"]["subconscious"]
        assert sub["enabled"] is True
        assert sub["provider"] == "mimo"
        assert sub["model"] == "cheap"


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


class TestSubconsciousJsonParsing:
    """Parse structured and unstructured LLM responses."""

    def test_parse_valid_json(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response(
            '{"insight": "pattern X matches Y", "confidence": 0.8, "source_memory": "snapshot_001"}'
        )
        assert result is not None
        assert result["insight"] == "pattern X matches Y"
        assert result["confidence"] == 0.8
        assert result["source_memory"] == "snapshot_001"

    def test_parse_null_insight_returns_none(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response('{"insight": null}')
        assert result is None

    def test_parse_empty_insight_returns_none(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response('{"insight": ""}')
        assert result is None

    def test_parse_markdown_wrapped_json(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response(
            '```json\n{"insight": "found it", "confidence": 0.9}\n```'
        )
        assert result is not None
        assert result["insight"] == "found it"

    def test_parse_unstructured_text(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response("This reminds me of something.")
        assert result is not None
        assert result["insight"] == "This reminds me of something."
        assert result["confidence"] == 0.5
        assert result["source_memory"] == "unstructured"

    def test_confidence_clamped(self):
        from lingtai_kernel.intrinsics.soul.subconscious import _parse_subconscious_response
        result = _parse_subconscious_response(
            '{"insight": "test", "confidence": 1.5}'
        )
        assert result["confidence"] == 1.0

        result = _parse_subconscious_response(
            '{"insight": "test", "confidence": -0.5}'
        )
        assert result["confidence"] == 0.0


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------


class TestSubconsciousJsonl:
    """Append, read, and clear the subconscious JSONL."""

    def test_append_and_read(self, tmp_path):
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _append_subconscious_record,
            _read_subconscious_tail,
            _clear_subconscious_jsonl,
        )

        _append_subconscious_record(agent, {
            "ts": time.time(),
            "fire_id": "test",
            "insight": "pattern found",
            "confidence": 0.7,
            "source_memory": "snap",
            "source_snapshot": "snapshot:foo",
            "model_used": "cheap",
        })

        tail = _read_subconscious_tail(agent, n=5)
        assert "pattern found" in tail
        assert "confidence=0.7" in tail

        _clear_subconscious_jsonl(agent)
        tail = _read_subconscious_tail(agent, n=5)
        assert tail == ""

    def test_read_reverse_order(self, tmp_path):
        """Newest-last ordering."""
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _append_subconscious_record,
            _read_subconscious_tail,
        )

        _append_subconscious_record(agent, {
            "ts": time.time() - 10,
            "fire_id": "f1",
            "insight": "first insight",
            "confidence": 0.5,
            "source_memory": "s1",
            "source_snapshot": "snapshot:a",
            "model_used": "m",
        })
        _append_subconscious_record(agent, {
            "ts": time.time(),
            "fire_id": "f2",
            "insight": "second insight",
            "confidence": 0.8,
            "source_memory": "s2",
            "source_snapshot": "snapshot:b",
            "model_used": "m",
        })

        tail = _read_subconscious_tail(agent, n=10)
        # second insight should appear after first (newest-last)
        first_pos = tail.index("first insight")
        second_pos = tail.index("second insight")
        assert second_pos > first_pos


# ---------------------------------------------------------------------------
# Timer lifecycle
# ---------------------------------------------------------------------------


class TestSubconsciousTimerLifecycle:
    """Timer starts on turn start, cancels on state transition."""

    def test_timer_not_started_when_disabled(self, tmp_path):
        agent = _make_agent(tmp_path, subconscious_enabled=False)
        from lingtai_kernel.intrinsics.soul.subconscious import _start_subconscious_timer
        _start_subconscious_timer(agent)
        assert getattr(agent, "_subconscious_timer", None) is None

    def test_timer_started_when_enabled(self, tmp_path):
        agent = _make_agent(tmp_path, subconscious_enabled=True)
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _start_subconscious_timer,
            _cancel_subconscious_timer,
        )
        _start_subconscious_timer(agent)
        assert agent._subconscious_timer is not None
        assert agent._subconscious_timer.is_alive()
        _cancel_subconscious_timer(agent)
        assert agent._subconscious_timer is None

    def test_state_transition_cancels_timer(self, tmp_path):
        agent = _make_agent(tmp_path, subconscious_enabled=True)
        from lingtai_kernel.intrinsics.soul.subconscious import _start_subconscious_timer
        _start_subconscious_timer(agent)
        assert agent._subconscious_timer is not None

        # Transition away from ACTIVE cancels the timer.
        agent._state = AgentState.ACTIVE
        agent._set_state(AgentState.IDLE, reason="turn done")
        assert agent._subconscious_timer is None


# ---------------------------------------------------------------------------
# Shared engine
# ---------------------------------------------------------------------------


class TestSharedConsultationEngine:
    """_run_consultation_voice respects allow_tool_recommendations."""

    def test_no_tools_when_disabled(self, tmp_path):
        """allow_tool_recommendations=False passes tools=None."""
        from lingtai_kernel.intrinsics.soul.consultation import _run_consultation_voice
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock

        agent = _make_agent(tmp_path)
        agent._config.model = "test-model"

        iface = ChatInterface()
        iface.add_user_message("hello")

        mock_response = MagicMock()
        mock_response.text = "response text"
        mock_response.tool_calls = []
        mock_response.thoughts = []
        mock_response.usage = MagicMock(
            input_tokens=0, output_tokens=0,
            thinking_tokens=0, cached_tokens=0,
        )

        mock_session = MagicMock()
        mock_session.interface = MagicMock()
        mock_session.interface.entries = [MagicMock(role="assistant", content=[TextBlock(text="response text")])]
        agent.service.create_session.return_value = mock_session

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
            return_value=mock_response,
        ):
            result = _run_consultation_voice(
                agent, iface, "test",
                system_prompt="test prompt",
                spark="test spark",
                allow_tool_recommendations=False,
            )

        # Verify tools=None was passed.
        call_kwargs = agent.service.create_session.call_args
        assert call_kwargs.kwargs.get("tools") is None or call_kwargs[1].get("tools") is None

    def test_tools_passed_when_enabled(self, tmp_path):
        """allow_tool_recommendations=True passes tool schemas."""
        from lingtai_kernel.intrinsics.soul.consultation import _run_consultation_voice
        from lingtai_kernel.llm.interface import ChatInterface, TextBlock

        agent = _make_agent(tmp_path)
        agent._config.model = "test-model"
        agent._session = MagicMock()
        agent._session._build_tool_schemas_fn.return_value = [{"name": "test"}]

        iface = ChatInterface()
        iface.add_user_message("hello")

        mock_response = MagicMock()
        mock_response.text = "done"
        mock_response.tool_calls = []
        mock_response.thoughts = []
        mock_response.usage = MagicMock(
            input_tokens=0, output_tokens=0,
            thinking_tokens=0, cached_tokens=0,
        )

        mock_session = MagicMock()
        mock_session.interface = MagicMock()
        mock_session.interface.entries = [MagicMock(role="assistant", content=[TextBlock(text="done")])]
        agent.service.create_session.return_value = mock_session

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
            return_value=mock_response,
        ):
            result = _run_consultation_voice(
                agent, iface, "test",
                system_prompt="test prompt",
                spark="test spark",
                allow_tool_recommendations=True,
            )

        # Verify tools were passed (not None).
        call_kwargs = agent.service.create_session.call_args
        tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
        assert tools is not None


# ---------------------------------------------------------------------------
# Session overrides
# ---------------------------------------------------------------------------


class TestSessionOverrides:
    """Session overrides are passed through to create_session."""

    def test_model_override(self, tmp_path):
        from lingtai_kernel.intrinsics.soul.subconscious import _build_session_overrides

        agent = _make_agent(tmp_path,
                            subconscious_provider="mimo",
                            subconscious_model="cheap-model",
                            subconscious_base_url="http://localhost:8080")

        overrides = _build_session_overrides(agent)
        assert overrides["provider"] == "mimo"
        assert overrides["model"] == "cheap-model"
        assert overrides["base_url"] == "http://localhost:8080"

    def test_empty_overrides(self, tmp_path):
        from lingtai_kernel.intrinsics.soul.subconscious import _build_session_overrides
        agent = _make_agent(tmp_path)
        overrides = _build_session_overrides(agent)
        assert overrides.get("provider") is None or "provider" not in overrides


# ---------------------------------------------------------------------------
# IDLE-gated soul flow
# ---------------------------------------------------------------------------


class TestIdleGatedSoulFlow:
    """Soul flow fires only on IDLE, not ACTIVE."""

    def test_soul_fire_allowed_idle(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._state = AgentState.IDLE
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed
        assert _soul_fire_allowed(agent) is True

    def test_soul_fire_not_allowed_active(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._state = AgentState.ACTIVE
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed
        assert _soul_fire_allowed(agent) is False

    def test_soul_fire_not_allowed_asleep(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._state = AgentState.ASLEEP
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed
        assert _soul_fire_allowed(agent) is False


# ---------------------------------------------------------------------------
# Architecture C — Selective snapshot sampling
# ---------------------------------------------------------------------------


class TestSelectiveSnapshotSampling:
    """Architecture C: random sample of N snapshots instead of all."""

    def test_sample_n_default_is_3(self, tmp_path):
        """Default sample_n is 3."""
        from lingtai_kernel.intrinsics.soul.subconscious import _SUBCONSCIOUS_SAMPLE_N
        assert _SUBCONSCIOUS_SAMPLE_N == 3

    def test_sample_n_config_persists(self, tmp_path):
        """sample_n persists to init.json."""
        agent = _make_agent(tmp_path)
        init_path = agent._working_dir / "init.json"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        init_path.write_text(json.dumps({
            "manifest": {"llm": {}}
        }), encoding="utf-8")

        from lingtai_kernel.intrinsics.soul.config import _persist_soul_config
        _persist_soul_config(agent, {
            "subconscious_sample_n": 5,
        })

        data = json.loads(init_path.read_text())
        sub = data["manifest"]["soul"]["subconscious"]
        assert sub["sample_n"] == 5

    def test_sample_n_config_validation(self, tmp_path):
        """sample_n must be a positive integer."""
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config

        # Zero fails.
        result = _handle_config(agent, {"subconscious_sample_n": 0})
        assert "error" in result

        # Negative fails.
        result = _handle_config(agent, {"subconscious_sample_n": -1})
        assert "error" in result

        # String fails.
        result = _handle_config(agent, {"subconscious_sample_n": "abc"})
        assert "error" in result

        # Positive succeeds.
        result = _handle_config(agent, {"subconscious_sample_n": 5})
        assert result["status"] == "ok"
        assert result["new"]["subconscious_sample_n"] == 5

    def test_fire_respects_sample_n(self, tmp_path):
        """When there are more snapshots than sample_n, only sample_n are used."""
        from lingtai_kernel.intrinsics.soul.subconscious import _run_subconscious_fire

        agent = _make_agent(tmp_path,
                            subconscious_enabled=True,
                            subconscious_provider="test",
                            subconscious_model="test",
                            subconscious_sample_n=2)
        agent._state = AgentState.ACTIVE
        agent._shutdown = threading.Event()

        # Create 5 fake snapshot files.
        snap_dir = agent._working_dir / "history" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (snap_dir / f"snapshot_{i:03d}.json").write_text(
                json.dumps({"schema_version": 1, "interface": []}),
                encoding="utf-8",
            )

        # Mock the snapshot runner to count invocations.
        call_count = {"n": 0}
        def mock_run_snapshot(agent, path, diary, fire_id, overrides):
            call_count["n"] += 1
            return None

        with patch(
            "lingtai_kernel.intrinsics.soul.subconscious._run_subconscious_snapshot",
            side_effect=mock_run_snapshot,
        ), patch(
            "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
            return_value="test diary",
        ):
            _run_subconscious_fire(agent)

        assert call_count["n"] == 2, f"Expected 2 snapshot calls, got {call_count['n']}"

    def test_fire_uses_all_when_fewer_than_sample_n(self, tmp_path):
        """When there are fewer snapshots than sample_n, all are used."""
        from lingtai_kernel.intrinsics.soul.subconscious import _run_subconscious_fire

        agent = _make_agent(tmp_path,
                            subconscious_enabled=True,
                            subconscious_provider="test",
                            subconscious_model="test",
                            subconscious_sample_n=5)
        agent._state = AgentState.ACTIVE
        agent._shutdown = threading.Event()

        # Create only 2 snapshot files.
        snap_dir = agent._working_dir / "history" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (snap_dir / f"snapshot_{i:03d}.json").write_text(
                json.dumps({"schema_version": 1, "interface": []}),
                encoding="utf-8",
            )

        call_count = {"n": 0}
        def mock_run_snapshot(agent, path, diary, fire_id, overrides):
            call_count["n"] += 1
            return None

        with patch(
            "lingtai_kernel.intrinsics.soul.subconscious._run_subconscious_snapshot",
            side_effect=mock_run_snapshot,
        ), patch(
            "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
            return_value="test diary",
        ):
            _run_subconscious_fire(agent)

        assert call_count["n"] == 2, f"Expected 2 snapshot calls, got {call_count['n']}"


# ---------------------------------------------------------------------------
# Architecture C — Confidence threshold filtering
# ---------------------------------------------------------------------------


class TestConfidenceThresholdFiltering:
    """Architecture C: only append insights with confidence > threshold."""

    def test_confidence_threshold_default_is_06(self, tmp_path):
        """Default threshold is 0.6."""
        from lingtai_kernel.intrinsics.soul.subconscious import _SUBCONSCIOUS_CONFIDENCE_THRESHOLD
        assert _SUBCONSCIOUS_CONFIDENCE_THRESHOLD == 0.6

    def test_confidence_threshold_config_persists(self, tmp_path):
        """confidence_threshold persists to init.json."""
        agent = _make_agent(tmp_path)
        init_path = agent._working_dir / "init.json"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        init_path.write_text(json.dumps({
            "manifest": {"llm": {}}
        }), encoding="utf-8")

        from lingtai_kernel.intrinsics.soul.config import _persist_soul_config
        _persist_soul_config(agent, {
            "subconscious_confidence_threshold": 0.8,
        })

        data = json.loads(init_path.read_text())
        sub = data["manifest"]["soul"]["subconscious"]
        assert sub["confidence_threshold"] == 0.8

    def test_confidence_threshold_config_validation(self, tmp_path):
        """confidence_threshold must be in [0.0, 1.0]."""
        agent = _make_agent(tmp_path)
        from lingtai_kernel.intrinsics.soul.config import _handle_config

        # Out of range high.
        result = _handle_config(agent, {"subconscious_confidence_threshold": 1.5})
        assert "error" in result

        # Out of range low.
        result = _handle_config(agent, {"subconscious_confidence_threshold": -0.1})
        assert "error" in result

        # NaN.
        result = _handle_config(agent, {"subconscious_confidence_threshold": float("nan")})
        assert "error" in result

        # Valid value.
        result = _handle_config(agent, {"subconscious_confidence_threshold": 0.7})
        assert result["status"] == "ok"
        assert result["new"]["subconscious_confidence_threshold"] == 0.7

    def _make_mock_iface(self):
        """Create a mock ChatInterface with at least one entry."""
        from lingtai_kernel.llm.interface import ChatInterface
        iface = ChatInterface()
        iface.add_user_message("test snapshot content")
        return iface

    def test_low_confidence_insight_discarded(self, tmp_path):
        """Insights below threshold are not appended to JSONL."""
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _run_subconscious_snapshot,
            _read_subconscious_tail,
        )

        agent = _make_agent(tmp_path,
                            subconscious_confidence_threshold=0.6)
        agent._state = AgentState.ACTIVE
        snap_path = tmp_path / "snapshot_001.json"

        # Mock consultation to return low-confidence result.
        from lingtai_kernel.llm.interface import TextBlock
        low_confidence_result = {
            "source": "snapshot:001",
            "blocks": [TextBlock(text='{"insight": "weak match", "confidence": 0.3, "source_memory": "test"}')],
        }

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
            return_value=self._make_mock_iface(),
        ), patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_voice",
            return_value=low_confidence_result,
        ):
            result = _run_subconscious_snapshot(
                agent, snap_path, "diary", "fire_1", {},
            )

        # Should return None (discarded).
        assert result is None

        # JSONL should be empty.
        tail = _read_subconscious_tail(agent, n=5)
        assert tail == ""

    def test_high_confidence_insight_kept(self, tmp_path):
        """Insights at or above threshold are appended to JSONL."""
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _run_subconscious_snapshot,
            _read_subconscious_tail,
        )

        agent = _make_agent(tmp_path,
                            subconscious_confidence_threshold=0.6)
        agent._state = AgentState.ACTIVE
        snap_path = tmp_path / "snapshot_001.json"

        # Mock consultation to return high-confidence result.
        from lingtai_kernel.llm.interface import TextBlock
        high_confidence_result = {
            "source": "snapshot:001",
            "blocks": [TextBlock(text='{"insight": "strong match", "confidence": 0.9, "source_memory": "test"}')],
        }

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
            return_value=self._make_mock_iface(),
        ), patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_voice",
            return_value=high_confidence_result,
        ):
            result = _run_subconscious_snapshot(
                agent, snap_path, "diary", "fire_1", {},
            )

        # Should return the record.
        assert result is not None
        assert result["insight"] == "strong match"
        assert result["confidence"] == 0.9

        # JSONL should contain the record.
        tail = _read_subconscious_tail(agent, n=5)
        assert "strong match" in tail

    def test_exact_threshold_included(self, tmp_path):
        """Insight at exactly the threshold is included (>= threshold)."""
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _run_subconscious_snapshot,
            _read_subconscious_tail,
        )

        agent = _make_agent(tmp_path,
                            subconscious_confidence_threshold=0.6)
        agent._state = AgentState.ACTIVE
        snap_path = tmp_path / "snapshot_001.json"

        # Mock consultation to return exactly-threshold result.
        from lingtai_kernel.llm.interface import TextBlock
        exact_threshold_result = {
            "source": "snapshot:001",
            "blocks": [TextBlock(text='{"insight": "exact match", "confidence": 0.6, "source_memory": "test"}')],
        }

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
            return_value=self._make_mock_iface(),
        ), patch(
            "lingtai_kernel.intrinsics.soul.consultation._run_consultation_voice",
            return_value=exact_threshold_result,
        ):
            result = _run_subconscious_snapshot(
                agent, snap_path, "diary", "fire_1", {},
            )

        # Should be included (>= threshold).
        assert result is not None
        assert result["insight"] == "exact match"
