from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lingtai.agent import Agent, build_agent_config
from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.config import AgentConfig, MOLT_PRESSURE_THRESHOLD


def _service():
    svc = MagicMock()
    svc.provider = "openai"
    svc.model = "gpt-4o"
    svc._base_url = None
    svc._provider_defaults = {"openai": {"max_rpm": AgentConfig().max_rpm}}
    return svc


def _init_data(manifest_overrides: dict | None = None) -> dict:
    manifest = {
        "agent_name": "test-agent",
        "language": "en",
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
            "base_url": None,
        },
        "capabilities": {},
        "soul": {"delay": 60},
        "context_limit": None,
        "admin": {"karma": True},
        "streaming": False,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    return {
        "manifest": manifest,
        "principle": "",
        "covenant": "",
        "pad": "",
        "lingtai": "",
        "soul": "",
    }


def _write_init(path: Path, data: dict) -> None:
    (path / "init.json").write_text(json.dumps(data), encoding="utf-8")


def _agent(path: Path, init_data: dict) -> Agent:
    _write_init(path, init_data)
    return Agent(_service(), agent_name="test-agent", working_dir=path)


def test_bare_agent_config_and_base_agent_defaults(tmp_path):
    cfg = AgentConfig()
    assert cfg.stamina == 86400.0
    assert cfg.max_aed_attempts == 3
    assert cfg.molt_pressure == MOLT_PRESSURE_THRESHOLD

    agent = BaseAgent(
        service=_service(),
        agent_name="bare-agent",
        working_dir=tmp_path / "bare-agent",
    )

    assert agent._config.stamina == 86400.0
    assert agent._config.max_aed_attempts == 3
    assert agent._config.molt_pressure == MOLT_PRESSURE_THRESHOLD

    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["stamina"] == 86400.0
    assert "24h" in agent._prompt_manager.read_section("identity")


def test_build_agent_config_overlays_explicit_values_and_ignores_stale_molt():
    defaults = AgentConfig()
    manifest = _init_data({
        "stamina": 7200.0,
        "max_aed_attempts": 5,
        "max_turns": 999,
        "molt_notice": 0.99,
        "molt_pressure": 0.99,
        "molt_urgency": 0.99,
        "molt_prompt": "ignore me",
        "soul": {
            "delay": 7.0,
            "consultation_past_count": 2,
            "voice": "custom",
            "voice_prompt": "speak plainly",
        },
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
            "thinking": "medium",
        },
        "language": "zh",
        "activeness": "quiet",
        "context_limit": 12345,
        "snapshot_interval": 30.0,
        "time_awareness": False,
        "timezone_awareness": False,
        "aed_timeout": 12.0,
    })["manifest"]

    cfg = build_agent_config(manifest, max_rpm=0)

    assert cfg.stamina == 7200.0
    assert cfg.max_aed_attempts == 5
    assert cfg.soul_delay == 7.0
    assert cfg.consultation_past_count == 2
    assert cfg.soul_voice == "custom"
    assert cfg.soul_voice_prompt == "speak plainly"
    assert cfg.thinking == "medium"
    assert cfg.language == "zh"
    assert cfg.activeness == "quiet"
    assert cfg.context_limit == 12345
    assert cfg.snapshot_interval == 30.0
    assert cfg.time_awareness is False
    assert cfg.timezone_awareness is False
    assert cfg.aed_timeout == 12.0
    assert cfg.max_rpm == 0

    assert cfg.max_turns == defaults.max_turns
    assert cfg.molt_notice == defaults.molt_notice
    assert cfg.molt_pressure == MOLT_PRESSURE_THRESHOLD
    assert cfg.molt_urgency == defaults.molt_urgency
    assert not hasattr(cfg, "molt_prompt")


def test_boot_omitted_defaults_update_artifacts_and_ignore_stale_molt(tmp_path):
    init_data = _init_data({
        "molt_notice": 0.99,
        "molt_pressure": 0.99,
        "molt_urgency": 0.99,
        "molt_prompt": "ignore me",
    })
    agent = _agent(tmp_path, init_data)

    pre_hydration = json.loads((tmp_path / ".agent.json").read_text())
    assert pre_hydration["stamina"] == 86400.0

    agent._setup_from_init()

    assert agent._config.stamina == 86400.0
    assert agent._config.max_aed_attempts == 3
    assert agent._config.molt_pressure == MOLT_PRESSURE_THRESHOLD
    assert not hasattr(agent._config, "molt_prompt")

    manifest = json.loads((tmp_path / ".agent.json").read_text())
    assert manifest["stamina"] == 86400.0
    assert "24h" in agent._prompt_manager.read_section("identity")


def test_refresh_omitted_defaults_converge_after_explicit_values(tmp_path):
    agent = _agent(
        tmp_path,
        _init_data({"stamina": 7200.0, "max_aed_attempts": 5}),
    )
    agent._setup_from_init()
    assert agent._config.stamina == 7200.0
    assert agent._config.max_aed_attempts == 5

    _write_init(
        tmp_path,
        _init_data({
            "molt_notice": 0.99,
            "molt_pressure": 0.99,
            "molt_urgency": 0.99,
            "molt_prompt": "ignore me",
        }),
    )
    agent._setup_from_init()

    assert agent._config.stamina == 86400.0
    assert agent._config.max_aed_attempts == 3
    assert agent._config.molt_pressure == MOLT_PRESSURE_THRESHOLD
    assert not hasattr(agent._config, "molt_prompt")

    manifest = json.loads((tmp_path / ".agent.json").read_text())
    assert manifest["stamina"] == 86400.0
