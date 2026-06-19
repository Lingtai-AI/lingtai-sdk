"""Zhipu vision service — standalone image analysis via Z.AI MCP server."""
from __future__ import annotations

from . import VisionService

from lingtai_kernel.logging import get_logger

logger = get_logger()


class ZhipuVisionService(VisionService):
    """Image understanding via Z.AI's ``image_analysis`` MCP tool.

    Creates its own MCPClient for the ``@z_ai/mcp-server`` Node.js server.
    The ``api_key`` is passed as ``Z_AI_API_KEY`` in the subprocess env.
    """

    def __init__(
        self,
        *,
        api_key: str,
        z_ai_mode: str = "ZAI",
        **_kwargs,
    ) -> None:
        self._api_key = api_key
        self._z_ai_mode = z_ai_mode
        self._client = None  # lazy init

    def _ensure_client(self):
        """Lazily start the MCP client subprocess."""
        if self._client is not None:
            from ...services.mcp import MCPClient
            if self._client.is_connected():
                return
            try:
                self._client.close()
            except Exception:
                pass

        import os
        import shutil
        from ...services.mcp import MCPClient

        npx_path = shutil.which("npx")
        if not npx_path:
            raise RuntimeError(
                "npx not found. Please install Node.js >= v22: "
                "https://nodejs.org/"
            )

        env = {
            **os.environ,
            "Z_AI_API_KEY": self._api_key,
            "Z_AI_MODE": self._z_ai_mode,
        }
        self._client = MCPClient(
            command=npx_path,
            args=["-y", "@z_ai/mcp-server"],
            env=env,
        )
        self._client.start()

    def analyze_image(self, image_path: str, prompt: str | None = None) -> str:
        """Analyze an image using Z.AI's analyze_image MCP tool."""
        question = prompt or "Describe this image."

        self._ensure_client()

        # The MCP server reads the file directly by path
        result = self._client.call_tool("analyze_image", {
            "image_source": image_path,
            "prompt": question,
        })
        if result.get("status") == "error":
            msg = result.get("message", "unknown error")
            logger.warning("Zhipu MCP vision error: %s", msg)
            return ""
        return result.get("text", "")

    def close(self) -> None:
        """Shut down the MCP client subprocess."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
