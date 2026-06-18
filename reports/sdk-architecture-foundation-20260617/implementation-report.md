# SDK Architecture Foundation — Implementation Report

**Branch:** `sdk/architecture-foundation-20260617`
**Base:** `origin/main@9fd62de`
**Worktree:** `.worktrees/sdk-architecture-foundation-20260617`
**Date:** 2026-06-17

## 1. Design summary

This PR establishes the **foundation** for the LingTai kernel → SDK split per
Jason's approved design. It is deliberately modest and low-risk: it adds a
public doorway and *seed contracts*, and changes **no** existing
`lingtai`/`lingtai_kernel` runtime or CLI behavior.

A third top-level package, **`lingtai_sdk`** (`src/lingtai_sdk/`), is introduced
as the curated public front door. It is a consumer of the two implementation
packages, never a dependency of them:

```
lingtai_sdk ──▶ lingtai (lazy)  ──▶ lingtai_kernel
lingtai_sdk ──▶ lingtai_kernel (eager)
```

Pillars:

1. **Eager-kernel / lazy-wrapper doorway.** `import lingtai_sdk` loads only the
   dependency-light kernel; wrapper-backed names (`Agent`, services) resolve
   lazily via PEP 562 `__getattr__`. A bare import pulls no heavy provider SDK
   (anthropic/openai/google-genai/mcp). (Borrowed conceptually from Candidate E.)
2. **Compatibility by re-export, never fork.** A machine-readable migration map
   (`_compat.py`) records legacy→SDK import paths; a round-trip test asserts each
   pair resolves to the *same object*. (Candidate E.)
3. **Runtime contract seed** (`runtime.py`) — pure DTOs + `Runtime`/`RuntimeSession`
   ABCs describing how a future live runtime is driven. No live runtime; no
   kernel import. (Borrowed conceptually from Candidate B.)
