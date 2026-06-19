"""Web search capability — web lookup via SearchService.

Adds the ability to search the web. Requires a SearchService (either
passed directly or created via ``provider``/``api_key`` kwargs).

Usage:
    agent.add_capability("web_search", search_service=my_svc)
    agent.add_capability("web_search", provider="gemini", api_key="...")
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...i18n import t
from ...services.websearch import SearchService, create_search_service

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {
    "providers": ["duckduckgo", "minimax", "zhipu", "gemini", "anthropic", "openai"],
    "default": "duckduckgo",
    "fallback_on_inherit": "duckduckgo",
}

def get_description(lang: str = "en") -> str:
    return t(lang, "web_search.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": t(lang, "web_search.query")},
        },
        "required": ["query"],
    }



class WebSearchManager:
    """Handles web_search tool calls."""

    def __init__(
        self,
        agent: "BaseAgent",
        search_service: SearchService | None = None,
    ) -> None:
        self._agent = agent
        self._search_service = search_service

    def handle(self, args: dict) -> dict:
        query = args.get("query")
        if not query:
            return {"status": "error", "message": "Missing required parameter: query"}

        if self._search_service is None:
            return {
                "status": "error",
                "message": (
                    "No SearchService configured. Pass search_service=... or "
                    "provider='...' + api_key='...' in capability kwargs."
                ),
            }

        try:
            results = self._search_service.search(query)
        except Exception as exc:
            return {"status": "error", "message": f"Web search failed: {exc}"}

        formatted = "\n\n".join(
            f"**{r.title}**\n{r.url}\n{r.snippet}" for r in results
        )
        return {"status": "ok", "results": formatted or "No results found."}


def setup(
    agent: "BaseAgent",
    search_service: Any | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> WebSearchManager:
    """Set up the web_search capability on an agent.

    Args:
        agent: The agent to attach the capability to.
        search_service: A pre-built SearchService instance.
        provider: Provider name for ``create_search_service()``.
        api_key: API key for the provider.
        model: Optional model override for the provider.
    """
    if search_service is None and provider is not None:
        # Graceful fallback: if the resolved provider isn't supported by web_search,
        # use fallback_on_inherit (duckduckgo). Never raise.
        if provider not in PROVIDERS["providers"]:
            agent._log(
                "capability_fallback",
                capability="web_search",
                requested_provider=provider,
                fallback=PROVIDERS["fallback_on_inherit"],
            )
            provider = PROVIDERS["fallback_on_inherit"]
            api_key = None  # local fallback — no creds

        if provider == "duckduckgo":
            search_service = create_search_service("duckduckgo")
        else:
            from .._media_host import resolve_media_host
            extra_kwargs: dict = {"api_host": resolve_media_host(agent)}
            if provider == "zhipu":
                from .._zhipu_mode import resolve_z_ai_mode
                extra_kwargs["z_ai_mode"] = resolve_z_ai_mode(agent)
            search_service = create_search_service(
                provider, api_key=api_key, model=model,
                **extra_kwargs,
            )
    elif search_service is None and provider is None:
        search_service = create_search_service("duckduckgo")

    lang = agent._config.language
    mgr = WebSearchManager(agent, search_service=search_service)
    agent.add_tool(
        "web_search",
        schema=get_schema(lang),
        handler=mgr.handle,
        description=get_description(lang),
    )
    return mgr
