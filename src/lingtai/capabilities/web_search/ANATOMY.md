# src/lingtai/capabilities/web_search/

Web search capability — web lookup via pluggable SearchService backends.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|---|---|---|
| `__init__.py` | 130 | `WebSearchManager`, `setup()`, provider registry, tool schema |

**Key symbols:**
- `PROVIDERS` (L20-24) — supported: `duckduckgo`, `minimax`, `zhipu`, `gemini`, `anthropic`, `openai`. Default: `duckduckgo`. Fallback on inherit: `duckduckgo`.
- `WebSearchManager` (L41) — handles tool calls; delegates to `SearchService.search()`.
- `setup()` (L77) — entry point. Creates manager, registers `"web_search"` tool (L124-129).

## Connections

- **→ `lingtai.i18n.t`** (L14) — i18n for tool description and schema strings.
- **→ `lingtai.services.websearch.SearchService`** (L15) — abstract service interface + `create_search_service()` factory.
- **→ `capabilities._media_host.resolve_media_host`** (L110) — injected for non-duckduckgo providers.
- **→ `capabilities._zhipu_mode.resolve_z_ai_mode`** (L113) — injected for `zhipu` provider.
- **→ `lingtai_kernel.base_agent.BaseAgent`** — type-only (L18).
- **← `capabilities.__init__`** — registered as `".web_search"` in `_BUILTIN`.

## Composition

Single file. No internal state — `WebSearchManager` instances hold agent + service refs.

## State

- `WebSearchManager._agent` / `_search_service` (L49-50) — per-agent instance. Service can be `None` (returns error on call, L57-64).
- `PROVIDERS` dict is module-level constant.

## Notes

- Graceful fallback (L97-105): unsupported providers fall back to `duckduckgo` (with `api_key=None`). Unlike vision, this never skips — always provides search.
- No-provider default (L119-120): if neither `search_service` nor `provider` is given, defaults to `duckduckgo`.
- Results are formatted as markdown `**title**\nurl\nsnippet` (L71-73).
