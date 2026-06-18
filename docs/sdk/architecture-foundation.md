# LingTai SDK — Architecture Foundation

> Status: **foundation PR**. This document describes the first, deliberately
> modest step of the kernel → SDK split. It establishes a public doorway and
> seed contracts; it does **not** migrate the runtime, the core bundles, or the
> distribution. The "Staged roadmap" section lists what is intentionally left
> for later.

## 1. Where we are today

The repository ships two packages inside one `lingtai` PyPI wheel:

- **`lingtai_kernel`** (`src/lingtai_kernel/`) — the minimal agent runtime.
  Dependency-light, usable standalone. Owns `BaseAgent`, intrinsics, the LLM
  protocol ABCs + service, mail/logging services, and core utilities.
- **`lingtai`** (`src/lingtai/`) — the batteries-included wrapper. Depends on
  the kernel one-directionally. Owns `Agent`, the 19 capabilities, LLM adapter
  implementations, FileIO/Vision/Search services, MCP, the CLI, and addons.

The dependency rule is strict and one-way: **the kernel never imports the
wrapper.**

## 2. What this PR adds: a third public name

This PR introduces a third top-level package, **`lingtai_sdk`** (`src/lingtai_sdk/`),
as the curated *public front door*. It is a consumer of the other two packages,
never a dependency of them:

```
lingtai_sdk  ──imports──▶  lingtai          (lazy)
            ──imports──▶  lingtai_kernel   (eager)

lingtai      ──imports──▶  lingtai_kernel
lingtai_kernel              (imports neither)
```

The SDK package is shipped inside the existing `lingtai` wheel for now; a
distribution/package rename is a later step (see roadmap).

### SDK / CLI split

- **SDK** = the curated *importable* surface (`import lingtai_sdk`) plus the
  contract DTOs. It is what an embedder builds against in-process.
- **CLI** stays exactly where it is: `lingtai.cli` (`lingtai-agent run …`). This
  PR does not touch the CLI; existing entrypoints and runtime behavior are
  unchanged.

## 3. Import purity: eager kernel, lazy wrapper

`import lingtai_sdk` must stay as dependency-light as `import lingtai_kernel` —
safe in tooling, type stubs, and environments where the wrapper's provider SDKs
(anthropic, openai, google-genai, mcp, …) are not installed.

- **Eager** (loaded at `import lingtai_sdk`): `BaseAgent` and the kernel-backed
  `types` / `errors` names. The kernel pulls no heavy provider SDK.
- **Lazy** (loaded on first attribute access): `Agent` and the service classes,
  resolved via :pep:`562` `__getattr__` and cached thereafter. Touching
  `lingtai_sdk.Agent` imports `lingtai`; if the wrapper is absent you get a
  clear `ModuleNotFoundError` naming `lingtai`, not an import-time crash.

This is enforced by a subprocess test (`tests/test_sdk_import_purity.py`) that
asserts a bare `import lingtai_sdk` loads neither the wrapper nor any heavy
provider submodule.

> Implementation note: importing the kernel does load the *bare* `google`
> namespace package — an ambient site-packages artifact pulled in transitively
> by `filelock` (`google.__file__ is None`). It is a harmless stub, not a
> provider SDK, so the purity check targets heavy submodules (`google.genai`,
> `google.generativeai`) rather than bare `google`.

## 4. Compatibility: re-export, never fork

The SDK exposes existing names under a stable public path **by re-export**, so
the SDK name and the legacy name are the *same object*. There is no parallel
hierarchy and no re-implementation.

`lingtai_sdk._compat.DEPRECATIONS` is the machine-readable map of legacy →
recommended-SDK import paths. No name is removed within a major version; a name
graduates from "active alias" to "removed" only across a major bump (its
`removed_in` is filled). A round-trip test (`tests/test_sdk_compat.py`) asserts
every active alias resolves to the same object on both sides.

### Migration table (current)

| Legacy path | SDK path | Symbol |
|---|---|---|
| `lingtai_kernel.BaseAgent` | `lingtai_sdk.BaseAgent` | `BaseAgent` |
| `lingtai.Agent` | `lingtai_sdk.Agent` | `Agent` |
| `lingtai_kernel.config.AgentConfig` | `lingtai_sdk.types.AgentConfig` | `AgentConfig` |
| `lingtai_kernel.state.AgentState` | `lingtai_sdk.types.AgentState` | `AgentState` |
| `lingtai_kernel.message.Message` | `lingtai_sdk.types.Message` | `Message` |
| `lingtai_kernel.types.UnknownToolError` | `lingtai_sdk.errors.UnknownToolError` | `UnknownToolError` |

The legacy paths keep working — this table records the *recommended* move, not a
breaking change.

## 5. Runtime contract seed

`lingtai_sdk.runtime` defines the provider-agnostic shapes for driving a future
live runtime: options in, messages in, a stream of events out. They are pure
dataclasses/ABCs with **no kernel import**, so importing the contract is
provider-free.

- `RuntimeOptions` — backend-neutral inputs (working dir, provider/model,
  capabilities, manifest, adapter-scoped `extra`).
- `RuntimeMessage` — an inbound message handed to a session.
- `RuntimeEvent` + `EventKind` — an outbound event (state/text/tool/usage/…),
  with `state()`/`text()`/`error()` convenience constructors.
- `RuntimeState` — coarse session lifecycle (pending/active/idle/asleep/stuck/stopped).
- `RuntimeSession` / `Runtime` — the ABCs a backend implements. `Runtime.run()`
  is a small convenience that creates and starts a session.

