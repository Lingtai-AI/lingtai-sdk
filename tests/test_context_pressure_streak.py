"""Tests for the sustained context-pressure streak (channel B).

The molt warning in ``_meta.agent_meta.context.molt`` is no longer an immediate
``usage >= 0.60`` nudge.  Instead it tracks *fresh provider rounds* whose context
usage is at/above the reconstruction threshold (0.75).  The warning only begins
on the THIRD consecutive high round, so a single spike (or even two) does not
nag the agent before the delayed-summarize reconstruction has had a chance to
relieve pressure.  Duplicate observations of the same provider round must not
advance the streak, and a drop below threshold resets it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.session import (
    SessionManager,
    CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
    CONTEXT_PRESSURE_WARN_AFTER_ROUNDS,
)


def make_session_manager(**kw):
    """Self-contained SessionManager factory (mirrors test_session.py)."""
    svc = MagicMock()
    svc.model = "test-model"
    mock_session = MagicMock()
    mock_session.context_window.return_value = 100000
    mock_session.interface.estimate_context_tokens.return_value = 5000
    mock_session.interface.current_system_prompt = "test prompt"
    svc.create_session.return_value = mock_session
    svc.check_and_compact.return_value = None
    config = kw.get("config", AgentConfig())
    return (
        SessionManager(
            llm_service=svc,
            config=config,
            agent_name="test",
            streaming=kw.get("streaming", False),
            build_system_prompt_fn=lambda: "test prompt",
            build_tool_schemas_fn=lambda: [],
            logger_fn=kw.get("logger_fn", None),
        ),
        svc,
        mock_session,
    )


def test_constants_match_contract():
    assert CONTEXT_PRESSURE_RECONSTRUCTION_RATIO == 0.75
    assert CONTEXT_PRESSURE_WARN_AFTER_ROUNDS == 3


def test_first_two_high_rounds_do_not_warn():
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.80, round_id=1)
    assert sm.context_pressure_streak == 1
    assert sm.context_pressure_warning_active is False

    sm.note_context_pressure_round(0.82, round_id=2)
    assert sm.context_pressure_streak == 2
    assert sm.context_pressure_warning_active is False


def test_third_consecutive_high_round_warns():
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.80, round_id=1)
    sm.note_context_pressure_round(0.81, round_id=2)
    sm.note_context_pressure_round(0.83, round_id=3)
    assert sm.context_pressure_streak == 3
    assert sm.context_pressure_warning_active is True


def test_streak_continues_warning_while_pressure_high():
    sm, _, _ = make_session_manager()
    for rid in (1, 2, 3, 4, 5):
        sm.note_context_pressure_round(0.90, round_id=rid)
    assert sm.context_pressure_streak == 5
    assert sm.context_pressure_warning_active is True


def test_duplicate_same_round_id_does_not_advance():
    """Multiple build_meta / tool results in one batch share the same provider
    round; observing the same round_id repeatedly must not advance the streak."""
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.80, round_id=7)
    sm.note_context_pressure_round(0.80, round_id=7)
    sm.note_context_pressure_round(0.80, round_id=7)
    assert sm.context_pressure_streak == 1
    assert sm.context_pressure_warning_active is False


def test_drop_below_threshold_resets_streak():
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.80, round_id=1)
    sm.note_context_pressure_round(0.81, round_id=2)
    sm.note_context_pressure_round(0.50, round_id=3)  # relieved
    assert sm.context_pressure_streak == 0
    assert sm.context_pressure_warning_active is False

    # Must climb back from scratch — two more highs still no warning.
    sm.note_context_pressure_round(0.80, round_id=4)
    sm.note_context_pressure_round(0.81, round_id=5)
    assert sm.context_pressure_warning_active is False
    sm.note_context_pressure_round(0.82, round_id=6)
    assert sm.context_pressure_warning_active is True


def test_threshold_is_inclusive_at_0_75():
    """Threshold interpretation: ``usage >= 0.75`` counts as a high round,
    matching the delayed-reconstruction release test (``usage >= ratio``)."""
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.75, round_id=1)
    assert sm.context_pressure_streak == 1
    sm.note_context_pressure_round(0.7499, round_id=2)
    assert sm.context_pressure_streak == 0


def _usage(input_tokens):
    from unittest.mock import MagicMock

    return MagicMock(
        input_tokens=input_tokens,
        output_tokens=10,
        thinking_tokens=0,
        cached_tokens=0,
        extra={},
    )


def _response(input_tokens, call_id):
    from unittest.mock import MagicMock

    return MagicMock(
        text="ok",
        tool_calls=[],
        thoughts=[],
        usage=_usage(input_tokens),
        api_call_id=call_id,
    )


def test_track_usage_advances_streak_on_fresh_high_rounds():
    """Each real provider round (one _track_usage call) is a fresh round keyed
    by the incrementing _api_calls counter; three high rounds arm the warning.

    The streak uses the PROVIDER-reported input tokens, not the local estimate:
    estimate_context_tokens is pinned LOW (30000 -> 0.30) yet the provider's
    80000 -> 0.80 still drives the streak."""
    sm, _, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 100000
    mock_session.interface.estimate_context_tokens.return_value = 30000  # ignored

    for i in range(3):
        sm._track_usage(_response(80000, f"call-{i}"))

    assert sm.context_pressure_streak == 3
    assert sm.context_pressure_warning_active is True


def test_track_usage_uses_provider_input_not_local_estimate():
    """If the local estimate is high but the provider reports low, the streak
    must follow the provider (the reconstruction threshold is provider-based)."""
    sm, _, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 100000
    mock_session.interface.estimate_context_tokens.return_value = 90000  # high, ignored

    for i in range(4):
        sm._track_usage(_response(20000, f"low-{i}"))  # provider 0.20 -> not high

    assert sm.context_pressure_streak == 0
    assert sm.context_pressure_warning_active is False


def test_track_usage_resets_streak_when_pressure_relieved():
    sm, _, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 100000

    sm._track_usage(_response(80000, "c1"))  # provider 0.80
    sm._track_usage(_response(80000, "c2"))
    assert sm.context_pressure_streak == 2

    sm._track_usage(_response(30000, "c3"))  # provider 0.30 -> relieved
    assert sm.context_pressure_streak == 0
    assert sm.context_pressure_warning_active is False


def test_unknown_usage_sentinel_does_not_advance_or_reset():
    """A -1.0 sentinel (decomposition not ready) is neither high nor a real
    relief; it must leave the streak untouched rather than spuriously reset it."""
    sm, _, _ = make_session_manager()
    sm.note_context_pressure_round(0.80, round_id=1)
    sm.note_context_pressure_round(0.82, round_id=2)
    sm.note_context_pressure_round(-1.0, round_id=3)
    assert sm.context_pressure_streak == 2  # untouched
    sm.note_context_pressure_round(0.83, round_id=4)
    assert sm.context_pressure_warning_active is True
