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

There is **no live runtime in this (stage 0) PR.** A thin `NativeRuntime`
(wrapping the existing `Agent` unchanged) lands in **stage 1** (see §8 and §10);
any non-native backend lands later, once the shapes have stabilized against the
capability/prompt contracts.

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
- `SecurityPolicy` — `permissions`, `requires_confirmation`, `danger`
  (validated against `SecurityDanger`: `safe` / `caution` / `destructive` — see
  §15.1).
- `TransportSpec` — `kind` (validated against `TransportKind`: `native` /
  `in_process` / `stdio` / `http` — see §15.1) + config.

The only concrete bundle is `proof_bundle()` — a synthetic, metadata-only,
read-only `echo` bundle that exercises the schema end to end at the lowest
possible risk. **Core bundles (`system` / `psyche` / `soul`) are deliberately
not migrated in this PR.**

### 6.1 The load/host boundary (proof)

The schema alone is a declaration; proving it is *usable* means walking the
whole path a non-native host walks for a declared bundle:

```
declared manifest (plain dict)
   -> capabilities.load_manifest()   # parse + validate -> typed BundleManifest
   -> capability_host.BundleHost      # register manifest + tool handlers
   -> BundleHost.invoke(tool, ...)    # call a declared, harmless tool
```

- `capabilities.load_manifest(data)` is the inverse of `BundleManifest.to_dict()`:
  it reconstructs the nested frozen dataclasses and the `BackendReplaceability`
  enum from a plain dict, then `validate()`s — so a loaded manifest is always a
  valid one. Unknown enum values, non-mapping nested blocks, or failed
  invariants raise `BundleLoadError`. Unknown keys are ignored (forward
  compatibility).
- `capability_host.BundleHost` is the **non-native** host. It validates the
  manifest on registration, **refuses** any `privileged`/`native_only` bundle
  (only the native runtime may host those — which is exactly why the core
  bundles stay out), and enforces the manifest↔implementation contract: every
  declared `surfaces.tools` name has a handler and no handler is undeclared.
  Breaches raise `BundleHostError`.
- `capability_host.proof_host()` wires `proof_bundle()` to a deterministic,
  network-free `echo` handler. `proof_host().invoke("echo", text="hi")` returns
  `{"echo": "hi"}` with no I/O — the end-to-end proof.

Both modules are import-pure (no wrapper, no provider SDK), so `import
lingtai_sdk.capability_host` stays as cheap as the schema. This proves the
*boundary*; it does **not** migrate any real intrinsic behavior, wire the
wrapper's real capabilities through manifests, or touch the kernel turn loop.


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

**Realized in stage 6 (§14):** the `lingtai-sdk-skill` asset is now committed at
`src/lingtai_sdk/assets/lingtai-sdk-skill/SKILL.md` and adopted as a real,
non-privileged CapabilityBundle (`lingtai_sdk.sdk_skill`). The asset and the
bundle exist; the broader CLI/backend assembly layer that *selects* templates per
profile is still future work.

## 8. Staged roadmap

This PR is **stage 0: the foundation.** Each later stage is its own reviewable
PR, sequenced so contracts stabilize before implementations depend on them.

1. **Stage 0 (this PR) — foundation.** Public doorway, import-purity guarantee,
   compatibility map, runtime + capability-bundle contract seeds, docs.
2. **Stage 1 — live NativeRuntime _(skeleton + LLM-service translation landed;
   see §10, §11)_.** A thin `Runtime`/`RuntimeSession` implementation wrapping
   the existing `Agent` (no kernel turn-loop changes), driven through the
   contract from stage 0. Tested against a fake agent — no real model, API key,
   or running process. Stacked on the stage-0 PR. The default-factory
   LLM-service translation (§11), `manifest.llm` translation (§12), and a
   **minimal live event bridge** (§13) landed as stacked follow-ups within
   this stage.
3. **Stage 2 — capability-bundle adoption** _(first real adoption landed; see
   §14)_. Express *low-risk* real capabilities as `BundleManifest`s. The
   committed `lingtai-sdk-skill` asset is now adopted as a real, non-privileged
   bundle hosted across tool/resource/prompt surfaces (§14); wiring the
   wrapper's own existing capabilities through manifests remains follow-up work.
   Still no `system`/`psyche`/`soul`.
4. **Stage 3 — core bundle migration.** Migrate the privileged core bundles,
   once the manifest schema and the native runtime have proven out.
5. **Stage 4 — non-native backend (e.g. Anthropic).** Only after the runtime,
   capability, and prompt contracts are stable. Maps `RuntimeOptions` onto the
   provider client and bridges its message stream onto `RuntimeEvent`.
6. **Stage 5 — distribution rename.** Split or rename the published package so
   `lingtai_sdk` is the headline import; today it rides inside the `lingtai`
   wheel.

### Intentionally deferred (NOT in the stage-0 PR)

- Any live runtime (`NativeRuntime` and friends). _(The stage-1 skeleton lands in
  the stacked PR described in §10.)_
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
  errors.py          # LingTaiSDKError (+ BundleLoadError/BundleHostError) + kernel UnknownToolError
  _compat.py         # legacy -> SDK migration map
  runtime.py         # runtime contract seed (DTOs + ABCs)
  capabilities.py    # CapabilityBundle manifest seed + load_manifest() + proof_bundle()
  capability_host.py # CapabilityBundle host boundary (BundleHost: tool/resource/prompt + proof_host)
  sdk_skill.py       # stage 6: committed lingtai-sdk-skill asset -> bundle -> host
  assets/            # committed read-only resource package (importlib.resources root)
    __init__.py      #   package marker (no code)
    ANATOMY.md       #   per-folder anatomy for the asset package
    lingtai-sdk-skill/
      SKILL.md       #   the top-level SDK observation-entry skill
  native.py          # NativeRuntime adapter (stages 1-4; wraps Agent; lazy)
  ANATOMY.md         # per-folder anatomy

