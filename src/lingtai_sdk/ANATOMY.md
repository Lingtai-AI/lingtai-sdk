# lingtai_sdk

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

The **public SDK doorway** — a curated, import-light front door for building and embedding LingTai agents. It re-exports the two implementation packages (`lingtai_kernel`, `lingtai`) under one stable path and carries the *seed contracts* (runtime, capability-bundle manifest) that later PRs will implement. This is a foundation package: it ships shapes and a doorway, not a live runtime.

> **What is an `ANATOMY.md`?** See the canonical convention at `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`. This file follows the same 6-section template as every other anatomy in the tree.

## Components

- `__init__.py` — the curated public surface. Eager kernel re-exports (`BaseAgent`, plus `types`/`errors` names); wrapper-backed names (`Agent`, `FileIOService`, `MailService`, `LoggingService`, `SearchService`, `VisionService`) resolve **lazily** via :pep:`562` `__getattr__` against `_LAZY_WRAPPER_EXPORTS`, caching into `globals()` on first access. `__all__` is the canonical export list; `__dir__` mirrors it.
- `_version.py` — `__version__`, resolved best-effort from the `lingtai` distribution metadata (`importlib.metadata.version("lingtai")`), falling back to `"0+unknown"` so import never fails over missing metadata.
- `types.py` — re-exports kernel public types (`AgentConfig`, `AgentState`, `Message`, `MSG_REQUEST`, `MSG_USER_INPUT`, `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall`, `LLMService`) under a stable path. Kernel-only; no heavy provider SDK is pulled.
- `errors.py` — `LingTaiSDKError` (SDK base) plus a re-export of kernel `UnknownToolError`.
- `_compat.py` — the machine-readable migration map: `Deprecation` dataclass, `DEPRECATIONS` tuple, `active_aliases()`, `migration_for()`. Drives the docs migration table and the same-object round-trip test.
- `runtime.py` — **runtime contract seed** (pure DTOs/ABCs, no kernel import): `RuntimeState`, `EventKind`, `RuntimeOptions`, `RuntimeMessage`, `RuntimeEvent` (with `state`/`text`/`error` constructors), and the `RuntimeSession`/`Runtime` ABCs. Describes how a *future* live runtime is driven (options in, messages in, events out). No live runtime here.
- `capabilities.py` — **CapabilityBundle manifest seed** (pure DTOs, no kernel import): `BackendReplaceability`, `RoleFlags`, `CapabilitySurfaces`, `SecurityPolicy`, `TransportSpec`, `BundleManifest` (with `validate()`/`to_dict()`), and `proof_bundle()` — a harmless metadata-only synthetic bundle. Public schema only; native privileged handlers stay in the kernel/wrapper.

## Connections

- **The kernel must never import this package.** Dependency flows one way: `lingtai_sdk` → (`lingtai`, `lingtai_kernel`). The SDK is a consumer of the other two, never a dependency of them.
- **Eager kernel, lazy wrapper.** `import lingtai_sdk` loads the dependency-light kernel only. Wrapper-backed names import `lingtai` on first attribute access, keeping the wrapper's provider SDKs (anthropic/openai/google-genai/mcp/…) optional for pure-kernel consumers. Enforced by `tests/test_sdk_import_purity.py`.
- `runtime.py` and `capabilities.py` import **nothing** from `lingtai`/`lingtai_kernel` — they are standalone contract modules, so importing them stays provider-free (`tests/test_sdk_runtime_contract.py`, `tests/test_sdk_capabilities.py`).
- Compatibility is **by re-export, not re-implementation**: `_compat.DEPRECATIONS` records legacy→SDK path moves; the round-trip test asserts each pair resolves to the *same object* (`tests/test_sdk_compat.py`).

## Seeds vs. implementations

This package deliberately stops at the contract boundary. The following are **intentionally deferred** to later PRs and are NOT present here:

- A live `NativeRuntime` (thin wrapper over the existing `Agent`) and any non-native backend (e.g. an Anthropic backend).
- Migration of the core `system` / `psyche` / `soul` bundles into `BundleManifest` form. The only bundle here is the synthetic `proof_bundle()`.
- A distribution/package rename (the SDK ships inside the existing `lingtai` wheel for now).

See `docs/sdk/architecture-foundation.md` for the staged roadmap and rationale.

## State

`lingtai_sdk` is **stateless** — it writes nothing to disk and holds no runtime state. Its only side effect is the PEP 562 attribute cache populated in the module's own `globals()` when a lazy wrapper name is first accessed.

## Notes

- Adding a public name: prefer re-exporting from `types.py`/`errors.py` (kernel-backed, eager) or `_LAZY_WRAPPER_EXPORTS` (wrapper-backed, lazy). Update `__all__` and, if it formalizes a legacy path, add a `_compat.Deprecation` entry.
- Do not import heavy provider SDKs at module load anywhere in this package; the import-purity tests will fail. Bare `google` (an ambient namespace stub pulled in transitively by `filelock`) is harmless and explicitly excluded from the purity check — only heavy submodules like `google.genai` count.
