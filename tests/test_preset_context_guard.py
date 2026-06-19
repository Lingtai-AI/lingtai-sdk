"""Tests for the context_limit guard on preset swap.

When swapping to a preset with a smaller context_limit than the agent's
current context usage, the swap must be refused with a clear error
message asking the agent to molt first."""
import json
from pathlib import Path

import pytest


def _build_lib(plib: Path, *, big_limit=200000, small_limit=8000):
    plib.mkdir(parents=True, exist_ok=True)
    (plib / "big.json").write_text(json.dumps({
        "name": "big",
        "description": {"summary": "big context preset"},
        "manifest": {
            "llm": {"provider": "p1", "model": "m1",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {"file": {}},
            "context_limit": big_limit,
        },
    }))
    (plib / "small.json").write_text(json.dumps({
        "name": "small",
        "description": {"summary": "small context preset"},
        "manifest": {
            "llm": {"provider": "p2", "model": "m2",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {"file": {}},
            "context_limit": small_limit,
        },
    }))
    (plib / "no_limit.json").write_text(json.dumps({
        "name": "no_limit",
        "description": {"summary": "no context_limit specified"},
        "manifest": {
            "llm": {"provider": "p3", "model": "m3",
                    "api_key": None, "api_key_env": "P3KEY"},
            "capabilities": {"file": {}},
            # NB: no context_limit field
        },
    }))


def _build_workdir(wd: Path, plib: Path, active: str = "big"):
    wd.mkdir(parents=True, exist_ok=True)
    env = wd / ".env"
    env.write_text("P1KEY=sk-test\nP2KEY=sk-test\nP3KEY=sk-test\n")
    init = {
        "manifest": {
            "agent_name": "test", "language": "en",
            "preset": {
                "path": str(plib),
                "active": active,
                "default": active,
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "PLACEHOLDER"},
            "capabilities": {},
            "context_limit": 200000,
            "soul": {"delay": 120}, "stamina": 3600,
            "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
            "admin": {"karma": True}, "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))


def _make_test_agent(tmp_path):
    """BaseAgent with stubs for _activate_preset, _perform_refresh, and get_token_usage."""
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    plib = tmp_path / "presets"
    _build_lib(plib)
    wd = tmp_path / "test"
    _build_workdir(wd, plib, active=str(plib / "big.json"))
    agent = BaseAgent(service=svc, agent_name="test", working_dir=wd)
    return agent, plib


def test_swap_refused_when_current_context_exceeds_target_limit(tmp_path, monkeypatch):
    """system(refresh, preset='small') refuses when ctx_total > small's context_limit."""
    agent, plib = _make_test_agent(tmp_path)

    # Stub current usage well above small's 8000 limit.
    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 50000,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0, "ctx_history_tokens": 50000,
    })

    # _activate_preset and _perform_refresh should NOT be called when the guard fires.
    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    log_events = []
    real_log = agent._log
    monkeypatch.setattr(agent, "_log",
                        lambda evt, **kw: (log_events.append((evt, kw)), real_log(evt, **kw))[1])

    small_path = str(plib / "small.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": small_path})

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "molt" in msg
    assert "50000" in result["message"] or "current" in msg
    assert "8000" in result["message"] or "small" in msg.lower()
    events = [e for e, _ in log_events]
    assert "preset_swap_refused_oversize" in events
    assert activate_calls == []  # swap NOT applied
    assert perform_calls == []   # refresh NOT triggered


def test_swap_allowed_when_current_context_fits(tmp_path, monkeypatch):
    """Refresh with target preset succeeds when ctx_total <= target's context_limit."""
    agent, plib = _make_test_agent(tmp_path)

    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 4000,  # well under 8000
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0, "ctx_history_tokens": 4000,
    })

    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    small_path = str(plib / "small.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": small_path})

    assert result["status"] == "ok"
    assert activate_calls == [small_path]
    assert perform_calls == [True]


def test_swap_allowed_when_target_has_no_context_limit(tmp_path, monkeypatch):
    """A preset without context_limit skips the guard (graceful backward-compat)."""
    agent, plib = _make_test_agent(tmp_path)

    # Even with high usage, no_limit preset has no field → guard skipped.
    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 999999,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0, "ctx_history_tokens": 999999,
    })

    activate_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh", lambda: None)

    no_limit_path = str(plib / "no_limit.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": no_limit_path})

    assert result["status"] == "ok"
    assert activate_calls == [no_limit_path]


