---
name: lingtai-sdk-skill
description: The top-level observation entry for the LingTai SDK — how the public doorway (lingtai_sdk) layers over the kernel and wrapper, the runtime contract, the CapabilityBundle contract, and what privileged core behavior is deliberately NOT migrated yet. Read this when a coding agent or system prompt needs LingTai's operating model without importing the runtime.
---

# lingtai-sdk-skill

> A **skill-shaped, read-only** description of the LingTai SDK boundary. It is a
> committed asset shipped inside `lingtai_sdk` and reachable via
> `importlib.resources` — no runtime import required. A coding agent can read it
> to understand LingTai's split before touching any code; a system prompt can
> point at it as the canonical SDK orientation.

This skill is the *superset and observation entry* for top-level SDK assets
described in `docs/sdk/architecture-foundation.md` §7. It does not host a live
runtime and contains no privileged behavior. It explains four things:

1. the SDK / kernel / wrapper split and its one-way dependency rule,
2. the runtime contract (`lingtai_sdk.runtime`),
3. the CapabilityBundle contract (`lingtai_sdk.capabilities` + `capability_host`),
4. the privileged-core deferral — why `system` / `psyche` / `soul` are not here.

## 1. The split: doorway, kernel, wrapper

`lingtai_sdk` is the **public doorway**. It re-exports two implementation
packages under one stable, typed path:

- `lingtai_kernel` — the minimal standalone runtime, with zero hard heavy
  third-party dependencies.
- `lingtai` — the batteries-included wrapper: adapters, capabilities, CLI, and
  the provider SDKs (`anthropic` / `openai` / `google-genai` / `mcp` / …).

The dependency direction is **one-way**:

```
lingtai_sdk  ->  (lingtai, lingtai_kernel)
```

The kernel and wrapper must never import `lingtai_sdk`. The SDK is a consumer of
the other two, never a dependency of them.

### Import purity (eager kernel, lazy wrapper)

`import lingtai_sdk` loads the dependency-light kernel only — it is as cheap and
side-effect-free as `import lingtai_kernel`. Wrapper-backed names (`Agent`, the
service classes) resolve **lazily** via PEP 562 `__getattr__`: touching
`lingtai_sdk.Agent` imports `lingtai` on first access. So a pure-kernel consumer,
or tooling that only reads contracts and assets, never pays for the wrapper's
heavy provider SDKs.

The SDK's own contract modules (`runtime`, `capabilities`, `capability_host`,
the asset loaders) are likewise import-pure: importing them pulls in no wrapper
and no provider SDK. This is enforced by the import-purity tests.

## 2. The runtime contract (`lingtai_sdk.runtime`)

`runtime.py` is a **contract seed**: pure DTOs and ABCs describing how a future
live runtime is driven, with no kernel import.

- `RuntimeOptions` — provider/model/config in.
- `RuntimeMessage` — messages in.
- `RuntimeEvent` (with `state` / `text` / `error` / `tool_call` / `tool_result`
  / `usage` constructors) — events out.
- `RuntimeSession` / `Runtime` ABCs — the driving interface.

The only live implementation is the `NativeRuntime` adapter (`native.py`), which
wraps the existing wrapper `Agent` unchanged — no kernel turn-loop change. It
translates `RuntimeOptions` into `Agent` kwargs, builds the `LLMService` from
options/manifest, drives the start/stop lifecycle, and bridges the running
agent's activity onto `RuntimeEvent`s. It is a skeleton, not a non-native
backend.

## 3. The CapabilityBundle contract

A **CapabilityBundle** is a unit of agent capability described by a *manifest* —
a public declaration of what the bundle contributes, separate from how it is
implemented.

### The manifest (`lingtai_sdk.capabilities`)

`BundleManifest` declares a bundle's identity (`name`, `version`, `summary`),
its `RoleFlags` (privilege / role posture), the `CapabilitySurfaces` it
contributes (`tools`, `resources`, `prompts`, `events`, `hooks`, `lifecycle`,
`state` — *names only*), its `SecurityPolicy`, its `TransportSpec`, and a
`manual` tuple of skill/manual asset paths.

- `BundleManifest.to_dict()` / `load_manifest(data)` are inverses: a host
  receives a declaration as data and gets back a **validated** typed manifest.
  `load_manifest` reconstructs the nested frozen dataclasses and the
  `BackendReplaceability` enum, then `validate()`s, so a loaded manifest is
  always a valid one. Unknown keys are ignored (forward compatibility).
- The manifest is a *declaration*. Native privileged handlers stay in the
  kernel/wrapper, never in the manifest.

### The host boundary (`lingtai_sdk.capability_host`)

`BundleHost` is the **non-native, in-process** host for a single validated,
non-privileged bundle. It is deliberately conservative:

- it **validates** the manifest on registration;
- it **refuses** any `privileged` / `native_only` bundle — only the native
  runtime may host those;
- it refuses any transport that is not `in_process`;
- it enforces the manifest↔implementation contract for every hosted surface:
  every declared name has a handler, and no handler is undeclared.

`BundleHost` hosts read-only **tools**, **resources**, and **prompts**:

- `invoke(tool, **kwargs)` dispatches a declared tool;
- `read_resource(name)` returns a declared resource's content;
- `read_prompt(name, **kwargs)` returns a declared prompt's rendered text.

### The committed SDK skill bundle

`sdk_skill_bundle()` (in `lingtai_sdk.sdk_skill`) is a **real, non-privileged**
bundle — not a synthetic echo. It declares:

- a read-only `read_sdk_skill` tool that returns this `SKILL.md`,
- a `sdk_skill` resource carrying the same content,
- a `sdk_skill_orientation` prompt rendering a short orientation header.

`load_sdk_skill()` reads this file through `importlib.resources`, and
`sdk_skill_host()` returns a ready `BundleHost`. The whole path —
declared manifest → `load_manifest()` → `BundleHost` → `read_resource` /
`invoke` / `read_prompt` — is deterministic and network-free.

## 4. Privileged-core deferral

The privileged core bundles — **`system`, `psyche`, `soul`** — are **NOT**
migrated into `BundleManifest` form and are **NOT** hosted by `BundleHost`. They
touch kernel-protected surfaces and the turn loop; migrating them is a later,
higher-risk PR. `BundleHost` refusing `privileged` / `native_only` manifests is
exactly the guardrail that keeps them out of the in-process host.

This skill, the SDK skill bundle, and the host boundary prove the *asset and
adoption boundary* end to end against one harmless, real capability — without
migrating any privileged behavior or changing the kernel turn loop.

## See also

- `docs/sdk/architecture-foundation.md` — the staged roadmap and rationale.
- `src/lingtai_sdk/ANATOMY.md` — the per-folder anatomy of the SDK package.
