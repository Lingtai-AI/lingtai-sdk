"""LingTai WhatsApp MCP server."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .licc import push_inbox_event
from .manager import WhatsAppManager, SCHEMA, DESCRIPTION
from .resources import resource_text
from .webhook_server import WhatsAppWebhookServer

log = logging.getLogger("lingtai_whatsapp")

_SERVER_INSTRUCTIONS = (
    "lingtai-whatsapp: official Meta WhatsApp Cloud API client. "
    "Configure via LINGTAI_WHATSAPP_CONFIG. Inbound delivery requires a public HTTPS webhook."
)


def load_config() -> tuple[dict[str, Any], Path]:
    raw = os.environ.get("LINGTAI_WHATSAPP_CONFIG")
    if not raw:
        raise ValueError("LINGTAI_WHATSAPP_CONFIG env var not set")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd())) / path
    if not path.is_file():
        raise FileNotFoundError(f"WhatsApp config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8")), path


def _accounts_from_config(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = cfg.get("accounts")
    if not accounts:
        raise ValueError("config must contain 'accounts' list")
    return list(accounts)


def build_manager() -> tuple[WhatsAppManager, Path]:
    cfg, config_path = load_config()
    working_dir = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
    working_dir.mkdir(parents=True, exist_ok=True)

    def _on_inbound(event: dict[str, Any]) -> None:
        push_inbox_event(sender=event["from"], subject=event["subject"], body=event["body"], metadata=event.get("metadata"), wake=event.get("wake", True))

    manager = WhatsAppManager(
        accounts_config=_accounts_from_config(cfg),
        working_dir=working_dir,
        on_inbound=_on_inbound,
        config_source=os.environ.get("LINGTAI_WHATSAPP_CONFIG") or str(config_path),
    )
    try:
        path = manager.write_identity_file()
        log.info("Wrote WhatsApp MCP identity metadata to %s", path)
    except Exception as e:
        log.warning("Failed to write WhatsApp MCP identity metadata (continuing): %s", e)
    return manager, working_dir


def _tool_result(obj: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(obj, ensure_ascii=False))]


def build_server(manager: WhatsAppManager | None) -> Server:
    server: Server = Server("lingtai-whatsapp", instructions=_SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [types.Tool(name="whatsapp", description=DESCRIPTION, inputSchema=SCHEMA)]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name != "whatsapp":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            return _tool_result({"status": "error", "error": "WhatsApp manager not initialized; check LINGTAI_WHATSAPP_CONFIG"})
        try:
            result = await asyncio.to_thread(manager.handle, arguments or {})
        except Exception as e:
            result = {"status": "error", "error": str(e), "error_type": type(e).__name__}
        return _tool_result(result)

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return [types.Resource(uri=uri, name=uri.rsplit("/", 1)[-1], mimeType=mime) for uri, mime in [
            ("lingtai://manifest", "application/json"),
            ("lingtai://skills/whatsapp", "text/markdown; profile=lingtai-skill"),
            ("lingtai://docs/configuration", "text/markdown"),
            ("lingtai://docs/troubleshooting", "text/markdown"),
            ("lingtai://status", "application/json"),
            ("lingtai://onboarding/whatsapp", "text/markdown"),
            ("lingtai://onboarding/html-template", "text/html"),
        ]]

    @server.read_resource()
    async def _read_resource(uri: str) -> str:
        status = manager.handle({"action": "status"}) if manager is not None else {"status": "not_initialized"}
        text, _mime = resource_text(str(uri), status)
        return text

    return server


async def serve() -> None:
    manager: WhatsAppManager | None = None
    webhook_server: WhatsAppWebhookServer | None = None
    try:
        manager, _wd = build_manager()
        log.info("WhatsApp manager initialized")
        webhook_server = WhatsAppWebhookServer.from_manager_config(manager)
        if webhook_server is not None:
            webhook_server.start()
    except Exception as e:
        log.error("eager start failed; tool calls will return errors until fixed: %s", e)
    server = build_server(manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        if webhook_server is not None:
            webhook_server.stop()
