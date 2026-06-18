"""Stage-3D proof: the high-state communication/execution bundle declarations + seams.

The communication/execution counterpart of ``test_sdk_lifecycle_tools.py`` (stage
3C). These tests assert that:

* the ``email`` manifest is privileged + native (intrinsic-carried) but not
  ``native_only``, and the ``daemon`` manifest is non-privileged + in-process
  (capability-carried) — each matching how the live wiring carries it;
* both manifests validate strictly and round-trip through ``load_manifest``;
* the **per-action risk tables** (``EMAIL_ACTION_RISK`` / ``DAEMON_ACTION_RISK``)
  cover exactly the declared actions, grade them faithfully (read-only → SAFE,
  sends/mutations → CAUTION, deletes/process-spawn/kill → DESTRUCTIVE), and the
  bundle-level posture equals the strongest action's grade;
* the host seams host each surface with its correct carrier — ``email`` as a
  ``NativeBundleHost`` (native authority), ``daemon`` as a non-native
  ``BundleHost`` — with injected dummy handlers, and the wrong host refuses;
* the **guard/audit invariant** holds: feeding the manifests to the stage-17
  ``guard_bridge`` denies the destructive surfaces in BLOCKING / warns in
  ADVISORY — *without* this stage installing any guard.

Crucially, **no real ``email`` / ``daemon`` is called or imported from the SDK**:
every handler here is a dummy, and a subprocess asserts importing
``communication_tools`` pulls in no ``lingtai`` wrapper module. The wrapper-side
bridge (which hosts the real handlers) is tested in
``tests/test_communication_bundle_bridge.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk import communication_tools as ct
from lingtai_sdk import guard_bridge as gb
from lingtai_sdk.errors import BundleHostError

# The guard bridge maps a manifest's danger posture onto kernel guard
# primitives; ToolProposal is the kernel-side type the resulting check consumes.
from lingtai_kernel.tool_call_guard import ToolProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- manifests: identity, posture, carrier ----------------------------------


def test_email_manifest_privileged_native_not_native_only():
    m = ct.email_comm_manifest()
    assert m.name == ct.EMAIL_TOOL_NAME == "email"
    assert m.roles.required is True
    assert m.roles.privileged is True
    # email is native-carried (intrinsic) but a backend could re-implement mail.
    assert m.roles.native_only is False
    assert m.roles.backend_replaceability is cap.BackendReplaceability.AUGMENTABLE
    assert m.transport.kind == cap.TransportKind.NATIVE.value
    # bundle-level posture is the strongest action's grade (delete -> destructive).
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value
    assert m.surfaces.tools == ("email",)


def test_daemon_manifest_non_privileged_in_process():
    m = ct.daemon_exec_manifest()
    assert m.name == ct.DAEMON_TOOL_NAME == "daemon"
    # daemon is a wrapper capability carried in-process (add_tool), not privileged.
    assert m.roles.privileged is False
    assert m.roles.native_only is False
    assert m.transport.kind == cap.TransportKind.IN_PROCESS.value
    # spawning/killing child processes -> destructive bundle posture.
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value
    assert m.surfaces.tools == ("daemon",)


def test_manifests_validate_and_round_trip():
    for builder in (ct.email_comm_manifest, ct.daemon_exec_manifest):
        original = builder()
        original.validate()  # does not raise
        loaded = cap.load_manifest(original.to_dict())
        assert loaded.to_dict() == original.to_dict()


def test_manifest_helpers_and_names():
    assert ct.communication_tool_names() == ("email", "daemon")
    manifests = ct.communication_tool_manifests()
    assert [m.name for m in manifests] == ["email", "daemon"]
    assert ct.is_email_comm_manifest(ct.email_comm_manifest()) is True
    assert ct.is_email_comm_manifest(ct.daemon_exec_manifest()) is False
    assert ct.is_daemon_exec_manifest(ct.daemon_exec_manifest()) is True
    assert ct.is_daemon_exec_manifest(ct.email_comm_manifest()) is False


# --- the per-action risk tables (the heart of stage 3D) ---------------------


def test_email_risk_table_covers_exactly_the_declared_actions():
    declared = set(ct.email_comm_manifest().metadata["actions"])
    assert set(ct.EMAIL_ACTION_RISK) == declared


def test_daemon_risk_table_covers_exactly_the_declared_actions():
    declared = set(ct.daemon_exec_manifest().metadata["actions"])
    assert set(ct.DAEMON_ACTION_RISK) == declared


def test_email_risk_grades():
    R = ct.EMAIL_ACTION_RISK
    # read-only inbox queries are SAFE.
    for a in ("check", "read", "search", "contacts"):
        assert R[a] is cap.SecurityDanger.SAFE
    # outbound sends can reach external recipients -> CAUTION.
    for a in ct.EMAIL_SEND_ACTIONS:
        assert R[a] is cap.SecurityDanger.CAUTION
    # contact / organize mutations -> CAUTION.
    for a in ("dismiss", "archive", "add_contact", "remove_contact", "edit_contact"):
        assert R[a] is cap.SecurityDanger.CAUTION
    # irreversible removal -> DESTRUCTIVE (the bundle-level posture).
    assert R["delete"] is cap.SecurityDanger.DESTRUCTIVE


def test_daemon_risk_grades():
    R = ct.DAEMON_ACTION_RISK
    # read-only status queries are SAFE.
    for a in ("list", "check"):
        assert R[a] is cap.SecurityDanger.SAFE
    # spawn / drive / kill child processes -> DESTRUCTIVE.
    for a in ct.DAEMON_PROCESS_ACTIONS:
        assert R[a] is cap.SecurityDanger.DESTRUCTIVE


def test_bundle_posture_is_strongest_action_grade():
    # the declared bundle danger must equal the strongest per-action grade, so a
    # single bundle posture never under-states any one action.
    assert (
        ct.email_comm_manifest().security.danger
        == max(
            (d.value for d in ct.EMAIL_ACTION_RISK.values()),
            key=lambda v: {"safe": 0, "caution": 1, "destructive": 2}[v],
        )
    )
    assert (
        ct.daemon_exec_manifest().security.danger
        == cap.SecurityDanger.DESTRUCTIVE.value
    )


def test_send_and_process_action_subsets_are_consistent():
    # the declared "reaches external" / "touches processes" subsets must be a
    # subset of the action space, and pin the highest-attention members.
    assert ct.EMAIL_SEND_ACTIONS <= set(ct.EMAIL_ACTION_RISK)
    assert ct.DAEMON_PROCESS_ACTIONS <= set(ct.DAEMON_ACTION_RISK)
    assert ct.EMAIL_SEND_ACTIONS == frozenset({"send", "reply", "reply_all"})
    assert ct.DAEMON_PROCESS_ACTIONS == frozenset({"emanate", "ask", "reclaim"})


def test_action_risk_helpers_fail_safe_on_unknown():
    # an action not in the table fails safe (destructive), never silently safe.
    assert ct.email_action_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    assert ct.daemon_action_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    # known actions return their graded posture.
    assert ct.email_action_risk("read") is cap.SecurityDanger.SAFE
    assert ct.email_action_risk("send") is cap.SecurityDanger.CAUTION
    assert ct.email_action_risk("delete") is cap.SecurityDanger.DESTRUCTIVE
    assert ct.daemon_action_risk("list") is cap.SecurityDanger.SAFE
    assert ct.daemon_action_risk("emanate") is cap.SecurityDanger.DESTRUCTIVE


# --- host seams: correct carrier per surface, injected dummy handlers --------


def test_email_host_is_native_authority():
    sentinel = object()
    h = ct.email_comm_host(lambda **kw: sentinel)
    assert isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "email"
    assert h.tools == ("email",)
    assert h.invoke("email") is sentinel


def test_daemon_host_is_non_native_in_process():
    sentinel = object()
    h = ct.daemon_exec_host(lambda **kw: sentinel)
    assert type(h) is host.BundleHost
    assert not isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "daemon"
    assert h.tools == ("daemon",)
    assert h.invoke("daemon") is sentinel


def test_non_native_host_refuses_email_bundle():
    # email is privileged + native -> BundleHost must refuse it.
    m = ct.email_comm_manifest()
    with pytest.raises(BundleHostError):
        host.BundleHost(m, {"email": lambda **kw: None})


def test_native_host_refuses_daemon_bundle():
    # daemon is in-process -> NativeBundleHost (native transport only) must refuse.
    m = ct.daemon_exec_manifest()
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(m, {"daemon": lambda **kw: None}, native_authority=True)


def test_hosts_require_callable_handlers():
    with pytest.raises(BundleHostError):
        ct.email_comm_host(object())
    with pytest.raises(BundleHostError):
        ct.daemon_exec_host(object())


def test_communication_tool_hosts_builds_both_with_correct_carriers():
    hosts = ct.communication_tool_hosts(
        {
            "email": lambda **kw: {"e": True},
            "daemon": lambda **kw: {"d": True},
        }
    )
    assert set(hosts) == {"email", "daemon"}
    assert isinstance(hosts["email"], host.NativeBundleHost)
    assert type(hosts["daemon"]) is host.BundleHost
    assert hosts["email"].invoke("email") == {"e": True}
    assert hosts["daemon"].invoke("daemon") == {"d": True}


def test_communication_tool_hosts_rejects_missing_handler():
    with pytest.raises(BundleHostError):
        ct.communication_tool_hosts({"email": lambda **kw: None})


def test_communication_tool_hosts_rejects_undeclared_handler():
    with pytest.raises(BundleHostError):
        ct.communication_tool_hosts(
            {
                "email": lambda **kw: None,
                "daemon": lambda **kw: None,
                "system": lambda **kw: None,
            }
        )


# --- guard/audit invariant: posture flows through the stage-17 guard bridge --


def test_guard_bridge_blocks_destructive_surfaces_in_blocking_mode():
    manifests = list(ct.communication_tool_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.BLOCKING)
    for tool, action in (("email", "delete"), ("daemon", "emanate")):
        decision = check(ToolProposal(tool_name=tool, tool_args={"action": action}))
        assert decision is not None, tool
        assert decision.allowed is False, tool
        assert decision.metadata.get("danger") == cap.SecurityDanger.DESTRUCTIVE.value
        assert decision.metadata.get("bundle") == tool


def test_guard_bridge_advisory_mode_warns_instead_of_denying():
    manifests = list(ct.communication_tool_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.ADVISORY)
    decision = check(ToolProposal(tool_name="daemon", tool_args={"action": "reclaim"}))
    assert decision is not None
    assert decision.allowed is True
    assert decision.action == "warn"


def test_guard_bridge_danger_index_reflects_posture():
    index = gb.tool_danger_index(list(ct.communication_tool_manifests()))
    assert index["email"] is cap.SecurityDanger.DESTRUCTIVE
    assert index["daemon"] is cap.SecurityDanger.DESTRUCTIVE


# --- import purity / no implementation migration ---------------------------


def test_communication_tools_import_is_pure_and_migrates_no_wrapper():
    code = (
        "import sys, lingtai_sdk.communication_tools as ct\n"
        "em = ct.email_comm_manifest()\n"
        "dm = ct.daemon_exec_manifest()\n"
        "assert em.name == 'email' and em.transport.kind == 'native'\n"
        "assert em.roles.privileged is True and em.roles.native_only is False\n"
        "assert dm.name == 'daemon' and dm.transport.kind == 'in_process'\n"
        "assert dm.roles.privileged is False\n"
        "eh = ct.email_comm_host(lambda **kw: 'e')\n"
        "dh = ct.daemon_exec_host(lambda **kw: 'd')\n"
        "assert eh.invoke('email') == 'e'\n"
        "assert dh.invoke('daemon') == 'd'\n"
        "assert ct.email_action_risk('delete').value == 'destructive'\n"
        "assert ct.daemon_action_risk('list').value == 'safe'\n"
        # importing communication_tools must NOT pull in the lingtai wrapper, i.e.
        # the real email/daemon implementation is not migrated/imported from the SDK.
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
