"""MCP clients — async-to-sync bridges for MCP servers.

MCPClient: stdio subprocess servers (e.g., uvx minimax-coding-plan-mcp).
HTTPMCPClient: remote HTTP/SSE servers (e.g., api.z.ai/api/mcp/...).

Both provide the same synchronous call_tool() interface. A background daemon
thread runs the async event loop; the public API is thread-safe.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from lingtai_kernel.logging import get_logger

logger = get_logger()


class MCPClient:
    """Async-to-sync bridge for any MCP stdio server.

    Args:
        command: Executable to run (e.g., "uvx").
        args: Arguments to the command (e.g., ["minimax-coding-plan-mcp", "-y"]).
        env: Environment variables for the subprocess. If None, inherits
            the current process environment.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env

        self._session: Any = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._stdio_cm: Any = None
        self._session_cm: Any = None

        # Activity log for debugging — last 50 calls
        self._activity_log: list[dict[str, Any]] = []
        self._activity_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Exception helpers — never surface a blank error (issue #104)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_exception(exc: BaseException) -> str:
        """Render an exception as ``ClassName: message``.

        Some MCP/anyio exceptions (notably ``ClosedResourceError``) have an
        empty ``str()``. Falling through to that empty string is what produced
        the blank ``{"status": "error", "message": ""}`` in issue #104. When the
        message is empty we fall back to the class name alone.
        """
        cls = type(exc).__name__
        msg = str(exc).strip()
        return f"{cls}: {msg}" if msg else cls

    @staticmethod
    def _is_stale_resource_error(exc: BaseException) -> bool:
        """Detect a dead/closed MCP transport that warrants a restart.

        Primary signal is the exception class name ``ClosedResourceError``
        (anyio) — matched by name so we need not import anyio. As a secondary
        signal we look for closed-stream substrings in the message.
        """
        if type(exc).__name__ == "ClosedResourceError":
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("closed", "broken pipe", "stream", "transport")
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background thread and connect to the MCP server.

        Called automatically by call_tool() if not yet connected.
        """
        if self.is_connected():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)
        if self._error:
            raise RuntimeError(f"MCP server failed to start: {self._error}")

    def close(self) -> None:
        """Shut down the MCP session and background thread."""
        if self._closed:
            return
        self._closed = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """Tear down a (possibly stale) session and reconnect from scratch.

        ``start()`` early-returns when it believes it is connected and never
        clears latched startup state, so a stale ``_ready``/``_error`` or a
        ``_closed`` flag from a prior ``close()`` would make a fresh ``start()``
        lie (return immediately, or raise on the *old* error). This resets all
        startup/session fields so the subsequent ``start()`` is a real reconnect.
        Used by ``call_tool`` to recover from a closed stdio resource (issue #104).
        """
        self.close()
        self._ready.clear()
        self._error = None
        self._closed = False
        self._session = None
        self._read_stream = None
        self._write_stream = None
        self._loop = None
        self._thread = None
        self._stdio_cm = None
        self._session_cm = None
        self.start()

    def is_connected(self) -> bool:
        """Check if the client has an active session."""
        return (
            self._session is not None
            and self._loop is not None
            and self._loop.is_running()
            and not self._closed
        )

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def call_tool(self, name: str, args: dict, timeout: float = 120) -> dict:
        """Call an MCP tool synchronously.

        Starts the connection lazily if not yet connected.

        Args:
            name: Tool name (e.g., "web_search").
            args: Tool arguments dict.
            timeout: Timeout in seconds.

        Returns:
            Parsed dict from the tool's JSON response.

        Raises:
            RuntimeError: If the client is closed or connection fails.
        """
        import asyncio

        if self._closed:
            raise RuntimeError("MCP client has been closed")

        # Lazy start
        if not self.is_connected():
            self.start()

        if self._session is None or self._loop is None:
            raise RuntimeError("MCP client not connected")

        def _attempt() -> dict:
            async def _call():
                result = await self._session.call_tool(
                    name=name,
                    arguments=args,
                    read_timeout_seconds=timedelta(seconds=timeout),
                )
                if result.isError:
                    error_text = (
                        result.content[0].text
                        if result.content
                        else "Unknown MCP error"
                    )
                    return {"status": "error", "message": error_text}

                if result.content:
                    for block in result.content:
                        if hasattr(block, "text"):
                            try:
                                return json.loads(block.text)
                            except (json.JSONDecodeError, TypeError):
                                return {"status": "success", "text": block.text}

                return {"status": "success", "text": ""}

            future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
            return future.result(timeout=timeout)

        try:
            result = _attempt()
        except Exception as exc:
            formatted = self._format_exception(exc)
            if not self._is_stale_resource_error(exc):
                # Non-stale failure: surface the class name so the error is
                # never blank (issue #104), but don't churn the subprocess.
                result = {"status": "error", "message": formatted}
            else:
                # Stale/closed resource: tear down and reconnect, retry once.
                logger.warning(
                    "MCP tool %s hit stale resource (%s); restarting and "
                    "retrying once", name, formatted,
                )
                try:
                    self.restart()
                except Exception as restart_exc:
                    result = {
                        "status": "error",
                        "message": (
                            f"{formatted}: MCP session closed; restart failed: "
                            f"{self._format_exception(restart_exc)}"
                        ),
                    }
                else:
                    try:
                        result = _attempt()
                    except Exception as retry_exc:
                        result = {
                            "status": "error",
                            "message": (
                                f"{self._format_exception(retry_exc)}: MCP "
                                "session closed; restarted once but retry failed"
                            ),
                        }

        # Log activity
        with self._activity_lock:
            self._activity_log.append({
                "tool": name,
                "args": args,
                "result": result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if len(self._activity_log) > 50:
                self._activity_log[:] = self._activity_log[-50:]

        return result

    def list_tools(self, timeout: float = 10) -> list[dict]:
        """List available tools from the MCP server.

        Returns a list of dicts with 'name', 'description', and 'schema' keys.
        """
        import asyncio

        if not self.is_connected():
            self.start()

        if self._session is None or self._loop is None:
            raise RuntimeError("MCP client not connected")

        async def _list():
            result = await self._session.list_tools()
            tools = []
            for tool in result.tools:
                schema = {}
                if tool.inputSchema:
                    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "schema": schema,
                })
            return tools

        future = asyncio.run_coroutine_threadsafe(_list(), self._loop)
        return future.result(timeout=timeout)

    def get_activity_log(self) -> list[dict[str, Any]]:
        """Get recent MCP tool calls for debugging."""
        with self._activity_lock:
            return list(self._activity_log)

    # ------------------------------------------------------------------
    # Internal — background thread
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Background thread: run the async event loop with the MCP session."""
        import asyncio

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_connect())
            loop.run_forever()
        except Exception as e:
            # Preserve the class name so a blank str(e) (e.g. ClosedResourceError)
            # does not produce "MCP server failed to start: " (issue #104).
            self._error = self._format_exception(e)
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._async_cleanup())
            except Exception:
                pass
            loop.close()

    async def _async_connect(self) -> None:
        """Establish the MCP stdio connection (runs in background thread)."""
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession

        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )

        self._stdio_cm = stdio_client(server_params)
        self._read_stream, self._write_stream = await self._stdio_cm.__aenter__()

        self._session_cm = ClientSession(self._read_stream, self._write_stream)
        self._session = await self._session_cm.__aenter__()

        await self._session.initialize()

        self._ready.set()

    async def _async_cleanup(self) -> None:
        """Clean up MCP session and stdio transport."""
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
        if self._stdio_cm:
            try:
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass


