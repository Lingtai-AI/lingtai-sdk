"""CapabilityBundle manifest seed: the public DTO describing a capability's
identity, role flags, surfaces, security, and transport. Native privileged
handlers stay in the kernel/wrapper — this is the public schema only. The proof
bundle is a harmless metadata-only synthetic bundle; we do NOT migrate core
system/psyche/soul here.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def test_proof_bundle_is_valid():
    b = cap.proof_bundle()
    assert b.name == "sdk_proof_echo"
    assert b.version
    assert b.surfaces.tools == ("echo",)
    assert b.roles.privileged is False
    b.validate()  # raises on invalid


def test_role_flag_invariant_native_only_requires_privileged():
    bad = cap.BundleManifest(
        name="x",
        version="0.0.1",
        roles=cap.RoleFlags(privileged=False, native_only=True),
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_role_flag_invariant_native_only_requires_native_replaceability():
    bad = cap.BundleManifest(
        name="x",
        version="0.0.1",
        roles=cap.RoleFlags(
            privileged=True,
            native_only=True,
            backend_replaceability=cap.BackendReplaceability.REPLACEABLE,
        ),
    )
    with pytest.raises(ValueError):
        bad.validate()

    ok = cap.BundleManifest(
        name="x",
        version="0.0.1",
        roles=cap.RoleFlags(
            privileged=True,
            native_only=True,
            backend_replaceability=cap.BackendReplaceability.NATIVE_ONLY,
        ),
    )
    ok.validate()  # does not raise


def test_required_name_and_version():
    with pytest.raises(ValueError):
        cap.BundleManifest(name="", version="0.0.1").validate()
    with pytest.raises(ValueError):
        cap.BundleManifest(name="x", version="").validate()


def test_manifest_round_trips_to_dict():
    b = cap.proof_bundle()
    d = b.to_dict()
    assert d["name"] == b.name
    assert d["roles"]["privileged"] == b.roles.privileged
    # enum is serialized to its value, not the Enum member
    assert d["roles"]["backend_replaceability"] == "replaceable"
    assert "surfaces" in d and "security" in d and "transport" in d
    assert d["surfaces"]["tools"] == ("echo",)


def test_surfaces_default_empty():
    s = cap.CapabilitySurfaces()
    assert s.tools == () and s.resources == () and s.prompts == ()
    assert s.events == () and s.hooks == () and s.lifecycle == () and s.state == ()


def test_capabilities_module_import_is_pure():
    code = (
        "import sys, lingtai_sdk.capabilities\n"
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
