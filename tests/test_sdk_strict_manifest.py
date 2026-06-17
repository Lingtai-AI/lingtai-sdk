"""Stage-7 proof: strict manifest validation.

The safety-contract layer immediately before the privileged-core declaration
(stage 8). It hardens ``load_manifest()`` / ``BundleManifest.validate()`` so a
declaration cannot quietly mean something other than it says:

* role flags (``required`` / ``privileged`` / ``native_only`` / ``can_override``)
  must be *real* booleans — ``"false"`` and ``1`` are rejected, not coerced;
* ``security.danger`` and ``transport.kind`` are validated against a public,
  enum-like allow-list — unknown values raise rather than slip through;
* ``metadata`` and ``transport.config`` must be mappings — a list or scalar is
  rejected with a clear message, never half-coerced.

We use only harmless synthetic declarations; no ``system`` / ``psyche`` /
``soul`` is named or migrated here.
"""
from __future__ import annotations

import pytest

from lingtai_sdk import capabilities as cap
from lingtai_sdk.errors import BundleLoadError


# --- strict role-flag booleans -------------------------------------------


@pytest.mark.parametrize("flag", ["required", "privileged", "native_only", "can_override"])
@pytest.mark.parametrize("bad", ["false", "true", 1, 0, "1", [], None])
def test_load_manifest_rejects_non_bool_role_flags(flag, bad):
    if bad is None:
        pytest.skip("None means 'unset' and falls back to the default — allowed")
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "roles": {flag: bad},
            }
        )


def test_load_manifest_accepts_real_bool_role_flags():
    loaded = cap.load_manifest(
        {
            "name": "x",
            "version": "0.0.1",
            "roles": {"required": True, "can_override": False},
        }
    )
    assert loaded.roles.required is True
    assert loaded.roles.can_override is False


def test_load_manifest_role_flags_default_when_absent():
    loaded = cap.load_manifest({"name": "x", "version": "0.0.1", "roles": {}})
    assert loaded.roles.required is False
    assert loaded.roles.privileged is False
    assert loaded.roles.native_only is False
    assert loaded.roles.can_override is False


# --- enum-like danger / transport ----------------------------------------


def test_security_danger_enum_values():
    assert {d.value for d in cap.SecurityDanger} == {"safe", "caution", "destructive"}


def test_transport_kind_enum_values():
    assert {t.value for t in cap.TransportKind} == {
        "native",
        "in_process",
        "stdio",
        "http",
    }


@pytest.mark.parametrize("danger", ["safe", "caution", "destructive"])
def test_load_manifest_accepts_known_danger(danger):
    loaded = cap.load_manifest(
        {"name": "x", "version": "0.0.1", "security": {"danger": danger}}
    )
    assert loaded.security.danger == danger


def test_load_manifest_rejects_unknown_danger():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {"name": "x", "version": "0.0.1", "security": {"danger": "spicy"}}
        )


@pytest.mark.parametrize("kind", ["native", "in_process", "stdio", "http"])
def test_load_manifest_accepts_known_transport_kind(kind):
    # native transport needs the privileged/native-only invariant satisfied,
    # but kind validation happens regardless — drive it through a non-native
    # kind plus an explicit native case kept valid.
    data = {"name": "x", "version": "0.0.1", "transport": {"kind": kind}}
    loaded = cap.load_manifest(data)
    assert loaded.transport.kind == kind


def test_load_manifest_rejects_unknown_transport_kind():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {"name": "x", "version": "0.0.1", "transport": {"kind": "carrier-pigeon"}}
        )


def test_validate_rejects_unknown_danger_on_constructed_manifest():
    bad = cap.BundleManifest(
        name="x", version="0.0.1", security=cap.SecurityPolicy(danger="spicy")
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_validate_rejects_unknown_transport_on_constructed_manifest():
    bad = cap.BundleManifest(
        name="x", version="0.0.1", transport=cap.TransportSpec(kind="carrier-pigeon")
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_validate_accepts_enum_members_directly():
    # the public Enums are str-valued, so passing a member is also valid.
    ok = cap.BundleManifest(
        name="x",
        version="0.0.1",
        security=cap.SecurityPolicy(danger=cap.SecurityDanger.CAUTION),
        transport=cap.TransportSpec(kind=cap.TransportKind.STDIO),
    )
    ok.validate()  # does not raise


# --- non-mapping metadata / transport.config -----------------------------


def test_load_manifest_rejects_non_mapping_metadata():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {"name": "x", "version": "0.0.1", "metadata": ["not", "a", "map"]}
        )
    with pytest.raises(BundleLoadError):
        cap.load_manifest({"name": "x", "version": "0.0.1", "metadata": 7})


def test_load_manifest_rejects_non_mapping_transport_config():
    with pytest.raises(BundleLoadError):
        cap.load_manifest(
            {
                "name": "x",
                "version": "0.0.1",
                "transport": {"kind": "stdio", "config": ["nope"]},
            }
        )


def test_load_manifest_accepts_empty_or_absent_metadata_and_config():
    loaded = cap.load_manifest({"name": "x", "version": "0.0.1"})
    assert loaded.metadata == {}
    assert loaded.transport.config == {}
    loaded2 = cap.load_manifest(
        {
            "name": "x",
            "version": "0.0.1",
            "metadata": {"k": "v"},
            "transport": {"kind": "stdio", "config": {"host": "localhost"}},
        }
    )
    assert loaded2.metadata == {"k": "v"}
    assert loaded2.transport.config == {"host": "localhost"}


# --- the proof bundle still round-trips through the stricter loader -------


def test_proof_bundle_round_trips_through_strict_loader():
    original = cap.proof_bundle()
    loaded = cap.load_manifest(original.to_dict())
    assert loaded.to_dict() == original.to_dict()
