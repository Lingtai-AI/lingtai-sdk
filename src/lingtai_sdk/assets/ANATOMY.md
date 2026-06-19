# assets

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

The SDK's committed, **read-only resource package** — the `lingtai_sdk.assets` resource root. It ships skill-shaped asset trees that other code reads via `importlib.resources`, never via raw filesystem paths, so they resolve identically from a source checkout and an installed wheel. It carries **no code and no runtime**; the only Python here is a package marker.

> **What is an `ANATOMY.md`?** See the canonical convention referenced from the parent `src/lingtai_sdk/ANATOMY.md`. This file follows the same 6-section template.

## Components

- `__init__.py` — package marker only (no code, no runtime). Its sole purpose is to make `lingtai_sdk.assets` an addressable resource package so `importlib.resources.files("lingtai_sdk.assets")` can traverse into the asset trees. `__all__` is empty.
- `lingtai-sdk-skill/` — the committed **`lingtai-sdk-skill`** asset, the top-level SDK *observation entry* (`docs/sdk/architecture-foundation.md` §7).
  - `SKILL.md` — a read-only, skill-shaped description of the SDK boundary: the SDK/kernel/wrapper split and one-way dependency rule, the runtime contract (`lingtai_sdk.runtime`), the CapabilityBundle contract (`lingtai_sdk.capabilities` + `capability_host`), and the privileged-core deferral (why `system`/`psyche`/`soul` are NOT here). It has YAML frontmatter (`name`/`description`) like every other skill in the tree. Read by `lingtai_sdk.sdk_skill.load_sdk_skill()`.
  - `scripts/` — SDK-oriented verification helpers that belong with the skill-shaped SDK observation asset instead of the repository root. `sdk_shape_import_smoke.py` is the quick manual/agent import-shape smoke; `smoke_wheel_sidecar.py` is the dependency-free installed-wheel sidecar smoke invoked by `.github/workflows/wheels.yml`.

## Connections

- **Inbound — `lingtai_sdk.sdk_skill`.** `load_sdk_skill()` reads `lingtai-sdk-skill/SKILL.md` through `importlib.resources.files("lingtai_sdk.assets")`. The `sdk_skill` bundle's `manual` pointer records the package-relative path `lingtai-sdk-skill/SKILL.md`.
- **Inbound — packaging.** `pyproject.toml` packages this tree via the `lingtai_sdk = ["assets/**/*"]` `package-data` glob, so new files under a skill directory are included without editing the list.
- **Inbound — wheel CI.** `.github/workflows/wheels.yml` runs `lingtai-sdk-skill/scripts/smoke_wheel_sidecar.py` from the source tree as cibuildwheel's installed-wheel smoke test.
- **Outbound — none.** This package imports nothing. It is a leaf data directory; reading it pulls in no wrapper and no provider SDK (verified by `tests/test_sdk_skill_bundle.py::test_sdk_skill_import_is_pure`).

## Composition

- **Parent:** `src/lingtai_sdk/` (see `ANATOMY.md`).
- **Siblings:** `sdk_skill.py` is the sole consumer; `bundles/contracts.py`/`bundles/host.py` (legacy paths `capabilities.py`/`capability_host.py`) define the bundle/host contract the asset is adopted through.
- **Subfolders:** `lingtai-sdk-skill/` — one skill-shaped asset tree with its own `SKILL.md` and `scripts/` helpers. Future SDK assets land as additional sibling trees here.

## State

- **On-disk:** the asset files (`lingtai-sdk-skill/SKILL.md` plus `lingtai-sdk-skill/scripts/*`) — read-only at runtime, the source of truth for the SDK skill text and its small verification helpers. Edited by developers; never mutated by the agent or the SDK.
- **In-memory:** none. There is no cache; `load_sdk_skill()` re-reads the resource each call (deterministic, network-free).

## Notes

- **Resource-addressed, not path-addressed.** Always reach these files via `importlib.resources` (`resources.files("lingtai_sdk.assets")`), never by computing a filesystem path off `__file__`. This is what keeps the asset readable from a zipped/installed wheel.
- **Read-only, non-privileged.** Assets here are inert text. The CapabilityBundle that adopts one (`sdk_skill.sdk_skill_bundle()`) is non-privileged and `in_process`; nothing here touches the kernel turn loop or the privileged core.
