# src/lingtai/services/vision/

Provider-specific image understanding — standalone services that own their own API clients.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 112 | `VisionService` ABC, `_MIME_BY_EXT` map, `_read_image()` helper, `create_vision_service()` factory |
| `anthropic.py` | 61 | `AnthropicVisionService` — base64 inline image via Anthropic Messages API |
| `codex.py` | 78 | `CodexVisionService` — ChatGPT Codex Responses API vision via OAuth token |
| `gemini.py` | 53 | `GeminiVisionService` — `genai.Client` + `types.Part.from_bytes` |
| `local.py` | 72 | `LocalVisionService` — mlx-vlm on Apple Silicon, lazy model load |
| `mimo.py` | 75 | `MiMoVisionService` — OpenAI SDK to `api.xiaomimimo.com/v1` |
| `minimax.py` | 94 | `MiniMaxVisionService` — MCP `understand_image` tool via `minimax-coding-plan-mcp` |
| `openai.py` | 56 | `OpenAIVisionService` — OpenAI chat completions with `image_url` content part |
| `zhipu.py` | 87 | `ZhipuVisionService` — MCP `analyze_image` via `@z_ai/mcp-server` Node.js subprocess |

## Connections

- **ABC contract** — all providers inherit `VisionService` (`__init__.py:17`); single abstract method `analyze_image(image_path, prompt) -> str`.
- **Factory** — `create_vision_service(provider, api_key=...)` at `__init__.py:63` dispatches by name with lazy imports. Supported: `anthropic`, `openai`, `gemini`, `minimax`, `zhipu`, `mimo`, `codex`, `local`.
- **MCP dependency** — `minimax.py` and `zhipu.py` import `lingtai.services.mcp.MCPClient` for subprocess-based tool calls.
- **External SDKs** — `anthropic` (Anthropic SDK), `openai` (OpenAI SDK; OpenAI/MiMo/Codex), `google.genai` (Gemini), `mlx_vlm` (local).
- **Logging** — MCP providers use `lingtai_kernel.logging.get_logger`.

## Composition

- **Standalone ownership** — each service creates and owns its own SDK client + credentials. API providers use API keys; Codex uses `CodexTokenManager`/ChatGPT OAuth; all remain independent of LLM adapters or agents.
- **Shared helper** — `_read_image(image_path) -> (bytes, mime_type)` at `__init__.py:47` used by all API-based providers.
- **MCP lazy init** — `minimax.py:34` (`_ensure_client`) and `zhipu.py:29` (`_ensure_client`) start MCP subprocesses on first call, with stale-connection recovery. Both expose `close()` for subprocess teardown.
- **Local lazy load** — `local.py:39` (`_ensure_loaded`) defers `mlx_vlm.load()` to first `analyze_image` call.

## State

- **Per-call stateless** — all services read the image file fresh each call, no caching.
- **Persistent MCP clients** — `minimax._client` and `zhipu._client` hold subprocess handles across calls.
- **Local model refs** — `local._model`, `local._processor`, `local._config` persist after first load.

## Notes

- **Image encoding** — Anthropic (`anthropic.py:34`) and OpenAI/MiMo/Codex (`openai.py:38`, `mimo.py:57`, `codex.py:45`) use base64 data URLs. Gemini (`gemini.py:37`) uses `types.Part.from_bytes`. Local passes file path directly to mlx-vlm. MCP providers send base64 in tool args (minimax) or file path (zhipu).
- **Default models** — Anthropic: `claude-sonnet-4-20250514`; Gemini: `gemini-2.5-flash`; OpenAI: `gpt-4o`; MiMo: `mimo-v2.5`; Codex: `gpt-5.5`; Local: `mlx-community/paligemma2-3b-ft-docci-448-8bit`.
- **MCP tool names** — MiniMax: `understand_image` (`minimax.py:77`); Zhipu: `analyze_image` (`zhipu.py:70`).
- **MCP launchers** — MiniMax uses `uvx minimax-coding-plan-mcp -y` (`minimax.py:64`); Zhipu uses `npx -y @z_ai/mcp-server` (`zhipu.py:58`).
- **Zhipu path vs base64** — Zhipu MCP reads the file directly by path (`zhipu.py:70`), unlike other providers that base64-encode.
- **Codex Responses API** — `codex.py` constructs `OpenAI(base_url="https://chatgpt.com/backend-api/codex")`, passes `instructions`, `stream=True`, `store=False`, `input_text` + `input_image` content blocks, and concatenates `response.output_text.delta` events. It omits `max_output_tokens` by default because the live ChatGPT Codex backend rejected that parameter (`codex.py:25-28`, `codex.py:70-71`).
- **Gemini thought filtering** — `gemini.py:51` skips `part.text` when `part.thought` is True to exclude reasoning output.
- **Git history** — 7 commits. Key: MiMo provider addition (`a728864`), zhipu path-based input fix (`2e6d53c`), region-aware ZAI/ZHIPU mode (`bed1c1e`).
