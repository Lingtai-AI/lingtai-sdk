# Design: `lingtai_sdk` public facade package

Date: 2026-06-17
Author: claude-p (drafting for Jason)
Status: approved-for-implementation

## Purpose

Add a new public, programmable SDK facade package `src/lingtai_sdk/`, inspired in
spirit by the Anthropic Agent SDK (`claude-agent-sdk`). It exposes **stable, typed
contracts and thin wrappers** over the existing `lingtai` / `lingtai_kernel`
runtime. No implementation moves out of the runtime; no runtime behavior changes.

The facade frames the existing code as a *LingTai runtime SDK*. The public package
gives host programs a programmable way to describe an agent (model, workdir,
capabilities, tools, MCP servers, prompt assets) and build/construct a native
`lingtai.agent.Agent` from that description — without reaching into runtime
internals or hand-assembling `init.json`.

Product assembly / `init.json` translation will eventually live in a future
`lingtai-cli`. This PR is **facade + contracts only**; CLI assembly is out of scope
and left as documented TODOs.

## Non-goals (explicit)

- No PyPI package rename, no script rename.
- No deletion or renaming of existing `lingtai` / `lingtai_kernel` APIs.
- No new enforcement engine for tool permissions — filtering reuses the runtime's
  existing `disable=` / capability mechanisms.
- The bare SDK does **not** auto-assemble a full product system prompt and does
  **not** decide a default project-loading policy. It exposes options and building
  blocks plus a native runtime adapter.
- `query()` does **not** drive the full ACTIVE turn-loop in this PR (see below).

## Architecture — small, single-purpose modules

```
src/lingtai_sdk/
  __init__.py    # public API surface, __all__, __version__ (mirrors lingtai version)
  options.py     # LingTaiOptions dataclass — typed config, secret redaction, to_dict()
  tools.py       # ToolSpec, ToolResult, PermissionMode, builtin tool-name constants
  mcp.py         # MCPServerConfig union (stdio/http/sse/sdk), to_runtime_dict()
  session.py     # SessionRef, SessionStore protocol + InMemorySessionStore
  runtime.py     # native adapter: LingTaiOptions -> lingtai.Agent kwargs + LLMService
  client.py      # LingTaiClient — build_agent_kwargs(), create_agent()
  query.py       # query(...) high-level convenience wrapper
  ANATOMY.md     # per-folder anatomy following the 6-section template
```

Each module has one clear purpose, communicates through dataclasses/protocols, and
is testable in isolation.

## Public API

### `LingTaiOptions` (options.py)

Frozen-style dataclass (mutable dataclass with redacting `__repr__`). LingTai-native
vocabulary, organized in the same spirit as `ClaudeAgentOptions`. Fields:

- `model: str | None`, `provider: str | None`
- `api_key: str | None` (redacted in `repr` and `to_dict`), `base_url: str | None`
- `working_dir: str | Path | None`; `cwd` accepted as an alias in `__init__`
- `agent_name: str | None`
- `capabilities: list[str] | dict[str, dict] | None` — passed through verbatim
- `allowed_tools: list[str] | None`, `disallowed_tools: list[str] | None`
- `mcp_servers: dict[str, MCPServerConfig] | None`
- `system_prompt: SystemPromptAssets | None` — lightweight holder for
  `covenant`/`principle`/`substrate`/`brief`/`pad`/`comment` strings (the runtime's
  existing prompt-asset slots). Not auto-assembled; passed straight through.
- `max_turns: int | None`, `context_limit: int | None`
- `env: dict[str, str] | None`, `add_dirs: list[str | Path] | None`
- `permission_mode: PermissionMode | None` (informational/forward-compat; see below)
- `setting_sources`-equivalent left as a documented TODO (CLI concern)

Methods:
- `to_dict(redact=True)` — JSON-friendly dict; redacts `api_key` to `"***"` when
  `redact=True`. `mcp_servers` serialized via each config's `to_runtime_dict`.
- `__repr__` — never prints `api_key` or MCP secret headers/env.
- `replace(**changes)` — returns a copy with overrides (dataclasses.replace sugar).

### `tools.py`

- `PermissionMode` — string-enum-like constants (`"default"`, `"acceptAll"`,
  `"plan"`, ...). Informational in this PR; documented as forward-compat.
- `ToolSpec` — dataclass `{name, description, input_schema, source}` describing a
  tool (built-in capability tool or MCP tool). Read-only metadata.
- `ToolResult` — dataclass `{tool, content, is_error}` — a stable result shape for
  callers that wrap tool dispatch. No runtime coupling.
- `BUILTIN_TOOLS` / `builtin_tool_names()` — pulls real capability/group names from
  `lingtai.capabilities` (`_BUILTIN` keys + `_GROUPS` keys) so the constant never
  drifts from the runtime registry.

### `mcp.py`

- `MCPServerConfig` — base + four variants mirroring the runtime's accepted shapes
  and the Anthropic SDK's vocabulary:
  - `MCPStdioServerConfig(command, args, env)` -> `{type:"stdio", command, args, env}`
  - `MCPHttpServerConfig(url, headers)` -> `{type:"http", url, headers}`
  - `MCPSSEServerConfig(url, headers)` -> `{type:"sse", url, headers}` (placeholder;
    runtime currently consumes stdio/http — sse documented as forward-compat)
  - `MCPSdkServerConfig(name, instance)` -> in-process placeholder; documented TODO.
- Each exposes `to_runtime_dict()` producing exactly the dict
  `Agent._load_mcp_from_workdir` / `connect_mcp*` consume.
- `__repr__` / `to_runtime_dict(redact=...)` keep `headers` and `env` secret values
  out of reprs and docs.

