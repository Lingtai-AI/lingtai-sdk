# lingtai_sdk

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

Public, programmable SDK facade over the LingTai runtime, inspired in spirit by the Anthropic Agent SDK. Exposes *stable, typed contracts* (options, tool/MCP/session types) and *thin wrappers* (`LingTaiClient`, `query`, runtime adapter) that build and construct a native `lingtai.agent.Agent` from a declarative `LingTaiOptions`. **Purely additive** — it moves no runtime implementation and changes no runtime behavior.

Framing: `lingtai` / `lingtai_kernel` are the *runtime SDK* (the engine); `lingtai_sdk` is the *public programmable API*. A future `lingtai-cli` will own product assembly / `init.json` translation from project state into these options — that layer is out of scope here.

## Components

- `__init__.py` — public API surface. Re-exports every public symbol, defines `__all__`, and sets `__version__` from the installed `lingtai` distribution metadata (falls back to `"0+unknown"`).
- `options.py` — `LingTaiOptions` (`options.py:58`), the single declarative config dataclass. LingTai-native field vocabulary (`working_dir`, `capabilities`, prompt assets) organized Anthropic-SDK-style. `cwd` is an `InitVar` alias resolved into `working_dir` in `__post_init__` (`options.py:107`). `to_dict(redact=True)` (`options.py:115`) redacts `api_key` and per-server MCP secrets; `__repr__` (`options.py:152`) prints `api_key='set'`, never the value; `replace(**changes)` (`options.py:111`) is `dataclasses.replace` sugar. `SystemPromptAssets` (`options.py:25`) holds the runtime's prompt-asset slots (covenant/principle/substrate/procedures/brief/pad/comment); `to_kwargs()` drops empty slots.
- `runtime.py` — native adapter, pure translation. `build_llm_service(options)` (`runtime.py:19`) constructs an `LLMService` or returns `None` when provider/model absent. `derive_disable_list(options)` (`runtime.py:40`) translates `disallowed_tools` into the runtime's capability `disable=` channel (group expansion via `_GROUPS`, unknown names dropped). `options_to_agent_kwargs(options, *, service=None)` (`runtime.py:66`) builds the `Agent(...)` kwarg dict, plus underscore-prefixed SDK-internal keys `_sdk_mcp_servers` / `_sdk_allowed_tools` (never forwarded to `Agent`). `_build_config` (`runtime.py:131`) emits an `AgentConfig` only when `context_limit`/`max_turns` is set.
- `client.py` — `LingTaiClient` (`client.py:21`). `build_agent_kwargs(*, service=None)` (`client.py:27`) delegates to the runtime adapter (pure with an injected service). `create_agent(*, service=None, connect_mcp=False)` (`client.py:37`) constructs a live `Agent`, strips the `_sdk_*` keys, and does **not** call `.start()` (caller owns lifecycle); requires `working_dir`. `_connect_mcp_servers` (`client.py:81`) best-effort connects stdio/http servers (sse/sdk skipped). `tool_inventory()` (`client.py:104`) reflects the constructed agent's intrinsics + tool schemas as `ToolSpec`s.
- `query.py` — `async query(prompt, *, options, service=None, autostart=True, connect_mcp=False, client=None)` (`query.py:26`). Conservative lifecycle wrapper: creates the agent, optionally starts it, sends the prompt, yields lifecycle event dicts. **Does NOT stream assistant turns** — documented limitation (the runtime loop is async/fire-and-forget with no synchronous request/response primitive). `client` injection seam makes it testable without a real loop.
- `tools.py` — `PermissionMode` (`tools.py:14`, forward-compat constants, not yet enforced), `ToolSpec` (`tools.py:31`, read-only tool metadata), `ToolResult` (`tools.py:46`, stable result shape for host-side dispatch wrappers), `builtin_tool_names()` (`tools.py:59`) reads `lingtai.capabilities._BUILTIN`/`_GROUPS` so the list never drifts. `BUILTIN_TOOLS` is a lazy compatibility export via `tools.py:71` / package `__getattr__`, so plain `import lingtai_sdk` does not eagerly load the runtime registry.
- `mcp.py` — `MCPServerConfig` base + four variants: `MCPStdioServerConfig` (`mcp.py:39`), `MCPHttpServerConfig` (`mcp.py:65`), `MCPSSEServerConfig` (`mcp.py:90`, forward-compat), `MCPSdkServerConfig` (`mcp.py:115`, in-process placeholder, instance not serialized). Each `to_runtime_dict(redact=False)` emits exactly the dict `Agent._load_mcp_from_workdir` / `connect_mcp*` consume; `_redact_mapping` (`mcp.py:20`) masks `env`/`headers` values; every `__repr__` shows only key names.
- `session.py` — `SessionRef` (`session.py:18`, frozen handle), `SessionStore` (`session.py:25`, runtime-checkable Protocol), `InMemorySessionStore` (`session.py:37`, dict-backed reference impl). Host-facing bookkeeping only; no coupling to `lingtai_kernel.session`.

