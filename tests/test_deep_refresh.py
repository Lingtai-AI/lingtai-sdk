# tests/test_deep_refresh.py
"""Tests for deep refresh (full agent reconstruct from init.json)."""
from __future__ import annotations

import hashlib
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


def _packaged_procedures() -> str:
    from importlib.resources import files

    return files("lingtai.prompts").joinpath("procedures.md").read_text(
        encoding="utf-8"
    )


def _packaged_guidance() -> dict:
    from importlib.resources import files

    return json.loads(
        files("lingtai.prompts").joinpath("guidance.json").read_text(
            encoding="utf-8"
        )
    )

def _events(tmp_path: Path, event_type: str) -> list[dict]:
    log_path = tmp_path / "logs" / "events.jsonl"
    if not log_path.is_file():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == event_type:
            events.append(event)
    return events


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


def test_init_procedures_override_is_migrated_not_prompted(tmp_path):
    """Legacy init.json procedures content is archived, removed, and ignored."""
    legacy = "LEGACY-PROCEDURES-OVERRIDE"
    init = _make_init()
    init["procedures"] = legacy
    agent = _make_agent(tmp_path, init)

    agent._setup_from_init()

    packaged = _packaged_procedures()
    prompt = agent._prompt_manager.render()
    assert legacy not in prompt
    assert packaged in prompt
    data = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert "procedures" not in data

    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    archive = tmp_path / "system" / "migrations" / f"init-procedures-{digest}.md"
    assert archive.read_text(encoding="utf-8") == legacy

    events = _events(tmp_path, "init_procedures_override_migrated")
    assert len(events) == 1
    event = events[0]
    assert event["archive_path"] == f"system/migrations/init-procedures-{digest}.md"
    assert event["content_hash"] == digest
    assert event["byte_length"] == len(legacy.encode("utf-8"))
    assert event["char_length"] == len(legacy)
    assert event["field_removed"] is True

    agent._setup_from_init()
    archives = list((tmp_path / "system" / "migrations").glob("init-procedures-*.md"))
    assert archives == [archive]
    assert len(_events(tmp_path, "init_procedures_override_migrated")) == 1


def test_single_space_procedures_no_longer_opts_out(tmp_path):
    """A single-space legacy procedures value is migrated, not treated as opt-out."""
    init = _make_init()
    init["procedures"] = " "
    agent = _make_agent(tmp_path, init)

    agent._setup_from_init()

    packaged = _packaged_procedures()
    procedures = agent._prompt_manager.read_section("procedures") or ""
    assert procedures == packaged
    data = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert "procedures" not in data

    digest = hashlib.sha256(b" ").hexdigest()
    archive = tmp_path / "system" / "migrations" / f"init-procedures-{digest}.md"
    assert archive.read_text(encoding="utf-8") == " "



def test_custom_procedures_file_is_removed_not_prompted(tmp_path):
    """Legacy procedures_file overrides are removed even for custom paths."""
    custom = tmp_path / "custom-procedures.md"
    custom.write_text("CUSTOM-PROCEDURES-FILE", encoding="utf-8")
    init = _make_init()
    init["procedures_file"] = str(custom)
    agent = _make_agent(tmp_path, init)

    agent._setup_from_init()

    packaged = _packaged_procedures()
    prompt = agent._prompt_manager.render()
    assert "CUSTOM-PROCEDURES-FILE" not in prompt
    assert agent._prompt_manager.read_section("procedures") == packaged
    data = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert "procedures_file" not in data


def test_system_procedures_is_overwritten_by_packaged_default(tmp_path):
    """Manual system/procedures.md edits are replaced by the packaged default."""
    agent = _make_agent(tmp_path, _make_init())
    system_dir = tmp_path / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "procedures.md").write_text("MANUAL-PROCEDURES-EDIT", encoding="utf-8")

    agent._setup_from_init()

    packaged = _packaged_procedures()
    assert (system_dir / "procedures.md").read_text(encoding="utf-8") == packaged
    assert agent._prompt_manager.read_section("procedures") == packaged


def test_system_guidance_is_overwritten_by_packaged_default(tmp_path):
    """Manual system/guidance.json edits are replaced by packaged guidance."""
    agent = _make_agent(tmp_path, _make_init())
    system_dir = tmp_path / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "guidance.json").write_text('{"stale": true}\n', encoding="utf-8")

    agent._setup_from_init()

    guidance_path = system_dir / "guidance.json"
    assert json.loads(guidance_path.read_text(encoding="utf-8")) == _packaged_guidance()
    assert guidance_path.read_text(encoding="utf-8").endswith("\n")

def test_procedures_falls_back_to_system_file_when_packaged_missing(tmp_path):
    """If the packaged default cannot be read, system/procedures.md is fallback."""
    fallback = "FALLBACK-PROCEDURES"
    agent = _make_agent(tmp_path, _make_init())
    system_dir = tmp_path / "system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "procedures.md").write_text(fallback, encoding="utf-8")

    with patch("importlib.resources.files", side_effect=FileNotFoundError):
        agent._setup_from_init()

    assert agent._prompt_manager.read_section("procedures") == fallback
    assert (system_dir / "procedures.md").read_text(encoding="utf-8") == fallback


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


