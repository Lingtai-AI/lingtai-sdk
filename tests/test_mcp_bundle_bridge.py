"""Stage-3F wrapper bridge: host the *real* ``mcp`` tool through the SDK
tool-config bundle.

Where ``tests/test_sdk_mcp_tools.py`` proves the SDK-side declaration + host seam
with a dummy handler (and import purity), this test proves the *wrapper* half —
``lingtai.core.mcp_bundle`` — that injects the genuine wrapper
``mcp.make_handler(agent)`` into the SDK bundle and so runs the real behavior
through the declared manifest.

The key assertion is **parity**: invoking ``mcp`` through the bundle host returns
exactly what the live path returns, because the bridge wires the *same* source of
truth (``mcp.make_handler`` the live ``mcp.setup()`` registers), bound to the same
agent.

**Safety:** every action exercised here is side-effect-free — ``show`` (read-only
registry view: re-read ``mcp_registry.jsonl`` and re-render the prompt section, no
write) and an unknown action (errors before any work). **No** real MCP server is
started, and the only registry write is the temp fixture setup below — the tool
itself never writes the registry.
"""
from __future__ import annotations

import json
import os

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai.core import mcp as mcpmod
from lingtai.core import mcp_bundle
from lingtai_sdk import mcp_tools as mt


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _write_mcp_fixture(working_dir):
    """Create a minimal mcp manual + a one-entry registry as a test fixture.

    This is the *only* registry write in this test — it is fixture setup, not the
    tool writing. ``_reconcile`` reads the manual from
    ``.library/intrinsic/capabilities/mcp/SKILL.md`` and the registry from
    ``mcp_registry.jsonl``; both must exist for a non-degraded health snapshot.
    """
    manual_dir = working_dir / ".library" / "intrinsic" / "capabilities" / "mcp"
    manual_dir.mkdir(parents=True, exist_ok=True)
    (manual_dir / "SKILL.md").write_text(
        "# mcp-manual\n\nregistration contract goes here.\n", encoding="utf-8"
    )
    record = {
        "name": "example",
        "summary": "an example stdio MCP for the fixture",
        "transport": "stdio",
        "command": "/usr/bin/true",
        "args": [],
        "source": "fixture",
    }
    (working_dir / "mcp_registry.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


@pytest.fixture
def agent(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    _write_mcp_fixture(wd)
    a = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=wd)
    try:
        yield a
    finally:
        a.stop(timeout=1.0)


# --- the bridge builds the right host ---------------------------------------


def test_mcp_bridge_builds_in_process_host(agent):
    host = mcp_bundle.mcp_config_bundle_host(agent)
    assert host.tools == ("mcp",)
    assert host.manifest.name == "mcp"
    assert host.manifest.roles.privileged is False
    assert host.manifest.transport.kind == "in_process"
    assert host.manifest.security.danger == "safe"


def test_bridge_builds_hosts_mapping(agent):
    hosts = mcp_bundle.mcp_config_bundle_hosts(agent)
    assert set(hosts) == {"mcp"}
    assert hosts["mcp"].tools == ("mcp",)


def _schema_actions(schema: dict) -> set[str]:
    return set(schema["properties"]["action"]["enum"])


# --- drift guard: SDK declared action set == live schema action enum ---------


def test_mcp_manifest_actions_match_live_schema():
    """Pin the SDK mcp declaration to the live wrapper schema action enum (show only)."""
    declared = set(mt.mcp_config_manifest().metadata["actions"])
    live = _schema_actions(mcpmod.get_schema())
    assert declared == live == {"show"}


# --- mcp parity: the bundle path runs the real handler, byte-identical --------


def test_mcp_show_parity(agent):
    """The read-only ``show`` matches the live handler, byte-identically.

    Both go through ``mcp.make_handler`` → ``_reconcile`` against the same fixture
    registry / manual — no MCP server is started, no registry write.
    """
    host = mcp_bundle.mcp_config_bundle_host(agent)
    via_bundle = host.invoke("mcp", action="show")
    via_live = mcpmod.make_handler(agent)({"action": "show"})
    assert via_bundle == via_live
    # the fixture is non-degraded and the one registered server shows up.
    assert via_bundle["status"] == "ok"
    assert via_bundle["registered_count"] == 1
    assert via_bundle["registered"][0]["name"] == "example"


def test_mcp_unknown_action_error_parity(agent):
    host = mcp_bundle.mcp_config_bundle_host(agent)
    via_bundle = host.invoke("mcp", action="does-not-exist")
    via_live = mcpmod.make_handler(agent)({"action": "does-not-exist"})
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "unknown action" in via_bundle["message"]


def test_mcp_make_handler_is_setup_single_source(agent):
    """``setup()`` and the bridge build the handler through the same factory.

    ``mcp.setup()`` registers ``make_handler(agent)`` via ``add_tool``, and the
    bridge hosts a handler from the *same* ``make_handler``, so the bundle host
    cannot drift from the registered tool. Registering via ``setup()`` leaves a
    working ``mcp`` tool whose ``show`` matches the bridge's ``show``.
    """
    mcpmod.setup(agent)
    assert "mcp" in agent._tool_handlers  # live path registered the tool
    setup_show = agent._tool_handlers["mcp"]({"action": "show"})
    host = mcp_bundle.mcp_config_bundle_host(agent)
    bundle_show = host.invoke("mcp", action="show")
    assert bundle_show == setup_show


# --- the bridge does not eagerly import the SDK at wrapper module load --------


def test_bridge_does_not_import_sdk_at_wrapper_module_load():
    """Importing the wrapper bridge module must not eagerly import the SDK.

    The SDK is imported lazily inside the bridge functions (wrapper -> sdk edge),
    so a bare import of the bridge module leaves ``lingtai_sdk`` unloaded until a
    host is actually built.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    code = (
        "import sys\n"
        "import lingtai.core.mcp_bundle as mb\n"
        "assert 'lingtai_sdk' not in sys.modules, "
        "'bridge import eagerly pulled the SDK'\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