class HTTPMCPClient:
    """Async-to-sync bridge for remote HTTP MCP servers.

    Connects to a remote MCP server via streamable HTTP transport.
    Same call_tool() interface as MCPClient.

    Args:
        url: HTTP endpoint of the MCP server (e.g., "https://api.z.ai/api/mcp/web_search_prime/mcp").
        headers: HTTP headers (e.g., {"Authorization": "Bearer ..."}).
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ):
        self._url = url
        self._headers = headers or {}

        self._session: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._transport_cm: Any = None
        self._session_cm: Any = None

        self._activity_log: list[dict[str, Any]] = []
        self._activity_lock = threading.Lock()

    def start(self) -> None:
        if self.is_connected():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)
        if self._error:
            raise RuntimeError(f"HTTP MCP server failed to connect: {self._error}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def is_connected(self) -> bool:
        return (
            self._session is not None
            and self._loop is not None
            and self._loop.is_running()
            and not self._closed
        )

    def call_tool(self, name: str, args: dict, timeout: float = 120) -> dict:
        """Call an MCP tool synchronously. Same interface as MCPClient."""
        import asyncio

        if self._closed:
            raise RuntimeError("HTTP MCP client has been closed")
        if not self.is_connected():
            self.start()
        if self._session is None or self._loop is None:
            raise RuntimeError("HTTP MCP client not connected")

        async def _call():
            result = await self._session.call_tool(
                name=name,
                arguments=args,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
            if result.isError:
                error_text = (
                    result.content[0].text if result.content else "Unknown MCP error"
                )
                return {"status": "error", "message": error_text}
            if result.content:
                for block in result.content:
                    if hasattr(block, "text"):
                        try:
                            return json.loads(block.text)
                        except (json.JSONDecodeError, TypeError):
                            return {"status": "success", "text": block.text}
            return {"status": "success", "text": ""}

        future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        result = future.result(timeout=timeout)

        with self._activity_lock:
            self._activity_log.append({
                "tool": name,
                "args": args,
                "result": result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if len(self._activity_log) > 50:
                self._activity_log[:] = self._activity_log[-50:]

        return result

    def list_tools(self, timeout: float = 10) -> list[dict]:
        """List available tools from the MCP server."""
        import asyncio

        if not self.is_connected():
            self.start()
        if self._session is None or self._loop is None:
            raise RuntimeError("HTTP MCP client not connected")

        async def _list():
            result = await self._session.list_tools()
            tools = []
            for tool in result.tools:
                schema = {}
                if tool.inputSchema:
                    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "schema": schema,
                })
            return tools

        future = asyncio.run_coroutine_threadsafe(_list(), self._loop)
        return future.result(timeout=timeout)

    def _run_loop(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_connect())
            loop.run_forever()
        except Exception as e:
            # Preserve the class name so a blank str(e) is not surfaced as an
            # empty connect error (issue #104).
            self._error = MCPClient._format_exception(e)
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._async_cleanup())
            except Exception:
                pass
            loop.close()

    async def _async_connect(self) -> None:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession

        self._transport_cm = streamablehttp_client(
            url=self._url,
            headers=self._headers,
        )
        self._read_stream, self._write_stream, _ = await self._transport_cm.__aenter__()

        self._session_cm = ClientSession(self._read_stream, self._write_stream)
        self._session = await self._session_cm.__aenter__()

        await self._session.initialize()
        self._ready.set()

    async def _async_cleanup(self) -> None:
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
        if self._transport_cm:
            try:
                await self._transport_cm.__aexit__(None, None, None)
            except Exception:
                pass
