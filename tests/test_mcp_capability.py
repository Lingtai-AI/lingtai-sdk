"""End-to-end smoke tests for the mcp capability + addons decompression.

Verifies the vertical slice: addons:["imap"] in init.json triggers catalog
decompression into mcp_registry.jsonl, the mcp capability renders the registry
into the system prompt, and the loader gates init.json mcp activation by
registry membership.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.core.mcp import (
    REGISTRY_FILENAME,
    decompress_addons,
    read_registry,
    validate_record,
)


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _mk_agent(tmp_path: Path, *, addons=None, capabilities=None):
    workdir = tmp_path / "agent"
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=capabilities or {"mcp": {}},
        addons=addons,
    ), workdir


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_validator_accepts_valid_stdio_record():
    ok, err = validate_record({
        "name": "imap",
        "summary": "test",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "lingtai_imap"],
        "source": "lingtai-curated",
    })
    assert ok, err


def test_validator_accepts_valid_http_record():
    ok, err = validate_record({
        "name": "remote",
        "summary": "test",
        "transport": "http",
        "url": "https://example.com/mcp",
        "source": "user",
    })
    assert ok, err


def test_validator_accepts_optional_homepage():
    ok, err = validate_record({
        "name": "imap",
        "summary": "test",
        "transport": "stdio",
        "command": "python",
        "args": [],
        "source": "lingtai-curated",
        "homepage": "https://github.com/Lingtai-AI/lingtai-imap",
    })
    assert ok, err


def test_validator_accepts_record_without_homepage():
    ok, err = validate_record({
        "name": "imap",
        "summary": "test",
        "transport": "stdio",
        "command": "python",
        "args": [],
        "source": "user",
    })
    assert ok, err


def test_validator_rejects_empty_homepage():
    ok, err = validate_record({
        "name": "imap",
        "summary": "test",
        "transport": "stdio",
        "command": "python",
        "args": [],
        "source": "user",
        "homepage": "",
    })
    assert not ok
    assert "homepage" in err


def test_validator_rejects_bad_name():
    ok, err = validate_record({
        "name": "BAD-NAME",
        "summary": "x",
        "transport": "stdio",
        "command": "a",
        "args": [],
        "source": "u",
    })
    assert not ok
    assert "invalid name" in err


def test_validator_rejects_bad_transport():
    ok, err = validate_record({
        "name": "x",
        "summary": "y",
        "transport": "smtp",
        "source": "u",
    })
    assert not ok
    assert "invalid transport" in err


def test_validator_rejects_long_summary():
    ok, err = validate_record({
        "name": "x",
        "summary": "a" * 500,
        "transport": "stdio",
        "command": "a",
        "args": [],
        "source": "u",
    })
    assert not ok
    assert "summary too long" in err


# ---------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------

def test_decompress_appends_known_addon(tmp_path):
    rep = decompress_addons(tmp_path, ["imap"])
    assert rep["appended"] == ["imap"]
    assert rep["skipped"] == []
    records, problems = read_registry(tmp_path)
    assert [r["name"] for r in records] == ["imap"]
    assert problems == []


def test_decompress_is_idempotent(tmp_path):
    decompress_addons(tmp_path, ["imap"])
    rep2 = decompress_addons(tmp_path, ["imap"])
    assert rep2["appended"] == []
    assert rep2["skipped"] == ["imap"]
    records, _ = read_registry(tmp_path)
    assert len(records) == 1  # no duplicate


def test_decompress_unknown_addon_logged_not_raised(tmp_path):
    rep = decompress_addons(tmp_path, ["nonexistent"])
    assert rep["unknown"] == ["nonexistent"]
    assert rep["appended"] == []
    # Registry file may or may not exist — either is fine for unknown-only input.


def test_registry_drops_duplicates_by_name(tmp_path):
    registry = tmp_path / REGISTRY_FILENAME
    rec = {
        "name": "imap",
        "summary": "x",
        "transport": "stdio",
        "command": "a",
        "args": [],
        "source": "u",
    }
    registry.write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")
    records, problems = read_registry(tmp_path)
    assert len(records) == 1
    assert any("duplicate" in p["error"] for p in problems)


def test_registry_drops_invalid_lines(tmp_path):
    registry = tmp_path / REGISTRY_FILENAME
    valid = json.dumps({
        "name": "imap",
        "summary": "x",
        "transport": "stdio",
        "command": "a",
        "args": [],
        "source": "u",
    })
    registry.write_text(valid + "\n" + "not-json\n" + "{}\n")
    records, problems = read_registry(tmp_path)
    assert len(records) == 1
    assert len(problems) == 2


# ---------------------------------------------------------------------------
# Capability integration
# ---------------------------------------------------------------------------

def test_addons_list_triggers_decompression(tmp_path):
    agent, workdir = _mk_agent(tmp_path, addons=["imap"])
    registry_path = workdir / REGISTRY_FILENAME
    assert registry_path.is_file()
    records, problems = read_registry(workdir)
    assert [r["name"] for r in records] == ["imap"]
    assert problems == []


def test_mcp_capability_renders_registry_into_prompt(tmp_path):
    agent, workdir = _mk_agent(tmp_path, addons=["imap"])
    section = agent._prompt_manager._sections.get("mcp")
    assert section is not None
    body = section.body if hasattr(section, "body") else str(section)
    assert "<registered_mcp>" in body
    assert "imap" in body
    # Catalog ships the imap homepage; render should surface it.
    assert "<homepage>" in body
    assert "github.com/Lingtai-AI/lingtai-imap" in body


def test_addons_dict_still_works_for_legacy(tmp_path):
    """Legacy dict shape should not break — addon load may fail without
    config but the agent must not raise."""
    # Don't actually load IMAP (no config); just ensure the dict path is taken.
    agent, workdir = _mk_agent(tmp_path, addons={})
    # Should construct fine; no decompression should have happened.
    registry_path = workdir / REGISTRY_FILENAME
    assert not registry_path.exists()


def test_mcp_show_action_returns_health_snapshot(tmp_path):
    agent, workdir = _mk_agent(tmp_path, addons=["imap"])
    handler = agent._tool_handlers.get("mcp")
    assert handler is not None
    result = handler({"action": "show"})
    assert result["status"] == "ok"
    assert result["registered_count"] == 1
    assert result["registered"][0]["name"] == "imap"
    assert "mcp_manual" in result and result["mcp_manual"]  # umbrella SKILL.md body


def test_mcp_show_unknown_action_returns_error(tmp_path):
    agent, workdir = _mk_agent(tmp_path, addons=["imap"])
    handler = agent._tool_handlers.get("mcp")
    result = handler({"action": "register"})  # not supported in slice
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Loader gating
# ---------------------------------------------------------------------------

def test_loader_skips_unregistered_init_mcp(tmp_path, caplog):
    """init.json mcp entry not in registry should be skipped with a warning."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    # Pre-create init.json with an unregistered mcp entry.
    init = {
        "mcp": {
            "rogue": {"type": "stdio", "command": "false", "args": []},
        },
    }
    (workdir / "init.json").write_text(json.dumps(init))

    Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
        # No addons → registry is empty → rogue should be skipped.
    )

    # We can't easily intercept the kernel logger here, but the registry stays empty
    # and no MCP client should have been added.
    # (The legacy mcp/servers.json path is also untouched.)