## Connections

- `__init__.py` → sibling contract modules at package load; `BUILTIN_TOOLS` resolves lazily via `__getattr__` because it reads the runtime capability registry.
- `options.py` → `mcp.py` (`MCPServerConfig` type + per-server serialization in `to_dict`).
- `runtime.py` → `lingtai` (lazy: `lingtai.llm.service.LLMService`, `lingtai.capabilities._BUILTIN`/`_GROUPS`), `lingtai_kernel.config.AgentConfig` (lazy), and `.options`.
- `client.py` → `.options`, `.runtime`, `.tools`, and `lingtai.agent.Agent` (lazy import inside `create_agent`).
- `query.py` → `.client`, `.options`.
- All runtime imports are **lazy** (inside function bodies) so importing `lingtai_sdk` and using the pure contract types never forces `lingtai`/`lingtai_kernel` to load. Dependency direction is strictly `lingtai_sdk` → `lingtai` → `lingtai_kernel`; the runtime never imports the facade.

## Composition

- **Parent:** `src/` (top-level package, sibling to `lingtai` and `lingtai_kernel`).
- **Siblings:** `src/lingtai/` (runtime capabilities layer — see its `ANATOMY.md`), `src/lingtai_kernel/` (kernel — see its `ANATOMY.md`).
- **Packaging:** discovered via `[tool.setuptools.packages.find] include` in `pyproject.toml` (matches `lingtai_sdk*`). No package-data; `ANATOMY.md` is not shipped.

## State

- Pure in-memory. The facade itself performs no disk or network I/O. Side effects occur only inside the wrapped runtime: `create_agent` constructs a live `Agent`, which (per its own contract) creates its working directory, writes `system/llm.json`, installs intrinsic manuals, etc. — all owned by `lingtai.agent.Agent`, unchanged by this package. `query` with `autostart=True` starts/stops the agent loop.

## Notes

- **Forward-compat placeholders** (no runtime enforcement yet): `PermissionMode` / `LingTaiOptions.permission_mode`, `LingTaiOptions.allowed_tools`, `LingTaiOptions.max_turns` (recorded in `AgentConfig` but not the active tool-loop guard), `MCPSSEServerConfig`, `MCPSdkServerConfig`, top-level `env`, and `LingTaiOptions.add_dirs`. These are recorded/serialized so the future `lingtai-cli` and runtime can adopt them without an API break.
- **`query` is intentionally not a turn-loop runner.** Use `LingTaiClient.create_agent` / `build_agent_kwargs` for programmatic control today. A full streaming `query` is a documented TODO pending a request/response contract in the runtime.
- **`_sdk_*` kwargs.** `options_to_agent_kwargs` returns `_sdk_mcp_servers` / `_sdk_allowed_tools` alongside real `Agent` kwargs; the underscore prefix marks them SDK-internal and `create_agent` strips them before construction. MCP runtime dicts there retain secrets (they feed live `connect_mcp*` calls), unlike the redacted `to_dict()` output.

- **Import-purity invariant.** Plain `import lingtai_sdk` must not load `lingtai` or `lingtai.capabilities`; tests guard this so the public facade stays contract-first. Accessing `BUILTIN_TOOLS` or calling `builtin_tool_names()` intentionally reads the live runtime registry.
- **Workdir lock.** `LingTaiClient.create_agent()` constructs a live runtime `Agent`; construction acquires the exclusive workdir lock, so callers must call `agent.stop()` to release it even if they never call `agent.start()`.