# ---------------------------------------------------------------------------
# Codex cache-affinity rebuild on refresh.
#
# A live refresh (re)builds the Codex adapter while preserving chat history. The
# cache-affinity id is a pure hash of the agent path (no epoch / no clock), so
# the rebuilt adapter MUST keep the byte-identical id — the agent stays pinned to
# the same sticky-warm backend cache slot across refresh. These tests prove the
# refresh rebuilds the Codex service/adapter and the id is stable across it.
# ---------------------------------------------------------------------------


def _codex_agent(tmp_path: Path, epoch: float):
    """Build a real Agent backed by a real Codex LLMService.

    ``time.time`` is patched during construction only to keep any incidental
    timestamps deterministic; the Codex id does NOT depend on the clock (it is a
    pure hash of the agent path), so the patched value never affects it. The
    returned agent's ``service`` is a genuine ``LLMService`` (not a mock), so
    ``_setup_from_init`` exercises the real Codex rebuild path.
    """
    from unittest.mock import patch as _patch

    from lingtai.agent import Agent
    from lingtai.llm.service import (
        LLMService,
        build_provider_defaults_from_manifest_llm,
    )
    from lingtai_kernel.config import AgentConfig
    import lingtai  # noqa: F401  (registers the codex adapter factory)

    init = _make_init(provider="codex", model="gpt-5.5")
    # Pin max_rpm so the provider-defaults bucket is byte-identical before and
    # after refresh — otherwise an incidental max_rpm change (default 60 on
    # refresh) would rebuild the service for the wrong reason and mask the bug.
    init["manifest"]["max_rpm"] = 60
    (tmp_path / "init.json").write_text(json.dumps(init))

    llm = init["manifest"]["llm"]
    provider_defaults = build_provider_defaults_from_manifest_llm(
        llm, max_rpm=60, working_dir=tmp_path
    )
    with _patch("lingtai.auth.codex.CodexTokenManager") as mgr_cls, _patch(
        "time.time", return_value=epoch
    ):
        mgr_cls.return_value.get_access_token.return_value = "fake-token"
        service = LLMService(
            provider="codex",
            model="gpt-5.5",
            api_key="fake",
            provider_defaults=provider_defaults,
        )
        agent = Agent(
            service,
            agent_name="test-agent",
            working_dir=tmp_path,
            config=AgentConfig(),
        )
    return agent


def test_refresh_rebuilds_codex_adapter_with_stable_id(tmp_path):
    """A live Codex refresh rebuilds the adapter but KEEPS the same affinity id.

    The Codex cache-affinity id is a pure hash of the agent path (no epoch, no
    time dependence), so a refresh — even at a different wall-clock — must yield
    a fresh adapter instance whose id is byte-identical to the pre-refresh id.
    This is the whole point of removing the epoch-stamp / rotation: the agent
    keeps routing to the same sticky-warm backend cache slot across restarts.
    """
    from unittest.mock import patch

    agent = _codex_agent(tmp_path, epoch=1_700_000_000)
    agent._sealed = True

    old_adapter = agent.service.get_adapter("codex")
    old_id = old_adapter._codex_id
    assert old_id is not None  # per-agent identity is wired by default

    # A later refresh at a DIFFERENT wall-clock must NOT change the id.
    mock_interface = MagicMock()
    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = mock_interface
    # The real Session._rebuild_session would call create_session; we only need
    # to confirm refresh hands it the preserved interface and a fresh service.
    agent._session = mock_session

    with patch("lingtai.auth.codex.CodexTokenManager") as mgr_cls, patch(
        "time.time", return_value=1_700_000_500
    ):
        mgr_cls.return_value.get_access_token.return_value = "fake-token"
        agent._setup_from_init()

    new_adapter = agent.service.get_adapter("codex")
    new_id = new_adapter._codex_id

    # 1. A genuinely fresh adapter instance (not the cached boot one).
    assert new_adapter is not old_adapter
    # 2. The id is STABLE across refresh despite the different clock.
    assert new_id == old_id
    assert new_id is not None
    # 3. The id is the pure per-agent hash of the anchor (no epoch).
    from lingtai.llm.openai.adapter import _codex_session_id

    anchor = str((tmp_path / "init.json").resolve())
    assert new_id == _codex_session_id(anchor)
    # 4. The new service object is wired into the session that rebuilds history.
    assert agent._session._llm_service is agent.service
    # 5. Chat history is preserved: the saved interface is replayed.
    mock_session._rebuild_session.assert_called_once_with(mock_interface)


def test_refresh_codex_adapter_keeps_per_agent_anchor(tmp_path):
    """The rebuilt adapter still anchors on the same agent path (identity).

    Both the old and new ids derive from the same ``init.json`` anchor (a pure
    hash of it), so the rebuilt adapter remains a per-agent identity (not a
    shared model-only key) and the id is byte-identical across refresh.
    """
    from unittest.mock import patch

    agent = _codex_agent(tmp_path, epoch=1_700_000_000)
    agent._sealed = True

    old_anchor = agent.service.get_adapter("codex")._codex_session_anchor

    mock_session = MagicMock()
    mock_session.chat = MagicMock()
    mock_session.chat.interface = MagicMock()
    agent._session = mock_session

    with patch("lingtai.auth.codex.CodexTokenManager") as mgr_cls, patch(
        "time.time", return_value=1_700_000_500
    ):
        mgr_cls.return_value.get_access_token.return_value = "fake-token"
        agent._setup_from_init()

    new_adapter = agent.service.get_adapter("codex")
    assert new_adapter._codex_session_anchor == old_anchor
    assert new_adapter._codex_session_anchor == str((tmp_path / "init.json").resolve())
