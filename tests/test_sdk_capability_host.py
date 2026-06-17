"""Stage-5 proof: the CapabilityBundle manifest/load/host boundary.

Exercises the full path a non-native host walks for a declared bundle:

    declared manifest (plain dict)
        -> load_manifest()  (parse + validate)
        -> BundleHost       (register manifest + tool handlers, enforce contract)
        -> invoke()         (call a declared, harmless tool)

The only bundle here is the synthetic, metadata-only ``proof_bundle()`` wired to
a deterministic pure ``echo`` handler. We do NOT migrate the privileged core
bundles (``system`` / ``psyche`` / ``soul``); the host explicitly *refuses* to
host a privileged/native-only manifest, which is itself part of the proof.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import capability_host as host
from lingtai_sdk.errors import BundleHostError, BundleLoadError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --- load_manifest: dict -> validated BundleManifest ----------------------


def test_load_manifest_round_trips_to_dict():
    original = cap.proof_bundle()
    loaded = cap.load_manifest(original.to_dict())
    assert isinstance(loaded, cap.BundleManifest)
    assert loaded.name == original.name
    assert loaded.version == original.version
    assert loaded.surfaces.tools == original.surfaces.tools
    assert loaded.roles.privileged is False
    # the enum is reconstructed from its string value, not left as a string
    assert (
        loaded.roles.backend_replaceability
        is cap.BackendReplaceability.REPLACEABLE
    )
    assert loaded.to_dict() == original.to_dict()


def test_load_manifest_accepts_partial_dict_with_defaults():
    loaded = cap.load_manifest({"name": "x", "version": "0.0.1"})
    assert loaded.name == "x"
    assert loaded.surfaces.tools == ()
    assert loaded.roles.backend_replaceability is cap.BackendReplaceability.REPLACEABLE
    assert loaded.transport.kind == "native"


def test_load_manifest_validates():
    with pytest.raises(BundleLoadError):
        cap.load_manifest({"name": "", "version": "0.0.1"})
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "roles": {"privileged": False, "native_only": True},
            }
        )


def test_load_manifest_rejects_unknown_replaceability():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "roles": {"backend_replaceability": "totally_bogus"},
            }
        )


def test_load_manifest_rejects_non_array_name_lists():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "surfaces": {"tools": "echo"},
            }
        )
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "surfaces": {"tools": ["echo", 3]},
            }
        )


# --- BundleHost: register + enforce contract + invoke ---------------------


def test_proof_host_invokes_echo_deterministically():
    h = host.proof_host()
    assert h.manifest.name == "sdk_proof_echo"
    assert h.tools == ("echo",)
    # deterministic, network-free, pure
    assert h.invoke("echo", text="hi") == {"echo": "hi"}
    assert h.invoke("echo", text="hi") == {"echo": "hi"}
    assert h.invoke("echo", text="") == {"echo": ""}


def test_host_rejects_privileged_bundle():
    manifest = cap.BundleManifest(
        name="priv",
        version="0.0.1",
        roles=cap.RoleFlags(privileged=True),
        surfaces=cap.CapabilitySurfaces(tools=("danger",)),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"danger": lambda **kw: None})


def test_host_rejects_native_only_bundle():
    manifest = cap.BundleManifest(
        name="nat",
        version="0.0.1",
        roles=cap.RoleFlags(
            privileged=True,
            native_only=True,
            backend_replaceability=cap.BackendReplaceability.NATIVE_ONLY,
        ),
        surfaces=cap.CapabilitySurfaces(tools=("k",)),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"k": lambda **kw: None})


def test_host_rejects_undeclared_handler():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=("echo",)),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(
            manifest,
            {"echo": lambda **kw: kw, "stowaway": lambda **kw: kw},
        )


def test_host_rejects_missing_handler():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=("echo", "reverse")),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"echo": lambda **kw: kw})


def test_host_rejects_non_in_process_transport():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=("echo",)),
        transport=cap.TransportSpec(kind="http"),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"echo": lambda **kw: kw})


def test_host_rejects_non_callable_handler():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=("echo",)),
        transport=cap.TransportSpec(kind="in_process"),
    )
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {"echo": object()})


def test_host_validates_manifest_on_register():
    manifest = cap.BundleManifest(name="", version="0.0.1")
    with pytest.raises(BundleHostError):
        host.BundleHost(manifest, {})


def test_host_invoke_unknown_tool_raises():
    h = host.proof_host()
    with pytest.raises(BundleHostError):
        h.invoke("does_not_exist")


def test_host_load_and_host_from_dict():
    # the full declared->loaded->hosted path from a plain dict
    data = cap.proof_bundle().to_dict()
    manifest = cap.load_manifest(data)
    h = host.BundleHost(manifest, {"echo": lambda text="": {"echo": text}})
    assert h.invoke("echo", text="round-trip") == {"echo": "round-trip"}


# --- import purity --------------------------------------------------------


def test_capability_host_import_is_pure():
    code = (
        "import sys, lingtai_sdk.capability_host\n"
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
