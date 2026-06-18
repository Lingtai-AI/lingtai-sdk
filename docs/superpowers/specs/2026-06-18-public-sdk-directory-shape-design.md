# Public SDK directory shape

Date: 2026-06-18
Branch: `sdk/public-sdk-directory-shape-20260618` (stacked on PR #367)

## Problem

`src/lingtai_sdk/` is intentionally flat from the migration phase — ~25 sibling
modules with no grouping. Jason approved formalizing the package shape into
public subpackages. The change must be **package-shape only**: no behavior
change, and every existing import path must keep working.

## Goal

Restructure into clear public subpackages while preserving every old top-level
import path as a thin compatibility shim that re-exports from the new location.

## Current dependency graph (one-directional, no cycles)

```
_version, errors, types, _compat        (leaves / foundation)
capabilities          -> errors
capability_host       -> capabilities, errors
file_tools            -> capabilities, capability_host, errors
file_mutation_tools   -> capabilities, capability_host, errors
communication_tools   -> capabilities, capability_host, errors
knowledge_tools       -> capabilities, capability_host, errors
mcp_tools             -> capabilities, capability_host, errors
skill_tools           -> capabilities, capability_host, errors
bash_tools            -> capabilities, capability_host, errors
avatar_tools          -> capabilities, capability_host, errors
core_bundles          -> capabilities, capability_host, errors
lifecycle_tools       -> capabilities, capability_host, core_bundles, errors
psyche_tools          -> capabilities, capability_host, core_bundles, errors
soul_tools            -> capabilities, capability_host, core_bundles, errors
guard_bridge          -> capabilities
bundle_registry       -> all *_tools manifests + core_bundles + capabilities + errors
runtime               (standalone contracts)
native                -> capabilities, capability_host, core_bundles, errors, runtime
client                -> runtime, native
sdk_skill             -> capabilities, capability_host
```

## Target shape

```
lingtai_sdk/
  __init__.py            # lazy __getattr__ targets repointed to new modules
  _version.py            # unchanged
  _compat.py             # unchanged (kernel->SDK deprecation map, not a layout concern)
  errors.py              # unchanged (foundation leaf, imported everywhere)
  types.py               # unchanged (foundation leaf)
  sdk_skill.py           # unchanged (CLI/skill entry, references bundles via shims)
  assets/                # unchanged

  runtime/
    __init__.py          # re-exports the runtime contracts surface
    contracts.py         # <- runtime.py

  client/
    __init__.py          # re-exports LingTaiClient, LingTaiSession, query, etc.
    facade.py            # <- client.py

  guard/
    __init__.py          # re-exports guard bridge surface
    bridge.py            # <- guard_bridge.py

  bundles/
    __init__.py          # re-exports BundleManifest, host, registry, core surfaces
    contracts.py         # <- capabilities.py
    host.py              # <- capability_host.py
    registry.py          # <- bundle_registry.py
    core.py              # <- core_bundles.py
    native.py            # <- native.py  (the native runtime adapter)
    file_tools.py
    file_mutation_tools.py
    communication_tools.py
    lifecycle_tools.py
    mcp_tools.py
    knowledge_tools.py
    skill_tools.py
    bash_tools.py
    avatar_tools.py
    psyche_tools.py
    soul_tools.py

  # --- compat shims at old top-level paths (thin, docstring'd) ---
  capabilities.py        -> bundles.contracts
  capability_host.py     -> bundles.host
  bundle_registry.py     -> bundles.registry
  core_bundles.py        -> bundles.core
  native.py              -> bundles.native
  guard_bridge.py        -> guard.bridge
  runtime.py             -> runtime (package)  [name clash handled below]
  client.py              -> client (package)   [name clash handled below]
  file_tools.py          -> bundles.file_tools
  ... (all 10 tool modules) ...
```

### Decisions

1. **Tool modules flat under `bundles/`** (not `bundles/tools/`). One `git mv`
   per file, simplest shim mapping, lowest review risk.
2. **`capability_host.py` -> `bundles/host.py`, `capabilities.py` ->
   `bundles/contracts.py`.** All bundle machinery groups together. Tests import
   `lingtai_sdk.capability_host` directly — preserved via shim.
3. **`native.py` -> `bundles/native.py`.** It is the native bundle-hosting
   runtime adapter and depends only on bundle modules + runtime contracts; it
   belongs with the bundles, and `client` imports it lazily. Top-level
   `lingtai_sdk.native` shim preserved.
4. **`_compat.py` stays at top level** — it is the kernel→SDK deprecation map,
   orthogonal to physical layout.
5. **`sdk_skill.py` stays at top level** — it is an entry module, not part of
   the public contract subpackages; it imports through the shims/new paths.

### The `runtime.py` / `client.py` name clash

Turning `runtime` and `client` into packages means the old single-file modules
`runtime.py` and `client.py` cannot coexist with directories of the same name.
Resolution: the directory wins (`runtime/`, `client/`), and the **package
`__init__.py` IS the compat surface** — it re-exports everything the old module
exported. No separate top-level shim file is needed for these two; `import
lingtai_sdk.runtime` / `from lingtai_sdk.client import query` resolve to the
package, which re-exports the same objects. This is itself the shim.

For modules that do NOT become packages (capabilities, capability_host,
bundle_registry, core_bundles, native, guard_bridge, the 10 tool modules), the
old path remains a real `.py` shim file:

```python
"""Compatibility shim — moved to lingtai_sdk.bundles.registry.

Re-exports the bundle registry surface from its new home. Kept so legacy
imports (lingtai_sdk.bundle_registry) keep resolving to the same objects.
"""
from __future__ import annotations
from lingtai_sdk.bundles.registry import *  # noqa: F401,F403
from lingtai_sdk.bundles.registry import (  # explicit: names not in __all__ if any
    BundleRegistry, DispatchTarget, all_bundle_manifests, default_registry,
)
```

## Internal import rewrites

Every intra-package `from .X import ...` inside a moved module must be updated to
its new sibling/parent path. Because modules move *together* into `bundles/`,
most become same-package relative imports again (e.g. inside `bundles/registry.py`,
`from .file_tools import ...` still works). Cross-subpackage imports use the
parent path (e.g. `bundles/native.py` does `from lingtai_sdk.runtime import ...`).

## `__init__.py` lazy map

`_LAZY_SDK_EXPORTS` targets repoint to the new canonical modules:
- `NativeRuntime`/`NativeRuntimeSession` -> `.bundles.native`
- `LingTaiClient`/`query`/... -> `.client` (package)
- `Runtime`/`RuntimeOptions`/... -> `.runtime` (package)
- `BundleRegistry`/`default_registry`/... -> `.bundles.registry`

Import purity is preserved: none of these subpackage `__init__` files import the
`lingtai` wrapper or any heavy provider at module load. `bundles/native.py` keeps
its lazy-wrapper-on-start behavior.

## Compatibility requirement (must all keep working)

```
import lingtai_sdk.runtime
from lingtai_sdk.native import NativeRuntime
from lingtai_sdk.bundle_registry import default_registry
from lingtai_sdk.bash_tools import bash_manifests
from lingtai_sdk.capability_host import NativeBundleHost
from lingtai_sdk.capabilities import BundleManifest
from lingtai_sdk import query, NativeRuntime, default_registry, Runtime
```

## Testing

- Update test imports only where a test reaches into a moved module AND we want
  it to exercise the new path; otherwise leave tests on old paths to prove shims
  work. Add explicit assertions that old and new paths resolve to the SAME object.
- Run the focused SDK suite:
  `pytest -q tests/test_sdk_import_purity.py tests/test_sdk_bundle_registry.py
  tests/test_sdk_core_bundles.py tests/test_sdk_native_runtime.py
  tests/test_sdk_client_facade.py tests/test_sdk_guard_bridge.py
  tests/test_knowledge_bundle_bridge.py tests/test_mcp_bundle_bridge.py
  tests/test_skills_bundle_bridge.py tests/test_tool_call_guard.py`
- Import smoke script: assert every old path + every new path imports, and that
  shimmed names `is` the new-module name.
- `ruff check` on touched files.

## Docs

- Rewrite `src/lingtai_sdk/ANATOMY.md` Composition section to describe the
  subpackage tree; add per-subpackage ANATOMY.md where the convention warrants.
- Note the shim layer and the runtime/client package-is-shim mechanic.

## Out of scope

- No new runtime behavior. No moving `types`/`errors`/`_compat`/`sdk_skill`.
- No `bundles/tools/` nesting. No kernel changes.
