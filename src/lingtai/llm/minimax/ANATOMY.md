# src/lingtai/llm/minimax

MiniMax adapter — inherits Anthropic-compatible endpoint, adds MCP client factory for MiniMax coding-plan tools.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 4 | Re-exports `MiniMaxAdapter`, `get_minimax_mcp_client` |
| `adapter.py` | 21 | `MiniMaxAdapter(AnthropicAdapter)` — thin subclass |
| `mcp_client.py` | 153 | Singleton MCP client factory for MiniMax MCP server (vision/compose/draw/talk) |
| `defaults.py` | 7 | `DEFAULTS` dict: `api_compat=anthropic`, `base_url=https://api.minimax.io/anthropic`, `api_key_env=MINIMAX_API_KEY`, `model=MiniMax-M2.7-highspeed`, `max_rpm=120` |

## Connections

- **Inherits**: `AnthropicAdapter` from `../anthropic/adapter.py` — full Anthropic Messages API protocol.
- **MCP client**: `mcp_client.py` imports `MCPClient` from `lingtai_kernel.services.mcp`.
- **External**: `anthropic` SDK (inherited), `uvx` binary (for MCP subprocess).
- **No direct google/openai imports**.

## Composition

### `MiniMaxAdapter(AnthropicAdapter)` — `adapter.py:7`

Only override is `__init__`:

| Override | Line | What changes |
|----------|------|-------------|
| `__init__` | 15 | Sets `base_url` to `https://api.minimax.io/anthropic` (or custom), calls `super().__init__()`, re-calls `_setup_gate(max_rpm=120)` |

All other methods (`create_chat`, `generate`, `make_tool_result_message`, `is_quota_error`, `send`, `send_stream`, etc.) are **inherited unchanged** from `AnthropicAdapter` / `AnthropicChatSession`.

### MCP client factory — `mcp_client.py`

Module-level singletons and config:

| Function | Line | Purpose |
|----------|------|---------|
| `set_enabled` | 35 | Enable/disable MCP client |
| `is_enabled` | 42 | Check enabled state |
| `set_api_host` | 47 | Set `MINIMAX_API_HOST` for subprocess |
| `set_extra_env` | 54 | Inject additional env vars (API keys not in `os.environ`) |
| `get_api_host` | 65 | Read current API host |
| `get_status` | 70 | Dict with `enabled`, `connected`, `error`, `api_host` |
| `get_minimax_mcp_client` | 88 | Singleton getter — lazy init with `_client_lock` (threading.Lock) |

**Singleton lifecycle** (`mcp_client.py:88-139`):
1. Checks `_client is not None and is_connected()` (double-checked locking).
2. Resolves `uvx` binary via `shutil.which("uvx")`.
3. Requires `_api_host` to be set (raises `RuntimeError` if not).
4. Builds subprocess env: `{**os.environ, "MINIMAX_API_HOST": host, **_extra_env}`.
5. Verifies at least one of `MINIMAX_API_KEY` / `MINIMAX_MCP_API_KEY` is present.
6. Creates `MCPClient(command=uvx, args=["minimax-coding-plan-mcp", "-y"], env=env)`.
7. Registers `atexit` cleanup (`_cleanup_at_exit` at line 142).

**API key model**: Two keys for different tools:
- `MINIMAX_API_KEY` — vision / code plan
- `MINIMAX_MCP_API_KEY` — talk, compose, draw, listen

## State

- `MiniMaxAdapter`: inherits all state from `AnthropicAdapter`; re-applies `_setup_gate(max_rpm=120)`.
- MCP module: `_enabled` (bool), `_api_host` (str), `_extra_env` (dict), `_client` (MCPClient singleton), `_client_lock` (threading.Lock).

## Notes

- **OpenAI-compatible quirks**: MiniMax's Anthropic-compatible endpoint (`/anthropic`) means the wire format is Anthropic Messages, not OpenAI Chat Completions. Tool shapes use `input_schema`, system is a separate parameter, etc.
- **Default RPM**: 120 (vs 0/unlimited for base Anthropic).
- **`defaults.py` `api_compat: "anthropic"`**: Signals to the custom-provider factory that this uses Anthropic wire format.
- **MCP subprocess**: Kept alive for agent process lifetime; `atexit` registered for cleanup. Requires `uvx` (from `uv` package manager).
- Git history: 5 commits; recent refactor removed compose/video/draw/talk/listen capabilities.
