"""End-to-end: preset library on disk → active_preset in init.json →
materialization at boot → _activate_preset rewrites init.json → re-read
yields new manifest."""
import json
from pathlib import Path

import pytest


def _build_lib(plib: Path):
    """Build a two-preset library (alpha and beta) for swap tests."""
    plib.mkdir(parents=True, exist_ok=True)
    (plib / "alpha.json").write_text(json.dumps({
        "name": "alpha",
        "description": {"summary": "alpha — text-only"},
        "manifest": {
            "llm": {"provider": "p1", "model": "m1",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {"file": {}, "web_search": {"provider": "duckduckgo"}},
        },
    }))
    (plib / "beta.json").write_text(json.dumps({
        "name": "beta",
        "description": {"summary": "beta — vision-capable",
                        "gains": ["vision"], "loses": ["text-only optimization"]},
        "manifest": {
            "llm": {"provider": "p2", "model": "m2",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {"file": {}, "vision": {"provider": "p2",
                                                    "api_key_env": "P2KEY"}},
        },
    }))


def _build_workdir(wd: Path, plib: Path, active: str, *,
                   allowed: list[str] | None = None,
                   default: str | None = None):
    """Build a workdir with init.json pointing at the named preset.

    Includes a stub .env file because validate_init requires env_file when
    api_key_env is set without api_key (which is true for our test presets).

    `allowed` defaults to a single-entry list containing `active` (and
    `default`, if it differs).
    """
    wd.mkdir(parents=True, exist_ok=True)
    env = wd / ".env"
    env.write_text("P1KEY=sk-test\nP2KEY=sk-test\n")

    if default is None:
        default = active
    if allowed is None:
        allowed = [default] if default == active else [default, active]

    init = {
        "manifest": {
            "agent_name": "test",
            "language": "en",
            "preset": {
                "active": active,
                "default": default,
                "allowed": allowed,
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "PLACEHOLDER"},
            "capabilities": {},
            "soul": {"delay": 120}, "stamina": 3600,
            "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
            "admin": {"karma": True}, "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "lingtai": "",
        "soul": "",
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))


def _make_probe(wd: Path):
    """Build a minimal Agent probe that exposes _read_init and _activate_preset
    without triggering full agent construction."""
    from lingtai.agent import Agent

    class _Probe(Agent):
        def __init__(self, working_dir):
            self._working_dir = Path(working_dir)
            self._log_events = []
        def _log(self, event, **kw):
            self._log_events.append((event, kw))
    return _Probe(wd)


def test_e2e_boot_with_alpha_then_swap_to_beta(tmp_path, monkeypatch):
    """Boot agent with active_preset=alpha → materializes alpha.
       Call _activate_preset('beta') → init.json now reflects beta.
       Re-read → materializes beta. Identity preserved."""
    plib = tmp_path / "presets"
    _build_lib(plib)
    wd = tmp_path / "agent"
    alpha_path = str(plib / "alpha.json")
    beta_path = str(plib / "beta.json")
    _build_workdir(wd, plib, alpha_path)
    monkeypatch.setenv("P1KEY", "sk-test")
    monkeypatch.setenv("P2KEY", "sk-test")

    agent = _make_probe(wd)

    # Initial boot: alpha materialized
    data1 = agent._read_init()
    assert data1 is not None, "initial _read_init failed"
    assert data1["manifest"]["llm"]["provider"] == "p1"
    assert data1["manifest"]["llm"]["model"] == "m1"
    assert "vision" not in data1["manifest"]["capabilities"]
    assert data1["manifest"]["agent_name"] == "test"

    # Swap to beta
    agent._activate_preset(beta_path)

    # Re-read: beta materialized, identity preserved
    data2 = agent._read_init()
    assert data2 is not None, "post-swap _read_init failed"
    assert data2["manifest"]["llm"]["provider"] == "p2"
    assert data2["manifest"]["llm"]["model"] == "m2"
    assert "vision" in data2["manifest"]["capabilities"]
    assert data2["manifest"]["agent_name"] == "test"  # identity preserved
    assert data2["manifest"]["admin"]["karma"] is True  # admin preserved
    assert data2["manifest"]["soul"]["delay"] == 120  # soul preserved
    assert "stamina" not in data2["manifest"]  # legacy runtime knob removed
    assert data2["manifest"]["preset"]["active"] == beta_path
    assert data2["manifest"]["preset"]["default"] == alpha_path  # original default preserved


def test_e2e_swap_to_unknown_preserves_init(tmp_path, monkeypatch):
    """Swap to nonexistent preset raises KeyError; init.json on disk untouched."""
    plib = tmp_path / "presets"
    _build_lib(plib)
    wd = tmp_path / "agent"
    _build_workdir(wd, plib, str(plib / "alpha.json"))
    monkeypatch.setenv("P1KEY", "sk-test")

    original = (wd / "init.json").read_text()
    agent = _make_probe(wd)

    with pytest.raises(KeyError):
        agent._activate_preset(str(plib / "ghost.json"))

    assert (wd / "init.json").read_text() == original


def test_e2e_inherit_resolves_after_swap(tmp_path, monkeypatch):
    """A preset that uses provider:'inherit' resolves to its own llm at boot."""
    plib = tmp_path / "presets"
    plib.mkdir(parents=True, exist_ok=True)
    (plib / "smart.json").write_text(json.dumps({
        "name": "smart",
        "description": {"summary": "vision via inherit"},
        "manifest": {
            "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                    "api_key": None, "api_key_env": "GEMINI_API_KEY"},
            "capabilities": {
                "file": {},
                "web_search": {"provider": "inherit"},
                "vision": {"provider": "inherit"},
            },
        },
    }))
    wd = tmp_path / "agent"

    # Build workdir with stub .env including GEMINI_API_KEY
    wd.mkdir(parents=True, exist_ok=True)
    env = wd / ".env"
    env.write_text("GEMINI_API_KEY=sk-test\n")
    init = {
        "manifest": {
            "agent_name": "test",
            "language": "en",
            "preset": {
                "active": str(plib / "smart.json"),
                "default": str(plib / "smart.json"),
                "allowed": [str(plib / "smart.json")],
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "PLACEHOLDER"},
            "capabilities": {},
            "soul": {"delay": 120}, "stamina": 3600,
            "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
            "admin": {"karma": True}, "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "lingtai": "",
        "soul": "",
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))

    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")

    agent = _make_probe(wd)
    data = agent._read_init()
    assert data is not None

    caps = data["manifest"]["capabilities"]
    assert caps["web_search"]["provider"] == "gemini"
    assert caps["web_search"]["api_key_env"] == "GEMINI_API_KEY"
    assert caps["vision"]["provider"] == "gemini"
    assert caps["vision"]["api_key_env"] == "GEMINI_API_KEY"
    # model is NOT inherited
    assert "model" not in caps["web_search"]
    assert "model" not in caps["vision"]
