"""Wiring tests for the explicit runtime-session / agent-session objects.

These cover the SAME-PR wiring (Jason 2026-07-02): the injected
``_meta.tool_meta.token_usage.session`` half must be backed by AGENT-SESSION
(since-current-molt) semantics, and a refresh/restart must PRESERVE the
since-molt counters instead of restoring LIFETIME ledger totals.

Each test is written so it would have FAILED under the pre-wiring behavior
(restore-from-lifetime-ledger), and PASSES with the agent-session rebuild + seed.

See:
- docs/references/runtime-vs-agent-session-objects.md
- src/lingtai_kernel/agent_session.py
- src/lingtai_kernel/base_agent/lifecycle.py::_start (rebuild + seed)
- src/lingtai_kernel/meta_block.py::_build_session_token_economy
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from lingtai_kernel.agent_session import (
    MOLT_BOUNDARY_EVENT,
    TOKEN_EVENT,
    AgentSession,
    RuntimeSession,
    rebuild_agent_session_from_events,
)
from lingtai_kernel.meta_block import _build_session_token_economy
from lingtai_kernel.session import SessionManager
from lingtai_kernel.config import AgentConfig
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session_manager() -> SessionManager:
    svc = MagicMock()
    svc.model = "test-model"
    return SessionManager(
        llm_service=svc,
        config=AgentConfig(),
        agent_name="test",
        streaming=False,
        build_system_prompt_fn=lambda: "prompt",
        build_tool_schemas_fn=lambda: [],
        logger_fn=None,
    )


def _write_events(dest: Path, events: list[dict]) -> Path:
    logs = dest / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "events.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return path


def _token_ev(ts: float, inp: int, out: int, cached: int) -> dict:
    return {
        "type": TOKEN_EVENT, "ts": ts,
        "input_tokens": inp, "output_tokens": out,
        "thinking_tokens": 10, "cached_tokens": cached,
    }


def _molt_ev(ts: float, molt_count: int, initiator: str = "agent") -> dict:
    return {
        "type": MOLT_BOUNDARY_EVENT, "ts": ts,
        "molt_count": molt_count, "initiator": initiator,
    }


# ---------------------------------------------------------------------------
# SessionManager: named objects + agent-session view
# ---------------------------------------------------------------------------


def test_runtime_session_object_is_fresh_and_idless():
    sm = _make_session_manager()
    rs = sm.runtime_session()
    assert isinstance(rs, RuntimeSession)
    assert not hasattr(rs, "id")
    assert not hasattr(rs, "session_id")
    # A runtime boundary mints a fresh object (no id, distinct instance).
    sm.reset_runtime_session_token_usage()  # the boundary helper
    rs2 = sm.runtime_session()
    assert rs2 is not rs


def test_agent_session_starts_uninstalled_and_installs_by_molt_count():
    sm = _make_session_manager()
    assert sm.agent_session() is None
    obj = AgentSession(molt_count=7, api_calls=3, input_tokens=900, cached_tokens=600)
    sm.install_agent_session(obj)
    got = sm.agent_session()
    assert got is obj
    assert got.molt_count == 7
    assert not hasattr(got, "id")


def test_agent_session_token_usage_reads_since_molt_counters():
    sm = _make_session_manager()
    # Seed the cumulative counters as a refresh restore would (since-molt totals).
    sm.restore_token_state(
        {
            "input_tokens": 2000,
            "output_tokens": 300,
            "thinking_tokens": 20,
            "cached_tokens": 1500,
            "api_calls": 4,
        }
    )
    usage = sm.agent_session_token_usage()
    assert usage["api_calls"] == 4
    assert usage["input_tokens"] == 2000
    assert usage["cached_tokens"] == 1500
    assert usage["cache_miss_tokens"] == 500
    assert usage["session_cache_rate"] == 0.75
    assert usage["avg_input_tokens_per_api_call"] == 500


# ---------------------------------------------------------------------------
# The #679 refresh regression, end-to-end through SessionManager + meta_block
# ---------------------------------------------------------------------------


def test_refresh_preserves_since_molt_not_lifetime(tmp_path):
    """The core regression.

    A long-lived agent has a LIFETIME of ~10k api calls across many molts, but
    the CURRENT molt generation is small. After a refresh the injected
    ``token_usage.session`` half must report the SMALL since-current-molt totals,
    never the huge lifetime totals.
    """
    # Trajectory: an old generation, a molt boundary to molt_count=1, then a
    # small current generation of 2 token events.
    events = [
        _token_ev(1.0, 9_000_000, 100, 100),  # lifetime-huge, PRE-molt (must be excluded)
        _molt_ev(2.0, 1),                      # boundary -> current gen (molt_count 1)
        _token_ev(3.0, 1000, 200, 700),        # current
        _token_ev(4.0, 1000, 200, 700),        # current
    ]
    _write_events(tmp_path, events)

    # Rebuild for the current molt_count (as lifecycle._start does).
    rebuilt = rebuild_agent_session_from_events(tmp_path, molt_count=1)
    assert rebuilt.rebuild_tier in ("sqlite", "reverse_scan", "full_scan")
    # Only the current generation is aggregated — NOT the 9M-token pre-molt event.
    assert rebuilt.api_calls == 2
    assert rebuilt.input_tokens == 2000
    assert rebuilt.cached_tokens == 1400

    # Seed the session manager from the rebuilt since-molt totals (the wiring).
    sm = _make_session_manager()
    sm.install_agent_session(rebuilt)
    sm.restore_token_state(
        {
            "input_tokens": rebuilt.input_tokens,
            "output_tokens": rebuilt.output_tokens,
            "thinking_tokens": rebuilt.thinking_tokens,
            "cached_tokens": rebuilt.cached_tokens,
            "api_calls": rebuilt.api_calls,
        }
    )

    # meta_block session half must now report the SMALL since-molt totals.
    agent = SimpleNamespace(
        _session=sm,
        _config=AgentConfig(),
        get_token_usage=sm.get_token_usage,
        agent_session_token_usage=sm.agent_session_token_usage,
    )
    session_half = _build_session_token_economy(agent)
    assert session_half["api_calls"] == 2
    assert session_half["input_tokens"] == 2000
    assert session_half["cached_tokens"] == 1400
    assert session_half["cache_miss_tokens"] == 600
    # Sanity: it is emphatically NOT the 9M lifetime input.
    assert session_half["input_tokens"] < 9_000_000


def test_live_rounds_after_refresh_accrue_on_top_of_since_molt(tmp_path):
    """After a refresh seeded from the rebuilt since-molt total, new provider
    rounds must ADD to the since-molt total (baseline + live deltas)."""
    events = [
        _molt_ev(1.0, 1),
        _token_ev(2.0, 1000, 200, 700),
    ]
    _write_events(tmp_path, events)
    rebuilt = rebuild_agent_session_from_events(tmp_path, molt_count=1)
    assert rebuilt.api_calls == 1

    sm = _make_session_manager()
    sm.install_agent_session(rebuilt)
    sm.restore_token_state(
        {
            "input_tokens": rebuilt.input_tokens,
            "output_tokens": rebuilt.output_tokens,
            "thinking_tokens": rebuilt.thinking_tokens,
            "cached_tokens": rebuilt.cached_tokens,
            "api_calls": rebuilt.api_calls,
        }
    )
    # Simulate one more live provider round after the refresh.
    sm._total_input_tokens += 500
    sm._total_cached_tokens += 300
    sm._api_calls += 1

    usage = sm.agent_session_token_usage()
    assert usage["api_calls"] == 2            # 1 rebuilt + 1 live
    assert usage["input_tokens"] == 1500      # 1000 rebuilt + 500 live
    assert usage["cached_tokens"] == 1000     # 700 rebuilt + 300 live


def test_meta_block_falls_back_to_get_token_usage_without_agent_session_accessor():
    """A stub agent with only get_token_usage (no agent_session_token_usage) must
    still produce a valid session half — the wiring is additive, not required."""
    agent = SimpleNamespace(
        _config=AgentConfig(),
        get_token_usage=lambda: {
            "api_calls": 3,
            "input_tokens": 600,
            "cached_tokens": 300,
            "ctx_total_tokens": 600,
        },
    )
    half = _build_session_token_economy(agent)
    assert half["api_calls"] == 3
    assert half["input_tokens"] == 600
    assert half["cached_tokens"] == 300
    assert half["cache_miss_tokens"] == 300


def test_runtime_and_agent_session_are_not_confused(tmp_path):
    """Runtime-session (since-refresh deltas) and agent-session (since-molt) must
    diverge after live rounds: the runtime view resets on refresh, the agent view
    survives it."""
    events = [
        _molt_ev(1.0, 1),
        _token_ev(2.0, 1000, 200, 700),
        _token_ev(3.0, 1000, 200, 700),
    ]
    _write_events(tmp_path, events)
    rebuilt = rebuild_agent_session_from_events(tmp_path, molt_count=1)

    sm = _make_session_manager()
    sm.install_agent_session(rebuilt)
    # A refresh: seed since-molt totals, which also re-anchors the runtime baseline.
    sm.restore_token_state(
        {
            "input_tokens": rebuilt.input_tokens,
            "output_tokens": rebuilt.output_tokens,
            "thinking_tokens": rebuilt.thinking_tokens,
            "cached_tokens": rebuilt.cached_tokens,
            "api_calls": rebuilt.api_calls,
        }
    )

    # Immediately after refresh: agent session shows since-molt (2 calls, 2000),
    # runtime session shows ~0 deltas (baseline == current).
    agent_usage = sm.agent_session_token_usage()
    runtime_usage = sm.get_runtime_session_token_usage()
    assert agent_usage["api_calls"] == 2
    assert agent_usage["input_tokens"] == 2000
    assert runtime_usage["api_calls"] == 0
    assert runtime_usage["input_tokens"] == 0

    # One live round: both grow by the same delta, but from different baselines.
    sm._total_input_tokens += 500
    sm._total_cached_tokens += 300
    sm._api_calls += 1
    agent_usage = sm.agent_session_token_usage()
    runtime_usage = sm.get_runtime_session_token_usage()
    assert agent_usage["api_calls"] == 3       # since molt
    assert agent_usage["input_tokens"] == 2500
    assert runtime_usage["api_calls"] == 1     # since refresh
    assert runtime_usage["input_tokens"] == 500


def test_brand_new_agent_rebuild_is_boot_none_tier(tmp_path):
    """A brand-new agent with no trajectory yields a zeroed boot session with
    tier 'none' — the signal ``lifecycle._start`` uses to fall back to the
    lifetime ledger for back-compat instead of seeding zeros over it."""
    rebuilt = rebuild_agent_session_from_events(tmp_path, molt_count=0)
    assert rebuilt.rebuild_tier == "none"
    assert rebuilt.boundary_source == "boot"
    assert rebuilt.api_calls == 0
    assert rebuilt.input_tokens == 0


def test_molt_reanchors_runtime_session_object(tmp_path):
    """A molt is a runtime boundary too: reset_session_token_usage (called by the
    molt path) must mint a fresh runtime-session object and zero the counters."""
    sm = _make_session_manager()
    rs_before = sm.runtime_session()
    sm._total_input_tokens = 5000
    sm._api_calls = 12
    sm.reset_session_token_usage(context_tokens=8000)
    # Counters zeroed for the new molt generation.
    assert sm.get_token_usage()["input_tokens"] == 0
    assert sm.get_token_usage()["api_calls"] == 0
    # Fresh runtime-session object minted at the boundary.
    assert sm.runtime_session() is not rs_before
