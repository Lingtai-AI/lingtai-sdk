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
- `SecurityPolicy` — `permissions`, `requires_confirmation`, `danger`.
- `TransportSpec` — `kind` (`native` / `stdio` / `http` / `in_process`) + config.

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
