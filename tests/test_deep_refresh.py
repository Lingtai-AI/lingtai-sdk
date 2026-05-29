# tests/test_deep_refresh.py
"""Tests for deep refresh (full agent reconstruct from init.json)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_resolve_env_fields_resolves_env_var(monkeypatch):
    """_resolve_env_fields replaces *_env keys with env var values."""
    from lingtai_kernel.config_resolve import _resolve_env_fields

    monkeypatch.setenv("TEST_SECRET", "hunter2")
    result = _resolve_env_fields({"api_key": None, "api_key_env": "TEST_SECRET"})
    assert result == {"api_key": "hunter2"}
    assert "api_key_env" not in result


def test_resolve_capabilities_resolves_env():
    """_resolve_capabilities applies _resolve_env_fields to each capability."""
    from lingtai_kernel.config_resolve import _resolve_capabilities

    caps = {"bash": {"policy_file": "p.json"}, "vision": {}}
    result = _resolve_capabilities(caps)
    assert result == {"bash": {"policy_file": "p.json"}, "vision": {}}


def _make_init(
    capabilities: dict | None = None,
    addons: list[str] | None = None,
    provider: str = "openai",
    model: str = "gpt-4o",
    covenant: str = "",
    principle: str = "",
    memory: str = "",
) -> dict:
    """Build a minimal valid init.json dict."""
    data = {
        "manifest": {
            "agent_name": "test-agent",
            "language": "en",
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": "test-key",
                "base_url": None,
            },
            "capabilities": capabilities or {},
            "soul": {"delay": 60},
            "stamina": 3600,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 100,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": principle,
        "covenant": covenant,
        "pad": memory,
        "prompt": "",
        "soul": "",
    }
    if addons:
        data["addons"] = addons
    return data


def _make_agent(tmp_path: Path, init_data: dict | None = None):
    """Create a bare Agent with a mock LLM service in a temp working dir."""
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig

    init = init_data or _make_init()
    (tmp_path / "init.json").write_text(json.dumps(init))

    service = MagicMock()
    service.provider = "openai"
    service.model = "gpt-4o"
    service._base_url = None

    agent = Agent(
        service,
        agent_name="test-agent",
        working_dir=tmp_path,
        config=AgentConfig(),
    )
    return agent


def test_deep_refresh_loads_new_capability(tmp_path):
    """After editing init.json to add a capability, refresh picks it up."""
    agent = _make_agent(tmp_path, _make_init(capabilities={}))
    agent._sealed = True

    mock_interface = MagicMock()
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = mock_interface
    agent._session = mock_session

    new_init = _make_init(capabilities={"read": {}})
    (tmp_path / "init.json").write_text(json.dumps(new_init))

    agent._setup_from_init()

    cap_names = [name for name, _ in agent._capabilities]
    assert "read" in cap_names
    assert agent._sealed is True


def test_deep_refresh_no_init_json_is_noop(tmp_path):
    """If init.json is missing, refresh is a no-op (no crash)."""
    agent = _make_agent(tmp_path)
    (tmp_path / "init.json").unlink()

    agent._sealed = True
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = MagicMock()
    agent._session = mock_session

    old_caps = list(agent._capabilities)
    agent._setup_from_init()
    assert agent._capabilities == old_caps


def test_deep_refresh_at_boot_no_history(tmp_path):
    """_setup_from_init works at boot time (no session, not sealed)."""
    init = _make_init(capabilities={"read": {}})
    agent = _make_agent(tmp_path, init)
    assert agent._sealed is False

    agent._setup_from_init()

    cap_names = [name for name, _ in agent._capabilities]
    assert "read" in cap_names
    assert agent._sealed is True


def test_cli_build_agent_uses_refresh(tmp_path):
    """cli.build_agent() constructs agent via _setup_from_init from init.json."""
    from lingtai.cli import load_init, build_agent

    init = _make_init(capabilities={"read": {}}, covenant="Be helpful.")
    (tmp_path / "init.json").write_text(json.dumps(init))

    data = load_init(tmp_path)
    agent = build_agent(data, tmp_path)

    # Capabilities loaded from init.json via _setup_from_init
    cap_names = [name for name, _ in agent._capabilities]
    assert "read" in cap_names

    # Covenant loaded
    covenant_content = agent._prompt_manager.read_section("covenant")
    assert covenant_content is not None
    assert "Be helpful" in covenant_content

    # Cleanup
    agent._workdir.release_lock()


def test_deep_refresh_invalid_init_keeps_old_config(tmp_path):
    """If init.json is invalid, refresh logs error and keeps old state."""
    init = _make_init(capabilities={"read": {}})
    agent = _make_agent(tmp_path, init)
    agent._setup_from_init()  # initial setup

    agent._sealed = True
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = MagicMock()
    agent._session = mock_session

    # Write invalid init.json
    (tmp_path / "init.json").write_text("not json")

    old_caps = list(agent._capabilities)
    agent._setup_from_init()

    # Old capabilities preserved (refresh was a no-op)
    assert agent._capabilities == old_caps


def test_deep_refresh_removes_old_capabilities(tmp_path):
    """Capabilities removed from init.json are gone after refresh.

    Tested against opt-in (non-core) capabilities so the assertion is about
    the refresh path, not about the core-defaults floor. Core capabilities
    persist across refresh regardless of init.json — that is by design;
    `manifest.disable` is the opt-out channel for those.
    """
    init = _make_init(capabilities={"web_search": {"provider": "duckduckgo"}})
    agent = _make_agent(tmp_path, init)
    agent._setup_from_init()  # initial setup

    agent._sealed = True
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = MagicMock()
    agent._session = mock_session

    cap_names_before = {name for name, _ in agent._capabilities}
    assert "web_search" in cap_names_before

    # Drop web_search from init.json
    new_init = _make_init(capabilities={})
    (tmp_path / "init.json").write_text(json.dumps(new_init))

    agent._setup_from_init()

    cap_names_after = {name for name, _ in agent._capabilities}
    assert "web_search" not in cap_names_after


def test_deep_refresh_preserves_chat_history(tmp_path):
    """ChatInterface is passed through to _rebuild_session after refresh."""
    agent = _make_agent(tmp_path, _make_init())
    agent._sealed = True

    mock_interface = MagicMock()
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = mock_interface
    agent._session = mock_session

    agent._setup_from_init()

    mock_session._rebuild_session.assert_called_once_with(mock_interface)


def test_deep_refresh_clears_stale_prompt_sections(tmp_path):
    """Prompt sections from old capabilities don't survive refresh."""
    agent = _make_agent(tmp_path, _make_init())

    # Simulate a stale prompt section from a removed capability
    agent._prompt_manager.write_section("some_old_section", "stale content")
    assert agent._prompt_manager.read_section("some_old_section") is not None

    agent._setup_from_init()

    # Stale section should be gone
    assert agent._prompt_manager.read_section("some_old_section") is None


