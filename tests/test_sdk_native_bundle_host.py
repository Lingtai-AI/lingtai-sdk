"""Stage-7 proof: the native privileged bundle host contract.

Where ``BundleHost`` is the *non-native* host that **refuses** privileged /
native-only bundles, ``NativeBundleHost`` is the conservative counterpart that
may host them — but only when **explicitly constructed as native authority** and
only for ``transport.kind == "native"`` bundles. It enforces exactly the same
declared↔provided handler contract per surface as ``BundleHost``.

This proves the privileged hosting *boundary* with a harmless, synthetic
``native_privileged_proof`` manifest — a native-proof dummy that names no real
privileged surface. ``system`` / ``psyche`` / ``soul`` are NOT named or migrated
here; their declaration is stage 8.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk.errors import BundleHostError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def _native_privileged_manifest(
    *, tools=("act",), native_only=True
) -> cap.BundleManifest:
    """A harmless native-proof privileged manifest (NOT a real core bundle)."""
    return cap.BundleManifest(
        name="native_privileged_proof",
        version="0.0.1",
        roles=cap.RoleFlags(
            required=False,
            privileged=True,
            native_only=native_only,
            backend_replaceability=(
                cap.BackendReplaceability.NATIVE_ONLY
                if native_only
                else cap.BackendReplaceability.AUGMENTABLE
            ),
        ),
        surfaces=cap.CapabilitySurfaces(tools=tools),
        transport=cap.TransportSpec(kind=cap.TransportKind.NATIVE.value),
    )


# --- NativeBundleHost: accepts privileged native-only with explicit authority


def test_native_host_hosts_privileged_native_only_with_authority():
    manifest = _native_privileged_manifest()
    h = host.NativeBundleHost(
        manifest, {"act": lambda **kw: {"acted": True}}, native_authority=True
    )
    assert h.manifest.name == "native_privileged_proof"
    assert h.tools == ("act",)
    assert h.invoke("act") == {"acted": True}


def test_native_host_hosts_privileged_non_native_only_with_authority():
    # privileged but not native_only is still privileged -> still needs authority.
    manifest = _native_privileged_manifest(native_only=False)
    h = host.NativeBundleHost(
        manifest, {"act": lambda **kw: 1}, native_authority=True
    )
    assert h.invoke("act") == 1


# --- explicit authority is required ---------------------------------------


def test_native_host_refuses_without_explicit_authority():
    manifest = _native_privileged_manifest()
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest, {"act": lambda **kw: None}, native_authority=False
        )


def test_native_host_authority_is_keyword_only_and_defaults_off():
    manifest = _native_privileged_manifest()
    # default (no native_authority passed) must refuse — authority is opt-in.
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(manifest, {"act": lambda **kw: None})


# --- transport must be native ---------------------------------------------


def test_native_host_requires_native_transport():
    manifest = cap.BundleManifest(
        name="native_privileged_proof",
        version="0.0.1",
        roles=cap.RoleFlags(
            privileged=True,
            native_only=True,
            backend_replaceability=cap.BackendReplaceability.NATIVE_ONLY,
        ),
        surfaces=cap.CapabilitySurfaces(tools=("act",)),
        transport=cap.TransportSpec(kind=cap.TransportKind.IN_PROCESS.value),
    )
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest, {"act": lambda **kw: None}, native_authority=True
        )


# --- same declared<->provided contract as BundleHost ----------------------


def test_native_host_rejects_missing_handler():
    manifest = _native_privileged_manifest(tools=("act", "react"))
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest, {"act": lambda **kw: None}, native_authority=True
        )


def test_native_host_rejects_undeclared_handler():
    manifest = _native_privileged_manifest(tools=("act",))
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest,
            {"act": lambda **kw: None, "stowaway": lambda **kw: None},
            native_authority=True,
        )


def test_native_host_rejects_non_callable_handler():
    manifest = _native_privileged_manifest(tools=("act",))
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest, {"act": object()}, native_authority=True
        )


def test_native_host_enforces_resource_and_prompt_contract():
    manifest = cap.BundleManifest(
        name="native_privileged_proof",
        version="0.0.1",
        roles=cap.RoleFlags(
            privileged=True,
            native_only=True,
            backend_replaceability=cap.BackendReplaceability.NATIVE_ONLY,
        ),
        surfaces=cap.CapabilitySurfaces(
            tools=("act",), resources=("snapshot",), prompts=("brief",)
        ),
        transport=cap.TransportSpec(kind=cap.TransportKind.NATIVE.value),
    )
    h = host.NativeBundleHost(
        manifest,
        {"act": lambda **kw: "ok"},
        resources={"snapshot": lambda: {"n": 1}},
        prompts={"brief": lambda **kw: "hi"},
        native_authority=True,
    )
    assert h.read_resource("snapshot") == {"n": 1}
    assert h.read_prompt("brief") == "hi"
    # a missing resource handler is rejected
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(
            manifest,
            {"act": lambda **kw: "ok"},
            prompts={"brief": lambda **kw: "hi"},
            native_authority=True,
        )


def test_native_host_validates_manifest_on_register():
    manifest = cap.BundleManifest(name="", version="0.0.1")
    with pytest.raises(BundleHostError):
        host.NativeBundleHost(manifest, {}, native_authority=True)


# --- the non-native BundleHost refusal is UNCHANGED -----------------------


def test_bundle_host_still_refuses_privileged():
    manifest = _native_privileged_manifest()
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"act": lambda **kw: None})


def test_bundle_host_still_refuses_native_transport():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=("echo",)),
        transport=cap.TransportSpec(kind=cap.TransportKind.NATIVE.value),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"echo": lambda **kw: None})


# --- a ready native-proof host helper -------------------------------------


def test_native_proof_host_invokes():
    h = host.native_proof_host()
    assert h.manifest.roles.privileged is True
    assert h.manifest.roles.native_only is True
    assert h.manifest.transport.kind == "native"
    # deterministic, network-free
    result = h.invoke("native_noop")
    assert result == {"native_noop": True}


# --- import purity (privileged host must stay wrapper-free) ----------------


def test_native_bundle_host_import_is_pure():
    code = (
        "import sys, lingtai_sdk.capability_host\n"
        "h = lingtai_sdk.capability_host.native_proof_host()\n"
        "assert h.invoke('native_noop') == {'native_noop': True}\n"
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
