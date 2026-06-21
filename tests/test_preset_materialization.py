"""Tests for preset materialization at boot — _read_init substitutes the
active preset's llm + capabilities into manifest before validation."""
import json
from pathlib import Path

import pytest


def _make_workdir(tmp_path: Path, active_preset: str | None = None,
                  presets_path: str | None = None,
                  manifest_extra: dict | None = None) -> Path:
    """Create a working dir with init.json. Optionally points at a preset."""
    wd = tmp_path / "agent"
    wd.mkdir()
    manifest = {
        "agent_name": "alice",
        "language": "en",
        "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
        "capabilities": {"file": {}},
        "soul": {"delay": 120},
        "stamina": 3600,
        "molt_pressure": 0.8,
        "molt_prompt": "",
        "max_turns": 50,
        "admin": {"karma": True},
        "streaming": False,
    }
    if active_preset is not None:
        preset_block: dict = {
            "active": active_preset,
            "default": active_preset,
            "allowed": [active_preset],
        }
        if presets_path is not None:
            # `presets_path` is unused under the new schema (allowed paths
            # carry their own location). Kept as an argument for backward
            # compatibility with existing call sites.
            pass
        manifest["preset"] = preset_block
    if manifest_extra:
        manifest.update(manifest_extra)
    # Create a dummy env_file so validate_init doesn't reject api_key_env
    env_file = wd / ".env"
    env_file.write_text("")
    init = {
        "manifest": manifest,
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env_file),
    }
    (wd / "init.json").write_text(json.dumps(init))
    return wd


def _make_preset_lib(tmp_path: Path, presets: dict[str, dict]) -> Path:
    """Create a presets dir with the given name → preset-content mapping."""
    pdir = tmp_path / "presets"
    pdir.mkdir()
    for name, content in presets.items():
        (pdir / f"{name}.json").write_text(json.dumps(content))
    return pdir


def _make_probe_agent(wd: Path):
    """Build a minimal Agent subclass that exposes _read_init for direct testing.

    We don't fully construct an Agent because that triggers full setup. Instead
    we shim the bare attributes _read_init needs: _working_dir and _log.
    """
    from lingtai.agent import Agent

    class _Probe(Agent):
        def __init__(self, working_dir):
            self._working_dir = Path(working_dir)
            self._log_events = []
        def _log(self, event, **kw):
            self._log_events.append((event, kw))
    return _Probe(wd)