tests/
  test_sdk_import_purity.py     # bare import loads no wrapper / heavy provider
  test_sdk_compat.py            # legacy paths resolve to the same object
  test_sdk_runtime_contract.py  # runtime DTOs (incl. activity constructors) + a usable concrete subclass
  test_sdk_capabilities.py      # manifest invariants + proof bundle
  test_sdk_capability_host.py   # manifest load + host boundary: load->validate->register->invoke; refuses privileged
  test_sdk_skill_bundle.py      # stage 6: committed skill asset -> bundle -> host (tool/resource/prompt), purity
  test_sdk_strict_manifest.py   # stage 7: strict bool/danger/transport/mapping validation
  test_sdk_native_bundle_host.py # stage 7: NativeBundleHost — privileged native hosting only with explicit authority
  test_sdk_native_runtime.py          # stage 1: translation, lifecycle, purity (fake agent)
  test_sdk_native_runtime_llm.py      # stage 2: LLM-service translation
  test_sdk_native_runtime_manifest.py # stage 3: manifest.llm translation
  test_sdk_native_runtime_events.py   # stage 4: live event bridge (fake agent)
```

## 10. Stage 1 — the NativeRuntime skeleton (stacked PR)

Stage 1 is a small PR **stacked on top of stage 0**. It adds the first live
runtime adapter — `lingtai_sdk.native.NativeRuntime` — proving the stage-0
contract can wrap the existing wrapper `Agent` without making the import
boundary worse. It deliberately stops at a *skeleton*: no kernel turn-loop
changes, no `LLMService` construction, no non-native backend.

### What it adds

- **`NativeRuntime(Runtime)`** (`id = "native"`) — a factory whose
  `create_session(options)` returns a `NativeRuntimeSession`. An injectable
  `agent_factory` lets tests substitute a fake agent.
- **`NativeRuntimeSession(RuntimeSession)`** — wraps an `Agent`:
  - `start()` builds the agent (lazily, via the factory), calls `Agent.start()`,
    and transitions `PENDING → ACTIVE`, emitting a `STATE` event. Idempotent.
  - `send(message)` normalizes `str`/`RuntimeMessage` and routes onto
    `Agent.send()` — the existing fire-and-forget inbox path, so it never blocks
    on a turn. Before `start()` it emits a non-fatal `ERROR` event and no-ops.
  - `events()` yields a non-blocking, re-iterable snapshot of queued
    lifecycle / notification / error events.
  - `stop()` calls `Agent.stop()` and transitions `→ STOPPED`. Idempotent.
- **`_agent_kwargs_from_options(options)`** — a pure translation helper
  returning `(agent_kwargs, deferred)`.

### Translation: applied vs. deferred

| `RuntimeOptions` field | Stage-1 handling |
|---|---|
| `working_dir` | → `Agent(working_dir=...)` (required) |
| `agent_name`, `capabilities`, `addons`, `streaming` | → `Agent(...)` kwargs when set |
| `provider`, `model`, `base_url`, `api_key` | stage 1: **deferred** → `session.deferred['llm']`. **Stage 2 (§11):** on the default factory path these build the `LLMService` and move to `session.applied['llm']` (secret-free) |
| `manifest`, `system_prompt_overrides`, `extra` | **deferred** → `session.deferred[...]` (recorded, not applied) |

Deferring rather than force-applying keeps stage 1 honest: anything recognized
but not yet wired is visible on the session, never silently dropped.

### Import purity

`native.py` imports only the pure `runtime` contract at module load. The
wrapper `Agent` is imported **lazily**, inside `start()` / the default factory.
`NativeRuntime` / `NativeRuntimeSession` are exported from the package root via
a separate `_LAZY_SDK_EXPORTS` map that resolves from the import-pure `.native`
module — so accessing the names and *constructing* a `NativeRuntime` stays free
of the wrapper's provider SDKs; they load only when a session actually starts.
A subprocess test asserts this.

### Tested without a model

All stage-1 tests run with no API key and no real agent process: a fake agent
is injected through `agent_factory`, so lifecycle transitions, event emission,
`send()` routing, and the translation table are all exercised in-memory.

## 11. Stage 2 — LLM-service translation (stacked PR)

Stage 2 is a small PR **stacked on top of the stage-1 skeleton**. It closes
GLM stage-1 review nit **N2**: the default `agent_factory` called
`Agent(**kwargs)` without a `service`, but the wrapper `Agent` (via `BaseAgent`)
requires one — so a real `start()` raised an opaque missing-`service`
`TypeError`. Stage 2 makes the default factory build the service the agent
needs, and fails loudly and early when it can't.

### What it adds

- **`_llm_service_from_options(options)`** — lazily imports
  `lingtai.llm.service.LLMService` (which registers the built-in adapters and
  pulls the active provider's SDK) and builds a service from `provider` /
  `model` / `base_url` / `api_key`. The lazy import keeps `import
  lingtai_sdk.native` and constructing a `NativeRuntime` provider-free.
- **`NativeRuntimeConfigurationError`** (`lingtai_sdk.errors`, subclass of
  `LingTaiSDKError`) — raised *before* any agent is constructed when the default
  factory is used but `provider`/`model` are partial or absent. The session
  stays `PENDING`; no opaque `TypeError` leaks. The message never echoes
  `api_key`.
- **`session.applied`** — a secret-free record of what was actually applied to
  the agent. On the default path, the LLM config moves out of
  `session.deferred['llm']` (cleared to `{}`) and into `session.applied['llm']`
  with `api_key` stripped (`_public_llm_fields`).
- **`_uses_default_factory`** — an injected `agent_factory` bypasses service
  building entirely (it supplies its own agent/service), so fakes and hosts boot
  without a real `LLMService` and the runtime stays network-free.

### Applied vs. deferred (stage 2)

| `RuntimeOptions` field | Default factory | Injected factory |
|---|---|---|
| `provider`, `model`, `base_url` | built into `LLMService`, recorded in `session.applied['llm']` | left in `session.deferred['llm']` (factory owns the service) |
| `api_key` | consumed into the `LLMService`; **never** stored on the session, in events, or in errors/reprs | **never** stored on the session; the injected factory owns any service/secret handling |
| `manifest`, `system_prompt_overrides`, `extra` | still deferred (init.json-level translation is a later stage) | still deferred |

### Secret hygiene

`api_key` is consumed into the `LLMService` constructor and **never** retained on
the session, surfaced in any `RuntimeEvent`, echoed in
`NativeRuntimeConfigurationError`, or written to `session.applied`. The applied
record is built by `_public_llm_fields`, which drops `api_key` by construction.

### Out of scope (deferred past stage 2)

- init.json-level manifest translation — stage 2 plumbs only
  `provider`/`model`/`base_url`/`api_key`; `api_compat`, `default_headers`,
  `compact_threshold`, `max_rpm`, `context_window`, etc.
  (`build_provider_defaults_from_manifest_llm`) land in **stage 3 (§12)**.
- A non-native backend, a live event bridge, and any kernel turn-loop change.

### Tested without a model

Stage-2 tests run with no API key and no network: the service builder is
monkeypatched with a fake, the default factory is exercised through a fake agent
that mirrors the real `service`-required contract, and a subprocess test asserts
that constructing a `NativeRuntime` and a session with LLM options stays
provider-free (`tests/test_sdk_native_runtime_llm.py`).

## 12. Stage 3 — manifest.llm translation (stacked PR)

Stage 3 is a small PR **stacked on top of stage 2**. It picks up the
init.json-level manifest translation that stage 2 left out of scope: the default
factory can now derive its `LLMService` config (and recognized provider defaults
/ context window) from `RuntimeOptions.manifest`, especially `manifest['llm']`,
when the explicit `RuntimeOptions` fields are absent.

### What it adds

- **`_llm_config_from_options(options)`** — a pure helper that merges the LLM
  config. Explicit `RuntimeOptions.provider/model/base_url/api_key` win; the
  `manifest['llm']` block fills only the fields the caller left unset. Never
  imports `lingtai`.
- **`_max_rpm_from_options_or_manifest(options)`** — resolves `max_rpm` in
  precedence order: `extra['native']['max_rpm']` → `extra['max_rpm']` →
  `manifest['max_rpm']` → `manifest['llm']['max_rpm']`. Returns `0` when unset —
  unlike the CLI (which defaults to 60), the SDK does **not** impose RPM gating
  unless a host opts in.
- **`_context_window_from_options_or_manifest(options)`** — resolves an optional
  `context_window`: `manifest['llm']['context_window']` →
  `manifest['context_limit']` → `extra['native']['context_window']` →
  `extra['context_window']`. Returns `None` (service keeps its own default) when
  unset.
- **`_llm_service_from_options` (updated)** — uses the merged config, and when a
  `manifest['llm']` block is present, plumbs recognized provider defaults
  through `build_provider_defaults_from_manifest_llm` **scoped to the merged
  provider** (so an explicit `provider` override does not strand the defaults
  under the manifest's provider key), and passes `context_window` when resolved.
  Both `LLMService` and the defaults builder are lazily imported — module-load
  import purity is unchanged.
- **Sanitized deferred manifest** — `session.deferred['manifest']` is a copy
  with `manifest['llm']['api_key']` redacted, so a manifest-carried secret never
  reaches the public deferred surface. The original `RuntimeOptions.manifest` is
  left untouched.

### Applied vs. deferred (stage 3)

| `RuntimeOptions` source | Default factory |
|---|---|
| explicit `provider`/`model`/`base_url` **or** `manifest['llm'].*` | merged (explicit wins), built into `LLMService`, recorded in `session.applied['llm']` |
| explicit `api_key` **or** `manifest['llm']['api_key']` | consumed into the `LLMService`; **never** stored on the session, in events, or in errors/reprs; redacted from `session.deferred['manifest']` |
| `manifest` provider defaults (`api_compat`, `default_headers`, `compact_threshold`, `max_rpm`) | passed to `LLMService(provider_defaults=...)`, scoped to the merged provider |
| `context_window` / `context_limit` | passed to `LLMService(context_window=...)` when resolved |

If both explicit and manifest LLM config are absent/partial, the merge still
yields no `provider`/`model` and `_llm_service_from_options` raises
`NativeRuntimeConfigurationError` (session stays `PENDING`, message never echoes
`api_key`) — same contract as stage 2, now evaluated *after* the manifest merge.

### Out of scope (still deferred)

- Migrating `system` / `psyche` / `soul` into `BundleManifest` form.
- A non-native (e.g. Anthropic) backend.
- A live event bridge from the agent's activity onto `RuntimeEvent` _(landed in
  the stacked follow-up described in §13)._
- Any kernel turn-loop / BaseAgent / Agent change.

### Tested without a model

Stage-3 tests run with no API key and no network: a fake `LLMService` and a fake
`build_provider_defaults_from_manifest_llm` are monkeypatched onto
`lingtai.llm.service` to capture their arguments, the default factory is
exercised through a `service`-required fake agent, the pure merge/`max_rpm`/
`context_window` helpers are asserted directly, and a subprocess test confirms
constructing a `NativeRuntime` and a session with a `manifest['llm']` block stays
provider-free (`tests/test_sdk_native_runtime_manifest.py`).

## 13. Stage 4 — a minimal live event bridge (stacked PR)

Stage 4 is a small PR **stacked on top of stage 3**. It closes the last item
§10 left open for the `NativeRuntime` line: making the running agent's *existing*
activity observable through the stage-0 `RuntimeEvent` contract, so a host that
drives a session through `events()` sees more than lifecycle/notification/error
records. It does this **without touching the kernel turn loop, BaseAgent, or
Agent** — the bridge is built entirely from surfaces the kernel already exposes.

### What it adds

- **`RuntimeEvent.tool_call` / `.tool_result` / `.usage`** — three new
  convenience constructors on the contract DTO (pure, no kernel import),
  mirroring the existing `state` / `text` / `error` ones, so emission is tidy
  and the event `data` shape is consistent.
- **`_install_event_bridge(agent)`** — called from `start()` right after the
  agent is built (when `bridge_events` is `True`, the default). It wraps the
  agent's overridable hooks as **instance attributes** (shadowing the bound
  methods on that one agent; the class and kernel source are untouched):
  - `_on_tool_result_hook(name, args, result)` → emits a `TOOL_CALL` (name +
    args) and a `TOOL_RESULT` (name + result), then calls the original and
    **returns its value unchanged** — an intercepting host hook still
    short-circuits the turn exactly as before.
  - `_post_request(msg, result)` → `_emit_turn_result` emits a `TEXT` event for
    non-empty `result['text']`, a non-fatal `ERROR` per entry in
    `result['errors']`, and a `USAGE` event sampled from
    `agent.get_token_usage()`.
- **`_sample_agent_state()`** — the agent owns its life-state on its own loop
  thread, so rather than splice a callback into that thread, `events()` samples
  `agent._state` on each read and appends a `STATE` event only when the value
  changed since the last read. `AgentState` is a `str` enum, so its `.value`
  (or `str(...)` for a plain-string fake) is used.
- **Thread-safety** — hooks fire on the agent's loop thread while a consumer
  may be reading `events()` on another, so all event appends and the
  last-sampled-state read/write are guarded by a per-session `threading.Lock`.
- **`bridge_events` flag** — threaded `NativeRuntime` → `NativeRuntimeSession`,
  default `True`. Set `False` to fall back to the stage-1 snapshot (lifecycle /
  notification / error only). Missing hooks/accessors are tolerated via
  `getattr` guards so a slimmer fake or future agent shape degrades gracefully.

### What it deliberately is NOT

This is a **minimal** bridge, not a streaming one. `events()` remains a
non-blocking snapshot: there is no incremental token streaming, no
async/await/generator that blocks until the next event, and no bridging of
intra-turn provider chunks. The mapping is also lossy on purpose — it surfaces
what the existing hooks and `_state` already expose, nothing more. Richer
streaming surfaces remain deferred (and overlap with the non-native-backend
work, doc §8 stage 5).

### Out of scope (still deferred)

- Incremental/streaming token events and a blocking `events()` iterator.
- Bridging intra-turn provider message chunks (vs. the per-turn `_post_request`
  summary used here).
- Migrating `system` / `psyche` / `soul` into `BundleManifest` form.
- A non-native (e.g. Anthropic) backend.
- Any kernel turn-loop / BaseAgent / Agent change.

### Tested without a model

Stage-4 tests run with no API key and no running agent: a fake agent that
*invokes* the wrapped hooks (the way the real turn loop would) stands in for the
wrapper `Agent`, asserting the `TOOL_CALL`/`TOOL_RESULT`/`TEXT`/`USAGE`/`ERROR`/
`STATE` mappings, intercept pass-through, state-change de-duplication, the
`bridge_events=False` opt-out, and graceful degradation when an accessor is
absent. One test borrows the genuine `BaseAgent._on_tool_result_hook` /
`_post_request` implementations to pin the bridge to the real hook contract, and
the new contract constructors are covered in
`tests/test_sdk_runtime_contract.py` (`tests/test_sdk_native_runtime_events.py`).

## 14. Stage 6 — committed SDK skill asset + real bundle adoption (stacked PR)

Stage 6 is a small PR **stacked on the stage-5 host proof**. Where stage 5 proved
the manifest/load/host *boundary* against a synthetic metadata-only echo, stage 6
makes the first **real, low-risk** adoption: it ships a committed asset and
expresses it as a non-privileged CapabilityBundle hosted in process. It still
does **not** migrate the privileged core (`system` / `psyche` / `soul`) or touch
the kernel turn loop.

### What it adds

- **A committed asset.** `src/lingtai_sdk/assets/lingtai-sdk-skill/SKILL.md` — the
  top-level SDK *observation entry* of §7, realized. It is a skill-shaped,
  read-only Markdown file describing the SDK/kernel/wrapper split, import purity,
  the runtime contract, the CapabilityBundle contract, and the privileged-core
  deferral, so a later coding-agent or system prompt can point at one stable
  surface without importing the runtime. It lives under the new
  `lingtai_sdk.assets` resource package (`assets/__init__.py` is a marker only),
  reachable via `importlib.resources` and shipped by the
  `lingtai_sdk = ["assets/**/*"]` `package-data` glob.
- **A real bundle over the asset** (`lingtai_sdk.sdk_skill`):
  - `load_sdk_skill()` reads the asset through `importlib.resources` (source
    checkout *or* installed wheel) — network-free and deterministic;
  - `sdk_skill_bundle()` declares a non-privileged, replaceable, `in_process`
    `BundleManifest` with one read-only tool (`read_sdk_skill`), one resource
    (`sdk_skill`), one prompt (`sdk_skill_orientation`), and `manual` pointing at
    the shipped `SKILL.md`;
  - `sdk_skill_host()` returns a ready `BundleHost` wiring those three surfaces to
    deterministic handlers that read the asset.
- **A conservative `BundleHost` extension.** `BundleHost` now hosts read-only
  **resources** (`read_resource(name)`) and **prompts** (`read_prompt(name,
  **kwargs)`) alongside tools, with the same per-surface contract enforcement
  (every declared name has a callable handler, no handler undeclared) via a small
  `_check_contract` helper. It still **refuses** privileged / native-only
  manifests and non-`in_process` transports — unchanged guardrails.

### The adoption path

```
asset (SKILL.md via importlib.resources)
   -> sdk_skill.load_sdk_skill()      # read the committed text
   -> sdk_skill.sdk_skill_bundle()    # declare a validated BundleManifest
   -> capabilities.load_manifest()    # round-trips (dict <-> manifest)
   -> sdk_skill.sdk_skill_host()      # BundleHost: tool + resource + prompt
   -> read_resource / invoke / read_prompt   # deterministic, network-free
