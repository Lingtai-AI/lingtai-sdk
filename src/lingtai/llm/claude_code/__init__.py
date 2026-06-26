"""Claude Code provider — drive the local ``claude`` CLI as the agent brain.

This adapter lets a LingTai agent think on a Claude **subscription** (Pro/Max)
through the official ``claude`` command-line binary, rather than calling the
Anthropic API with a key. It is the Claude analogue of the Codex provider:
log in once with ``claude`` (or ``claude setup-token``), then point a preset at
``provider: "claude-code"``.

Design: the ``claude`` CLI is used in print mode (``claude -p --output-format
json``) as a *stateless reasoning core*. Each turn the adapter serialises the
canonical ChatInterface (system prompt + tool schemas + conversation) into a
single prompt, asks the CLI to emit exactly one JSON *action* (a tool call or a
final answer), and parses that back into the kernel's ``LLMResponse``. LingTai's
own message loop still executes the tools — so the main agent loop, intrinsics,
capabilities and MCP all stay intact. Claude's built-in tools are disabled so it
behaves as a pure brain.

Auth is owned entirely by the ``claude`` CLI (its stored OAuth credentials or
``CLAUDE_CODE_OAUTH_TOKEN``). The adapter never reads, stores, or replays a
token — it only strips ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` from the
child env so the subprocess cannot fall back to API-key billing. This keeps
usage on the sanctioned Claude Code / subscription channel.
"""

from __future__ import annotations

from .adapter import ClaudeCodeAdapter, ClaudeCodeChatSession

__all__ = ["ClaudeCodeAdapter", "ClaudeCodeChatSession"]