def test_materialize_substitutes_llm_and_capabilities(tmp_path, monkeypatch):
    """When init.json has active_preset, _read_init substitutes the preset's
    llm + capabilities into the manifest."""
    plib = _make_preset_lib(tmp_path, {
        "minimax": {
            "name": "minimax",
            "description": {"summary": "MiniMax M2.7"},
            "manifest": {
                "llm": {"provider": "minimax", "model": "MiniMax-M2.7-highspeed",
                        "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
                "capabilities": {"file": {}, "vision": {"provider": "minimax",
                                                        "api_key_env": "MINIMAX_API_KEY"}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset=str(plib / "minimax.json"),
                       presets_path=str(plib))
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "minimax"
    assert data["manifest"]["llm"]["model"] == "MiniMax-M2.7-highspeed"
    assert "vision" in data["manifest"]["capabilities"]


def test_materialize_preserves_thinking_from_active_preset(tmp_path):
    plib = _make_preset_lib(tmp_path, {
        "codex": {
            "name": "codex",
            "description": {"summary": "Codex"},
            "manifest": {
                "llm": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "thinking": "xhigh",
                },
                "capabilities": {"file": {}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset=str(plib / "codex.json"))

    a = _make_probe_agent(wd)
    data = a._read_init()

    assert data is not None
    assert data["manifest"]["llm"]["thinking"] == "xhigh"


def test_materialize_no_preset_field_unchanged(tmp_path):
    """init.json without active_preset behaves exactly as before."""
    wd = _make_workdir(tmp_path)
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data["manifest"]["llm"]["provider"] == "deepseek"  # original


def test_refresh_preset_thinking_reaches_session_path(tmp_path):
    from unittest.mock import MagicMock
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig

    plib = _make_preset_lib(tmp_path, {
        "codex": {
            "name": "codex",
            "description": {"summary": "Codex"},
            "manifest": {
                "llm": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "thinking": "xhigh",
                },
                "capabilities": {"file": {}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset=str(plib / "codex.json"))

    svc = MagicMock()
    svc.provider = "codex"
    svc.model = "gpt-5.5"
    svc._base_url = None
    svc._provider_defaults = {"codex": {"max_rpm": 60}}
    svc.create_session.return_value = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(svc, working_dir=wd, config=AgentConfig())

    # ``_setup_from_init`` rebuilds a real ``LLMService`` from the resolved
    # manifest (provider/model changed → service is reconstructed) and
    # reassigns it onto the session, discarding the injected mock above. So
    # the assertion must inspect the *real* session that ``ensure_session``
    # creates, not the mock's ``create_session.call_args`` (which is never
    # touched once the service is rebuilt). The real OpenAI Responses session
    # folds the thinking level into ``reasoning.effort`` via
    # ``_responses_reasoning_kwargs`` — asserting on that proves the preset's
    # ``manifest.llm.thinking`` reaches the actual API reasoning payload.
    agent._setup_from_init()
    chat = agent._session.ensure_session()

    assert agent._config.thinking == "xhigh"
    assert chat._extra_kwargs.get("reasoning") == {"effort": "xhigh"}


def test_materialize_unknown_preset_returns_none_and_logs(tmp_path):
    """Active preset that doesn't exist → _read_init returns None and logs."""
    plib = _make_preset_lib(tmp_path, {})
    wd = _make_workdir(tmp_path, active_preset=str(plib / "ghost.json"),
                       presets_path=str(plib))
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is None
    events = [e for e, _ in a._log_events]
    assert "refresh_init_error" in events


def test_materialize_default_presets_path(tmp_path, monkeypatch):
    """active_preset without presets_path uses ~/.lingtai-tui/presets/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    plib = fake_home / ".lingtai-tui" / "presets"
    plib.mkdir(parents=True)
    (plib / "deepseek.json").write_text(json.dumps({
        "name": "deepseek",
        "description": {"summary": "DeepSeek default"},
        "manifest": {
            "llm": {"provider": "deepseek-NEW", "model": "v4-NEW",
                    "api_key": None, "api_key_env": "X"},
            "capabilities": {"file": {}},
        },
    }))
    wd = _make_workdir(tmp_path, active_preset="~/.lingtai-tui/presets/deepseek.json")
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "deepseek-NEW"


def test_materialize_relative_presets_path_resolves_against_workdir(tmp_path, monkeypatch):
    """A relative presets_path is resolved against the agent's working dir, not CWD."""
    wd = tmp_path / "agent"
    wd.mkdir()
    plib = wd / "my_presets"
    plib.mkdir()
    (plib / "local.json").write_text(json.dumps({
        "name": "local",
        "description": {"summary": "local preset"},
        "manifest": {
            "llm": {"provider": "p1", "model": "m1",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {"file": {}},
        },
    }))
    # Set up env_file (validate_init requires it when api_key_env is set)
    env = wd / ".env"
    env.write_text("P1KEY=sk-test\n")

    init = {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "preset": {
                "active": "./my_presets/local.json",  # RELATIVE to agent workdir
                "default": "./my_presets/local.json",
                "allowed": ["./my_presets/local.json"],
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {},
            "soul": {"delay": 120},
            "stamina": 3600,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))

    # Change CWD to a different location entirely so a CWD-relative resolution would fail
    monkeypatch.chdir(tmp_path)

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "p1"


def test_materialize_omitted_path_falls_back_to_default(tmp_path, monkeypatch):
    """preset block uses a `~/...` style active path resolved via $HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    plib = fake_home / ".lingtai-tui" / "presets"
    plib.mkdir(parents=True)
    (plib / "fallback.json").write_text(json.dumps({
        "name": "fallback",
        "description": {"summary": "fallback preset"},
        "manifest": {
            "llm": {"provider": "p2", "model": "m2",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {"file": {}},
        },
    }))

    wd = tmp_path / "agent"
    wd.mkdir()
    env = wd / ".env"
    env.write_text("P2KEY=sk-test\n")

    init = {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "preset": {
                "active": "~/.lingtai-tui/presets/fallback.json",
                "default": "~/.lingtai-tui/presets/fallback.json",
                "allowed": ["~/.lingtai-tui/presets/fallback.json"],
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {},
            "soul": {"delay": 120},
            "stamina": 3600,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "p2"


def test_materialize_picks_up_context_limit_from_legacy_layout(tmp_path, monkeypatch):
    """A legacy preset (context_limit at manifest root) still works end-to-end.

    The kernel migration system (m001) runs from inside load_preset and
    relocates the field to the canonical location before the materializer
    reads the preset. The materializer then writes it into init.json's
    manifest root (init.json's schema is unchanged).
    """
    from lingtai_kernel.migrate.migrate import reset_process_cache
    reset_process_cache()
    plib = _make_preset_lib(tmp_path, {
        "narrow": {
            "name": "narrow",
            "description": {"summary": "narrow context"},
            "manifest": {
                "llm": {"provider": "p", "model": "m",
                        "api_key": None, "api_key_env": "PKEY"},
                "capabilities": {"file": {}},
                "context_limit": 16384,
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset=str(plib / "narrow.json"),
                       presets_path=str(plib))
    monkeypatch.setenv("PKEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["context_limit"] == 16384
    # The migration rewrote the on-disk preset to the canonical layout.
    on_disk = json.loads((plib / "narrow.json").read_text())
    assert "context_limit" not in on_disk["manifest"]
    assert on_disk["manifest"]["llm"]["context_limit"] == 16384


def test_materialize_picks_up_context_limit_from_llm_block(tmp_path, monkeypatch):
    """Canonical layout: context_limit lives inside manifest.llm in the preset.

    The materializer must lift it out of llm and write it to manifest root
    in init.json (the runtime contract — init.json schema is unchanged).
    The materialized llm block must NOT carry context_limit forward, since
    init.json's llm block doesn't have that field.
    """
    plib = _make_preset_lib(tmp_path, {
        "narrow": {
            "name": "narrow",
            "description": {"summary": "narrow context"},
            "manifest": {
                "llm": {"provider": "p", "model": "m",
                        "api_key": None, "api_key_env": "PKEY",
                        "context_limit": 16384},
                "capabilities": {"file": {}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset=str(plib / "narrow.json"),
                       presets_path=str(plib))
    monkeypatch.setenv("PKEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["context_limit"] == 16384
    # llm block in init.json schema does not carry context_limit
    assert "context_limit" not in data["manifest"]["llm"]


def test_materialize_preserves_init_capability_overrides(tmp_path, monkeypatch):
    """Per-agent capability kwargs in init.json survive preset materialization.

    A preset enabling daemon must not clobber init.json's
    manifest.capabilities.daemon.max_emanations — the preset decides *which*
    capabilities run, but per-agent overrides win key-by-key. Regression for
    daemon(list) reporting the default ceiling instead of the configured one.
    """
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset with daemon"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                # Preset enables daemon with its own (default-ish) ceiling.
                "capabilities": {"file": {}, "daemon": {"max_emanations": 10}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    # init.json declares daemon with a per-agent override of 30.
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "smart.json"),
        manifest_extra={"capabilities": {"daemon": {"max_emanations": 30}}},
    )
    a = _make_probe_agent(wd)
    data = a._read_init()
    caps = data["manifest"]["capabilities"]
    # Preset still chose the capability set (file is present from the preset)...
    assert "file" in caps
    assert "daemon" in caps
    # ...but the init.json override wins for the key it specified.
    assert caps["daemon"]["max_emanations"] == 30


def test_materialize_preset_only_capability_kwargs_kept(tmp_path, monkeypatch):
    """Preset kwargs survive for capabilities init.json does NOT override.

    The merge must not erase preset-supplied kwargs for capabilities the agent
    never mentions — only same-key overrides win.
    """
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {"daemon": {"max_emanations": 50, "max_turns": 99}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    # init.json overrides only max_emanations; max_turns must come from preset.
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "smart.json"),
        manifest_extra={"capabilities": {"daemon": {"max_emanations": 30}}},
    )
    a = _make_probe_agent(wd)
    data = a._read_init()
    daemon_kw = data["manifest"]["capabilities"]["daemon"]
    assert daemon_kw["max_emanations"] == 30   # init override wins
    assert daemon_kw["max_turns"] == 99        # preset kwarg preserved


def test_materialize_preserves_core_default_override_when_preset_omits_it(tmp_path, monkeypatch):
    """A CORE_DEFAULTS capability's init.json kwargs survive even when the
    active preset OMITS that capability entirely.

    Jason's real case: the codex preset enables file/web tools but never
    mentions daemon. daemon is always-on (CORE_DEFAULTS), so init.json's
    daemon.max_emanations=30 must be carried into the materialized manifest;
    otherwise apply_core_defaults later re-adds daemon={} and the override is
    lost. Non-core capabilities the preset omits are still dropped (the swap).
    """
    plib = _make_preset_lib(tmp_path, {
        "codex": {
            "name": "codex",
            "description": {"summary": "codex preset — no daemon entry"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                # Note: NO daemon key. Has a non-core optional cap instead.
                "capabilities": {"file": {}, "web_search": {"provider": "inherit"}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "codex.json"),
        manifest_extra={"capabilities": {
            "daemon": {"max_emanations": 30},   # core default — must survive
            "vision": {"provider": "custom"},   # non-core, preset omits — must drop
        }},
    )
    a = _make_probe_agent(wd)
    data = a._read_init()
    caps = data["manifest"]["capabilities"]
    # Core-default daemon override carried forward despite preset omitting it.
    assert "daemon" in caps
    assert caps["daemon"]["max_emanations"] == 30
    # Preset still owns the explicit opt-in set.
    assert "web_search" in caps
    # Non-core optional capability that the preset omits is NOT carried over.
    assert "vision" not in caps


def test_materialize_core_default_no_init_override_left_to_apply_core_defaults(tmp_path, monkeypatch):
    """When init.json has no kwargs for a core-default cap and the preset omits
    it too, materialize leaves it absent — apply_core_defaults adds the floor
    later. Materialize must not invent empty core entries."""
    plib = _make_preset_lib(tmp_path, {
        "codex": {
            "name": "codex",
            "description": {"summary": "codex preset"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {"file": {}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    # init.json declares only file (no daemon kwargs).
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "codex.json"),
        manifest_extra={"capabilities": {"file": {}}},
    )
    a = _make_probe_agent(wd)
    data = a._read_init()
    caps = data["manifest"]["capabilities"]
    # No daemon entry invented by materialize — apply_core_defaults owns the floor.
    assert "daemon" not in caps


def test_refresh_preset_omitting_daemon_keeps_override_in_manager(tmp_path, monkeypatch):
    """End-to-end refresh for Jason's case: active preset omits daemon, init.json
    sets daemon.max_emanations=30 → live DaemonManager ceiling is 30."""
    from unittest.mock import MagicMock
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig

    plib = _make_preset_lib(tmp_path, {
        "codex": {
            "name": "codex",
            "description": {"summary": "codex preset, no daemon"},
            "manifest": {
                "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                        "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
                "capabilities": {"file": {}},   # no daemon entry
            },
        },
    })
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "codex.json"),
        manifest_extra={"capabilities": {"daemon": {"max_emanations": 30}}},
    )

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(svc, working_dir=wd, capabilities=["daemon"],
                  config=AgentConfig())
    agent._setup_from_init()

    mgr = agent.get_capability("daemon")
    assert mgr is not None
    assert mgr._max_emanations == 30
    assert mgr._handle_list()["max_emanations"] == 30


def test_refresh_preset_keeps_daemon_override_in_manager(tmp_path, monkeypatch):
    """End-to-end refresh: a real Agent with an active preset that enables
    daemon at 10 plus an init.json daemon override of 30 ends up with a live
    DaemonManager whose ceiling is 30 (and daemon(list) reports 30).

    This exercises the full path the bug report described: materialize active
    preset → _setup_from_init → _setup_capability("daemon", ...) → DaemonManager.
    """
    from unittest.mock import MagicMock
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig

    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset enabling daemon"},
            "manifest": {
                "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                        "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
                "capabilities": {"daemon": {"max_emanations": 10}},
            },
        },
    })
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "smart.json"),
        manifest_extra={"capabilities": {"daemon": {"max_emanations": 30}}},
    )

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(svc, working_dir=wd, capabilities=["daemon"],
                  config=AgentConfig())

    # Drive the documented "fix config → refresh" reconstruct path.
    agent._setup_from_init()

    mgr = agent.get_capability("daemon")
    assert mgr is not None
    assert mgr._max_emanations == 30
    assert mgr._handle_list()["max_emanations"] == 30


def test_materialize_inherit_expansion_runs(tmp_path, monkeypatch):
    """Capabilities with provider:inherit get the main LLM's provider after materialization."""
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {
                    "file": {},
                    "web_search": {"provider": "inherit"},
                },
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    wd = _make_workdir(tmp_path, active_preset=str(plib / "smart.json"),
                       presets_path=str(plib))
    a = _make_probe_agent(wd)
    data = a._read_init()
    caps = data["manifest"]["capabilities"]
    assert caps["web_search"]["provider"] == "gemini"
    assert caps["web_search"]["api_key_env"] == "GEMINI_API_KEY"


# ---------------------------------------------------------------------------
# Missing-active fallback to default — cross-machine portability
# ---------------------------------------------------------------------------

def _build_init_with_active_and_default(
    active_path: str, default_path: str, env_file: Path
) -> dict:
    """Build a raw init.json dict with separate active/default preset paths."""
    return {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                    "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
            "capabilities": {"file": {}},
            "soul": {"delay": 120},
            "stamina": 3600,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
            "preset": {
                "active": active_path,
                "default": default_path,
                "allowed": [active_path, default_path],
            },
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env_file),
    }


def test_materialize_missing_active_falls_back_to_default(tmp_path, monkeypatch):
    """Active preset file gone (e.g. project hard-copied across machines): fall
    back to default, mutate manifest.preset.active in place, log a warning."""
    plib = _make_preset_lib(tmp_path, {
        "minimax_cn": {
            "name": "minimax_cn",
            "description": {"summary": "MiniMax fallback"},
            "manifest": {
                "llm": {"provider": "minimax", "model": "MiniMax-M2.7-highspeed",
                        "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
                "capabilities": {"file": {}},
            },
        },
    })
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    active_missing = str(plib / "mimo-pro.json")  # never created
    default_present = str(plib / "minimax_cn.json")
    env_file = tmp_path / ".env"
    env_file.write_text("")
    data = _build_init_with_active_and_default(active_missing, default_present, env_file)

    from lingtai.presets import materialize_active_preset
    materialize_active_preset(data, tmp_path)

    assert data["manifest"]["preset"]["active"] == default_present
    assert data["manifest"]["llm"]["provider"] == "minimax"
    assert data["manifest"]["llm"]["model"] == "MiniMax-M2.7-highspeed"


def test_materialize_missing_active_and_missing_default_raises(tmp_path):
    """When BOTH active and default are missing there is nothing to fall back
    to — propagate the original KeyError so the operator sees the real issue."""
    plib = tmp_path / "presets"
    plib.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")

    data = _build_init_with_active_and_default(
        active_path=str(plib / "ghost-active.json"),
        default_path=str(plib / "ghost-default.json"),
        env_file=env_file,
    )

    from lingtai.presets import materialize_active_preset
    with pytest.raises(KeyError):
        materialize_active_preset(data, tmp_path)


def test_materialize_missing_active_with_same_default_raises(tmp_path):
    """Active and default point at the same missing file — no fallback target,
    propagate KeyError instead of looping."""
    plib = tmp_path / "presets"
    plib.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")

    same_missing = str(plib / "gone.json")
    data = _build_init_with_active_and_default(
        active_path=same_missing, default_path=same_missing, env_file=env_file,
    )

    from lingtai.presets import materialize_active_preset
    with pytest.raises(KeyError):
        materialize_active_preset(data, tmp_path)


def test_materialize_malformed_active_does_not_fall_back(tmp_path):
    """A malformed (but present) active preset is an authoring error — surface
    ValueError unchanged. Silently swapping in default would mask the bug and
    let the agent boot on a different model than its config claims."""
    plib = tmp_path / "presets"
    plib.mkdir()
    bad_active = plib / "bad.json"
    bad_active.write_text(json.dumps({
        "name": "bad",
        "description": {"summary": "missing manifest"},
        # no manifest key at all → ValueError in load_preset
    }))
    good_default = plib / "good.json"
    good_default.write_text(json.dumps({
        "name": "good",
        "description": {"summary": "fine"},
        "manifest": {
            "llm": {"provider": "p", "model": "m",
                    "api_key": None, "api_key_env": "K"},
            "capabilities": {"file": {}},
        },
    }))
    env_file = tmp_path / ".env"
    env_file.write_text("")
    data = _build_init_with_active_and_default(
        active_path=str(bad_active), default_path=str(good_default), env_file=env_file,
    )

    from lingtai.presets import materialize_active_preset
    with pytest.raises(ValueError):
        materialize_active_preset(data, tmp_path)
    # active was NOT silently rewritten
    assert data["manifest"]["preset"]["active"] == str(bad_active)


def test_read_init_recovers_when_active_preset_missing(tmp_path, monkeypatch):
    """End-to-end via Agent._read_init: a project hard-copied to a machine
    that lacks the active preset still boots cleanly using default."""
    plib = _make_preset_lib(tmp_path, {
        "minimax_cn": {
            "name": "minimax_cn",
            "description": {"summary": "fallback target"},
            "manifest": {
                "llm": {"provider": "minimax", "model": "MiniMax-M2.7-highspeed",
                        "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
                "capabilities": {"file": {}},
            },
        },
    })
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    wd = tmp_path / "agent"
    wd.mkdir()
    env_file = wd / ".env"
    env_file.write_text("")
    init = _build_init_with_active_and_default(
        active_path=str(plib / "mimo-pro.json"),       # missing on this machine
        default_path=str(plib / "minimax_cn.json"),    # present
        env_file=env_file,
    )
    (wd / "init.json").write_text(json.dumps(init))

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "minimax"
