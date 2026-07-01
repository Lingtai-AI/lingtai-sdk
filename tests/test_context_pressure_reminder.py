"""Tests for the unified molt/context-pressure reminder abstraction.

``ContextPressureReminder`` (``lingtai_kernel/reminders/context_pressure.py``)
owns the whole molt/context-pressure reminder that used to be split between
``SessionManager`` (raw streak counters) and ``meta_block`` (warning decision +
prose). These tests exercise the abstraction directly, plus the compatibility
delegation from both callers, and assert that behavior/prose is unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lingtai_kernel.config import (
    CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
    CONTEXT_PRESSURE_WARN_AFTER_ROUNDS,
)
from lingtai_kernel.reminders.context_pressure import (
    ContextPressureReminder,
    CURRENT_MOLT_EVENT,
    CURRENT_MOLT_TARGET_PATH,
    RECONSTRUCTION_MOLT_EVENT,
    RECONSTRUCTION_MOLT_TARGET_PATH,
    TRANSITION_DUPLICATE,
    TRANSITION_HIGH_ROUND,
    TRANSITION_INITIAL,
    TRANSITION_RELIEVED,
    TRANSITION_UNKNOWN_USAGE,
    TRANSITION_WARNING_ACTIVE,
    current_molt_emission_descriptor,
    reconstruction_molt_emission_descriptor,
    reminder_message_hash,
    render_current_molt_context,
    render_reconstruction_molt,
)


# ---------------------------------------------------------------------------
# Default thresholds mirror the kernel-fixed constants.
# ---------------------------------------------------------------------------


def test_defaults_match_kernel_constants():
    r = ContextPressureReminder()
    assert r.reconstruction_ratio == CONTEXT_PRESSURE_RECONSTRUCTION_RATIO == 0.75
    assert r.warn_after_rounds == CONTEXT_PRESSURE_WARN_AFTER_ROUNDS == 3
    assert r.recovery_target == CONTEXT_PRESSURE_RECOVERY_TARGET == 0.60


# ---------------------------------------------------------------------------
# Channel B — streak state machine (behavior parity with the old SessionManager
# streak; mirrors tests/test_context_pressure_streak.py at the abstraction level).
# ---------------------------------------------------------------------------


def test_first_two_high_rounds_do_not_warn():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    assert r.streak == 1 and r.active is False
    r.note_round(0.82, round_id=2)
    assert r.streak == 2 and r.active is False


def test_third_consecutive_high_round_warns():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    r.note_round(0.81, round_id=2)
    r.note_round(0.83, round_id=3)
    assert r.streak == 3 and r.active is True


def test_duplicate_round_id_is_noop():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=7)
    r.note_round(0.80, round_id=7)
    r.note_round(0.80, round_id=7)
    assert r.streak == 1 and r.active is False


def test_drop_below_ratio_resets_streak():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    r.note_round(0.81, round_id=2)
    r.note_round(0.50, round_id=3)  # relieved
    assert r.streak == 0 and r.active is False


def test_threshold_is_inclusive_at_ratio():
    r = ContextPressureReminder()
    r.note_round(0.75, round_id=1)
    assert r.streak == 1
    r.note_round(0.7499, round_id=2)
    assert r.streak == 0


def test_unknown_usage_sentinel_leaves_streak_untouched():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    r.note_round(0.82, round_id=2)
    r.note_round(-1.0, round_id=3)  # sentinel: not high, not a real relief
    assert r.streak == 2
    r.note_round(0.83, round_id=4)
    assert r.active is True


def test_unparseable_usage_leaves_streak_untouched():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    r.note_round("not-a-number", round_id=2)
    assert r.streak == 1
    assert r.last_transition_reason == TRANSITION_UNKNOWN_USAGE


# ---------------------------------------------------------------------------
# Transition-reason tracking (the "why" surfaced in the debug dict).
# ---------------------------------------------------------------------------


def test_transition_reasons_track_last_observation():
    r = ContextPressureReminder()
    assert r.last_transition_reason == TRANSITION_INITIAL

    r.note_round(0.80, round_id=1)
    assert r.last_transition_reason == TRANSITION_HIGH_ROUND

    r.note_round(0.80, round_id=1)  # duplicate
    assert r.last_transition_reason == TRANSITION_DUPLICATE

    r.note_round(0.81, round_id=2)
    r.note_round(0.82, round_id=3)  # advances to warn count
    assert r.active is True
    assert r.last_transition_reason == TRANSITION_WARNING_ACTIVE

    r.note_round(-1.0, round_id=4)  # sentinel
    assert r.last_transition_reason == TRANSITION_UNKNOWN_USAGE

    r.note_round(0.20, round_id=5)  # relieved
    assert r.last_transition_reason == TRANSITION_RELIEVED
    assert r.streak == 0


# ---------------------------------------------------------------------------
# snapshot / to_debug_dict
# ---------------------------------------------------------------------------


def test_to_debug_dict_reports_state_thresholds_and_why():
    r = ContextPressureReminder()
    r.note_round(0.80, round_id=1)
    r.note_round(0.81, round_id=2)
    r.note_round(0.82, round_id=3)
    d = r.to_debug_dict()
    assert d == {
        "reconstruction_ratio": 0.75,
        "warn_after_rounds": 3,
        "recovery_target": 0.60,
        "streak": 3,
        "active": True,
        "last_round_id": 3,
        "last_usage": 0.82,
        "last_transition_reason": TRANSITION_WARNING_ACTIVE,
    }
    assert r.snapshot() == d  # alias


def test_injected_thresholds_flow_through_decisions_and_debug():
    r = ContextPressureReminder(
        reconstruction_ratio=0.90, warn_after_rounds=2, recovery_target=0.50
    )
    r.note_round(0.92, round_id=1)
    assert r.active is False
    r.note_round(0.95, round_id=2)
    assert r.active is True
    d = r.to_debug_dict()
    assert d["reconstruction_ratio"] == 0.90
    assert d["warn_after_rounds"] == 2
    assert d["recovery_target"] == 0.50


# ---------------------------------------------------------------------------
# Channel B — current-state reminder rendering.
# ---------------------------------------------------------------------------


def test_current_molt_context_none_until_active():
    r = ContextPressureReminder()
    r.note_round(0.90, round_id=1)
    r.note_round(0.90, round_id=2)
    assert r.current_molt_context(0.90) is None  # streak 2, not active


def test_current_molt_context_prose_from_third_round():
    r = ContextPressureReminder()
    for rid in (1, 2, 3):
        r.note_round(0.90, round_id=rid)
    molt = r.current_molt_context(0.90)
    assert isinstance(molt, str)
    assert "Context has stayed high" in molt
    assert "3 consecutive fresh model calls" in molt
    assert "90%" in molt
    assert "recovery target is 60%" in molt
    assert "batch tool results" in molt
    assert "Repeated summarize calls while context stays above 75%" in molt
    assert "substantially hurt token efficiency" in molt
    assert "batched summarize/reconstruction pass" in molt
    assert "stop repeating summarize" in molt
    assert "molt deliberately" in molt
    assert "psyche-manual" in molt


def test_render_current_molt_context_is_pure_and_natural_language():
    molt = render_current_molt_context(streak=3, usage=0.90)
    assert "stage" not in molt
    assert '"threshold"' not in molt
    assert "recovery_target" not in molt


# ---------------------------------------------------------------------------
# Channel A — reconstruction annotation.
# ---------------------------------------------------------------------------


def test_annotate_reconstruction_none_below_recovery_target():
    r = ContextPressureReminder()
    assert r.annotate_reconstruction(0.40) is None


def test_annotate_reconstruction_inclusive_at_recovery_target():
    r = ContextPressureReminder()
    assert r.annotate_reconstruction(0.60) is not None


def test_annotate_reconstruction_above_recovery_but_below_ratio():
    r = ContextPressureReminder()
    molt = r.annotate_reconstruction(0.70)
    assert isinstance(molt, str)
    assert "runtime already rebuilt the provider context" in molt
    assert "70%" in molt
    assert "60%" in molt
    assert "one batch" in molt
    assert "molt deliberately" in molt
    assert "psyche-manual" in molt


def test_annotate_reconstruction_still_above_ratio_says_stop_looping():
    r = ContextPressureReminder()
    molt = r.annotate_reconstruction(0.80)
    assert "80%" in molt
    assert "above the 75% high-context threshold" in molt
    assert "substantially hurt token efficiency" in molt
    assert "stop repeating summarize" in molt
    assert "molt deliberately" in molt


def test_annotate_reconstruction_honors_event_recovery_target_override():
    r = ContextPressureReminder()
    # After-usage 0.55 is below the default 0.60 target -> no reminder normally,
    # but an event that carried a 0.50 recovery target must still warn.
    assert r.annotate_reconstruction(0.55) is None
    assert r.annotate_reconstruction(0.55, recovery_target=0.50) is not None


def test_render_reconstruction_molt_unparseable_returns_none():
    assert render_reconstruction_molt(after_usage="nope") is None


# ---------------------------------------------------------------------------
# Delegation — SessionManager compat shims read through to the reminder.
# ---------------------------------------------------------------------------


def _make_session_manager():
    from unittest.mock import MagicMock

    from lingtai_kernel.config import AgentConfig
    from lingtai_kernel.session import SessionManager

    svc = MagicMock()
    svc.model = "test-model"
    return SessionManager(
        llm_service=svc,
        config=AgentConfig(),
        agent_name="test",
        streaming=False,
        build_system_prompt_fn=lambda: "test prompt",
        build_tool_schemas_fn=lambda: [],
        logger_fn=None,
    )


def test_session_manager_delegates_streak_to_reminder():
    sm = _make_session_manager()
    assert isinstance(sm.context_pressure_reminder, ContextPressureReminder)

    sm.note_context_pressure_round(0.80, round_id=1)
    sm.note_context_pressure_round(0.81, round_id=2)
    assert sm.context_pressure_streak == 2
    assert sm.context_pressure_warning_active is False
    # The compat surface and the reminder must agree.
    assert sm.context_pressure_streak == sm.context_pressure_reminder.streak

    sm.note_context_pressure_round(0.82, round_id=3)
    assert sm.context_pressure_warning_active is True
    assert sm.context_pressure_reminder.active is True


# ---------------------------------------------------------------------------
# Delegation — meta_block uses the reminder when present, and its
# compatibility fallback still renders identical prose for bare session
# stand-ins that only expose context_pressure_* attributes.
# ---------------------------------------------------------------------------


def _agent_with_reminder(reminder):
    return SimpleNamespace(
        _intrinsics={"psyche": object()},
        _session=SimpleNamespace(context_pressure_reminder=reminder),
    )


def _agent_with_compat_attrs(*, active, streak):
    # No context_pressure_reminder attribute -> meta_block falls back.
    return SimpleNamespace(
        _intrinsics={"psyche": object()},
        _session=SimpleNamespace(
            context_pressure_warning_active=active,
            context_pressure_streak=streak,
        ),
    )


def test_meta_block_build_molt_context_uses_reminder():
    from lingtai_kernel.meta_block import build_molt_context

    r = ContextPressureReminder()
    for rid in (1, 2, 3):
        r.note_round(0.90, round_id=rid)
    molt = build_molt_context(_agent_with_reminder(r), 0.90)
    assert molt is not None
    assert "3 consecutive fresh model calls" in molt


def test_meta_block_build_molt_context_compat_fallback_matches_reminder():
    from lingtai_kernel.meta_block import build_molt_context

    via_reminder = ContextPressureReminder()
    for rid in (1, 2, 3):
        via_reminder.note_round(0.90, round_id=rid)
    reminder_prose = build_molt_context(_agent_with_reminder(via_reminder), 0.90)

    fallback_prose = build_molt_context(
        _agent_with_compat_attrs(active=True, streak=3), 0.90
    )
    assert fallback_prose == reminder_prose

    # Fallback stays silent when not active, exactly like the reminder path.
    assert build_molt_context(_agent_with_compat_attrs(active=False, streak=2), 0.90) is None


# ---------------------------------------------------------------------------
# Emission descriptors — pure builders used by the _meta assembly layer to emit
# structured runtime events ONLY when a reminder is actually attached to the
# permanent tool_meta.  The reminder abstraction stays pure: it computes
# descriptors, it does not log.
# ---------------------------------------------------------------------------


def test_reminder_message_hash_is_short_and_stable():
    h1 = reminder_message_hash("hello reminder")
    h2 = reminder_message_hash("hello reminder")
    assert h1 == h2
    assert isinstance(h1, str)
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)
    assert reminder_message_hash("different") != h1
    # Non-string / empty input degrades to a stable sentinel, never raises.
    assert isinstance(reminder_message_hash(None), str)
    assert isinstance(reminder_message_hash(""), str)


def test_current_molt_emission_descriptor_fields():
    r = ContextPressureReminder()
    for rid in (1, 2, 3):
        r.note_round(0.90, round_id=rid)
    message = r.current_molt_context(0.90)
    assert message is not None
    desc = current_molt_emission_descriptor(r, usage=0.90, message=message)
    assert desc["event_name"] == CURRENT_MOLT_EVENT
    payload = desc["payload"]
    assert payload["target_path"] == CURRENT_MOLT_TARGET_PATH == "_meta.tool_meta.context.molt"
    assert payload["message_hash"] == reminder_message_hash(message)
    assert payload["threshold_high"] == 0.75
    assert payload["recovery_target"] == 0.60
    assert payload["usage"] == pytest.approx(0.90)
    assert payload["streak"] == 3
    assert payload["last_round_id"] == 3
    assert payload["transition_reason"] == TRANSITION_WARNING_ACTIVE
    # JSON-safe and carries no full reminder prose.
    import json as _json

    encoded = _json.dumps(payload)
    assert message not in encoded
    assert "Context has stayed high" not in encoded


def test_reconstruction_molt_emission_descriptor_still_high_branch():
    event = {
        "type": "delayed_summarize_reconstruction",
        "trigger_threshold": 0.75,
        "recovery_target": 0.60,
        "before": {"usage": 0.85},
        "after": {"usage": 0.80, "source": "provider_input_tokens"},
    }
    message = "the rebuilt context is still at 80%..."
    desc = reconstruction_molt_emission_descriptor(event, message=message)
    assert desc["event_name"] == RECONSTRUCTION_MOLT_EVENT
    payload = desc["payload"]
    assert payload["target_path"] == RECONSTRUCTION_MOLT_TARGET_PATH
    assert payload["target_path"] == "_meta.tool_meta.reconstruction.molt"
    assert payload["message_hash"] == reminder_message_hash(message)
    assert payload["trigger_threshold"] == 0.75
    assert payload["recovery_target"] == 0.60
    assert payload["before_usage"] == pytest.approx(0.85)
    assert payload["after_usage"] == pytest.approx(0.80)
    assert payload["after_source"] == "provider_input_tokens"
    # >= trigger_threshold -> still_high branch
    assert payload["branch"] == "still_high"
    import json as _json

    _json.dumps(payload)  # JSON-safe


def test_reconstruction_molt_emission_descriptor_above_recovery_branch():
    event = {
        "trigger_threshold": 0.75,
        "recovery_target": 0.60,
        "before": {"usage": 0.85},
        "after": {"usage": 0.70, "source": "local_estimate"},
    }
    payload = reconstruction_molt_emission_descriptor(event, message="text")["payload"]
    # recovery_target <= after < trigger_threshold -> above_recovery branch
    assert payload["branch"] == "above_recovery"
    assert payload["after_usage"] == pytest.approx(0.70)
    assert payload["after_source"] == "local_estimate"


def test_reconstruction_molt_emission_descriptor_missing_before_is_none():
    event = {
        "trigger_threshold": 0.75,
        "recovery_target": 0.60,
        "after": {"usage": 0.80},
    }
    payload = reconstruction_molt_emission_descriptor(event, message="t")["payload"]
    assert payload["before_usage"] is None
    assert payload["after_source"] is None
