"""Tests for issue #78: sanitized active-preset and LLM identity in .agent.json
and the identity system-prompt section.

Goal: `.agent.json` should surface a sanitized `llm` block (provider/model/
base_url) plus a `preset` block (active/default/allowed), and the system
prompt identity section should render enough of it for the agent to see
which model/backend it is currently running. **No API keys, no env-var
names, no secrets** must ever appear in either surface.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lingtai.agent import Agent
from lingtai_kernel.base_agent import BaseAgent, _build_identity_section
from lingtai_kernel.base_agent.identity import _build_manifest, _safe_llm_from_service


def _mock_service(
    provider: str = "gemini",
    model: str = "gemini-test",
    base_url: str | None = None,
):
    """Build a mock LLMService with the live attributes the manifest reads."""
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = provider
    svc.model = model
    svc._base_url = base_url
    return svc


def _write_init(
    workdir: Path,
    *,
    active: str | None = None,
    default: str | None = None,
    allowed: list[str] | None = None,
    llm_extras: dict | None = None,
) -> Path:
    """Write a minimal init.json that exercises the preset/llm surface."""
    workdir.mkdir(parents=True, exist_ok=True)
    llm: dict = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
    }
    if llm_extras:
        llm.update(llm_extras)
    manifest: dict = {
        "agent_name": "test",
        "language": "en",
        "llm": llm,
        "capabilities": {},
    }
    if active or default or allowed:
        preset: dict = {}
        if active:
            preset["active"] = active
        if default:
            preset["default"] = default
        if allowed:
            preset["allowed"] = allowed
        manifest["preset"] = preset
    init = {"manifest": manifest}
    init_path = workdir / "init.json"
    init_path.write_text(json.dumps(init, indent=2))
    return init_path


def test_kernel_manifest_includes_llm_from_service(tmp_path):
    agent = BaseAgent(
        service=_mock_service("anthropic", "claude-opus-4-7", "https://api.anthropic.com"),
        agent_name="alice",
        working_dir=tmp_path / "alice",
    )
    data = _build_manifest(agent)
    assert data["llm"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "base_url": "https://api.anthropic.com",
    }
    agent.stop(timeout=1.0)


def test_kernel_manifest_omits_base_url_when_none(tmp_path):
    agent = BaseAgent(
        service=_mock_service("openai", "gpt-4.6", base_url=None),
        agent_name="bob",
        working_dir=tmp_path / "bob",
    )
    data = _build_manifest(agent)
    assert data["llm"] == {"provider": "openai", "model": "gpt-4.6"}
    agent.stop(timeout=1.0)


def test_kernel_manifest_drops_mock_attrs(tmp_path):
    svc = MagicMock()
    agent = BaseAgent(service=svc, agent_name="ghost", working_dir=tmp_path / "ghost")
    data = _build_manifest(agent)
    if "llm" in data:
        for v in data["llm"].values():
            assert isinstance(v, (str, int)), f"non-scalar in llm: {v!r}"
    agent.stop(timeout=1.0)


def test_safe_llm_from_service_unit():
    agent = MagicMock()
    agent.service = _mock_service("p", "m", "https://example.test")
    assert _safe_llm_from_service(agent) == {
        "provider": "p",
        "model": "m",
        "base_url": "https://example.test",
    }


def test_safe_llm_from_service_uses_provider_default_base_url():
    agent = MagicMock()
    svc = _mock_service("custom", "model-x", None)
    svc._provider_defaults = {
        "custom": {
            "base_url": "https://relay.example.test/v1",
            "api_compat": "openai",
        }
    }
    svc._context_window = 123456
    agent.service = svc

    out = _safe_llm_from_service(agent)
    assert out["base_url"] == "https://relay.example.test/v1"
    assert out["api_compat"] == "openai"
    assert out["context_limit"] == 123456


def test_safe_llm_from_service_with_no_service():
    agent = MagicMock()
    agent.service = None
    assert _safe_llm_from_service(agent) == {}


def test_identity_section_renders_llm_line():
    text = _build_identity_section({
        "agent_name": "alice",
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
        },
    })
    assert "deepseek-v4-pro" in text
    assert "deepseek" in text
    assert "https://api.deepseek.com" in text


def test_identity_section_renders_preset_line():
    active = "~/.lingtai-tui/presets/saved/codex-gpt5.5.json"
    text = _build_identity_section({
        "agent_name": "alice",
        "preset": {"active": active, "default": active},
    })
    assert active in text
    assert "active preset" in text


def test_identity_section_shows_default_when_active_differs():
    text = _build_identity_section({
        "agent_name": "alice",
        "preset": {"active": "~/p/beta.json", "default": "~/p/alpha.json"},
    })
    assert "~/p/beta.json" in text
    assert "~/p/alpha.json" in text
    assert "default" in text


def test_identity_section_silent_when_llm_missing():
    text = _build_identity_section({"agent_name": "alice"})
    assert "alice" in text
    assert "You are running on" not in text


def test_identity_section_drops_non_scalar_metadata():
    text = _build_identity_section({
        "agent_name": "alice",
        "llm": {"provider": {"bad": "dict"}, "model": ["bad"], "base_url": "https://ok.example"},
        "preset": {"active": {"not": "path"}, "default": ["also", "bad"]},
    })
    assert "bad" not in text
    assert "not" not in text
    assert "also" not in text
    assert "active preset" not in text
    assert "https://ok.example" not in text


def test_identity_section_silent_when_preset_active_absent():
    text = _build_identity_section({"agent_name": "alice", "preset": {"allowed": []}})
    assert "active preset" not in text


def test_manifest_never_contains_api_key(tmp_path):
    workdir = tmp_path / "leaky"
    _write_init(
        workdir,
        active="~/p/secret.json",
        default="~/p/secret.json",
        allowed=["~/p/secret.json"],
        llm_extras={
            "api_key": "sk-LEAKED-KEY-12345",
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_secret": "shhh",
        },
    )
    agent = Agent(
        service=_mock_service("deepseek", "deepseek-v4-pro", "https://api.deepseek.com"),
        agent_name="leaky",
        working_dir=workdir,
    )
    manifest = agent._build_manifest()
    forbidden = {"api_key", "api_key_env", "api_secret", "token", "password"}
    assert forbidden.isdisjoint(manifest["llm"].keys())
    agent_json = json.loads((workdir / ".agent.json").read_text())
    blob = json.dumps(agent_json)
    assert "sk-LEAKED-KEY-12345" not in blob
    assert "DEEPSEEK_API_KEY" not in blob
    assert "shhh" not in blob
    agent.stop(timeout=1.0)


def test_wrapper_manifest_includes_preset_from_init(tmp_path):
    workdir = tmp_path / "withpreset"
    active_path = "~/.lingtai-tui/presets/saved/codex-gpt5.5.json"
    _write_init(
        workdir,
        active=active_path,
        default=active_path,
        allowed=[active_path, "~/.lingtai-tui/presets/saved/deepseek_pro.json"],
    )
    agent = Agent(
        service=_mock_service("codex", "gpt-5.5", "https://chatgpt.com/backend-api/codex"),
        agent_name="withpreset",
        working_dir=workdir,
    )
    manifest = agent._build_manifest()
    assert manifest["preset"] == {
        "active": active_path,
        "default": active_path,
        "allowed": [active_path, "~/.lingtai-tui/presets/saved/deepseek_pro.json"],
    }
    assert manifest["llm"] == {
        "provider": "codex",
        "model": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }
    agent.stop(timeout=1.0)


def test_wrapper_manifest_no_preset_when_init_has_none(tmp_path):
    workdir = tmp_path / "nopreset"
    _write_init(workdir)
    agent = Agent(service=_mock_service(), agent_name="nopreset", working_dir=workdir)
    manifest = agent._build_manifest()
    assert "preset" not in manifest
    agent.stop(timeout=1.0)


def test_wrapper_manifest_no_preset_when_no_init(tmp_path):
    workdir = tmp_path / "noinit"
    workdir.mkdir(parents=True)
    agent = Agent(service=_mock_service(), agent_name="noinit", working_dir=workdir)
    manifest = agent._build_manifest()
    assert "preset" not in manifest
    agent.stop(timeout=1.0)


def test_wrapper_manifest_resilient_to_corrupt_init(tmp_path):
    workdir = tmp_path / "corrupt"
    workdir.mkdir(parents=True)
    (workdir / "init.json").write_text("{not valid json")
    agent = Agent(service=_mock_service(), agent_name="corrupt", working_dir=workdir)
    manifest = agent._build_manifest()
    assert "preset" not in manifest
    agent.stop(timeout=1.0)


def test_wrapper_preset_block_filters_unknown_keys(tmp_path):
    workdir = tmp_path / "filtered"
    workdir.mkdir(parents=True)
    init = {
        "manifest": {
            "agent_name": "filtered",
            "language": "en",
            "llm": {"provider": "p", "model": "m"},
            "capabilities": {},
            "preset": {
                "active": "a.json",
                "default": "a.json",
                "allowed": ["a.json"],
                "internal_credential": "shhh",
                "deploy_token": "leak-me",
            },
        }
    }
    (workdir / "init.json").write_text(json.dumps(init))
    agent = Agent(service=_mock_service(), agent_name="filtered", working_dir=workdir)
    manifest = agent._build_manifest()
    assert manifest["preset"] == {
        "active": "a.json",
        "default": "a.json",
        "allowed": ["a.json"],
    }
    agent_json = json.loads((workdir / ".agent.json").read_text())
    assert "internal_credential" not in json.dumps(agent_json)
    assert "leak-me" not in json.dumps(agent_json)
    agent.stop(timeout=1.0)


def test_wrapper_preset_block_drops_non_string_values(tmp_path):
    workdir = tmp_path / "typed"
    workdir.mkdir(parents=True)
    init = {
        "manifest": {
            "agent_name": "typed",
            "language": "en",
            "llm": {"provider": "p", "model": "m"},
            "capabilities": {},
            "preset": {
                "active": {"not": "a string"},
                "default": "default.json",
                "allowed": ["default.json", {"bad": True}, ""],
            },
        }
    }
    (workdir / "init.json").write_text(json.dumps(init))
    agent = Agent(service=_mock_service(), agent_name="typed", working_dir=workdir)
    manifest = agent._build_manifest()
    assert manifest["preset"] == {
        "default": "default.json",
        "allowed": ["default.json"],
    }
    agent.stop(timeout=1.0)


def _seed_minimal_preset(plib: Path, name: str, *, provider: str, model: str) -> str:
    plib.mkdir(parents=True, exist_ok=True)
    path = plib / f"{name}.json"
    path.write_text(json.dumps({
        "name": name,
        "description": {"summary": f"{name} test preset"},
        "manifest": {
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": None,
                "api_key_env": f"{name.upper()}_KEY",
            },
            "capabilities": {},
        },
    }))
    return str(path)


def test_manifest_tracks_active_after_preset_swap(tmp_path, monkeypatch):
    plib = tmp_path / "presets"
    alpha = _seed_minimal_preset(plib, "alpha", provider="p1", model="m1")
    beta = _seed_minimal_preset(plib, "beta", provider="p2", model="m2")
    workdir = tmp_path / "swap"
    workdir.mkdir(parents=True)
    init = {
        "manifest": {
            "agent_name": "swap",
            "language": "en",
            "preset": {"active": alpha, "default": alpha, "allowed": [alpha, beta]},
            "llm": {"provider": "p1", "model": "m1", "api_key": None, "api_key_env": "P1_KEY"},
            "capabilities": {},
        }
    }
    (workdir / "init.json").write_text(json.dumps(init))
    monkeypatch.setenv("P1_KEY", "sk-test")
    monkeypatch.setenv("P2_KEY", "sk-test")
    agent = Agent(service=_mock_service("p1", "m1"), agent_name="swap", working_dir=workdir)
    assert agent._build_manifest()["preset"]["active"] == alpha
    agent._activate_preset(beta)
    manifest_after = agent._build_manifest()
    assert manifest_after["preset"]["active"] == beta
    assert manifest_after["preset"]["default"] == alpha
    assert beta in manifest_after["preset"]["allowed"]
    agent.stop(timeout=1.0)


def test_identity_section_after_swap_names_new_active(tmp_path, monkeypatch):
    plib = tmp_path / "presets"
    alpha = _seed_minimal_preset(plib, "alpha", provider="p1", model="m1")
    beta = _seed_minimal_preset(plib, "beta", provider="p2", model="m2")
    workdir = tmp_path / "swap2"
    workdir.mkdir(parents=True)
    init = {
        "manifest": {
            "agent_name": "swap2",
            "language": "en",
            "preset": {"active": alpha, "default": alpha, "allowed": [alpha, beta]},
            "llm": {"provider": "p1", "model": "m1"},
            "capabilities": {},
        }
    }
    (workdir / "init.json").write_text(json.dumps(init))
    monkeypatch.setenv("P1_KEY", "sk-test")
    monkeypatch.setenv("P2_KEY", "sk-test")
    agent = Agent(service=_mock_service("p1", "m1"), agent_name="swap2", working_dir=workdir)
    agent._activate_preset(beta)
    text = _build_identity_section(agent._build_manifest())
    assert beta in text
    assert alpha in text
    assert "default" in text
    agent.stop(timeout=1.0)
