# src/lingtai/services/websearch/

Provider-specific web search — standalone services behind a common `SearchService` ABC.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 116 | `SearchResult` dataclass, `SearchService` ABC, `create_search_service()` factory |
| `anthropic.py` | 62 | `AnthropicSearchService` — Claude `web_search_20250305` native tool |
| `duckduckgo.py` | 25 | `DuckDuckGoSearchService` — `ddgs` package, zero API key |
| `gemini.py` | 63 | `GeminiSearchService` — Gemini `GoogleSearch` grounding tool |
| `minimax.py` | 108 | `MiniMaxSearchService` — MCP `web_search` via `minimax-coding-plan-mcp` |
| `openai.py` | 52 | `OpenAISearchService` — `gpt-4o-search-preview` with `web_search_options` |
| `zhipu.py` | 101 | `ZhipuSearchService` — `HTTPMCPClient` to Z.AI `web_search_prime` remote MCP |

## Connections

- **ABC contract** — all providers inherit `SearchService` (`__init__.py:27`); single abstract method `search(query, max_results) -> list[SearchResult]`.
- **Factory** — `create_search_service(provider, api_key=...)` at `__init__.py:48` dispatches by name. Supported: `duckduckgo`, `anthropic`, `openai`, `gemini`, `minimax`, `zhipu`.
- **MCP dependencies** — `minimax.py` uses `lingtai.services.mcp.MCPClient` (subprocess); `zhipu.py` uses `lingtai.services.mcp.HTTPMCPClient` (remote HTTP).
- **External SDKs** — `anthropic`, `openai`, `google.genai`, `ddgs`.
- **Logging** — all providers except DuckDuckGo use `lingtai_kernel.logging.get_logger`.

## Composition

- **LLM-grounded providers** (anthropic, openai, gemini) — create a fresh SDK client inside each `search()` call, send a one-shot prompt with a search tool enabled, extract text from the response. Fully stateless.
- **MCP providers** (minimax, zhipu) — use lazy `_ensure_client()` with connection health checks and stale-connection recovery.
- **DuckDuckGo** — simplest provider; direct `DDGS().text()` call, no client state.
- **Result normalization** — LLM-grounded providers return a single `SearchResult` with the model's synthesized text as `snippet`. MCP providers attempt structured extraction (minimax `organic` array at `minimax.py:87`, zhipu JSON array at `zhipu.py:76`) and fall back to raw text.

## State

- **LLM-grounded** — stateless per-call. Fresh client created each invocation.
- **DuckDuckGo** — stateless per-call.
- **MiniMax** — persistent `MCPClient` subprocess; class-level `_atexit_registered` flag for cleanup (`minimax.py:59`).
- **Zhipu** — persistent `HTTPMCPClient` connection (`zhipu.py:42`).

## Notes

- **Provider search mechanisms** — Anthropic: `web_search_20250305` tool type (`anthropic.py:37`); OpenAI: `web_search_options={}` kwarg (`openai.py:32`); Gemini: `GoogleSearch()` tool (`gemini.py:33`); MiniMax: MCP `web_search` tool (`minimax.py:76`); Zhipu: MCP `web_search_prime` tool (`zhipu.py:61`).
- **Default models** — Anthropic: `claude-sonnet-4-20250514`; OpenAI: `gpt-4o-search-preview`; Gemini: `gemini-3-flash-preview`.
- **Zhipu MCP modes** — Two endpoints configured in `MCP_URLS` dict (`zhipu.py:21`): `ZAI` → `api.z.ai`, `ZHIPU` → `open.bigmodel.cn`. Selected by `z_ai_mode` param.
- **Zhipu result parsing** — Tries JSON array parse first (`zhipu.py:76`), supports `title`/`link`/`url` and `content`/`snippet` field variants (`zhipu.py:81-84`).
- **MiniMax atexit** — `MiniMaxSearchService._atexit_registered` (`minimax.py:26`) ensures the class-level atexit handler is registered exactly once across all instances.
- **MiniMax structured results** — Checks for `result["organic"]` array (`minimax.py:87`) before falling back to `result["text"]` or `result["answer"]`.
- **Error handling** — All providers catch exceptions in `search()` and return `[]` on failure with a `logger.warning`.
- **Git history** — 6 commits. Key: zhipu provider addition (`3100311`), HTTPMCPClient switch (`092bff6`), region-aware mode (`bed1c1e`).