def test_deep_refresh_reseals(tmp_path):
    """Tool surface is re-sealed after refresh completes."""
    agent = _make_agent(tmp_path, _make_init())
    agent._sealed = True
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = MagicMock()
    agent._session = mock_session

    agent._setup_from_init()

    assert agent._sealed is True


# ---------------------------------------------------------------------------
# Prompt-section reconstruction: covenant vs character separation + molt
# (regression for the "lingtai.md folded into covenant, dropped after molt"
# bug — these fail on main and pass after the single-writer fix).
# ---------------------------------------------------------------------------


def test_reload_keeps_covenant_and_character_separate(tmp_path):
    """Boot/refresh-style reload: covenant.md → `covenant`, lingtai.md →
    `character`. The character text must never be folded into covenant."""
    agent = _make_agent(tmp_path, _make_init(covenant="The operator contract."))
    # Author a character file as the agent would via psyche(lingtai, update).
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "lingtai.md").write_text("I am a meticulous archivist.")

    agent._setup_from_init()

    covenant = agent._prompt_manager.read_section("covenant") or ""
    character = agent._prompt_manager.read_section("character") or ""

    assert "The operator contract." in covenant
    assert "I am a meticulous archivist." in character
    # Separation: neither section bleeds into the other.
    assert "I am a meticulous archivist." not in covenant
    assert "The operator contract." not in character


def test_post_molt_preserves_character_section(tmp_path):
    """Firing the post-molt hooks (as _molt.py does) must leave the
    `character` section intact — not overwritten with covenant-only content.

    On main, _reload_prompt_sections runs last and overwrites covenant with
    covenant.md-only, silently dropping the character until process restart.
    With the single-writer fix both hooks produce identical complete output,
    so order no longer matters."""
    agent = _make_agent(tmp_path, _make_init(covenant="The operator contract."))
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "lingtai.md").write_text("I am a meticulous archivist.")

    # Boot/refresh registers both post-molt hooks (psyche lambda + _reload).
    agent._setup_from_init()

    # Mirror _molt.py:351 — fire every registered post-molt hook in order.
    for cb in getattr(agent, "_post_molt_hooks", []):
        cb()

    character = agent._prompt_manager.read_section("character") or ""
    covenant = agent._prompt_manager.read_section("covenant") or ""
    assert "I am a meticulous archivist." in character
    assert "I am a meticulous archivist." not in covenant


def test_post_molt_preserves_pad_append_pinned_reference(tmp_path):
    """Firing the post-molt hooks must keep the `pad_append.json` pinned
    reference in the `pad` section. On main the pad.md-only writer runs last
    and drops the appended reference; the single-writer fix routes both
    hooks through `_pad_load`, which always composes pad.md + appends."""
    agent = _make_agent(tmp_path, _make_init())
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "pad.md").write_text("Working notes line.")

    # Pin a reference file via pad_append.json (what psyche(pad, append) writes).
    ref = agent._working_dir / "reference.md"
    ref.write_text("PINNED-REFERENCE-MARKER")
    (system_dir / "pad_append.json").write_text(json.dumps(["reference.md"]))

    agent._setup_from_init()

    # Mirror _molt.py:351 — fire every registered post-molt hook in order.
    for cb in getattr(agent, "_post_molt_hooks", []):
        cb()

    pad = agent._prompt_manager.read_section("pad") or ""
    assert "Working notes line." in pad
    assert "PINNED-REFERENCE-MARKER" in pad
