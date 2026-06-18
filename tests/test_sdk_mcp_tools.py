"""Stage-3F proof: the ``mcp`` tool-config/catalog bundle declaration + seam.

The tool-config/catalog counterpart of ``test_sdk_communication_tools.py`` (stage
3D). These tests assert that:

* the ``mcp`` manifest is non-privileged + in-process (capability-carried),
  matching how the live ``setup()`` (``agent.add_tool``) path carries it, and
  declares the read-only / configuration / catalog metadata posture;
* the manifest validates strictly and round-trips through ``load_manifest``;
* the **per-action risk table** (``MCP_ACTION_RISK``) covers exactly the declared
  actions (``show`` only), grades them faithfully (``show`` → SAFE), and the
  bundle-level posture equals the strongest action's grade (SAFE);
* the host seam hosts the surface with its correct carrier — a non-native
  ``BundleHost`` — with an injected dummy handler, and the native host refuses it;
* the **guard/audit invariant** holds: feeding the SAFE manifest to the stage-17
  ``guard_bridge`` is a clean pass-through (no deny, no warn) — *without* this
  stage installing any guard.

Crucially, **no real ``mcp`` is called or imported from the SDK**: every handler
here is a dummy, and a subprocess asserts importing ``mcp_tools`` pulls in no
``lingtai`` wrapper module. The wrapper-side bridge (which hosts the real handler)
is tested in ``tests/test_mcp_bundle_bridge.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk import guard_bridge as gb
from lingtai_sdk import mcp_tools as mt
from lingtai_sdk.errors import BundleHostError

# The guard bridge maps a manifest's danger posture onto kernel guard
# primitives; ToolProposal is the kernel-side type the resulting check consumes.
from lingtai_kernel.tool_call_guard import ToolProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- manifest: identity, posture, carrier -----------------------------------


def test_mcp_manifest_non_privileged_in_process():
    m = mt.mcp_config_manifest()
    assert m.name == mt.MCP_TOOL_NAME == "mcp"
    # mcp is a wrapper capability carried in-process (add_tool), not privileged.
    assert m.roles.required is False
    assert m.roles.privileged is False
    assert m.roles.native_only is False
    assert m.roles.backend_replaceability is cap.BackendReplaceability.REPLACEABLE
    assert m.transport.kind == cap.TransportKind.IN_PROCESS.value
    # read-only registry view -> SAFE bundle posture.
    assert m.security.danger == cap.SecurityDanger.SAFE.value
    assert m.surfaces.tools == ("mcp",)


def test_mcp_manifest_declares_config_catalog_metadata():
    md = mt.mcp_config_manifest().metadata
    assert md["config"] is True
    assert md["catalog"] is True
    assert md["read_only"] is True
    assert md["agent_state_sensitive"] is True
    assert md["actions"] == ["show"]
    # a language-neutral copy of the live schema's action enum.
    assert md["schema"]["properties"]["action"]["enum"] == ["show"]


def test_manifest_validates_and_round_trips():
    original = mt.mcp_config_manifest()
    original.validate()  # does not raise
    loaded = cap.load_manifest(original.to_dict())
    assert loaded.to_dict() == original.to_dict()


def test_manifest_helpers_and_names():
    assert mt.mcp_config_names() == ("mcp",)
    manifests = mt.mcp_config_manifests()
    assert [m.name for m in manifests] == ["mcp"]
    assert mt.is_mcp_config_manifest(mt.mcp_config_manifest()) is True


# --- the per-action risk table ----------------------------------------------


def test_mcp_risk_table_covers_exactly_the_declared_actions():
    declared = set(mt.mcp_config_manifest().metadata["actions"])
    assert set(mt.MCP_ACTION_RISK) == declared


def test_mcp_risk_grades():
    R = mt.MCP_ACTION_RISK
    # the one read-only registry-view action is SAFE.
    assert R["show"] is cap.SecurityDanger.SAFE


def test_bundle_posture_is_strongest_action_grade():
    # the declared bundle danger must equal the strongest per-action grade.
    assert (
        mt.mcp_config_manifest().security.danger
        == max(
            (d.value for d in mt.MCP_ACTION_RISK.values()),
            key=lambda v: {"safe": 0, "caution": 1, "destructive": 2}[v],
        )
    )
    assert mt.mcp_config_manifest().security.danger == cap.SecurityDanger.SAFE.value


def test_action_risk_helper_fails_safe_high_on_unknown():
    # an unknown action fails safe HIGH (destructive), matching the other
    # stage-3 action-risk helpers rather than silently treating it as safe.
    assert mt.mcp_action_risk("totally-unknown") is cap.SecurityDanger.DESTRUCTIVE
    # the known action returns its graded posture.
    assert mt.mcp_action_risk("show") is cap.SecurityDanger.SAFE


# --- host seam: correct carrier, injected dummy handler ----------------------


def test_mcp_host_is_non_native_in_process():
    sentinel = object()
    h = mt.mcp_config_host(lambda **kw: sentinel)
    assert type(h) is host.BundleHost
    assert not isinstance(h, host.NativeBundleHost)
    assert h.manifest.name == "mcp"
    assert h.tools == ("mcp",)
    assert h.invoke("mcp") is sentinel


def test_native_host_refuses_mcp_bundle():
    # mcp is in-process -> NativeBundleHost (native transport only) must refuse.
    m = mt.mcp_config_manifest()
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(m, {"mcp": lambda **kw: None}, native_authority=True)


def test_host_requires_callable_handler():
    with pytest.raises(BundleHostError):
        mt.mcp_config_host(object())


def test_mcp_config_hosts_builds_with_correct_carrier():
    hosts = mt.mcp_config_hosts({"mcp": lambda **kw: {"m": True}})
    assert set(hosts) == {"mcp"}
    assert type(hosts["mcp"]) is host.BundleHost
    assert hosts["mcp"].invoke("mcp") == {"m": True}


def test_mcp_config_hosts_rejects_missing_handler():
    with pytest.raises(BundleHostError):
        mt.mcp_config_hosts({})


def test_mcp_config_hosts_rejects_undeclared_handler():
    with pytest.raises(BundleHostError):
        mt.mcp_config_hosts(
            {
                "mcp": lambda **kw: None,
                "system": lambda **kw: None,
            }
        )


# --- guard/audit invariant: SAFE posture is a clean pass-through -------------


def test_guard_bridge_safe_surface_is_clean_pass_through():
    manifests = list(mt.mcp_config_manifests())
    for mode in (gb.GuardPolicyMode.BLOCKING, gb.GuardPolicyMode.ADVISORY):
        check = gb.guard_check_from_manifests(manifests, mode=mode)
        decision = check(ToolProposal(tool_name="mcp", tool_args={"action": "show"}))
        # a SAFE bundle is a clean pass-through (no deny, no advisory) in either mode.
        assert decision is None, mode


def test_guard_bridge_danger_index_reflects_safe_posture():
    index = gb.tool_danger_index(list(mt.mcp_config_manifests()))
    assert index["mcp"] is cap.SecurityDanger.SAFE


# --- import purity / no implementation migration ----------------------------


def test_mcp_tools_import_is_pure_and_migrates_no_wrapper():
    code = (
        "import sys, lingtai_sdk.mcp_tools as mt\n"
        "m = mt.mcp_config_manifest()\n"
        "assert m.name == 'mcp' and m.transport.kind == 'in_process'\n"
        "assert m.roles.privileged is False and m.roles.native_only is False\n"
        "assert m.security.danger == 'safe'\n"
        "h = mt.mcp_config_host(lambda **kw: 'm')\n"
        "assert h.invoke('mcp') == 'm'\n"
        "assert mt.mcp_action_risk('show').value == 'safe'\n"
        "assert mt.mcp_action_risk('nope').value == 'destructive'\n"
        # importing mcp_tools must NOT pull in the lingtai wrapper, i.e. the real
        # mcp implementation is not migrated/imported from the SDK.
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