### `session.py`

- `SessionRef` — dataclass `{session_id, working_dir}` — a cheap handle.
- `SessionStore` — `typing.Protocol` with `save(ref)`, `load(session_id)`,
  `list()`. Minimal contract; no runtime coupling.
- `InMemorySessionStore` — trivial reference implementation for tests/hosts.

### `runtime.py` (native adapter)

Pure translation, no I/O beyond constructing objects:
- `build_llm_service(options) -> LLMService | None` — constructs `LLMService` from
  `provider`/`model`/`api_key`/`base_url`. Returns `None` when provider/model absent
  (caller may inject their own service).
- `options_to_agent_kwargs(options, *, service=None) -> dict` — produces the kwarg
  dict for `lingtai.Agent(...)`: `service`, `agent_name`, `working_dir`,
  `capabilities`, `disable` (derived from `disallowed_tools`/allowed filtering),
  prompt assets (`covenant`/`principle`/...), `config` (AgentConfig with
  `context_limit`, etc.). `mcp_servers` are NOT injected as constructor kwargs (the
  runtime loads MCP from workdir); instead they are returned under a separate
  `mcp_servers` key for the client to connect post-construction, or surfaced for the
  future CLI to persist into init.json. Documented clearly.

Tool filtering: `allowed_tools`/`disallowed_tools` are translated into a `disable`
list against the resolved capability set — reusing the runtime's opt-out channel.
Names not recognized as capabilities are ignored with the translation recorded in
the returned kwargs metadata (no silent enforcement claims).

### `client.py`

`LingTaiClient(options: LingTaiOptions)`:
- `build_agent_kwargs() -> dict` — delegates to `runtime.options_to_agent_kwargs`.
  Pure, side-effect-free, fully unit-testable without an LLM or disk.
- `create_agent(*, service=None, connect_mcp=False) -> lingtai.Agent` — constructs a
  live `Agent`. Does **not** call `.start()` — the caller owns the loop. When
  `connect_mcp=True` and `mcp_servers` are set, connects them via the Agent's
  `connect_mcp` / `connect_mcp_http` after construction (best-effort, errors logged
  not raised). Requires a `working_dir`.
- `tool_inventory() -> list[ToolSpec]` — after `create_agent`, reflects the agent's
  registered tool schemas as `ToolSpec`s (optional convenience).

### `query.py`

`async def query(prompt, *, options, service=None) -> AsyncIterator[dict]`:
- Conservative wrapper. Creates the agent via `LingTaiClient`, starts it, sends the
  prompt as a user message, and yields a small set of lifecycle dicts
  (`{"type": "agent_created"}`, `{"type": "message_sent"}`, `{"type": "stopped"}`).
- It does **NOT** synchronously collect or stream full assistant turns — the runtime
  loop is async-peer / fire-and-forget, with no synchronous reply mechanism
  (`send()` is fire-and-forget; there is no request/response primitive). This
  limitation is documented in the docstring and README.
- Stable primitives `build_agent_kwargs` / `create_agent` are the recommended path
  for programmatic control until a future turn-loop query is designed.

## Backward compatibility

- `lingtai_sdk` is purely additive. `lingtai` and `lingtai_kernel` are unchanged.
- `import lingtai`, existing tests, and the CLI keep working untouched.
- `pyproject.toml` `packages.find.include` already matches `lingtai*` /
  `lingtai_kernel*` but NOT `lingtai_sdk*`; add `lingtai_sdk*` to `include` and add a
  `package-data` entry if any non-code files ship (none expected beyond ANATOMY.md,
  which is not packaged). Verify the package is discovered.

## Testing

New `tests/test_sdk_*.py` files, runtime-free where possible (MagicMock for
`LLMService`, tmp_path for `working_dir`):
- `test_sdk_imports.py` — every public symbol importable from `lingtai_sdk`.
- `test_sdk_options.py` — defaults, `cwd` alias, `to_dict` redaction, `repr`
  redaction, `replace`.
- `test_sdk_mcp.py` — each config variant's `to_runtime_dict` shape; secret
  redaction in repr.
- `test_sdk_tools.py` — `builtin_tool_names()` matches the live registry;
  `ToolSpec`/`ToolResult` defaults.
- `test_sdk_runtime.py` — `options_to_agent_kwargs` mapping, `disable` derivation
  from `disallowed_tools`, no-service path.
- `test_sdk_client.py` — `build_agent_kwargs` purity; `create_agent` builds an Agent
  with a mock service and tmp workdir; does not auto-start.
- `test_sdk_query.py` — `query` yields the documented lifecycle events against a mock
  service; asserts the no-turn-loop caveat behavior.

Run: targeted `pytest tests/test_sdk_*.py`, then a broad `pytest tests/` if feasible.

## Docs & anatomy

- `src/lingtai_sdk/ANATOMY.md` — 6-section template: purpose, components,
  composition, data flow, extension points, citations.
- Package docstring + a `docs/` note explaining SDK = programmable runtime API;
  future CLI = assembly/translation from project state/init.json to runtime options.
- Local PR report: `reports/sdk-public-facade-20260617/implementation.md`.
- Update `src/lingtai/ANATOMY.md` / `src/lingtai_kernel/ANATOMY.md` only if a
  citation/claim is affected (expected: none, since this is additive).

## Caveats / TODOs

- `query()` is intentionally not a full turn-loop runner — documented limitation.
- `permission_mode`, `setting_sources`, `MCPSdkServerConfig`, `MCPSSEServerConfig`
  are forward-compat placeholders with no runtime enforcement yet.
- `add_dirs` is recorded in options but not yet wired into the runtime (TODO for
  CLI assembly).