def test_swap_skips_guard_when_target_limit_is_zero(tmp_path, monkeypatch):
    """context_limit: 0 means unlimited / unset, not 'always refuse'."""
    agent, plib = _make_test_agent(tmp_path)
    # Add a preset with limit=0 to the library
    (plib / "zero.json").write_text(json.dumps({
        "name": "zero",
        "description": {"summary": "zero limit"},
        "manifest": {
            "llm": {"provider": "p4", "model": "m4",
                    "api_key": None, "api_key_env": "P4KEY"},
            "capabilities": {"file": {}},
            "context_limit": 0,
        },
    }))

    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 999999,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
        "ctx_history_tokens": 999999,
    })

    activate_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh", lambda: None)

    zero_path = str(plib / "zero.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": zero_path})
    assert result["status"] == "ok"
    assert activate_calls == [zero_path]


def test_swap_skips_guard_when_target_limit_is_negative(tmp_path, monkeypatch):
    """context_limit: -1 means unlimited, not 'always refuse'."""
    agent, plib = _make_test_agent(tmp_path)
    (plib / "negone.json").write_text(json.dumps({
        "name": "negone",
        "description": {"summary": "negative limit"},
        "manifest": {
            "llm": {"provider": "p5", "model": "m5",
                    "api_key": None, "api_key_env": "P5KEY"},
            "capabilities": {"file": {}},
            "context_limit": -1,
        },
    }))

    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 999999,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
        "ctx_history_tokens": 999999,
    })

    activate_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh", lambda: None)

    negone_path = str(plib / "negone.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": negone_path})
    assert result["status"] == "ok"
    assert activate_calls == [negone_path]


def test_guard_reads_context_limit_from_llm_block(tmp_path, monkeypatch):
    """Canonical layout: target preset's context_limit lives in manifest.llm.

    The guard must find it there (not just at manifest root). This is the
    layout new presets will use; the existing test_swap_refused/allowed
    cases prove the old root-level layout still works (back-compat).
    """
    agent, plib = _make_test_agent(tmp_path)

    # Add a preset with context_limit nested inside llm.
    (plib / "tight.json").write_text(json.dumps({
        "name": "tight",
        "description": {"summary": "context_limit lives in llm block"},
        "manifest": {
            "llm": {"provider": "px", "model": "mx",
                    "api_key": None, "api_key_env": "PXKEY",
                    "context_limit": 8000},
            "capabilities": {"file": {}},
        },
    }))

    # Current usage exceeds the 8000 cap → swap should be refused.
    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 50000,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
        "ctx_history_tokens": 50000,
    })

    activate_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh", lambda: None)

    tight_path = str(plib / "tight.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": tight_path})

    assert result["status"] == "error"
    assert "molt" in result["message"].lower()
    assert activate_calls == []  # swap aborted


def test_revert_refused_when_current_context_exceeds_default_limit(tmp_path, monkeypatch):
    """revert_preset=True is subject to the context-limit guard.

    If the default preset has a smaller context_limit than the agent's
    current usage, revert fails with molt-first error — same as a manual
    swap to a too-narrow preset."""
    agent, plib = _make_test_agent(tmp_path)

    # Reconfigure: current is 'big' (200k limit), default is 'small' (8k limit).
    # The library was already built with big.json (200k) and small.json (8k)
    # by _make_test_agent. We need to rewrite init.json so active=big, default=small.
    import json
    init_path = agent._working_dir / "init.json"
    data = json.loads(init_path.read_text())
    data["manifest"]["preset"] = {
        "active": str(plib / "big.json"),
        "default": str(plib / "small.json"),
        "path": str(plib),
    }
    init_path.write_text(json.dumps(data))

    # Agent's current usage is well above small's 8k limit
    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 50000,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
        "ctx_history_tokens": 50000,
    })

    activate_default_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_default_preset",
                        lambda: activate_default_calls.append(True))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    log_events = []
    real_log = agent._log
    monkeypatch.setattr(agent, "_log",
                        lambda evt, **kw: (log_events.append((evt, kw)), real_log(evt, **kw))[1])

    result = agent._intrinsics["system"]({"action": "refresh", "revert_preset": True})

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "molt" in msg
    events = [e for e, _ in log_events]
    assert "preset_swap_refused_oversize" in events
    assert activate_default_calls == []  # default not activated
    assert perform_calls == []  # refresh not triggered


def test_swap_to_unknown_preset_still_returns_not_found(tmp_path, monkeypatch):
    """Guard does not interfere with the existing 'unknown preset' error."""
    agent, plib = _make_test_agent(tmp_path)

    monkeypatch.setattr(agent, "get_token_usage", lambda: {
        "ctx_total_tokens": 4000,
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "api_calls": 0,
        "ctx_system_tokens": 0, "ctx_tools_tokens": 0, "ctx_history_tokens": 4000,
    })

    ghost_path = str(plib / "ghost.json")
    result = agent._intrinsics["system"]({"action": "refresh", "preset": ghost_path})

    assert result["status"] == "error"
    assert "ghost" in result["message"]
