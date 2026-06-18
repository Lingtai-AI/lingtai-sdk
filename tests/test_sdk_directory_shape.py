"""The public SDK directory shape: modules live in subpackages
(``runtime`` / ``client`` / ``guard`` / ``bundles``) and every legacy
top-level import path remains a thin re-export shim resolving to the SAME
object as its new canonical home — compatibility by re-export, never a fork.

This is the layout-level companion to :mod:`tests.test_sdk_compat` (which
covers the kernel→SDK ``_compat`` deprecation map) and to
:mod:`tests.test_sdk_import_purity` (which covers the lazy boundary).
"""
from __future__ import annotations

import importlib

import pytest

# (legacy top-level module, canonical new module). The legacy module is a shim
# that re-exports the canonical module's public surface. ``runtime`` and
# ``client`` are intentionally absent here: they are packages whose ``__init__``
# IS the compat surface (no separate legacy file), covered separately below.
_SHIM_PAIRS = [
    ("lingtai_sdk.capabilities", "lingtai_sdk.bundles.contracts"),
    ("lingtai_sdk.capability_host", "lingtai_sdk.bundles.host"),
    ("lingtai_sdk.bundle_registry", "lingtai_sdk.bundles.registry"),
    ("lingtai_sdk.core_bundles", "lingtai_sdk.bundles.core"),
    ("lingtai_sdk.native", "lingtai_sdk.bundles.native"),
    ("lingtai_sdk.guard_bridge", "lingtai_sdk.guard.bridge"),
    ("lingtai_sdk.file_tools", "lingtai_sdk.bundles.file_tools"),
    ("lingtai_sdk.file_mutation_tools", "lingtai_sdk.bundles.file_mutation_tools"),
    ("lingtai_sdk.communication_tools", "lingtai_sdk.bundles.communication_tools"),
    ("lingtai_sdk.lifecycle_tools", "lingtai_sdk.bundles.lifecycle_tools"),
    ("lingtai_sdk.mcp_tools", "lingtai_sdk.bundles.mcp_tools"),
    ("lingtai_sdk.knowledge_tools", "lingtai_sdk.bundles.knowledge_tools"),
    ("lingtai_sdk.skill_tools", "lingtai_sdk.bundles.skill_tools"),
    ("lingtai_sdk.bash_tools", "lingtai_sdk.bundles.bash_tools"),
    ("lingtai_sdk.avatar_tools", "lingtai_sdk.bundles.avatar_tools"),
    ("lingtai_sdk.psyche_tools", "lingtai_sdk.bundles.psyche_tools"),
    ("lingtai_sdk.soul_tools", "lingtai_sdk.bundles.soul_tools"),
]


@pytest.mark.parametrize("legacy, canonical", _SHIM_PAIRS, ids=lambda v: v)
def test_legacy_module_imports(legacy, canonical):
    assert importlib.import_module(legacy) is not None
    assert importlib.import_module(canonical) is not None


@pytest.mark.parametrize("legacy, canonical", _SHIM_PAIRS, ids=lambda p: p[0])
def test_shim_reexports_same_objects(legacy, canonical):
    legacy_mod = importlib.import_module(legacy)
    canonical_mod = importlib.import_module(canonical)
    exported = getattr(canonical_mod, "__all__", None)
    assert exported, f"{canonical} should declare __all__"
    for name in exported:
        assert hasattr(legacy_mod, name), (
            f"{legacy} is missing re-exported name {name!r}"
        )
        assert getattr(legacy_mod, name) is getattr(canonical_mod, name), (
            f"{legacy}.{name} forked from {canonical}.{name}"
        )


def test_runtime_package_is_compat_surface():
    # ``import lingtai_sdk.runtime`` resolves to the package, whose __init__
    # re-exports the contracts so the old flat-module surface still works.
    import lingtai_sdk.runtime as runtime
    from lingtai_sdk.runtime import contracts

    for name in contracts.__all__:
        assert getattr(runtime, name) is getattr(contracts, name)


def test_client_package_is_compat_surface():
    import lingtai_sdk.client as client
    from lingtai_sdk.client import facade

    for name in facade.__all__:
        assert getattr(client, name) is getattr(facade, name)


def test_guard_package_reexports_bridge():
    import lingtai_sdk.guard as guard
    from lingtai_sdk.guard import bridge

    for name in bridge.__all__:
        assert getattr(guard, name) is getattr(bridge, name)


def test_bundles_package_reexports_core_surfaces():
    import lingtai_sdk.bundles as bundles
    from lingtai_sdk.bundles import contracts, core, host, native, registry

    for mod in (contracts, host, core, registry):
        for name in mod.__all__:
            assert getattr(bundles, name) is getattr(mod, name)
    # native surface is also re-exported at the bundles package root
    for name in native.__all__:
        assert getattr(bundles, name) is getattr(native, name)


def test_top_level_lazy_names_target_new_modules():
    # The PEP 562 lazy map should resolve to the canonical subpackage objects.
    import lingtai_sdk
    from lingtai_sdk.bundles.native import NativeRuntime
    from lingtai_sdk.bundles.registry import default_registry
    from lingtai_sdk.runtime import Runtime
    from lingtai_sdk.client import query

    assert lingtai_sdk.NativeRuntime is NativeRuntime
    assert lingtai_sdk.default_registry is default_registry
    assert lingtai_sdk.Runtime is Runtime
    assert lingtai_sdk.query is query