4. **CapabilityBundle manifest seed** (`capabilities.py`) — the public schema for
   what a bundle *declares* (role flags, surfaces, security, transport), with a
   harmless metadata-only `proof_bundle()`. Public DTOs in the SDK; native
   privileged handlers stay in kernel/wrapper (decision #2). No core
   `system`/`psyche`/`soul` migration (decision #3).
5. **Architecture docs** — `docs/sdk/architecture-foundation.md` explains the
   SDK/CLI split, import-purity rule, compatibility strategy, the contract
   seeds, and the staged roadmap.

### Jason-approved decisions, mapped to delivery

| Decision | How honored |
|---|---|
| #1 Add `lingtai_sdk` in-repo now; rename distribution later | Package added under `src/`; ships in the existing `lingtai` wheel; rename listed as roadmap stage 5. |
| #2 Public DTOs in SDK; native privileged handlers in kernel/native | `runtime.py` + `capabilities.py` are pure public DTOs; no handler logic; documented as "public schema only". |
| #3 Skeleton + low-risk proof; do NOT migrate system/psyche/soul | Only `proof_bundle()` (synthetic, metadata-only); core bundles explicitly deferred. |
| #4 No Anthropic backend now | Not implemented; documented as deferred (roadmap stage 4). |
| #5 Worktree-only and proven before merge | All work in this worktree; full suite green (see §4). No push/PR/merge. |

## 2. Files

New package `src/lingtai_sdk/`:

| File | Purpose |
|---|---|
| `__init__.py` | Curated public surface: eager kernel re-exports + PEP 562 lazy wrapper names + `__version__`. |
| `_version.py` | Best-effort `__version__` from `lingtai` distribution metadata; falls back to `0+unknown`. |
| `types.py` | Kernel public type re-exports (config/state/message/LLM-protocol) under a stable path. |
| `errors.py` | `LingTaiSDKError` base + kernel `UnknownToolError` re-export. |
| `_compat.py` | `Deprecation` DTO, `DEPRECATIONS` map, `active_aliases()`, `migration_for()`. |
| `runtime.py` | Runtime contract seed: `RuntimeState`, `EventKind`, `RuntimeOptions`, `RuntimeMessage`, `RuntimeEvent`, `RuntimeSession`, `Runtime`. |
| `capabilities.py` | CapabilityBundle manifest seed: `RoleFlags`, `CapabilitySurfaces`, `SecurityPolicy`, `TransportSpec`, `BundleManifest`, `BackendReplaceability`, `proof_bundle()`. |
| `ANATOMY.md` | Per-folder anatomy (6-section template). |

New tests `tests/`:

| File | Coverage |
|---|---|
| `test_sdk_import_purity.py` | Bare import loads no wrapper / heavy provider; kernel names stay clean; lazy `Agent`/`VisionService` resolve to the same wrapper object; unknown attr → `AttributeError`. (4 tests) |
| `test_sdk_compat.py` | Migration map non-empty; each active alias resolves to the same object; `migration_for` lookup; `is_active_alias` flag. (9 tests, parametrized) |
| `test_sdk_runtime_contract.py` | DTO defaults; `for_adapter`; event constructors; ABC non-instantiable; a usable concrete subclass drives the full contract; module import purity. (7 tests) |
| `test_sdk_capabilities.py` | Proof bundle validity; `native_only` invariants; required name/version; `to_dict` round-trip (enum→value); empty surfaces; module import purity. (7 tests) |

New docs:

| File | Purpose |
|---|---|
| `docs/sdk/architecture-foundation.md` | Human-readable architecture foundation: SDK/CLI split, import purity, compatibility, contract seeds, staged roadmap, deferred list. (force-added; `docs/` is gitignored) |

No existing files were modified. `pyproject.toml` needed **no change** — the
existing `include = ["lingtai*", "lingtai_kernel*"]` glob already discovers
`lingtai_sdk` (verified via `setuptools.find_packages`).

## 3. Commits

```
1c30b11 docs(sdk): add SDK package anatomy and architecture-foundation doc
5fa1a90 feat(sdk): add CapabilityBundle manifest seed + harmless proof bundle
13589c2 feat(sdk): add runtime contract seed (DTOs + Runtime/RuntimeSession ABCs)
11cc3f4 feat(sdk): add migration map + same-object compat round-trip test
bb089b3 feat(sdk): add lingtai_sdk public doorway (eager-kernel, lazy-wrapper)
```

(Plus a final commit adding this report.)

## 4. Tests & validation

All run with `PYTHONPATH=src` (the editable install resolves to a different
worktree; `PYTHONPATH=src` pins imports to this worktree's source).

| Check | Result |
|---|---|
| `git diff --check origin/main...HEAD` | clean (no whitespace/conflict markers) |
| Packaging discovery (`find_packages('src')`) | `['lingtai_sdk']` — auto-discovered, no pyproject change |
| New SDK tests (4 files) | **27 passed** |
| Regression slice (`workdir/loop_guard/token/notification/filesystem_mail`) | **211 passed** |
| CLI/runtime smoke (`import lingtai; from lingtai.cli import main`) | ok, version `0.12.3` |
| **Full suite** (`pytest tests/`) | **2272 passed, 3 skipped, 0 failed** (4m16s) |

The 3 skips are pre-existing and unrelated to this change. No new failures.


## 5. Jason clarification: top-level assets and `lingtai-sdk-skill`

After the initial implementation pass, Jason clarified that the design should not
be read as abandoning top-level assets. The intended direction is that a top-level
`lingtai-sdk-skill` can replace/superset top-level assets: it holds system prompt
templates and related reusable prompt fragments while also serving as an
observation entrypoint for other coding agents.

This has been reflected in `docs/sdk/architecture-foundation.md`. In practical
terms:

- `CapabilityBundle.manual` points at skill/manual layouts;
- per-capability manuals/assets can stay local to each bundle;
- top-level SDK-wide templates can live in a future `lingtai-sdk-skill/` with
  `SKILL.md`, `reference/`, `templates/`, and `scripts/`;
- CLI/backend assembly chooses which templates to load for a runtime/profile.

## 6. Risks

- **Low.** The package is additive and import-light; nothing existing is
  modified, and the full suite is green.
- **Import purity is environment-sensitive.** The purity test deliberately
  excludes the bare `google` namespace stub (pulled transitively by `filelock`,
  `__file__ is None`) and targets heavy submodules (`google.genai`, etc.). If a
  future kernel dependency starts importing a real provider SDK at module load,
  the purity test will (correctly) fail — that is the intended guardrail.
- **Version coupling.** `lingtai_sdk.__version__` tracks the `lingtai`
  distribution. Until the distribution is renamed/split (roadmap stage 5), the
  SDK has no independent version; this is intentional per decision #1.
- **Contracts are seeds, not load-bearing yet.** `runtime.py`/`capabilities.py`
  are not wired into any live code path, so they carry no runtime risk; the
  trade-off is that their ergonomics are only validated by tests + the proof
  bundle, not by a real runtime. That validation is staged (roadmap 1–3).

## 7. Intentionally left for later

- A live `NativeRuntime` (thin `Runtime`/`RuntimeSession` over the existing
  `Agent`, no kernel turn-loop changes). Roadmap stage 1.
- Expressing one or two low-risk real capabilities as `BundleManifest`s and
  wiring the wrapper to read them. Roadmap stage 2.
- Migration of core `system`/`psyche`/`soul` bundles. Roadmap stage 3.
- A non-native (e.g. Anthropic) backend. Roadmap stage 4.
- Distribution/package rename so `lingtai_sdk` is the headline import. Roadmap
  stage 5.
- A `lingtai-sdk-doctor`-style audit that scans a consumer's imports and prints
  the recommended move from `_compat.DEPRECATIONS` (nice-to-have).

## 8. PR title / body draft

**Title:** `feat(sdk): architecture foundation — lingtai_sdk public doorway + contract seeds`

**Body:**

> ## What
> Establishes the foundation for the kernel → SDK split (Jason-approved design,
> stage 0). Adds a new public package `lingtai_sdk` as a curated front door,
> plus seed contracts for the runtime and capability-bundle manifest, a
> compatibility map, import-purity tests, anatomy, and an architecture doc.
>
> This PR is deliberately small and low-risk: **shapes and a doorway, not a
> live runtime or a bundle migration.** No existing `lingtai`/`lingtai_kernel`
> runtime or CLI behavior changes.
>
> ## Highlights
> - **Eager-kernel / lazy-wrapper doorway** — `import lingtai_sdk` loads only the
>   dependency-light kernel; `Agent`/services resolve lazily (PEP 562). A bare
>   import pulls no heavy provider SDK. Enforced by a subprocess test.
> - **Compatibility by re-export** — `_compat.DEPRECATIONS` maps legacy→SDK
>   paths; a round-trip test asserts each pair is the *same object*.
> - **Runtime contract seed** (`lingtai_sdk.runtime`) — pure DTOs +
>   `Runtime`/`RuntimeSession` ABCs. No live runtime.
> - **CapabilityBundle manifest seed** (`lingtai_sdk.capabilities`) — public
>   schema (role flags, surfaces, security, transport) + a harmless
>   metadata-only `proof_bundle()`. No `system`/`psyche`/`soul` migration.
> - **Docs** — `docs/sdk/architecture-foundation.md` (SDK/CLI split,
>   import-purity, compatibility, contract seeds, staged roadmap).
>
> ## Deferred (next PRs)
> Live NativeRuntime; real capability-bundle adoption; core bundle migration;
> Anthropic backend; distribution rename. See the roadmap in the architecture
> doc.
>
> ## Tests
> - 27 new SDK tests (import-purity, compat, runtime contract, capabilities).
> - Full suite: **2272 passed, 3 skipped, 0 failed.**
> - `git diff --check` clean; packaging auto-discovers `lingtai_sdk` (no
>   `pyproject.toml` change).

## 9. Notes on running tests

This worktree has no local `venv`; the system `python` editable-installs
`lingtai` from a *different* worktree. Always run tests here with
`PYTHONPATH=src` so imports resolve to this worktree:

```bash
PYTHONPATH=src python -m pytest tests/test_sdk_import_purity.py tests/test_sdk_compat.py \
    tests/test_sdk_runtime_contract.py tests/test_sdk_capabilities.py -q
```
