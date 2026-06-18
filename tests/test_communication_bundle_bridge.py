"""Stage-3D wrapper bridge: host the *real* ``email`` and ``daemon`` tools through
the SDK communication/execution bundles.

Where ``tests/test_sdk_communication_tools.py`` proves the SDK-side declarations +
host seams with dummy handlers (and import purity), this test proves the *wrapper*
half — ``lingtai.core.communication_bundle`` — that injects the genuine kernel
intrinsic ``email.handle`` and the genuine wrapper ``daemon.make_handler(agent)``
into the SDK bundles and so runs the real behavior through the declared manifests.

The key assertion is **parity**: invoking ``email`` / ``daemon`` through the
bundle host returns exactly what the live path returns, because the bridge wires
the *same* sources of truth (``intrinsics.email.handle`` the live
``_wire_intrinsics`` dispatches, and ``daemon.make_manager`` the live
``daemon.setup()`` builds), bound to the same agent.

**Safety:** every action exercised here is side-effect-free —

* email: ``check`` / ``contacts`` (read-only inbox/contact queries on a fresh
  mailbox), the reserved ``unread`` (errors before touching anything), and an
  unknown action. No external send, no SMTP/IMAP, no delete.
* daemon: ``list`` (read-only emanation status query on a fresh run dir) and an
  unknown action. **No** ``emanate`` / ``ask`` / ``reclaim`` — nothing spawns,
  drives, or kills any process or subagent.
"""
from __future__ import annotations

import os

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.intrinsics import email as emailintr
from lingtai.core import communication_bundle
from lingtai.core import daemon as daemonmod


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


# --- the bridge builds the right host per surface --------------------------


def test_email_bridge_builds_native_host(agent):
    host = communication_bundle.email_comm_bundle_host(agent)
    assert host.tools == ("email",)
    assert host.manifest.name == "email"
    assert host.manifest.roles.privileged is True
    assert host.manifest.transport.kind == "native"
    assert host.manifest.security.danger == "destructive"


def test_daemon_bridge_builds_in_process_host(agent):
    host = communication_bundle.daemon_exec_bundle_host(agent)
    assert host.tools == ("daemon",)
    assert host.manifest.name == "daemon"
    assert host.manifest.roles.privileged is False
    assert host.manifest.transport.kind == "in_process"
    assert host.manifest.security.danger == "destructive"


def test_bridge_builds_hosts_mapping(agent):
    hosts = communication_bundle.communication_bundle_hosts(agent)
    assert set(hosts) == {"email", "daemon"}
    assert hosts["email"].tools == ("email",)
    assert hosts["daemon"].tools == ("daemon",)


# --- email parity: the bundle path runs the real intrinsic, byte-identical ---


def test_email_check_parity(agent):
    """The read-only ``check`` (empty inbox) matches the live intrinsic."""
    host = communication_bundle.email_comm_bundle_host(agent)
    via_bundle = host.invoke("email", action="check")
    via_intrinsic = emailintr.handle(agent, {"action": "check"})
    assert via_bundle == via_intrinsic


def test_email_contacts_parity(agent):
    host = communication_bundle.email_comm_bundle_host(agent)
    via_bundle = host.invoke("email", action="contacts")
    via_intrinsic = emailintr.handle(agent, {"action": "contacts"})
    assert via_bundle == via_intrinsic


def test_email_reserved_unread_error_parity(agent):
    """The reserved ``unread`` action errors identically — before any work."""
    host = communication_bundle.email_comm_bundle_host(agent)
    via_bundle = host.invoke("email", action="unread")
    via_intrinsic = emailintr.handle(agent, {"action": "unread"})
    assert via_bundle == via_intrinsic
    assert via_bundle["status"] == "error"
    assert "reserved" in via_bundle["message"]


def test_email_unknown_action_error_parity(agent):
    host = communication_bundle.email_comm_bundle_host(agent)
    via_bundle = host.invoke("email", action="does-not-exist")
    via_intrinsic = emailintr.handle(agent, {"action": "does-not-exist"})
    assert via_bundle == via_intrinsic
    assert "Unknown email action" in via_bundle["error"]


def test_email_bridge_uses_the_same_intrinsic_the_live_path_wires(agent):
    """The agent's live ``email`` intrinsic and the bridge wire the same behavior."""
    assert "email" in agent._intrinsics  # live path unchanged
    live = agent._intrinsics["email"]({"action": "contacts"})
    host = communication_bundle.email_comm_bundle_host(agent)
    bundled = host.invoke("email", action="contacts")
    assert bundled == live


# --- daemon parity: the bundle path runs the real handler, byte-identical -----


def test_daemon_list_parity(agent):
    """The read-only ``list`` (no emanations) matches the live handler.

    Both build the manager through ``daemon.make_manager`` and run ``_handle_list``
    against the same fresh run dir — no process is spawned.
    """
    host = communication_bundle.daemon_exec_bundle_host(agent)
    via_bundle = host.invoke("daemon", action="list")
    via_live = daemonmod.make_handler(agent)({"action": "list"})
    assert via_bundle == via_live


def test_daemon_unknown_action_error_parity(agent):
    host = communication_bundle.daemon_exec_bundle_host(agent)
    via_bundle = host.invoke("daemon", action="does-not-exist")
    via_live = daemonmod.make_handler(agent)({"action": "does-not-exist"})
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "Unknown action" in via_bundle["message"]


def test_daemon_make_handler_is_setup_single_source(agent):
    """``setup()`` and the bridge build the manager through the same factory.

    ``daemon.setup()`` and ``daemon.make_handler`` both go through
    ``daemon.make_manager`` — one construction path — so the bundle host cannot
    drift from the registered tool. Registering via ``setup()`` leaves a working
    ``daemon`` handler, and a fresh bridge handler produces the same ``list``.
    """
    mgr = daemonmod.setup(agent)
    assert mgr is not None
    setup_list = mgr.handle({"action": "list"})
    host = communication_bundle.daemon_exec_bundle_host(agent)
    bundle_list = host.invoke("daemon", action="list")
    assert bundle_list == setup_list


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
        "import lingtai.core.communication_bundle as cb\n"
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