There is **no live runtime in this PR.** A thin `NativeRuntime` (wrapping the
existing `Agent` unchanged) and any non-native backend land later, once the
shapes have stabilized against the capability/prompt contracts.

## 6. CapabilityBundle manifest seed

`lingtai_sdk.capabilities` defines the *public schema* for what a capability
bundle **declares** — decoupled from how it is **implemented**. Native,
privileged handlers stay in the kernel/wrapper; only the declaration lives in
the SDK. This lets the kernel, the wrapper, and external embedders agree on the
shape of a bundle without coupling to its internals.

- `BundleManifest` — `name`, `version`, `summary`, `roles`, `surfaces`,
  `security`, `transport`, `manual`, `metadata`; with `validate()` and
  `to_dict()`.
- `RoleFlags` — `required`, `privileged`, `native_only`, `can_override`,
  `backend_replaceability` (`native_only` / `replaceable` / `augmentable`).
  Invariant: a `native_only` bundle must also be `privileged` and declare
  `backend_replaceability=NATIVE_ONLY`.
- `CapabilitySurfaces` — the named surfaces a bundle contributes: `tools`,
  `resources`, `prompts`, `events`, `hooks`, `lifecycle`, `state`.
- `SecurityPolicy` — `permissions`, `requires_confirmation`, `danger`.
- `TransportSpec` — `kind` (`native` / `stdio` / `http` / `in_process`) + config.

The only concrete bundle is `proof_bundle()` — a synthetic, metadata-only,
read-only `echo` bundle that exercises the schema end to end at the lowest
possible risk. **Core bundles (`system` / `psyche` / `soul`) are deliberately
not migrated in this PR.**


## 7. Top-level assets and `lingtai-sdk-skill`

The CapabilityBundle model does **not** require abandoning top-level SDK assets.
Instead, the long-term direction is that a top-level **`lingtai-sdk-skill`** can
act as a superset and observation entrypoint for those assets:

- it can contain the system-prompt templates and reusable prompt fragments that
  used to look like standalone top-level assets;
- it can use the familiar skill layout (`SKILL.md`, `reference/`, `scripts/`,
  `templates/`, examples) so other coding agents can inspect LingTai's operating
  model without importing the runtime;
- the CLI/backend assembly layer decides which templates from that skill are
  loaded for a specific runtime/profile;
- CapabilityBundles may point at skill/manual assets, but they do not force every
  asset to live inside each bundle.

So the relationship is:

```text
lingtai-sdk-skill/        # top-level skill-shaped asset superset and observer entry
  SKILL.md
  reference/
  templates/             # system prompt templates, covenant/procedures/substrate fragments
  scripts/

CapabilityBundle.manual  # per-capability manual/asset pointer into skill-shaped layouts
```

This preserves the usefulness of top-level assets while still giving the SDK a
single, skill-shaped semantic container for manuals, templates, and coding-agent
observation.

## 8. Staged roadmap

This PR is **stage 0: the foundation.** Each later stage is its own reviewable
PR, sequenced so contracts stabilize before implementations depend on them.

1. **Stage 0 (this PR) — foundation.** Public doorway, import-purity guarantee,
   compatibility map, runtime + capability-bundle contract seeds, docs.
2. **Stage 1 — live NativeRuntime.** A thin `Runtime`/`RuntimeSession`
   implementation wrapping the existing `Agent` (no kernel turn-loop changes),
   driven through the contract from stage 0. Wrapper-side, tested against a fake
   agent.
3. **Stage 2 — capability-bundle adoption.** Express one or two *low-risk*
   real capabilities as `BundleManifest`s and wire the wrapper to read the
   manifest. Still no `system`/`psyche`/`soul`.
4. **Stage 3 — core bundle migration.** Migrate the privileged core bundles,
   once the manifest schema and the native runtime have proven out.
5. **Stage 4 — non-native backend (e.g. Anthropic).** Only after the runtime,
   capability, and prompt contracts are stable. Maps `RuntimeOptions` onto the
   provider client and bridges its message stream onto `RuntimeEvent`.
6. **Stage 5 — distribution rename.** Split or rename the published package so
   `lingtai_sdk` is the headline import; today it rides inside the `lingtai`
   wheel.

### Intentionally deferred (NOT in this PR)

- Any live runtime (`NativeRuntime` and friends).
- An Anthropic (or other non-native) backend.
- Migration of core `system` / `psyche` / `soul` bundles.
- Distribution / package rename.
- Changes to the CLI or to existing `lingtai` / `lingtai_kernel` runtime
  behavior.

## 9. Files

```
src/lingtai_sdk/
  __init__.py        # curated public surface (eager kernel, lazy wrapper)
  _version.py        # best-effort __version__ from lingtai metadata
  types.py           # kernel type re-exports under a stable path
  errors.py          # LingTaiSDKError + kernel UnknownToolError
  _compat.py         # legacy -> SDK migration map
  runtime.py         # runtime contract seed (DTOs + ABCs)
  capabilities.py    # CapabilityBundle manifest seed + proof_bundle()
  ANATOMY.md         # per-folder anatomy

tests/
  test_sdk_import_purity.py     # bare import loads no wrapper / heavy provider
  test_sdk_compat.py            # legacy paths resolve to the same object
  test_sdk_runtime_contract.py  # runtime DTOs + a usable concrete subclass
  test_sdk_capabilities.py      # manifest invariants + proof bundle
```
