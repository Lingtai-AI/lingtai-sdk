"""Stage-3C wrapper bridge: host the *real* ``system`` lifecycle tool through the
SDK bundle.

Where ``tests/test_sdk_lifecycle_tools.py`` proves the SDK-side declaration + host
seam with dummy handlers (and import purity), this test proves the *wrapper*
half — ``lingtai.core.system_bundle`` — that injects the genuine kernel intrinsic
``system.handle`` into the SDK ``system`` lifecycle bundle and so runs the real
behavior through the declared manifest.

The key assertion is **parity**: invoking ``system`` through the bundle host
returns exactly what the kernel intrinsic returns, because the bridge wires the
*same* ``intrinsics.system.handle`` the live ``BaseAgent._wire_intrinsics`` path
dispatches, bound to the same agent.

**Safety:** every action exercised here is side-effect-free — the pure
``notification`` placeholder, an unknown-action error, and karma/nirvana actions
*denied by missing authority* (the gate returns an error before any teardown).
Nothing here sleeps, refreshes, or destroys an agent.
"""
from __future__ import annotations

import os

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.intrinsics import system as sysintr
from lingtai.core import system_bundle


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


@pytest.fixture
def agent(tmp_path):
    wd = tmp_path / "wd"
    a = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=wd)
    try:
        yield a
    finally:
        a.stop(timeout=1.0)


# --- the bridge builds the native host for the real system tool ------------


def test_bridge_builds_native_system_host(agent):
    host = system_bundle.system_lifecycle_bundle_host(agent)
    # one tool, native-authority host, privileged native transport.
    assert host.tools == ("system",)
    assert host.manifest.name == "system"
    assert host.manifest.roles.privileged is True
    assert host.manifest.transport.kind == "native"
    assert host.manifest.security.danger == "destructive"


def test_bridge_builds_hosts_mapping(agent):
    hosts = system_bundle.system_lifecycle_bundle_hosts(agent)
    assert set(hosts) == {"system"}
    assert hosts["system"].tools == ("system",)


# --- parity: the bundle path runs the real intrinsic, byte-identical -------


def test_bridge_notification_placeholder_parity(agent):
    """The pure ``notification`` placeholder read — no side effect — matches."""
    host = system_bundle.system_lifecycle_bundle_host(agent)
    via_bundle = host.invoke("system", action="notification")
    via_intrinsic = sysintr.handle(agent, {"action": "notification"})
    assert via_bundle == via_intrinsic
    assert via_bundle.get("_notification_placeholder") is True
    assert "message" in via_bundle


def test_bridge_unknown_action_error_parity(agent):
    host = system_bundle.system_lifecycle_bundle_host(agent)
    via_bundle = host.invoke("system", action="does-not-exist")
    via_intrinsic = sysintr.handle(agent, {"action": "does-not-exist"})
    assert via_bundle == via_intrinsic
    assert via_bundle["status"] == "error"
    assert "Unknown system action" in via_bundle["message"]


# --- the karma/nirvana authority gate flows through the bridge unchanged ----


def test_bridge_karma_action_denied_without_authority_parity(agent):
    """A karma action without ``admin.karma`` is denied — through the bridge too.

    This proves the real per-action authority gate
    (``intrinsics.system.karma._check_karma_gate``) runs unchanged through the
    bundle host, and it does so *before* any side effect (no agent is touched).
    """
    agent._admin = {}  # strip all authority
    host = system_bundle.system_lifecycle_bundle_host(agent)
    via_bundle = host.invoke("system", action="lull", address="somewhere")
    via_intrinsic = sysintr.handle(agent, {"action": "lull", "address": "somewhere"})
    assert via_bundle == via_intrinsic
    assert via_bundle["error"] is True
    assert "Not authorized for lull" in via_bundle["message"]


def test_bridge_nirvana_denied_without_nirvana_authority_parity(agent):
    """Nirvana requires karma AND nirvana; denied without them — through the bridge."""
    agent._admin = {"karma": True}  # karma but NOT nirvana
    host = system_bundle.system_lifecycle_bundle_host(agent)
    via_bundle = host.invoke("system", action="nirvana", address="somewhere")
    via_intrinsic = sysintr.handle(agent, {"action": "nirvana", "address": "somewhere"})
    assert via_bundle == via_intrinsic
    assert via_bundle["error"] is True
    assert "admin.nirvana=True" in via_bundle["message"]


# --- the bridge wires the SAME function the live path dispatches ------------


def test_bridge_uses_the_same_intrinsic_the_live_path_wires(agent):
    """The agent's live ``system`` intrinsic and the bridge wire the same behavior.

    Both go through ``intrinsics.system.handle`` — one source of truth — so the
    bundle host cannot drift from the registered intrinsic. The live
    ``_wire_intrinsics`` registration is untouched (``system`` is still present).
    """
    assert "system" in agent._intrinsics  # live path unchanged

    # the live intrinsic and the bridge produce identical results for a pure call.
    live = agent._intrinsics["system"]({"action": "notification"})
    host = system_bundle.system_lifecycle_bundle_host(agent)
    bundled = host.invoke("system", action="notification")
    # the live closure stamps the injected _tc_id only when dispatched via the
    # turn loop; called directly here, both paths hit the same handler body.
    assert bundled == live


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
        "import lingtai.core.system_bundle as sb\n"
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
