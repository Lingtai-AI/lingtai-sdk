"""Stage-3C proof: the high-state ``system`` lifecycle bundle declaration + seam.

The high-state counterpart of ``test_sdk_file_tools.py`` (stage 3A, read-only) and
``test_sdk_file_mutation_tools.py`` (stage 3B, side-effecting). These tests assert
that:

* the ``system`` lifecycle manifest is the *same* privileged, native-only,
  ``destructive`` core bundle from ``core_bundles`` (single source of truth — not
  a fork), validates strictly, and round-trips through ``load_manifest``;
* the **per-action risk table** (``SYSTEM_ACTION_RISK``) faithfully grades each
  action — self/normal lifecycle vs karma vs nirvana — and its grading mirrors the
  authority the real kernel intrinsic enforces in code
  (``intrinsics.system.karma._KARMA_ACTIONS`` / ``_NIRVANA_ACTIONS``);
* the native host seam hosts the ``system`` bundle alone (without ``psyche`` /
  ``soul``) with an injected handler, as a ``NativeBundleHost`` (native
  authority), and the non-native ``BundleHost`` refuses it;
* the **guard/audit invariant** holds: feeding the ``system`` manifest to the
  stage-17 ``guard_bridge`` denies ``system`` in BLOCKING / warns in ADVISORY —
  *without* this stage installing any guard.

Crucially, **no real ``system`` is called or imported from the SDK**: every
handler here is a dummy, and a subprocess asserts importing ``lifecycle_tools``
pulls in no ``lingtai`` wrapper module. The wrapper-side bridge (which hosts the
real kernel intrinsic) is tested in ``tests/test_system_bundle_bridge.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk import core_bundles as core
from lingtai_sdk import guard_bridge as gb
from lingtai_sdk import lifecycle_tools as lt
from lingtai_sdk.errors import BundleHostError

# The guard bridge maps a manifest's danger posture onto kernel guard
# primitives; ToolProposal is the kernel-side type the resulting check consumes.
from lingtai_kernel.tool_call_guard import ToolProposal

# The real kernel intrinsic action sets — the source the SDK risk table mirrors.
# Importing the kernel intrinsic here is allowed (kernel, not wrapper) and lets
# the test pin the declaration against the live authority gate.
from lingtai_kernel.intrinsics.system.karma import _KARMA_ACTIONS, _NIRVANA_ACTIONS

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- the manifest is the single-source-of-truth core ``system`` bundle ------


def test_lifecycle_manifest_is_the_core_system_bundle():
    m = lt.lifecycle_system_manifest()
    # identical to the core-bundle declaration — not a fork.
    assert m.to_dict() == core.system_bundle().to_dict()
    assert m.name == lt.SYSTEM_TOOL_NAME == "system"


def test_lifecycle_manifest_privileged_native_destructive():
    m = lt.lifecycle_system_manifest()
    assert m.roles.required is True
    assert m.roles.privileged is True
    assert m.roles.native_only is True
    assert m.roles.backend_replaceability is cap.BackendReplaceability.NATIVE_ONLY
    assert m.transport.kind == cap.TransportKind.NATIVE.value
    # bundle-level posture is the strongest action's grade.
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value


def test_lifecycle_manifest_declares_only_system_tool():
    m = lt.lifecycle_system_manifest()
    assert m.surfaces.tools == ("system",)
    assert m.surfaces.resources == ()
    assert m.surfaces.prompts == ()
    assert m.surfaces.events == ()
    assert m.surfaces.hooks == ()


def test_lifecycle_manifest_validates_and_round_trips():
    original = lt.lifecycle_system_manifest()
    original.validate()  # does not raise
    loaded = cap.load_manifest(original.to_dict())
    assert loaded.to_dict() == original.to_dict()
    assert loaded.roles.privileged is True
    assert loaded.roles.native_only is True
    assert loaded.security.danger == cap.SecurityDanger.DESTRUCTIVE.value


def test_is_lifecycle_system_manifest():
    assert lt.is_lifecycle_system_manifest(lt.lifecycle_system_manifest()) is True
    # the other core bundles are NOT the system lifecycle bundle.
    assert lt.is_lifecycle_system_manifest(core.psyche_bundle()) is False
    assert lt.is_lifecycle_system_manifest(core.soul_bundle()) is False
    assert lt.is_lifecycle_system_manifest(cap.proof_bundle()) is False


# --- the per-action risk table (the heart of stage 3C) ---------------------


def test_action_risk_table_covers_exactly_the_declared_actions():
    declared = set(lt.lifecycle_system_manifest().metadata["actions"])
    assert set(lt.SYSTEM_ACTION_RISK) == declared


def test_action_risk_grades_self_caution_or_safe():
    # self/normal lifecycle actions are never destructive.
    for action in lt.SELF_ACTIONS:
        grade = lt.SYSTEM_ACTION_RISK[action]
        assert grade in (cap.SecurityDanger.SAFE, cap.SecurityDanger.CAUTION)
    # explicit spot-checks of the intended grading.
    assert lt.SYSTEM_ACTION_RISK["sleep"] is cap.SecurityDanger.CAUTION
    assert lt.SYSTEM_ACTION_RISK["refresh"] is cap.SecurityDanger.CAUTION
    assert lt.SYSTEM_ACTION_RISK["presets"] is cap.SecurityDanger.SAFE
    assert lt.SYSTEM_ACTION_RISK["notification"] is cap.SecurityDanger.SAFE


def test_action_risk_grades_karma_and_nirvana_destructive():
    for action in lt.KARMA_ACTIONS | lt.NIRVANA_ACTIONS:
        assert lt.SYSTEM_ACTION_RISK[action] is cap.SecurityDanger.DESTRUCTIVE


def test_action_risk_sets_mirror_the_kernel_authority_gate():
    # the SDK declaration must match what the kernel intrinsic enforces in code,
    # so the manifest never drifts from the live karma/nirvana gate.
    assert set(lt.KARMA_ACTIONS) == set(_KARMA_ACTIONS)
    assert set(lt.NIRVANA_ACTIONS) == set(_NIRVANA_ACTIONS)
    # and the three families partition the action space, no overlap.
    assert lt.KARMA_ACTIONS.isdisjoint(lt.NIRVANA_ACTIONS)
    assert lt.SELF_ACTIONS.isdisjoint(lt.KARMA_ACTIONS)
    assert lt.SELF_ACTIONS.isdisjoint(lt.NIRVANA_ACTIONS)
    assert (
        lt.SELF_ACTIONS | lt.KARMA_ACTIONS | lt.NIRVANA_ACTIONS
        == set(lt.SYSTEM_ACTION_RISK)
    )


def test_action_risk_unknown_action_is_conservatively_destructive():
    # an action not in the table fails safe (destructive), never silently safe.
    assert lt.action_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    # a known action returns its graded posture.
    assert lt.action_risk("sleep") is cap.SecurityDanger.CAUTION
    assert lt.action_risk("nirvana") is cap.SecurityDanger.DESTRUCTIVE


# --- non-native BundleHost refuses; native seam hosts with injected handler --


def test_bundle_host_refuses_system_lifecycle_bundle():
    m = lt.lifecycle_system_manifest()
    with pytest.raises(BundleHostError):
        host.BundleHost(m, {"system": lambda **kw: None})


def test_system_lifecycle_host_with_injected_dummy():
    sentinel = object()
    h = lt.system_lifecycle_host(lambda **kw: sentinel)
    assert isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "system"
    assert h.tools == ("system",)
    # the host invokes the *injected* dummy, never a real implementation.
    assert h.invoke("system") is sentinel


def test_system_lifecycle_host_requires_callable_handler():
    with pytest.raises(BundleHostError):
        lt.system_lifecycle_host(object())  # not callable


def test_system_lifecycle_host_is_native_authority_not_in_process():
    h = lt.system_lifecycle_host(lambda **kw: None)
    assert type(h) is host.NativeBundleHost
    assert not isinstance(h, host.BundleHost)


def test_system_lifecycle_hosts_builds_system_only():
    hosts = lt.system_lifecycle_hosts({"system": lambda **kw: {"ok": True}})
    assert set(hosts) == {"system"}
    h = hosts["system"]
    assert isinstance(h, host.NativeBundleHost)
    assert h.manifest.roles.privileged is True
    assert h.manifest.transport.kind == "native"
    assert h.invoke("system") == {"ok": True}


def test_system_lifecycle_hosts_rejects_missing_handler():
    with pytest.raises(BundleHostError):
        lt.system_lifecycle_hosts({})


def test_system_lifecycle_hosts_rejects_undeclared_handler():
    with pytest.raises(BundleHostError):
        lt.system_lifecycle_hosts(
            {"system": lambda **kw: None, "psyche": lambda **kw: None}
        )


def test_system_lifecycle_hosts_rejects_non_callable_handler():
    with pytest.raises(BundleHostError):
        lt.system_lifecycle_hosts({"system": object()})


# --- guard/audit invariant: posture flows through the stage-17 guard bridge --


def test_guard_bridge_blocks_system_in_blocking_mode():
    manifests = [lt.lifecycle_system_manifest()]
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.BLOCKING)
    decision = check(ToolProposal(tool_name="system", tool_args={"action": "nirvana"}))
    # system is destructive -> BLOCKING denies it before dispatch.
    assert decision is not None
    assert decision.allowed is False
    assert decision.metadata.get("danger") == cap.SecurityDanger.DESTRUCTIVE.value
    assert decision.metadata.get("bundle") == "system"


def test_guard_bridge_advisory_mode_warns_system_instead_of_denying():
    manifests = [lt.lifecycle_system_manifest()]
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.ADVISORY)
    decision = check(ToolProposal(tool_name="system", tool_args={"action": "sleep"}))
    # destructive in ADVISORY mode -> allowed but warned, never denied.
    assert decision is not None
    assert decision.allowed is True
    assert decision.action == "warn"


def test_guard_bridge_danger_index_reflects_system_posture():
    index = gb.tool_danger_index([lt.lifecycle_system_manifest()])
    assert index["system"] is cap.SecurityDanger.DESTRUCTIVE


# --- import purity / no implementation migration ---------------------------


def test_lifecycle_tools_import_is_pure_and_migrates_no_wrapper():
    code = (
        "import sys, lingtai_sdk.lifecycle_tools as lt\n"
        "m = lt.lifecycle_system_manifest()\n"
        "assert m.name == 'system'\n"
        "assert m.security.danger == 'destructive'\n"
        "assert m.roles.privileged is True\n"
        "h = lt.system_lifecycle_host(lambda **kw: 'dummy')\n"
        "assert h.invoke('system') == 'dummy'\n"
        "assert lt.action_risk('sleep').value == 'caution'\n"
        "assert lt.action_risk('nirvana').value == 'destructive'\n"
        # importing lifecycle_tools must NOT pull in the lingtai wrapper, i.e.
        # the real system implementation is not migrated/imported from the SDK.
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
