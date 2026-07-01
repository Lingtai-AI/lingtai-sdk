"""LingTai daemon common MCP server.

The model-visible contract is the ``finish`` tool. The JSON file it writes is
an internal daemon transport and is validated again by the daemon runner before
any backend is allowed to mark a run done.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from lingtai_kernel._fsutil import atomic_write_json

STATUSES = {"done", "failed", "incomplete"}

FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": sorted(STATUSES),
            "description": "Terminal daemon status: done, failed, or incomplete.",
        },
        "summary": {
            "type": "string",
            "description": "Short result summary for the parent agent.",
        },
        "reason": {
            "type": "string",
            "description": "Required when status is failed or incomplete.",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional run-dir-relative or absolute artifact paths.",
        },
    },
    "required": ["status"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Finish this LingTai daemon run. Call exactly once before your final answer. "
    "Use status='done' only when the task is complete; use status='failed' or "
    "status='incomplete' when blocked, unvalidated, or unable to finish."
)


def _completion_path() -> Path:
    raw = os.environ.get("LINGTAI_DAEMON_COMPLETION_FILE")
    if not raw:
        raise RuntimeError("missing LINGTAI_DAEMON_COMPLETION_FILE")
    return Path(raw)


def _validate_finish(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("finish arguments must be an object")
    status = arguments.get("status")
    if status not in STATUSES:
        raise ValueError("status must be one of: done, failed, incomplete")
    summary = arguments.get("summary")
    reason = arguments.get("reason")
    artifacts = arguments.get("artifacts")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("summary must be a string")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    if artifacts is not None and (
        not isinstance(artifacts, list)
        or not all(isinstance(item, str) for item in artifacts)
    ):
        raise ValueError("artifacts must be an array of strings")
    if status in {"failed", "incomplete"} and not (reason and reason.strip()):
        raise ValueError("reason is required for failed or incomplete status")
    payload = {
        "schema": "lingtai.daemon_completion.v1",
        "status": status,
        "run_id": os.environ.get("LINGTAI_DAEMON_RUN_ID"),
    }
    if summary is not None:
        payload["summary"] = summary
    if reason is not None:
        payload["reason"] = reason
    if artifacts is not None:
        payload["artifacts"] = artifacts
    return payload


def build_server() -> Server:
    server: Server = Server(
        "lingtai-daemon-common",
        instructions=(
            "Use the `finish` tool to explicitly complete the daemon run. "
            "Do not rely on final text alone."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="finish",
                description=DESCRIPTION,
                inputSchema=FINISH_SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "finish":
            raise ValueError(f"unknown tool: {name!r}")
        try:
            payload = _validate_finish(arguments or {})
            path = _completion_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, payload, ensure_ascii=False, indent=2)
            result = {
                "status": "ok",
                "completion_status": payload["status"],
                "message": "daemon completion recorded",
            }
        except Exception as e:
            result = {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
            }
        return [types.TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False),
        )]

    return server


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
