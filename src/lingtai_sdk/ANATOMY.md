# lingtai_sdk

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

The **public SDK doorway** — a curated, import-light front door for building and embedding LingTai agents. It re-exports the two implementation packages (`lingtai_kernel`, `lingtai`) under one stable path and carries the *seed contracts* (runtime, capability-bundle manifest) that later PRs will implement. This is a foundation package: it ships shapes and a doorway, not a live runtime.

> **What is an `ANATOMY.md`?** See the canonical convention at `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`. This file follows the same 6-section template as every other anatomy in the tree.

## Components

- `__init__.py` — the curated public surface. Eager kernel re-exports (`BaseAgent`, plus `types`/`errors` names); wrapper-backed names (`Agent`, `FileIOService`, `MailService`, `LoggingService`, `SearchService`, `VisionService`) resolve **lazily** via :pep:`562` `__getattr__` against `_LAZY_WRAPPER_EXPORTS`, caching into `globals()` on first access. `__all__` is the canonical export list; `__dir__` mirrors it.
- `_version.py` — `__version__`, resolved best-effort from the `lingtai` distribution metadata (`importlib.metadata.version("lingtai")`), falling back to `"0+unknown"` so import never fails over missing metadata.
- `types.py` — re-exports kernel public types (`AgentConfig`, `AgentState`, `Message`, `MSG_REQUEST`, `MSG_USER_INPUT`, `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall`, `LLMService`) under a stable path. Kernel-only; no heavy provider SDK is pulled.
- `errors.py` — `LingTaiSDKError` (SDK base), `NativeRuntimeConfigurationError` (raised by the default native factory when LLM config is partial/absent), plus a re-export of kernel `UnknownToolError`. Import-pure (kernel `types` only).
- `_compat.py` — the machine-readable migration map: `Deprecation` dataclass, `DEPRECATIONS` tuple, `active_aliases()`, `migration_for()`. Drives the docs migration table and the same-object round-trip test.
- `runtime.py` — **runtime contract seed** (pure DTOs/ABCs, no kernel import): `RuntimeState`, `EventKind`, `RuntimeOptions`, `RuntimeMessage`, `RuntimeEvent` (with `state`/`text`/`error` constructors), and the `RuntimeSession`/`Runtime` ABCs. Describes how a *future* live runtime is driven (options in, messages in, events out). No live runtime here.
- `capabilities.py` — **CapabilityBundle manifest seed** (pure DTOs, no kernel import): `BackendReplaceability`, `RoleFlags`, `CapabilitySurfaces`, `SecurityPolicy`, `TransportSpec`, `BundleManifest` (with `validate()`/`to_dict()`), and `proof_bundle()` — a harmless metadata-only synthetic bundle. Public schema only; native privileged handlers stay in the kernel/wrapper.
- `native.py` — **live `NativeRuntime` adapter skeleton** (NOT a full backend; stage 1 + stage-2 LLM-service translation). `NativeRuntime(Runtime)` (id `"native"`) is a factory for `NativeRuntimeSession(RuntimeSession)`, which wraps the wrapper `Agent` unchanged: translates `RuntimeOptions` → `Agent` kwargs (`_agent_kwargs_from_options`), drives `start()`/`stop()` lifecycle (`PENDING`→`ACTIVE`→`STOPPED`), routes `send()` onto `Agent.send()` (fire-and-forget queue), and exposes a non-blocking `events()` snapshot of lifecycle/notification/error events. Import-pure at module load (imports only `runtime` + the import-pure `errors`); the wrapper `Agent` and `lingtai.llm.LLMService` are imported lazily on first `start()`. **Stage-2 LLM-service translation:** on the *default* factory path (`agent_factory` not injected), `start()` builds the `LLMService` the wrapper `Agent` requires via `_llm_service_from_options(options)` (lazy `lingtai.llm` import) from `provider`/`model`/`base_url`/`api_key`, passes it as `service`, and moves the secret-free LLM config from `session.deferred['llm']` into `session.applied['llm']` (`_public_llm_fields` strips `api_key`). Partial/absent `provider`/`model` raises `NativeRuntimeConfigurationError` *before* any agent is built (session stays `PENDING`) — the opaque missing-`service` `TypeError` no longer leaks (GLM stage-1 nit N2). An injected `agent_factory` **bypasses** service building (`_uses_default_factory` flag), so fakes/hosts boot without a real service. `api_key` is consumed into the service and never stored on the session, surfaced in events, or echoed in errors/reprs.

