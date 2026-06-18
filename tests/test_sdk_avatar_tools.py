"""Stage-3I proof: the ``avatar_spawn`` / ``avatar_rules`` peer-spawn bundle
declarations + seams.

The independent-peer-spawning counterpart of ``test_sdk_bash_tools.py`` (stage 3H)
and the ``daemon`` half of ``test_sdk_communication_tools.py`` (stage 3D). Unlike
those single-tool-with-an-``action`` bundles, the avatar capability is **two
separate public tools**, each its own surface — so these tests assert the *pair*
shape (two manifests, per-tool risk) the way the email/daemon pair does. They
assert that:

* each avatar manifest is non-privileged + in-process (capability-carried),
  matching how the live ``setup()`` (``agent.add_tool``) path carries it, and
  declares the side-effecting / process-spawning (spawn) and descendant-distribution
  (rules) metadata posture;
* the manifests validate strictly and round-trip through ``load_manifest``;
* the **per-tool risk table** (``AVATAR_TOOL_RISK``) covers exactly the two declared
  tools, grades both DESTRUCTIVE, and the bundle-level posture of each manifest
  equals its tool's grade;
* the **per-args refinement** ``avatar_spawn_risk`` grades a ``dry_run=true`` spawn
  SAFE and everything else DESTRUCTIVE (fail-safe high);
* the host seams host each surface with its correct carrier — a non-native
  ``BundleHost`` — with an injected dummy handler, the native host refuses them, and
  the mapping seam enforces the exactly-two-handlers contract;
* the **guard/audit invariant** holds: feeding the DESTRUCTIVE manifests to the
  stage-17 ``guard_bridge`` denies in BLOCKING / warns in ADVISORY — *without* this
  stage installing any guard.

Crucially, **no real avatar is spawned or imported from the SDK**: every handler
here is a dummy, and a subprocess asserts importing ``avatar_tools`` pulls in no
``lingtai`` wrapper module. The wrapper-side bridge (which hosts the real handlers)
is tested in ``tests/test_avatar_bundle_bridge.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import avatar_tools as at
from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk import guard_bridge as gb
from lingtai_sdk.errors import BundleHostError

from lingtai_kernel.tool_call_guard import ToolProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- manifests: identity, posture, carrier -----------------------------------


def test_avatar_spawn_manifest_non_privileged_in_process():
    m = at.avatar_spawn_manifest()
    assert m.name == at.AVATAR_SPAWN_TOOL_NAME == "avatar_spawn"
    # avatar is a wrapper capability carried in-process (add_tool), not privileged.
    assert m.roles.required is False
    assert m.roles.privileged is False
    assert m.roles.native_only is False
    assert m.roles.backend_replaceability is cap.BackendReplaceability.REPLACEABLE
    assert m.transport.kind == cap.TransportKind.IN_PROCESS.value
    # independent detached peer-process spawn -> DESTRUCTIVE bundle posture.
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value
    assert m.surfaces.tools == ("avatar_spawn",)


def test_avatar_rules_manifest_non_privileged_in_process():
    m = at.avatar_rules_manifest()
    assert m.name == at.AVATAR_RULES_TOOL_NAME == "avatar_rules"
    assert m.roles.required is False
    assert m.roles.privileged is False
    assert m.roles.native_only is False
    assert m.roles.backend_replaceability is cap.BackendReplaceability.REPLACEABLE
    assert m.transport.kind == cap.TransportKind.IN_PROCESS.value
    # network-wide rules mutation -> DESTRUCTIVE bundle posture.
    assert m.security.danger == cap.SecurityDanger.DESTRUCTIVE.value
    assert m.surfaces.tools == ("avatar_rules",)


def test_avatar_spawn_manifest_declares_spawn_metadata():
    md = at.avatar_spawn_manifest().metadata
    assert md["execution"] is True
    assert md["side_effect"] is True
    assert md["process_spawning"] is True
    assert md["spawns_independent_peer"] is True
    assert md["supports_dry_run"] is True
    # a language-neutral copy of the live spawn schema.
    assert md["schema"]["required"] == ["name"]
    assert md["schema"]["properties"]["type"]["enum"] == ["shallow", "deep"]


def test_avatar_rules_manifest_declares_distribution_metadata():
    md = at.avatar_rules_manifest().metadata
    assert md["execution"] is True
    assert md["side_effect"] is True
    assert md["distributes_to_descendants"] is True
    assert md["admin_gated"] is True
    assert md["schema"]["required"] == ["rules_content"]


def test_manifests_validate_and_round_trip():
    for original in at.avatar_tool_manifests():
        original.validate()  # does not raise
        loaded = cap.load_manifest(original.to_dict())
        assert loaded.to_dict() == original.to_dict()


def test_manifest_helpers_and_names():
    assert at.avatar_tool_names() == ("avatar_spawn", "avatar_rules")
    manifests = at.avatar_tool_manifests()
    assert [m.name for m in manifests] == ["avatar_spawn", "avatar_rules"]
    assert at.is_avatar_spawn_manifest(at.avatar_spawn_manifest()) is True
    assert at.is_avatar_rules_manifest(at.avatar_rules_manifest()) is True
    # cross-checks: each predicate rejects the other bundle.
    assert at.is_avatar_spawn_manifest(at.avatar_rules_manifest()) is False
    assert at.is_avatar_rules_manifest(at.avatar_spawn_manifest()) is False
    # the umbrella predicate accepts both.
    assert at.is_avatar_manifest(at.avatar_spawn_manifest()) is True
    assert at.is_avatar_manifest(at.avatar_rules_manifest()) is True


# --- the per-tool risk table -------------------------------------------------


def test_avatar_risk_table_covers_exactly_the_declared_tools():
    declared = set(at.avatar_tool_names())
    assert set(at.AVATAR_TOOL_RISK) == declared == {"avatar_spawn", "avatar_rules"}


def test_avatar_risk_grades():
    R = at.AVATAR_TOOL_RISK
    # independent peer spawn + network-wide rules mutation are both highest danger.
    assert R["avatar_spawn"] is cap.SecurityDanger.DESTRUCTIVE
    assert R["avatar_rules"] is cap.SecurityDanger.DESTRUCTIVE


def test_avatar_side_effect_tools_is_full_set():
    assert at.AVATAR_SIDE_EFFECT_TOOLS == frozenset({"avatar_spawn", "avatar_rules"})
    assert at.AVATAR_SIDE_EFFECT_TOOLS <= set(at.AVATAR_TOOL_RISK)


def test_bundle_posture_equals_its_tool_grade():
    assert (
        at.avatar_spawn_manifest().security.danger
        == at.AVATAR_TOOL_RISK["avatar_spawn"].value
        == cap.SecurityDanger.DESTRUCTIVE.value
    )
    assert (
        at.avatar_rules_manifest().security.danger
        == at.AVATAR_TOOL_RISK["avatar_rules"].value
        == cap.SecurityDanger.DESTRUCTIVE.value
    )


def test_tool_risk_helper_fails_safe_high_on_unknown():
    # an unknown tool fails safe HIGH (destructive), matching the other stage-3
    # risk helpers rather than silently treating it as safe.
    assert at.avatar_tool_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    # the known tools return their graded posture.
    assert at.avatar_tool_risk("avatar_spawn") is cap.SecurityDanger.DESTRUCTIVE
    assert at.avatar_tool_risk("avatar_rules") is cap.SecurityDanger.DESTRUCTIVE


def test_spawn_per_args_risk_dry_run_is_safe():
    # a dry_run preview performs no mutation -> SAFE.
    assert at.avatar_spawn_risk({"dry_run": True}) is cap.SecurityDanger.SAFE
    assert (
        at.avatar_spawn_risk({"name": "x", "dry_run": True})
        is cap.SecurityDanger.SAFE
    )


def test_spawn_per_args_risk_fails_safe_high_otherwise():
    # everything that is not an explicit dry_run=True grades DESTRUCTIVE.
    assert at.avatar_spawn_risk(None) is cap.SecurityDanger.DESTRUCTIVE
    assert at.avatar_spawn_risk({}) is cap.SecurityDanger.DESTRUCTIVE
    assert at.avatar_spawn_risk({"name": "x"}) is cap.SecurityDanger.DESTRUCTIVE
    assert (
        at.avatar_spawn_risk({"dry_run": False}) is cap.SecurityDanger.DESTRUCTIVE
    )
    # a non-bool / truthy-but-not-True dry_run is NOT treated as a safe preview.
    assert (
        at.avatar_spawn_risk({"dry_run": "true"}) is cap.SecurityDanger.DESTRUCTIVE
    )
    assert at.avatar_spawn_risk({"dry_run": 1}) is cap.SecurityDanger.DESTRUCTIVE


# --- host seams: correct carrier, injected dummy handler ----------------------


def test_avatar_spawn_host_is_non_native_in_process():
    sentinel = object()
    h = at.avatar_spawn_host(lambda **kw: sentinel)
    assert type(h) is host.BundleHost
    assert not isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "avatar_spawn"
    assert h.tools == ("avatar_spawn",)
    assert h.invoke("avatar_spawn", name="x") is sentinel


def test_avatar_rules_host_is_non_native_in_process():
    sentinel = object()
    h = at.avatar_rules_host(lambda **kw: sentinel)
    assert type(h) is host.BundleHost
    assert not isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "avatar_rules"
    assert h.tools == ("avatar_rules",)
    assert h.invoke("avatar_rules", rules_content="x") is sentinel


def test_native_host_refuses_avatar_bundles():
    # avatar tools are in-process -> NativeBundleHost (native transport only) refuses.
    for m, name in (
        (at.avatar_spawn_manifest(), "avatar_spawn"),
        (at.avatar_rules_manifest(), "avatar_rules"),
    ):
        with pytest.raises(BundleHostError):
            host.NativeBundleHost(m, {name: lambda **kw: None}, native_authority=True)


def test_hosts_require_callable_handler():
    with pytest.raises(BundleHostError):
        at.avatar_spawn_host(object())
    with pytest.raises(BundleHostError):
        at.avatar_rules_host(object())


def test_avatar_tool_hosts_builds_with_correct_carrier():
    hosts = at.avatar_tool_hosts(
        {
            "avatar_spawn": lambda **kw: {"s": True},
            "avatar_rules": lambda **kw: {"r": True},
        }
    )
    assert set(hosts) == {"avatar_spawn", "avatar_rules"}
    assert type(hosts["avatar_spawn"]) is host.BundleHost
    assert type(hosts["avatar_rules"]) is host.BundleHost
    assert hosts["avatar_spawn"].invoke("avatar_spawn", name="x") == {"s": True}
    assert hosts["avatar_rules"].invoke("avatar_rules", rules_content="x") == {
        "r": True
    }


def test_avatar_tool_hosts_rejects_missing_handler():
    with pytest.raises(BundleHostError):
        at.avatar_tool_hosts({"avatar_spawn": lambda **kw: None})  # rules missing


def test_avatar_tool_hosts_rejects_undeclared_handler():
    with pytest.raises(BundleHostError):
        at.avatar_tool_hosts(
            {
                "avatar_spawn": lambda **kw: None,
                "avatar_rules": lambda **kw: None,
                "daemon": lambda **kw: None,
            }
        )


# --- guard/audit invariant: DESTRUCTIVE posture denies / warns ----------------


def test_guard_bridge_blocks_avatar_in_blocking_mode():
    manifests = list(at.avatar_tool_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.BLOCKING)
    for tool, args in (
        ("avatar_spawn", {"name": "researcher"}),
        ("avatar_rules", {"rules_content": "be concise"}),
    ):
        decision = check(ToolProposal(tool_name=tool, tool_args=args))
        assert decision is not None
        assert decision.allowed is False
        assert decision.metadata.get("danger") == cap.SecurityDanger.DESTRUCTIVE.value
        assert decision.metadata.get("bundle") == tool


def test_guard_bridge_advisory_mode_warns_instead_of_denying():
    manifests = list(at.avatar_tool_manifests())
    check = gb.guard_check_from_manifests(manifests, mode=gb.GuardPolicyMode.ADVISORY)
    decision = check(
        ToolProposal(tool_name="avatar_spawn", tool_args={"name": "x"})
    )
    assert decision is not None
    assert decision.allowed is True
    assert decision.action == "warn"


def test_guard_bridge_danger_index_reflects_posture():
    index = gb.tool_danger_index(list(at.avatar_tool_manifests()))
    assert index["avatar_spawn"] is cap.SecurityDanger.DESTRUCTIVE
    assert index["avatar_rules"] is cap.SecurityDanger.DESTRUCTIVE


# --- import purity / no implementation migration ----------------------------


def test_avatar_tools_import_is_pure_and_migrates_no_wrapper():
    code = (
        "import sys, lingtai_sdk.avatar_tools as at\n"
        "ms = at.avatar_tool_manifests()\n"
        "assert [m.name for m in ms] == ['avatar_spawn', 'avatar_rules']\n"
        "for m in ms:\n"
        "    assert m.transport.kind == 'in_process'\n"
        "    assert m.roles.privileged is False and m.roles.native_only is False\n"
        "    assert m.security.danger == 'destructive'\n"
        "hs = at.avatar_spawn_host(lambda **kw: 's')\n"
        "assert hs.invoke('avatar_spawn', name='x') == 's'\n"
        "hr = at.avatar_rules_host(lambda **kw: 'r')\n"
        "assert hr.invoke('avatar_rules', rules_content='x') == 'r'\n"
        "assert at.avatar_tool_risk('avatar_spawn').value == 'destructive'\n"
        "assert at.avatar_tool_risk('nope').value == 'destructive'\n"
        "assert at.avatar_spawn_risk({'dry_run': True}).value == 'safe'\n"
        "assert at.avatar_spawn_risk({}).value == 'destructive'\n"
        # importing avatar_tools must NOT pull in the lingtai wrapper, i.e. the real
        # avatar implementation is not migrated/imported from the SDK.
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
