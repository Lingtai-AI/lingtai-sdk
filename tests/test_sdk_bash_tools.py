"""Stage-3H proof: the ``bash`` shell-execution bundle declaration + seam.

The arbitrary-shell-execution counterpart of ``test_sdk_communication_tools.py``
(the ``daemon`` half, stage 3D). These tests assert that:

* the ``bash`` manifest is non-privileged + in-process (capability-carried),
  matching how the live ``setup()`` (``agent.add_tool``) path carries it, and
  declares the side-effecting / shell / process-spawning metadata posture;
* the manifest validates strictly and round-trips through ``load_manifest``;
* the **per-action risk table** (``BASH_ACTION_RISK``) covers exactly the declared
  actions (``run`` / ``poll`` / ``cancel``), grades them faithfully (``run`` /
  ``cancel`` → DESTRUCTIVE, ``poll`` → CAUTION), and the bundle-level posture equals
  the strongest action's grade (DESTRUCTIVE);
* the host seam hosts the surface with its correct carrier — a non-native
  ``BundleHost`` — with an injected dummy handler, and the native host refuses it;
* the **guard/audit invariant** holds: feeding the DESTRUCTIVE manifest to the
  stage-17 ``guard_bridge`` denies in BLOCKING / warns in ADVISORY — *without* this
  stage installing any guard.

Crucially, **no real ``bash`` is called or imported from the SDK**: every handler
here is a dummy, and a subprocess asserts importing ``bash_tools`` pulls in no
``lingtai`` wrapper module. The wrapper-side bridge (which hosts the real handler)
is tested in ``tests/test_bash_bundle_bridge.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import bash_tools as bt
from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk import guard_bridge as gb
from lingtai_sdk.errors import BundleHostError

from lingtai_kernel.tool_call_guard import ToolProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- manifest: identity, posture, carrier -----------------------------------


def test_bash_manifest_non_privileged_in_process():
    m = bt.bash_exec_manifest()
    assert m.name == bt.BASH_TOOL_NAME == "bash"
    # bash is a wrapper capability carried in-process (add_tool), not privileged.
    assert m.roles.required is False
    assert m.roles.privileged is False
    assert m.roles.native_only is False
    assert m.roles.backend_replaceability is cap.BackendReplaceability.REPLACEABLE
    assert m.transport.kind == cap.TransportKind.IN_PROCESS.value
    # arbitrary shell execution -> DESTRUCTIVE bundle posture.
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value
    assert m.surfaces.tools == ("bash",)


def test_bash_manifest_declares_execution_metadata():
    md = bt.bash_exec_manifest().metadata
    assert md["execution"] is True
    assert md["side_effect"] is True
    assert md["shell"] is True
    assert md["arbitrary_command"] is True
    assert md["process_spawning"] is True
    assert md["manages_async_jobs"] is True
    assert md["actions"] == ["run", "poll", "cancel"]
    # a language-neutral copy of the live schema's action enum.
    assert md["schema"]["properties"]["action"]["enum"] == ["run", "poll", "cancel"]
    # the top-level schema requires nothing (per-action requirements are the
    # handler's concern), matching the live bash schema.
    assert md["schema"]["required"] == []


def test_manifest_validates_and_round_trips():
    original = bt.bash_exec_manifest()
    original.validate()  # does not raise
    loaded = cap.load_manifest(original.to_dict())
    assert loaded.to_dict() == original.to_dict()


def test_manifest_helpers_and_names():
    assert bt.bash_exec_names() == ("bash",)
    manifests = bt.bash_exec_manifests()
    assert [m.name for m in manifests] == ["bash"]
    assert bt.is_bash_exec_manifest(bt.bash_exec_manifest()) is True


# --- the per-action risk table ----------------------------------------------


def test_bash_risk_table_covers_exactly_the_declared_actions():
    declared = set(bt.bash_exec_manifest().metadata["actions"])
    assert set(bt.BASH_ACTION_RISK) == declared


def test_bash_risk_grades():
    R = bt.BASH_ACTION_RISK
    # arbitrary shell execution + process kill are the highest danger.
    assert R["run"] is cap.SecurityDanger.DESTRUCTIVE
    assert R["cancel"] is cap.SecurityDanger.DESTRUCTIVE
    # poll is mostly read-only but cleans up the job dir on completion.
    assert R["poll"] is cap.SecurityDanger.CAUTION


def test_bash_process_actions_subset():
    # the actions that execute commands or kill processes.
    assert bt.BASH_PROCESS_ACTIONS == frozenset({"run", "cancel"})
    assert bt.BASH_PROCESS_ACTIONS <= set(bt.BASH_ACTION_RISK)


def test_bundle_posture_is_strongest_action_grade():
    assert (
        bt.bash_exec_manifest().security.danger
        == max(
            (d.value for d in bt.BASH_ACTION_RISK.values()),
            key=lambda v: {"safe": 0, "caution": 1, "destructive": 2}[v],
        )
    )
    assert (
        bt.bash_exec_manifest().security.danger
        == cap.SecurityDanger.DESTRUCTIVE.value
    )


def test_action_risk_helper_fails_safe_high_on_unknown():
    # an unknown action fails safe HIGH (destructive), matching the other stage-3
    # action-risk helpers rather than silently treating it as safe.
    assert bt.bash_action_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    # the known actions return their graded posture.
    assert bt.bash_action_risk("run") is cap.SecurityDanger.DESTRUCTIVE
    assert bt.bash_action_risk("poll") is cap.SecurityDanger.CAUTION
    assert bt.bash_action_risk("cancel") is cap.SecurityDanger.DESTRUCTIVE


# --- host seam: correct carrier, injected dummy handler ----------------------


def test_bash_host_is_non_native_in_process():
    sentinel = object()
    h = bt.bash_exec_host(lambda **kw: sentinel)
    assert type(h) is host.BundleHost
    assert not isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "bash"
    assert h.tools == ("bash",)
    assert h.invoke("bash") is sentinel


def test_native_host_refuses_bash_bundle():
    # bash is in-process -> NativeBundleHost (native transport only) must refuse.
    m = bt.bash_exec_manifest()
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(m, {"bash": lambda **kw: None}, native_authority=True)


def test_host_requires_callable_handler():
    with pytest.raises(BundleHostError):
        bt.bash_exec_host(object())


def test_bash_exec_hosts_builds_with_correct_carrier():
    hosts = bt.bash_exec_hosts({"bash": lambda **kw: {"b": True}})
    assert set(hosts) == {"bash"}
    assert type(hosts["bash"]) is host.BundleHost
    assert hosts["bash"].invoke("bash") == {"b": True}


def test_bash_exec_hosts_rejects_missing_handler():
    with pytest.raises(BundleHostError):
        bt.bash_exec_hosts({})


def test_bash_exec_hosts_rejects_undeclared_handler():
    with pytest.raises(BundleHostError):
        bt.bash_exec_hosts(
            {
                "bash": lambda **kw: None,
                "daemon": lambda **kw: None,
            }
        )


# --- guard/audit invariant: DESTRUCTIVE posture denies / warns ----------------


def test_guard_bridge_blocks_bash_in_blocking_mode():
    manifests = list(bt.bash_exec_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.BLOCKING)
    decision = check(
        ToolProposal(tool_name="bash", tool_args={"action": "run", "command": "ls"})
    )
    assert decision is not None
    assert decision.allowed is False
    assert decision.metadata.get("danger") == cap.SecurityDanger.DESTRUCTIVE.value
    assert decision.metadata.get("bundle") == "bash"


def test_guard_bridge_advisory_mode_warns_instead_of_denying():
    manifests = list(bt.bash_exec_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.ADVISORY)
    decision = check(ToolProposal(tool_name="bash", tool_args={"action": "run"}))
    assert decision is not None
    assert decision.allowed is True
    assert decision.action == "warn"


def test_guard_bridge_danger_index_reflects_posture():
    index = gb.tool_danger_index(list(bt.bash_exec_manifests()))
    assert index["bash"] is cap.SecurityDanger.DESTRUCTIVE


# --- import purity / no implementation migration ----------------------------


def test_bash_tools_import_is_pure_and_migrates_no_wrapper():
    code = (
        "import sys, lingtai_sdk.bash_tools as bt\n"
        "m = bt.bash_exec_manifest()\n"
        "assert m.name == 'bash' and m.transport.kind == 'in_process'\n"
        "assert m.roles.privileged is False and m.roles.native_only is False\n"
        "assert m.security.danger == 'destructive'\n"
        "h = bt.bash_exec_host(lambda **kw: 'b')\n"
        "assert h.invoke('bash') == 'b'\n"
        "assert bt.bash_action_risk('run').value == 'destructive'\n"
        "assert bt.bash_action_risk('poll').value == 'caution'\n"
        "assert bt.bash_action_risk('nope').value == 'destructive'\n"
        # importing bash_tools must NOT pull in the lingtai wrapper, i.e. the real
        # bash implementation is not migrated/imported from the SDK.
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
