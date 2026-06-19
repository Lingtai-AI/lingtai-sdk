"""Tests for time_veil helpers."""
from types import SimpleNamespace

from lingtai_kernel.time_veil import now_iso, veil, scrub_time_fields, TIME_KEYS


def _mk_agent(time_awareness: bool):
    # Existing tests assume UTC output (Z suffix), so timezone_awareness=False here.
    return SimpleNamespace(_config=SimpleNamespace(
        time_awareness=time_awareness,
        timezone_awareness=False,
    ))


def test_now_iso_time_aware_returns_iso_string():
    agent = _mk_agent(True)
    result = now_iso(agent)
    assert isinstance(result, str)
    assert len(result) == 20  # YYYY-MM-DDTHH:MM:SSZ
    assert result.endswith("Z")


def test_now_iso_time_blind_returns_empty():
    agent = _mk_agent(False)
    assert now_iso(agent) == ""


def test_veil_time_aware_passthrough():
    agent = _mk_agent(True)
    assert veil(agent, "2026-04-15T12:00:00Z") == "2026-04-15T12:00:00Z"
    assert veil(agent, "") == ""


def test_veil_time_blind_blanks():
    agent = _mk_agent(False)
    assert veil(agent, "2026-04-15T12:00:00Z") == ""
    assert veil(agent, "anything") == ""


def test_scrub_time_fields_time_aware_unchanged():
    agent = _mk_agent(True)
    payload = {"received_at": "2026-04-15T12:00:00Z", "subject": "hi"}
    out = scrub_time_fields(agent, payload)
    assert out == payload


def test_scrub_time_fields_time_blind_blanks_listed_keys():
    agent = _mk_agent(False)
    payload = {
        "received_at": "2026-04-15T12:00:00Z",
        "sent_at": "2026-04-15T12:01:00Z",
        "subject": "hi",
        "from": "alice",
    }
    out = scrub_time_fields(agent, payload)
    assert out["received_at"] == ""
    assert out["sent_at"] == ""
    assert out["subject"] == "hi"  # non-time keys untouched
    assert out["from"] == "alice"


def test_scrub_time_fields_time_blind_drops_drop_keys():
    agent = _mk_agent(False)
    payload = {"current_time": "2026-04-15T12:00:00Z", "_elapsed_ms": 42, "status": "ok"}
    out = scrub_time_fields(agent, payload, drop_keys=("current_time", "_elapsed_ms"))
    assert "current_time" not in out
    assert "_elapsed_ms" not in out
    assert out["status"] == "ok"


def test_scrub_time_fields_returns_copy_not_mutating_input():
    agent = _mk_agent(False)
    payload = {"received_at": "2026-04-15T12:00:00Z"}
    out = scrub_time_fields(agent, payload)
    assert payload["received_at"] == "2026-04-15T12:00:00Z"  # original unchanged
    assert out["received_at"] == ""


def test_time_keys_contains_expected():
    assert "received_at" in TIME_KEYS
    assert "sent_at" in TIME_KEYS
    assert "deliver_at" in TIME_KEYS
    assert "current_time" in TIME_KEYS
    assert "started_at" in TIME_KEYS
    assert "_elapsed_ms" not in TIME_KEYS  # _elapsed_ms handled via drop_keys, not default TIME_KEYS


def _mk_agent_full(time_awareness: bool, timezone_awareness: bool):
    return SimpleNamespace(_config=SimpleNamespace(
        time_awareness=time_awareness,
        timezone_awareness=timezone_awareness,
    ))


def test_now_iso_local_tz_returns_offset_suffix():
    """When timezone_awareness=True, now_iso ends with ±HH:MM (not Z)."""
    import re
    agent = _mk_agent_full(time_awareness=True, timezone_awareness=True)
    result = now_iso(agent)
    assert isinstance(result, str)
    assert not result.endswith("Z"), f"expected local-tz offset suffix, got {result!r}"
    # Must end with ±HH:MM (e.g. -07:00, +08:00, +00:00)
    assert re.search(r"[+-]\d{2}:\d{2}$", result), f"no ±HH:MM suffix in {result!r}"


def test_now_iso_utc_when_timezone_awareness_off():
    """When timezone_awareness=False, now_iso emits UTC with Z suffix."""
    agent = _mk_agent_full(time_awareness=True, timezone_awareness=False)
    result = now_iso(agent)
    assert result.endswith("Z"), f"expected UTC Z suffix, got {result!r}"
    assert len(result) == 20  # YYYY-MM-DDTHH:MM:SSZ


def test_now_iso_blind_overrides_timezone_awareness():
    """time_awareness=False wins regardless of timezone_awareness."""
    a1 = _mk_agent_full(time_awareness=False, timezone_awareness=True)
    a2 = _mk_agent_full(time_awareness=False, timezone_awareness=False)
    assert now_iso(a1) == ""
    assert now_iso(a2) == ""