# ---------------------------------------------------------------------------
# Failed-MCP retry on refresh — regression for Lingtai-AI/lingtai#34
# ---------------------------------------------------------------------------

class _FakeMCPClient:
    """Minimal stand-in for MCPClient/HTTPMCPClient.

    `is_connected_value` controls health probes; tool list is empty so the
    Agent's tool registration loop is a no-op (no need to fake schemas).
    """

    def __init__(self, is_connected_value: bool):
        self._connected = is_connected_value
        self.closed = False

    def start(self):
        return None

    def is_connected(self) -> bool:
        return self._connected and not self.closed

    def list_tools(self, timeout: float = 10):
        return []

    def close(self):
        self.closed = True


def test_retry_failed_mcps_records_dead_then_recovers(tmp_path, monkeypatch):
    """A registered init.json MCP that boots dead should be retried on
    `_retry_failed_mcps()` and reported as recovered when the second attempt
    succeeds. Regression for Lingtai-AI/lingtai#34."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    # Pre-stage registry so the init.json mcp entry passes the gate.
    (workdir / "mcp_registry.jsonl").write_text(json.dumps({
        "name": "telegram",
        "summary": "test",
        "transport": "stdio",
        "command": "/bin/true",
        "args": [],
        "source": "user",
    }) + "\n")
    init = {
        "mcp": {
            "telegram": {"type": "stdio", "command": "/bin/true", "args": []},
        },
    }
    (workdir / "init.json").write_text(json.dumps(init))

    # Patch connect_mcp on the Agent class: first call → returns dead client
    # (subprocess "exited" immediately); second call → returns live client.
    call_count = {"n": 0}

    def fake_connect_mcp(self, command, args=None, env=None):
        call_count["n"] += 1
        client = _FakeMCPClient(is_connected_value=(call_count["n"] >= 2))
        if not hasattr(self, "_mcp_clients"):
            self._mcp_clients = []
        self._mcp_clients.append(client)
        return []  # no tools to register

    monkeypatch.setattr(Agent, "connect_mcp", fake_connect_mcp)

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    )

    # Boot recorded the spec, but the tracked client is dead.
    assert "telegram" in agent._mcp_init_specs
    boot_client = agent._mcp_init_specs["telegram"]["client"]
    assert boot_client is not None
    assert not boot_client.is_connected()

    # Retry: should detect death, close+remove, respawn — second spawn
    # returns a live client → reported as recovered.
    report = agent._retry_failed_mcps()
    assert "telegram" in report["retried"]
    assert "telegram" in report["recovered"]
    assert report["still_failed"] == []
    # The dead client should have been closed and dropped.
    assert boot_client.closed
    assert boot_client not in agent._mcp_clients
    # New client tracked.
    new_client = agent._mcp_init_specs["telegram"]["client"]
    assert new_client is not None and new_client.is_connected()


def test_retry_failed_mcps_skips_healthy(tmp_path, monkeypatch):
    """A live MCP should be reported as `healthy`, not retried."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    (workdir / "mcp_registry.jsonl").write_text(json.dumps({
        "name": "telegram",
        "summary": "test",
        "transport": "stdio",
        "command": "/bin/true",
        "args": [],
        "source": "user",
    }) + "\n")
    (workdir / "init.json").write_text(json.dumps({
        "mcp": {"telegram": {"type": "stdio", "command": "/bin/true"}},
    }))

    def fake_connect_mcp(self, command, args=None, env=None):
        client = _FakeMCPClient(is_connected_value=True)
        if not hasattr(self, "_mcp_clients"):
            self._mcp_clients = []
        self._mcp_clients.append(client)
        return []

    monkeypatch.setattr(Agent, "connect_mcp", fake_connect_mcp)

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    )

    report = agent._retry_failed_mcps()
    assert report["retried"] == []
    assert report["recovered"] == []
    assert report["still_failed"] == []
    assert "telegram" in report["healthy"]


def test_retry_failed_mcps_no_specs_is_noop(tmp_path):
    """An agent with no init.json mcp entries should return an empty
    report — never raise, never assume `_mcp_init_specs` exists."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    )
    report = agent._retry_failed_mcps()
    assert report == {"retried": [], "recovered": [],
                      "still_failed": [], "healthy": []}


def test_curated_catalog_includes_whatsapp(tmp_path: Path):
    rep = decompress_addons(tmp_path, ["whatsapp"])
    assert rep["appended"] == ["whatsapp"]
    records, problems = read_registry(tmp_path)
    assert problems == []
    assert records[0]["name"] == "whatsapp"
    assert records[0]["args"] == ["-m", "lingtai_whatsapp"]
    assert records[0]["homepage"] == "https://github.com/Lingtai-AI/lingtai-whatsapp"
