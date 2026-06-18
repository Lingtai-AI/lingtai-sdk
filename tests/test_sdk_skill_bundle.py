"""Stage-6: real, low-risk CapabilityBundle adoption via the committed
``lingtai-sdk-skill`` asset.

Exercises the whole path from a *shipped* asset to a hosted, invokable bundle:

    asset (SKILL.md via importlib.resources)
        -> load_sdk_skill()        # read the committed asset text
        -> sdk_skill_bundle()      # declare a validated BundleManifest over it
        -> load_manifest(round-trip)
        -> sdk_skill_host()        # BundleHost: tool + resource + prompt
        -> read_resource / invoke / read_prompt   # deterministic, network-free

Unlike the Stage-5 synthetic ``proof_bundle()`` echo, this is a real committed
asset. The privileged core bundles (``system`` / ``psyche`` / ``soul``) are NOT
migrated here, and the bundle is explicitly non-privileged / ``in_process`` so
``BundleHost`` accepts it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk import sdk_skill
from lingtai_sdk.capability_host import BundleHost
from lingtai_sdk.errors import BundleHostError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_PRIVILEGED_CORE = ("system", "psyche", "soul")


# --- the committed asset --------------------------------------------------


def test_skill_asset_exists_via_importlib_resources():
    asset = resources.files("lingtai_sdk.assets").joinpath(
        "lingtai-sdk-skill", "SKILL.md"
    )
    assert asset.is_file()
    text = asset.read_text(encoding="utf-8")
    assert text.startswith("---")  # YAML frontmatter
    assert "name: lingtai-sdk-skill" in text


def test_load_sdk_skill_reads_committed_text():
    text = sdk_skill.load_sdk_skill()
    assert "lingtai-sdk-skill" in text
    # the four things the skill must explain
    assert "CapabilityBundle" in text
    assert "runtime contract" in text
    assert "system" in text and "psyche" in text and "soul" in text
    # deterministic: same text every call
    assert text == sdk_skill.load_sdk_skill()


# --- the manifest ---------------------------------------------------------


def test_bundle_manifest_validates_and_is_non_privileged():
    manifest = sdk_skill.sdk_skill_bundle()
    manifest.validate()  # must not raise
    assert manifest.name == "lingtai_sdk_skill"
    assert manifest.roles.privileged is False
    assert manifest.roles.native_only is False
    assert manifest.transport.kind == "in_process"
    assert manifest.surfaces.tools == ("read_sdk_skill",)
    assert manifest.surfaces.resources == ("sdk_skill",)
    assert manifest.surfaces.prompts == ("sdk_skill_orientation",)
    assert manifest.manual == (sdk_skill.SKILL_MANUAL_PATH,)


def test_bundle_manifest_round_trips_through_load_manifest():
    original = sdk_skill.sdk_skill_bundle()
    loaded = cap.load_manifest(original.to_dict())
    assert isinstance(loaded, cap.BundleManifest)
    assert loaded.to_dict() == original.to_dict()
    assert (
        loaded.roles.backend_replaceability
        is cap.BackendReplaceability.REPLACEABLE
    )


def test_bundle_does_not_name_privileged_core():
    manifest = sdk_skill.sdk_skill_bundle()
    names = (
        manifest.surfaces.tools
        + manifest.surfaces.resources
        + manifest.surfaces.prompts
        + (manifest.name,)
    )
    for surface_name in names:
        assert surface_name not in _PRIVILEGED_CORE


# --- the host: tool + resource + prompt -----------------------------------


def test_host_reads_resource_deterministically():
    h = sdk_skill.sdk_skill_host()
    assert h.resources == ("sdk_skill",)
    text = h.read_resource("sdk_skill")
    assert text == sdk_skill.load_sdk_skill()
    assert h.read_resource("sdk_skill") == text  # deterministic


def test_host_invokes_read_only_tool():
    h = sdk_skill.sdk_skill_host()
    assert h.tools == ("read_sdk_skill",)
    payload = h.invoke("read_sdk_skill")
    assert payload["name"] == sdk_skill.SKILL_MANUAL_PATH
    assert payload["text"] == sdk_skill.load_sdk_skill()


def test_host_renders_prompt():
    h = sdk_skill.sdk_skill_host()
    assert h.prompts == ("sdk_skill_orientation",)
    default = h.read_prompt("sdk_skill_orientation")
    assert "coding agent" in default
    assert sdk_skill.SKILL_MANUAL_PATH in default
    custom = h.read_prompt("sdk_skill_orientation", audience="system prompt")
    assert "system prompt" in custom


def test_host_rejects_unknown_resource_and_prompt():
    h = sdk_skill.sdk_skill_host()
    with pytest.raises(BundleHostError):
        h.read_resource("does_not_exist")
    with pytest.raises(BundleHostError):
        h.read_prompt("does_not_exist")


def test_host_enforces_resource_contract():
    # a resource declared with no handler is refused at construction
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(resources=("missing",)),
        transport=cap.TransportSpec(kind="in_process"),
    )
    with pytest.raises(BundleHostError):
        BundleHost(manifest, {})
    # an undeclared resource handler is also refused
    manifest2 = cap.BundleManifest(
        name="x",
        version="0.0.1",
        transport=cap.TransportSpec(kind="in_process"),
    )
    with pytest.raises(BundleHostError):
        BundleHost(manifest2, {}, resources={"stowaway": lambda: None})


def test_host_enforces_prompt_contract():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(prompts=("missing",)),
        transport=cap.TransportSpec(kind="in_process"),
    )
    with pytest.raises(BundleHostError):
        BundleHost(manifest, {})


def test_host_rejects_non_callable_resource_handler():
    manifest = cap.BundleManifest(
        name="x",
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(resources=("r",)),
        transport=cap.TransportSpec(kind="in_process"),
    )
    with pytest.raises(BundleHostError):
        BundleHost(manifest, {}, resources={"r": object()})


def test_full_declared_to_hosted_path_from_dict():
    # declared dict -> load_manifest -> BundleHost -> read_resource
    data = sdk_skill.sdk_skill_bundle().to_dict()
    manifest = cap.load_manifest(data)
    h = BundleHost(
        manifest,
        handlers={"read_sdk_skill": lambda: {"ok": True}},
        resources={"sdk_skill": sdk_skill.load_sdk_skill},
        prompts={"sdk_skill_orientation": lambda **kw: "hi"},
    )
    assert h.read_resource("sdk_skill") == sdk_skill.load_sdk_skill()


# --- import purity --------------------------------------------------------


def test_sdk_skill_import_is_pure():
    code = (
        "import sys, lingtai_sdk.sdk_skill as s\n"
        "s.sdk_skill_host().read_resource('sdk_skill')\n"
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
