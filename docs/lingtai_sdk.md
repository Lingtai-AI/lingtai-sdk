# LingTai SDK (`lingtai_sdk`)

`lingtai_sdk` is the **public, programmable API** for the LingTai agent runtime.
It is inspired in spirit by the Anthropic Agent SDK: a small set of typed
contracts plus thin wrappers that let a host program *describe* an agent and
then *build* or *construct* it — without reaching into runtime internals or
hand-assembling an `init.json`.

## Layering

```
lingtai_sdk        ← public programmable API (stable contracts + wrappers)   [this package]
   │  imports
   ▼
lingtai            ← runtime SDK: Agent + capabilities + adapters + services
   │  imports
   ▼
lingtai_kernel     ← minimal agent kernel (BaseAgent, intrinsics, LLM protocol)
```

The dependency is strictly one-directional. `lingtai_sdk` imports `lingtai`;
`lingtai` and `lingtai_kernel` never import `lingtai_sdk`.

## SDK vs. the future CLI

- **`lingtai_sdk` (this package)** is the *programmable runtime API*. It exposes
  options, tool/MCP/session contracts, and a client that constructs a native
  `lingtai.agent.Agent`. It does **not** auto-assemble a product system prompt
  and does **not** decide a default project-loading policy — it surfaces options
  and building blocks plus a native runtime adapter.
- **A future `lingtai-cli`** will own *product assembly / translation*: turning
  project state and `init.json` manifests into `LingTaiOptions`, choosing
  prompt-asset policy, and persisting MCP/preset config. That layer is **out of
  scope** for this package; placeholders here (`add_dirs`, `permission_mode`,
  SSE/SDK MCP transports) exist so the CLI can adopt them without an API break.

## Quick start

```python
from lingtai_sdk import LingTaiOptions, LingTaiClient

options = LingTaiOptions(
    provider="anthropic",
    model="claude-opus-4-8",
    working_dir="/agents/alice",       # `cwd=` accepted as an alias
    agent_name="alice",
    capabilities=["file", "web_search"],
    disallowed_tools=["bash"],          # translated to the runtime `disable=` channel
)

client = LingTaiClient(options)

# Pure, side-effect-free: inspect exactly what would be passed to Agent(...)
kwargs = client.build_agent_kwargs()

# Construct a live Agent (does NOT call .start() — you own the lifecycle).
# Construction acquires the runtime workdir lock, so call agent.stop() even if
# you never start the loop.
agent = client.create_agent()
# try:
#     agent.start(); ...
# finally:
#     agent.stop()
```

### Options and secrets

`LingTaiOptions.to_dict()` redacts `api_key`, top-level `env` values, and MCP
`headers`/`env` values by default (`to_dict(redact=False)` for the raw form).
`repr(options)` never prints the API key. MCP config `repr`s show only key names,
never secret values.

### MCP servers

```python
from lingtai_sdk import LingTaiOptions, MCPHttpServerConfig, MCPStdioServerConfig

options = LingTaiOptions(
    working_dir="/agents/alice",
    mcp_servers={
        "search": MCPHttpServerConfig(url="https://api.example/mcp",
                                      headers={"Authorization": "Bearer ..."}),
        "tools": MCPStdioServerConfig(command="npx", args=["-y", "some-mcp"],
                                      env={"API_KEY": "..."}),
    },
)
agent = LingTaiClient(options).create_agent(connect_mcp=True)
```

`MCPServerConfig.to_runtime_dict()` emits exactly the dict shape the runtime's
MCP loader consumes.

## `query()` — current limitation

```python
import asyncio
from lingtai_sdk import LingTaiOptions, query

async def main():
    options = LingTaiOptions(provider="anthropic", model="claude-opus-4-8",
                             working_dir="/agents/alice")
    async for event in query("hello", options=options):
        print(event)   # {"type": "agent_created"}, "started", "message_sent", "note", "stopped"

asyncio.run(main())
```

`query()` mirrors the *shape* of the Anthropic SDK's `query` (an async iterator
of events) but **does not stream assistant turns**. The LingTai runtime loop is
async-peer / fire-and-forget — `Agent.send` enqueues a message and returns; there
is no synchronous request/response primitive. So `query` constructs the agent,
optionally starts it, sends the prompt, and yields lifecycle events only.

For deterministic programmatic control today, use
`LingTaiClient.build_agent_kwargs` / `LingTaiClient.create_agent`. A full
turn-loop `query` is a documented TODO pending a request/response contract in the
runtime. With `autostart=True`, the queued prompt may begin a real turn before
`stop()` completes; `query()` still does not wait for or collect that reply.

## Forward-compatible placeholders

Some fields deliberately exist before full runtime enforcement so the future
`lingtai-cli` can translate project state into a stable SDK contract without an
API break:

- `permission_mode` is recorded but not enforced.
- `add_dirs` and top-level `env` are serialized/redacted but not yet wired into
  `Agent(...)`.
- `allowed_tools` is surfaced for hosts but is not a runtime allowlist yet; use
  `disallowed_tools` for current capability opt-out.
- `max_turns` is recorded in `AgentConfig` for compatibility but is not an
  enforced active tool-loop limit in the current runtime.
- SSE and SDK MCP configs are typed placeholders; stdio/http are the current live
  runtime connection paths.