```

### What it deliberately is NOT

- It does **not** migrate `system` / `psyche` / `soul`, nor any privileged or
  native-only behavior — `BundleHost` would refuse those, and none are named by
  the bundle's surfaces (asserted by a test).
- It does **not** change the kernel turn loop or the wrapper `Agent`.
- It does **not** wire the wrapper's *own existing* runtime capabilities through
  manifests yet, nor add the CLI/backend assembly layer that *selects* templates
  per profile (§7). Those remain follow-up work.

### Import purity

`lingtai_sdk.sdk_skill` and `lingtai_sdk.assets` import no wrapper and no provider
SDK; a bare `import lingtai_sdk` does not eagerly load either. Reads go through
`importlib.resources`, never a raw filesystem path, so they resolve identically
from an installed wheel.

### Tested without a model

`tests/test_sdk_skill_bundle.py` exercises the whole path with no API key and no
running agent: the asset exists and loads via `importlib.resources`; the manifest
validates and round-trips through `load_manifest`; the host reads the resource,
invokes the read-only tool, and renders the prompt deterministically; the host
enforces the resource/prompt contract and rejects unknown/undeclared names; the
bundle names no privileged-core surface; and `lingtai_sdk.sdk_skill` imports
purely.

## 14. Stage 6 — real low-risk bundle adoption + the `lingtai-sdk-skill` asset (stacked PR)

Stage 5 proved the manifest/load/host *boundary* against a synthetic
metadata-only echo (`proof_bundle()`). Stage 6 takes the smallest *real* step
past that: it ships a **committed asset** and adopts it through the same boundary
as a non-privileged CapabilityBundle. It still migrates **no privileged behavior**
and does not touch the kernel turn loop.

### What it adds

- **A committed, read-only asset.** `src/lingtai_sdk/assets/lingtai-sdk-skill/
  SKILL.md` — the top-level SDK *observation entry* (§7). It is skill-shaped
  (YAML frontmatter + prose) and explains the SDK/kernel/wrapper split, the
  runtime contract, the CapabilityBundle contract, and the privileged-core
  deferral, so a coding agent or system prompt can point at one stable surface
  without importing the runtime. `src/lingtai_sdk/assets/` is a resource package
  (`__init__.py` marker only) so the file is reachable via
  `importlib.resources.files("lingtai_sdk.assets")` from a checkout or a wheel;
  packaging is handled by the `lingtai_sdk = ["assets/**/*"]` `package-data` glob.
- **A real bundle over the asset.** `lingtai_sdk.sdk_skill`:
  - `load_sdk_skill()` reads the committed `SKILL.md` through
    `importlib.resources` (deterministic, network-free).
  - `sdk_skill_bundle()` declares a non-privileged, replaceable, `in_process`
    `BundleManifest` with three read-only surfaces — a `read_sdk_skill` tool, an
    `sdk_skill` resource, and an `sdk_skill_orientation` prompt — plus a `manual`
    pointer at the shipped asset.
  - `sdk_skill_host()` returns a ready `BundleHost` wiring those surfaces to
    deterministic handlers reading the asset.
- **A conservative `BundleHost` extension.** `BundleHost` now hosts three
  *read-only* surfaces, not just tools: `invoke(tool, ...)`,
  `read_resource(name)`, and `read_prompt(name, ...)`. Each surface enforces the
  same declared↔provided parity (`_check_contract`): every declared name has a
  callable handler and no handler is undeclared. The privileged/native-only and
  non-`in_process` refusals are unchanged — the guardrail that keeps the core
  bundles out still holds.

The end-to-end adoption path:

```
asset (SKILL.md, importlib.resources)
   -> sdk_skill.load_sdk_skill()      # read the committed asset text
   -> sdk_skill.sdk_skill_bundle()    # declare a validated non-privileged manifest
   -> capabilities.load_manifest()    # round-trips the declaration
   -> sdk_skill.sdk_skill_host()       # BundleHost: tool + resource + prompt
   -> invoke / read_resource / read_prompt   # deterministic, network-free
