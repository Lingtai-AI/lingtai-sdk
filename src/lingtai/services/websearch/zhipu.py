"""Zhipu web search — uses Z.AI's web search MCP server (HTTP remote)."""
from __future__ import annotations

from lingtai_kernel.logging import get_logger

from . import SearchResult, SearchService

logger = get_logger()


class ZhipuSearchService(SearchService):
    """Web search via Z.AI's ``web_search_prime`` HTTP MCP tool.

    Connects to the remote MCP server at
    ``https://api.z.ai/api/mcp/web_search_prime/mcp``.

    Args:
        api_key: Z.AI API key (ZHIPU_API_KEY).
    """

    MCP_URLS = {
        "ZAI": "https://api.z.ai/api/mcp/web_search_prime/mcp",
        "ZHIPU": "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp",
    }

    def __init__(self, api_key: str, z_ai_mode: str = "ZAI", **_kwargs) -> None:
        self._api_key = api_key
        self._z_ai_mode = z_ai_mode
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            if self._client.is_connected():
                return
            try:
                self._client.close()
            except Exception:
                pass

        from ...services.mcp import HTTPMCPClient

        self._client = HTTPMCPClient(
            url=self.MCP_URLS.get(self._z_ai_mode, self.MCP_URLS["ZAI"]),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        self._client.start()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import json as _json

        try:
            self._ensure_client()
            result = self._client.call_tool("web_search_prime", {"search_query": query})
        except Exception as e:
            logger.warning("Zhipu MCP web search failed: %s", e)
            return []

        # The MCP tool returns various formats depending on the response
        if isinstance(result, dict):
            if result.get("status") == "error":
                logger.warning("Zhipu MCP web search error: %s", result.get("message"))
                return []
            text = result.get("text", "") or str(result)
        elif isinstance(result, str):
            text = result
            # Try to parse as JSON array of search results
            try:
                parsed = _json.loads(text)
                if isinstance(parsed, list) and parsed:
                    items: list[SearchResult] = []
                    for item in parsed[:max_results]:
                        if isinstance(item, dict):
                            items.append(SearchResult(
                                title=item.get("title", ""),
                                url=item.get("link", item.get("url", "")),
                                snippet=item.get("content", item.get("snippet", "")),
                            ))
                    return items
            except (_json.JSONDecodeError, TypeError):
                pass
        else:
            text = str(result)

        if not text or text == "[]":
            return []

        return [
            SearchResult(
                title="Zhipu Web Search",
                url="",
                snippet=text,
            )
        ]