## Connections

- **The kernel must never import this package.** Dependency flows one way: `lingtai_sdk` → (`lingtai`, `lingtai_kernel`). The SDK is a consumer of the other two, never a dependency of them.
- **Eager kernel, lazy wrapper.** `import lingtai_sdk` loads the dependency-light kernel only. Wrapper-backed names (`_LAZY_WRAPPER_EXPORTS`) import `lingtai` on first attribute access, keeping the wrapper's provider SDKs (anthropic/openai/google-genai/mcp/…) optional for pure-kernel consumers. Enforced by `tests/test_sdk_import_purity.py`.
- **Lazy SDK-internal names.** `NativeRuntime`/`NativeRuntimeSession` (`_LAZY_SDK_EXPORTS`) resolve from the import-pure `.native` module, NOT from the wrapper — accessing them and constructing a `NativeRuntime` stays provider-free; the wrapper `Agent` loads only when a session is started. Enforced by `tests/test_sdk_native_runtime.py::test_importing_native_and_constructing_runtime_is_pure`.
- `runtime.py`, `capabilities.py`, and `native.py` import **nothing** from `lingtai`/`lingtai_kernel` at module load — `native.py` imports only the local `runtime` contract; its wrapper `Agent` import is deferred to `start()` (`tests/test_sdk_runtime_contract.py`, `tests/test_sdk_capabilities.py`, `tests/test_sdk_native_runtime.py`).
- Compatibility is **by re-export, not re-implementation**: `_compat.DEPRECATIONS` records legacy→SDK path moves; the round-trip test asserts each pair resolves to the *same object* (`tests/test_sdk_compat.py`).

## Seeds vs. implementations

This package implements only the runtime *adapter* boundary; everything below it stops at the contract. **Present** (stage 1 + stage 2): the `NativeRuntime` skeleton in `native.py` — it wraps the existing `Agent`, drives its start/stop lifecycle, and (stage 2) builds the `LLMService` the default factory needs from `RuntimeOptions`. It still does not change the kernel turn loop or implement a non-native backend. The following remain **intentionally deferred** to later PRs and are NOT present here:

- A non-native backend (e.g. an Anthropic backend) and init.json-level manifest translation (only `provider`/`model`/`base_url`/`api_key` are translated into the default-factory `LLMService`; `api_compat`, `default_headers`, `compact_threshold`, etc. are not yet plumbed through).
- A live event bridge from the agent's output stream onto `RuntimeEvent` — `events()` is currently a snapshot of lifecycle/notification/error events only.
- Migration of the core `system` / `psyche` / `soul` bundles into `BundleManifest` form. The only bundle here is the synthetic `proof_bundle()`.
- A distribution/package rename (the SDK ships inside the existing `lingtai` wheel for now).

See `docs/sdk/architecture-foundation.md` for the staged roadmap and rationale.

## State

`lingtai_sdk` is **stateless** — it writes nothing to disk and holds no runtime state. Its only side effect is the PEP 562 attribute cache populated in the module's own `globals()` when a lazy wrapper name is first accessed.

## Notes

- Adding a public name: prefer re-exporting from `types.py`/`errors.py` (kernel-backed, eager) or `_LAZY_WRAPPER_EXPORTS` (wrapper-backed, lazy). Update `__all__` and, if it formalizes a legacy path, add a `_compat.Deprecation` entry.
- Do not import heavy provider SDKs at module load anywhere in this package; the import-purity tests will fail. Bare `google` (an ambient namespace stub pulled in transitively by `filelock`) is harmless and explicitly excluded from the purity check — only heavy submodules like `google.genai` count.