```

### What it deliberately is NOT

- It does **not** migrate `system` / `psyche` / `soul`, nor wire any *privileged*
  wrapper capability through a manifest. `BundleHost` still refuses
  privileged/native-only bundles.
- It does **not** change the kernel turn loop, `BaseAgent`, or `Agent`.
- It does **not** add the CLI/backend assembly layer that selects templates per
  profile (§7) — only the asset and the bundle exist.
- The hosted surfaces are read-only: no mutation, no I/O beyond reading the
  committed asset.

### Import purity

`lingtai_sdk.sdk_skill` and `lingtai_sdk.assets` import nothing from the
`lingtai` wrapper and no provider SDK. `sdk_skill` imports only
`importlib.resources` and the import-pure `.capabilities` / `.capability_host`
siblings; `assets` is a marker package. Enforced by
`tests/test_sdk_skill_bundle.py::test_sdk_skill_import_is_pure`.

### Tested without a model

Stage-6 tests need no API key and no running agent. They assert: the asset
exists and loads through `importlib.resources`; the manifest validates and
round-trips through `load_manifest`; the host exposes and invokes the read-only
tool/resource/prompt deterministically; the per-surface contract (missing /
undeclared / non-callable handler) is enforced; the privileged-core refusal
still holds with the new surfaces present; the bundle names none of
`system`/`psyche`/`soul`; and importing the module stays wrapper-free
(`tests/test_sdk_skill_bundle.py`).

## 15. Stage 7 — strict manifest validation + native privileged bundle contract (stacked PR)

Stage 7 is the **safety-contract layer** immediately before the privileged-core
declaration. Stages 5–6 proved the manifest/load/host *boundary* against
harmless non-privileged bundles; stage 7 *hardens* that boundary on two fronts so
the next stage can declare `system` / `psyche` / `soul` manifests against a
contract that cannot quietly mean something other than it says. It still migrates
**no privileged behavior** and does not touch the kernel turn loop — `system` /
`psyche` / `soul` remain deferred (named only in docs).

### 15.1 Strict manifest validation

`load_manifest()` and `BundleManifest.validate()` no longer accept loose input:

- **Real-boolean role flags.** `required` / `privileged` / `native_only` /
  `can_override` must be *actual* booleans. A previous `bool(...)` coercion let
  the string `"false"` (truthy!) silently *enable* a privilege, or `1` stand in
  for `True`. `load_manifest()` now rejects any non-`bool` present value with
  `BundleLoadError` (an absent flag still falls back to its default). The check
  tests `bool` before `int` because `True`/`False` are `int` instances.
- **Enum-like `danger` and `transport.kind`.** Two public, `str`-valued Enums are
  added to `lingtai_sdk.capabilities`:
  - `SecurityDanger` — `safe` / `caution` / `destructive`;
  - `TransportKind` — `native` / `in_process` / `stdio` / `http`.
  Being `str`-valued, a member compares equal to its wire string and serializes
  transparently through `to_dict()` / JSON, so the `SecurityPolicy.danger` and
  `TransportSpec.kind` fields stay plain strings (no breaking type change). Both
  `validate()` and `load_manifest()` now **reject** any value outside the
  allow-list — `validate()` so a *directly constructed* manifest is also checked,
  `load_manifest()` transitively via the validate step.
- **Strict mappings.** `metadata` and `transport.config` must be mappings. A
  list, tuple, or scalar is rejected with a clear `BundleLoadError` instead of
  being half-coerced (e.g. `dict([...])` quietly accepting a list of pairs). An
  absent block still yields `{}`.

### 15.2 Native privileged bundle contract

A second host joins `BundleHost` in `lingtai_sdk.capability_host`, sharing one
boundary contract via a private `_BaseBundleHost` (manifest validation, the
per-surface declared↔provided handler contract, the three read-only surfaces).
The two hosts differ only in *admission*:

- **`BundleHost` (unchanged).** Still **refuses** any `privileged` / `native_only`
  bundle and hosts only `in_process` transports — the guardrail that keeps the
  privileged core out is byte-for-byte the same behavior (asserted by tests).
- **`NativeBundleHost` (new).** May host a privileged / native-only bundle, but
  only when **explicitly constructed as native authority** — `native_authority`
  is a keyword-only flag defaulting to `False`, so privileged hosting is never
  granted by accident — and only for a `native` transport. It applies the *same*
  manifest validation and the *same* declared↔provided handler contract; a
  privileged bundle with a missing / undeclared / non-callable handler is still
  refused.

`native_privileged_proof_bundle()` + `native_proof_host()` are the
privileged-side mirror of `proof_bundle()` / `proof_host()`: a harmless,
synthetic, native-proof bundle (privileged, `native_only`, `native` transport,
one deterministic `native_noop` tool) that proves the native-authority hosting
boundary end to end. **It names no real privileged surface** — `system` /
`psyche` / `soul` are not migrated here.

### What it deliberately is NOT

- It does **not** declare or migrate `system` / `psyche` / `soul` (that is the
  next stage) nor any real privileged surface; `NativeBundleHost` is exercised
  only by the synthetic `native_privileged_proof`.
- It does **not** change the kernel turn loop, `BaseAgent`, or `Agent`.
- It does **not** change `SecurityPolicy.danger` / `TransportSpec.kind` from
  `str` to an Enum-typed field — the public Enums are an *allow-list*, and the
  fields stay string-valued for backward-compatible (de)serialization.

### Import purity

`lingtai_sdk.capabilities` and `lingtai_sdk.capability_host` remain import-pure:
the new Enums, helpers, host, and native proof all import nothing from the
`lingtai` wrapper or any provider SDK. Asserted by the existing module-purity
tests plus `tests/test_sdk_native_bundle_host.py::test_native_bundle_host_import_is_pure`.

### Tested without a model

Stage-7 tests need no API key and no running agent
(`tests/test_sdk_strict_manifest.py`, `tests/test_sdk_native_bundle_host.py`).
They assert: non-`bool` role flags are rejected while real booleans (and absent
defaults) are accepted; the two Enums expose exactly the documented values;
known `danger` / `transport.kind` load and unknown ones raise (through both
`load_manifest()` and `validate()`); non-mapping `metadata` / `transport.config`
are rejected; the proof bundle still round-trips through the stricter loader;
`NativeBundleHost` hosts the privileged native proof *only* with explicit
authority and a `native` transport, enforces the full per-surface contract, and
refuses without authority or with the wrong transport; the `BundleHost`
privileged / native-transport refusals are unchanged; and the privileged host
stays wrapper-free.

### Roadmap note

This closes the **strict-contract** prerequisite for the deferred core-bundle
migration (doc §8 stage 3 / “core bundle migration”): the next stage may declare
`system` / `psyche` / `soul` manifests knowing that (a) a declaration's role
flags, danger, transport, and nested blocks cannot be silently mis-read, and
(b) a privileged manifest can be hosted only by an explicitly-constructed native
authority over a `native` transport. The migration itself — and any real
privileged surface — remains future, higher-risk work.

## 16. Stage 8 — core bundle manifests + a native adapter shim (stacked PR)

Stage 8 is the **first deliberate contact** with the privileged core surfaces
`system` / `psyche` / `soul` — but **only** as a manifest contract plus a
stub-injection seam. It declares the three core bundles as `BundleManifest`s and
adds a thin native adapter that hosts them from an *injected* handler. It
migrates **no implementation**: it never imports, moves, rewrites, or calls the
real `system` / `psyche` / `soul`, and it does not touch the kernel turn loop.
This is the payoff of stage 7's strict-contract layer — the manifests are now
declared against a contract that cannot be silently mis-read, and they can be
hosted only by an explicitly-constructed native authority.

### 16.1 The three core manifests

A new import-pure module `lingtai_sdk.core_bundles` adds `system_bundle()`,
`psyche_bundle()`, and `soul_bundle()`, each returning a `BundleManifest` with
the identical privileged posture:

- `required=True` — boots with every agent;
- `privileged=True` + `native_only=True` — touches kernel-protected surfaces and
  only the native runtime may host it;
- `backend_replaceability=NATIVE_ONLY` — no non-native backend may re-implement it;
- `transport.kind == native` — carried by the native runtime in-process;
- `surfaces.tools == (name,)` — exactly the one public tool, named after the
  bundle (`system` / `psyche` / `soul`), and no other surface.

They differ only in declared `SecurityDanger` (the strict stage-7 allow-list),
mirroring the real surfaces:

- **`system` → `destructive`** — runtime inspection, lifecycle control,
  synchronization, and inter-agent management; some actions irreversibly tear
  down agent state. Highest risk.
- **`psyche` → `caution`** — identity, pad, and context management (edit/load
  identity and pad, molt, naming); context shedding has lasting effects.
- **`soul` → `caution`** — the agent's inner voice (past-self consultation,
  self-inquiry, flow/voice tuning); lower-risk than lifecycle or context
  shedding, but `config` / `voice` persist preferences and therefore are not
  read-only.

Each manifest carries **helpful, non-secret** metadata only (`core`, a one-line
`role` statement, and an `actions` list) — description, never an implementation.
`core_bundle_manifests()` returns the three in stable order (`system`, `psyche`,
`soul`); `core_bundle_names()` and `is_core_manifest()` are the matching
helpers.

### 16.2 The native adapter shim

The shim turns a core manifest plus an *injected* callable into a hosted bundle,
without ever calling the real core:

- `native_core_host(manifest, handler)` builds a `capability_host.NativeBundleHost`
  constructed with `native_authority=True`, hosting the bundle's single declared
  tool with the supplied `handler`. It refuses a non-core manifest or a
  non-callable handler with `BundleHostError`, then defers the manifest/handler
  parity and the native-authority/`native`-transport rules to `NativeBundleHost`
  itself (it never relaxes that contract).
- `native_core_hosts(handlers)` builds `{name: NativeBundleHost}` for all three
  core bundles from a `{name: callable}` mapping. A missing handler, or a handler
  for a name that is not a core bundle, raises `BundleHostError` — so a partial
  wiring can never boot a subset of the privileged core silently.

The handler callables are whatever the native runtime *injects in a later stage*.
This shim provides only the contract enforcement around a supplied callable.

### What it deliberately is NOT

- It does **not** migrate, move, rewrite, import, or call the existing `system` /
  `psyche` / `soul` implementations. The handlers are injected; this stage ships
  manifests + the injection seam only.
- It does **not** change the kernel turn loop, `BaseAgent`, or `Agent`.
- It does **not** export the core manifests or the adapter from the
  `lingtai_sdk` package doorway — like the stage-5/7 host helpers, they live on
  the `core_bundles` submodule, keeping the public surface minimal until a real
  runtime wires them.
- The non-native `BundleHost` still **refuses** every core bundle (they are all
  privileged / native-only); only a native authority may host them.

### Import purity

`lingtai_sdk.core_bundles` is import-pure: it imports only the import-pure
`.capabilities` / `.capability_host` / `.errors` siblings, so importing it (and
declaring the core manifests) pulls in **no** `lingtai` wrapper module — i.e. the
real `system` / `psyche` / `soul` is not migrated. Asserted by
`tests/test_sdk_core_bundles.py::test_core_bundles_import_is_pure_and_migrates_nothing`.

### Tested without a model

Stage-8 tests need no API key and no running agent
(`tests/test_sdk_core_bundles.py`), and use **dummy lambda handlers only** — no
real core call. They assert: all three manifests carry the shared
`required`/`privileged`/`native_only`/`NATIVE_ONLY`/`native` posture and declare
exactly their one public tool; each validates strictly and round-trips through
`load_manifest()`; the danger postures are `destructive`/`caution`/`caution` and all
allow-listed; the ordering is stable; the non-native `BundleHost` refuses every
core bundle; `native_core_host()` hosts a core bundle with an injected dummy
(and refuses a non-core manifest or non-callable handler); `native_core_hosts()`
builds all three and refuses a missing / undeclared / non-callable handler; every
produced host is a `NativeBundleHost` (never an in-process `BundleHost`); and
importing `core_bundles` migrates nothing (no `lingtai` module loaded).

### Roadmap note

This is the contract-and-seam half of the deferred core-bundle migration: the
manifests now *exist* and can be hosted via an explicit native authority, but the
privileged *behavior* is still deferred. A later, higher-risk stage supplies the
real injected handlers from the native runtime and wires the hosted core bundles
into a live turn loop.


## 17. Stage 9 — NativeRuntime core bundle seam (stacked PR)

Stage 9 connects the Stage-8 core bundle declarations to the native runtime
**as a visible contract seam only**. It does not migrate the privileged behavior.

`NativeRuntimeSession` now exposes:

- `core_bundle_manifests` — the stable `(system, psyche, soul)` manifests from
  `lingtai_sdk.core_bundles`; every session can inspect the required /
  privileged / native-only capability contract without booting a model or
  importing the wrapper.
- `core_bundle_hosts` — an empty mapping by default. If the host constructs
  `NativeRuntime(core_handlers={...})`, session creation validates the supplied
  injected handlers with `native_core_hosts()` and exposes the resulting
  `NativeBundleHost`s.

The validation remains strict: all three core handlers must be present, no extra
non-core handler is accepted, and every handler must be callable. The hosts are
created through the Stage-8 native-authority path, so the non-native `BundleHost`
still refuses the privileged core. The exposed mapping is a shallow copy, so
callers can inspect or invoke the injected handlers without mutating session
wiring.

What Stage 9 deliberately is **not**:

- no import, movement, rewrite, or call of the real `system` / `psyche` / `soul`
  implementations;
- no kernel turn-loop, wrapper, provider, or non-native backend change;
- no root `lingtai_sdk.__all__` expansion for these submodule helpers.

The import-purity boundary still holds: `import lingtai_sdk.native`, constructing
`NativeRuntime(core_handlers=dummies)`, and creating a session load only
import-pure SDK siblings and do not import the `lingtai` wrapper. Tests exercise
the seam with dummy callables only.


## 18. Stage 10 — thin public client facade (stacked PR)

Stage 10 adds the first small user-facing SDK facade on top of the already
defined runtime contract. It is deliberately a wrapper around the contract, not
a new backend and not a revival of the held PR #321 implementation.

New public pieces:

- `LingTaiClient(runtime=None, options=None)` — a convenience object that owns a
  `Runtime` and optional default `RuntimeOptions`. If no runtime is supplied, it
  imports `NativeRuntime` lazily; the wrapper `Agent` still loads only when a
  native session starts.
- `LingTaiClient.query(message, ...)` — creates a fresh runtime session, starts
  it, sends one `RuntimeMessage`, drains the immediately available events, stops
  by default, and returns a `QueryResult`.
- `QueryResult(text, events)` — stores concatenated `TEXT` event chunks and the
  full event tuple for callers that need state/tool/usage/error data.
- module-level `query(...)` — a one-shot helper around `LingTaiClient`.

The facade is intentionally synchronous and minimal. It does not block on a
full LingTai turn beyond whatever the supplied runtime/session implements; it
only follows the current runtime contract. Tests inject a fake runtime, so the
slice needs no model, no provider key, and no wrapper process.

Import-purity rule: `lingtai_sdk.client` imports only `lingtai_sdk.runtime` and
standard-library modules. The root package exposes `LingTaiClient`,
`QueryResult`, and `query` through the existing SDK-internal lazy export table,
so `import lingtai_sdk` and then accessing the facade stays wrapper-free.

What Stage 10 deliberately is **not**:

- no Anthropic / Claude Code / non-native backend;
- no kernel turn-loop, wrapper, provider, or core bundle behavior change;
- no wholesale import of the held #321 candidate.


## 19. Stage 11 — session facade over RuntimeSession (stacked PR)

Stage 11 fills the gap called out by the Stage-10 GLM review: `query()` is an
immediate-events one-shot helper and should not pretend to hold a conversation
or wait for a full asynchronous native turn. The new session facade gives
callers an explicit live-session surface instead.

New pieces:

- `LingTaiClient.open_session(options=None)` — creates, starts, and returns a
  `LingTaiSession`. Options may come from the client default or the call.
- `LingTaiSession` — a small public wrapper over `RuntimeSession` with
  `send(...)`, `events()`, `text()`, `close()`, `state`, `working_dir`,
  `raw_session`, and context-manager support.
- module-level `open_session(...)` — a one-shot helper mirroring `query(...)` but
  returning a live session.

This is still only a facade over the runtime contract. It adds no backend, no
blocking turn loop, and no wrapper/provider behavior. Tests use the existing
fake runtime; they verify multiple sends, event polling, explicit/context close,
missing-options errors, and root lazy exports without importing the `lingtai`
wrapper.
