#!/usr/bin/env python3
"""Import smoke for the public SDK directory shape.

Verifies that — after the flat ``lingtai_sdk`` modules were moved into the
``runtime`` / ``client`` / ``guard`` / ``bundles`` subpackages — every legacy
top-level import path AND every new canonical subpackage path import, and that
each shimmed name resolves to the *same object* as its canonical home (so the
compatibility layer is a re-export, never a fork). Also asserts import purity:
``import lingtai_sdk`` may load only the import-light ``lingtai`` root/kernel
parents, not the batteries-included wrapper modules.

Run from the repo root:  ``PYTHONPATH=src python src/lingtai_sdk/assets/lingtai-sdk-skill/scripts/sdk_shape_import_smoke.py``
The pytest equivalent lives in ``tests/test_sdk_directory_shape.py``.
"""
from __future__ import annotations

import importlib
import sys

# (legacy top-level module, canonical new module). Excludes ``runtime`` and
# ``client``: those are packages whose ``__init__`` IS the compat surface.
_TOOL_MODULES = (
    "file_tools",
    "file_mutation_tools",
    "communication_tools",
    "lifecycle_tools",
    "mcp_tools",
    "knowledge_tools",
    "skill_tools",
    "bash_tools",
    "avatar_tools",
    "psyche_tools",
    "soul_tools",
)
PAIRS = [
    ("lingtai_sdk.capabilities", "lingtai_sdk.bundles.contracts"),
    ("lingtai_sdk.capability_host", "lingtai_sdk.bundles.host"),
    ("lingtai_sdk.bundle_registry", "lingtai_sdk.bundles.registry"),
    ("lingtai_sdk.core_bundles", "lingtai_sdk.bundles.core"),
    ("lingtai_sdk.native", "lingtai_sdk.bundles.native"),
    ("lingtai_sdk.guard_bridge", "lingtai_sdk.guard.bridge"),
] + [(f"lingtai_sdk.{t}", f"lingtai_sdk.bundles.{t}") for t in _TOOL_MODULES]


def main() -> int:
    for legacy, canonical in PAIRS:
        legacy_mod = importlib.import_module(legacy)
        canonical_mod = importlib.import_module(canonical)
        for name in canonical_mod.__all__:
            assert getattr(legacy_mod, name) is getattr(canonical_mod, name), (
                f"{legacy}.{name} forked from {canonical}.{name}"
            )
        print(f"OK shim {legacy:38s} -> {canonical}  ({len(canonical_mod.__all__)} names)")

    import lingtai_sdk.runtime as rt
    from lingtai_sdk.runtime import contracts

    assert all(getattr(rt, n) is getattr(contracts, n) for n in contracts.__all__)
    print("OK pkg  lingtai_sdk.runtime  <- contracts")

    import lingtai_sdk.client as cl
    from lingtai_sdk.client import facade

    assert all(getattr(cl, n) is getattr(facade, n) for n in facade.__all__)
    print("OK pkg  lingtai_sdk.client   <- facade")

    import lingtai_sdk
    from lingtai_sdk.bundles.native import NativeRuntime
    from lingtai_sdk.bundles.registry import default_registry
    from lingtai_sdk.client import query
    from lingtai_sdk.runtime import Runtime

    assert lingtai_sdk.NativeRuntime is NativeRuntime
    assert lingtai_sdk.default_registry is default_registry
    assert lingtai_sdk.Runtime is Runtime
    assert lingtai_sdk.query is query
    print("OK root lazy names -> canonical modules")

    leaked = [
        m
        for m in sys.modules
        if (m == "lingtai" or m.startswith("lingtai."))
        and not (m == "lingtai" or m == "lingtai._version" or m.startswith("lingtai.kernel"))
    ]
    assert not leaked, f"heavy lingtai wrapper modules leaked into sys.modules: {leaked}"
    print("OK import purity: only import-light lingtai root/kernel modules loaded")

    print("\nSMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
