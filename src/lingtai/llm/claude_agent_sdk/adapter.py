"""Claude Agent SDK completion adapter — clean-room provider.

This wraps the public ``claude_agent_sdk`` package and treats it as a plain
next-turn text generator. The SDK normally runs its *own* agentic tool loop;
LingTai already owns the tool loop, so this adapter deliberately disables the
SDK's tools (``allowed_tools=[]``, ``max_turns=1``) and only asks for one
assistant text turn per ``send()``.

Design (see ANATOMY.md):

- The canonical :class:`ChatInterface` is the single source of truth. Every
  ``send()`` commits the incoming message, runs the pre-request hook, then
  renders the whole conversation into a single role-labeled prompt string
  that is handed to ``query()``.
- The SDK authenticates through the local Claude CLI / login (no per-request
  API key), so there is no key handling here. A missing ``claude_agent_sdk``
  package raises a clear :class:`RuntimeError` only when first used — importing
  ``lingtai.llm`` never fails just because the optional SDK is absent.
- ``AssistantMessage`` text blocks are concatenated into ``LLMResponse.text``;
  ``ResultMessage.usage`` (when present) is parsed into ``UsageMetadata``.
  ``tool_calls`` is always empty — the SDK runs no tools in this mode.

This adapter is the **only** module that imports ``claude_agent_sdk``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
    UsageMetadata,
)
from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolResultBlock
from lingtai_kernel.logging import get_logger
from lingtai.llm.base import LLMAdapter

logger = get_logger()


_IMPORT_ERROR_HINT = (
    "The 'claude-agent-sdk' provider requires the optional 'claude_agent_sdk' "
    "package and a working Claude CLI login.\n"
    "  1. pip install claude-agent-sdk\n"
    "  2. Install the Claude CLI and run `claude login` (the SDK authenticates "
    "through the local CLI session, not an API key).\n"
    "Original import error: {err}"
)


def _import_sdk():
    """Lazily import ``claude_agent_sdk``.

    Raises a clear :class:`RuntimeError` with install/auth guidance if the
    package is missing, so importing ``lingtai.llm`` never breaks when the
    optional SDK is absent — only first *use* of the provider does.
    """
    try:
        import claude_agent_sdk  # type: ignore
    except ImportError as err:  # pragma: no cover - exercised via fake module
        raise RuntimeError(_IMPORT_ERROR_HINT.format(err=err)) from err
    return claude_agent_sdk


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
}


def _render_block(block: Any) -> str:
    """Render one canonical content block as plain transcript text."""
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ToolResultBlock):
        content = block.content
        if not isinstance(content, str):
            import json

            content = json.dumps(content, default=str)
        return f"[tool result: {block.name}]\n{content}"
    # ToolCallBlock / ThinkingBlock — surface a compact form so the transcript
    # round-trips even though this provider issues no tool calls itself.
    text = getattr(block, "text", None)
    if text:
        return text
    name = getattr(block, "name", None)
    if name:
        import json

        args = getattr(block, "args", {})
        return f"[tool call: {name}({json.dumps(args, default=str)})]"
    return ""


def _build_prompt(interface: ChatInterface) -> str:
    """Render the conversation (excluding system) into one role-labeled prompt.

    The SDK's ``system_prompt`` carries the system text separately, so here we
    only join the user/assistant turns. Each entry becomes a ``Role:`` block;
    the trailing line nudges the model to produce the next assistant turn.
    """
    lines: list[str] = []
    for entry in interface.conversation_entries():
        label = _ROLE_LABELS.get(entry.role, entry.role.capitalize())
        rendered = "\n".join(
            part for part in (_render_block(b) for b in entry.content) if part
        )
        if rendered:
            lines.append(f"{label}: {rendered}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Response collection
# ---------------------------------------------------------------------------


def _collect_response(sdk, messages: list[Any]) -> LLMResponse:
    """Gather SDK message objects into a provider-agnostic LLMResponse.

    Pulls text from ``AssistantMessage`` ``TextBlock``s and usage from a
    ``ResultMessage`` if one is present. Detection is duck-typed (``type``
    name + attribute presence) so a faked SDK in tests works without the
    real classes.
    """
    text_parts: list[str] = []
    usage = UsageMetadata()

    assistant_cls = getattr(sdk, "AssistantMessage", None)
    text_block_cls = getattr(sdk, "TextBlock", None)
    result_cls = getattr(sdk, "ResultMessage", None)

    for msg in messages:
        if assistant_cls is not None and isinstance(msg, assistant_cls):
            for block in getattr(msg, "content", []) or []:
                if text_block_cls is not None and isinstance(block, text_block_cls):
                    text_parts.append(getattr(block, "text", ""))
                elif getattr(block, "text", None) is not None:
                    # Duck-typed fallback for text-bearing blocks.
                    text_parts.append(block.text)
        elif result_cls is not None and isinstance(msg, result_cls):
            usage = _parse_usage(getattr(msg, "usage", None)) or usage

    return LLMResponse(
        text="".join(text_parts),
        tool_calls=[],  # LingTai owns the tool loop; the SDK runs no tools here
        usage=usage,
        raw=messages,
    )


def _parse_usage(raw_usage: Any) -> UsageMetadata | None:
    """Parse a ``ResultMessage.usage`` payload into UsageMetadata.

    The SDK reports usage as a dict shaped like the Anthropic Messages API
    (``input_tokens`` / ``output_tokens`` plus optional cache fields). Tolerate
    both dict and attribute access, and a missing payload.
    """
    if raw_usage is None:
        return None

    def _get(key: str) -> int:
        if isinstance(raw_usage, dict):
            return int(raw_usage.get(key, 0) or 0)
        return int(getattr(raw_usage, key, 0) or 0)

    cache_read = _get("cache_read_input_tokens")
    cache_write = _get("cache_creation_input_tokens")
    return UsageMetadata(
        input_tokens=_get("input_tokens") + cache_read + cache_write,
        output_tokens=_get("output_tokens"),
        cached_tokens=cache_read,
    )


def _run_query(sdk, prompt: str, options: Any) -> list[Any]:
    """Drive the SDK's async ``query()`` to completion, returning all messages.

    ``query()`` is an async generator; we collect it on a private event loop so
    the synchronous ChatSession contract holds regardless of the caller's loop
    state.
    """

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for message in sdk.query(prompt=prompt, options=options):
            out.append(message)
        return out

    return asyncio.run(_collect())


# ---------------------------------------------------------------------------
# ClaudeAgentSDKChatSession
# ---------------------------------------------------------------------------


class ClaudeAgentSDKChatSession(ChatSession):
    """Single-turn chat session backed by ``claude_agent_sdk.query``.

    Uses :class:`ChatInterface` as the single source of truth and rebuilds a
    role-labeled prompt from it on every call.
    """

    def __init__(
        self,
        model: str,
        system_prompt: str,
        interface: ChatInterface,
        *,
        cwd: str | None = None,
        extra_options: dict | None = None,
        context_window: int = 0,
    ):
        self._model = model
        self._system = system_prompt
        self._interface = interface
        self._cwd = cwd
        self._extra_options = extra_options or {}
        self._context_window = context_window

    @property
    def interface(self) -> ChatInterface:
        return self._interface

    def _build_options(self, sdk) -> Any:
        """Build ``ClaudeAgentOptions`` for a single next-turn generation.

        No SDK tools (``allowed_tools=[]``), a single turn (``max_turns=1``):
        LingTai keeps the tool loop, so the SDK is just a text generator.
        """
        opts: dict[str, Any] = {
            "model": self._model,
            "system_prompt": self._system,
            "allowed_tools": [],
            "max_turns": 1,
            "setting_sources": [],
        }
        if self._cwd:
            opts["cwd"] = self._cwd
        opts.update(self._extra_options)
        return sdk.ClaudeAgentOptions(**opts)

    def send(self, message) -> LLMResponse:
        """Send a user message (str), tool results (list), or continue (``None``).

        Commits the message to the canonical interface, fires the pre-request
        hook, renders the full transcript into one prompt, and asks the SDK for
        one assistant turn. The assistant text is appended back to the
        interface. On failure the trailing user entry is reverted.
        """
        if message is None:
            pass  # caller pre-staged the wire
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        try:
            if self.pre_request_hook is not None:
                self.pre_request_hook(self._interface)

            sdk = _import_sdk()
            self._interface.enforce_tool_pairing()
            prompt = _build_prompt(self._interface)
            options = self._build_options(sdk)
            messages = _run_query(sdk, prompt, options)
        except Exception:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        response = _collect_response(sdk, messages)

        if response.text:
            self._interface.add_assistant_message(
                [TextBlock(text=response.text)],
                model=self._model,
                provider="claude-agent-sdk",
                usage={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            )

        return response

    def send_stream(
        self,
        message,
        on_chunk: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Non-streaming under the hood — deliver the final text as one chunk.

        The SDK exposes incremental messages, but this provider treats it as a
        plain next-turn generator, so we run ``send()`` and emit the final text
        once via ``on_chunk`` if provided.
        """
        response = self.send(message)
        if on_chunk and response.text:
            on_chunk(response.text)
        return response

    def commit_tool_results(self, tool_results: list) -> None:
        if tool_results:
            self._interface.add_tool_results(tool_results)

    def update_system_prompt(self, system_prompt: str) -> None:
        self._system = system_prompt
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)

    def context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# ClaudeAgentSDKAdapter
# ---------------------------------------------------------------------------


class ClaudeAgentSDKAdapter(LLMAdapter):
    """Adapter that drives ``claude_agent_sdk.query`` as a next-turn generator.

    Clean-room completion provider: it relies only on the public SDK surface
    (``query``, ``ClaudeAgentOptions``, ``AssistantMessage``, ``TextBlock``,
    ``ResultMessage``). The SDK is imported lazily inside the call path.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        cwd: str | None = None,
        max_rpm: int = 0,
    ):
        self._default_model = model or "sonnet"
        self._cwd = cwd
        self._setup_gate(max_rpm)

    # -- LLMAdapter interface --------------------------------------------------

    def create_chat(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        *,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        interaction_id: str | None = None,  # ignored
        context_window: int = 0,
    ) -> ClaudeAgentSDKChatSession:
        # Tools are recorded in the canonical interface for transcript/usage
        # bookkeeping, but the SDK is never given them — LingTai dispatches all
        # tools itself, parsing them out of the assistant text turn upstream.
        if interface is not None:
            iface = interface
        else:
            iface = ChatInterface()
            tool_dicts = FunctionSchema.list_to_dicts(tools)
            iface.add_system(system_prompt, tools=tool_dicts)

        session = ClaudeAgentSDKChatSession(
            model=model or self._default_model,
            system_prompt=system_prompt,
            interface=iface,
            cwd=self._cwd,
            context_window=context_window,
        )
        return self._wrap_with_gate(session)

    def generate(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,  # unsupported by this provider
        json_schema: dict | None = None,  # unsupported by this provider
        max_output_tokens: int | None = None,  # unsupported by this provider
    ) -> LLMResponse:
        """One-shot generation. Renders ``contents`` into a single prompt."""
        sdk = _import_sdk()

        if isinstance(contents, str):
            prompt = contents
        elif isinstance(contents, list):
            parts: list[str] = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                else:
                    parts.append(str(item))
            prompt = "\n\n".join(parts)
        else:
            prompt = str(contents)

        opts: dict[str, Any] = {
            "model": model or self._default_model,
            "allowed_tools": [],
            "max_turns": 1,
            "setting_sources": [],
        }
        if system_prompt:
            opts["system_prompt"] = system_prompt
        if self._cwd:
            opts["cwd"] = self._cwd
        options = sdk.ClaudeAgentOptions(**opts)

        messages = self._gated_call(lambda: _run_query(sdk, prompt, options))
        return _collect_response(sdk, messages)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        return ToolResultBlock(
            id=tool_call_id or f"toolu_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        """Best-effort rate-limit detection by message text.

        The SDK does not expose a typed rate-limit error, so match on the
        usual signals (HTTP 429 / "rate limit") in the exception string.
        """
        msg = (str(exc) or "").lower()
        return "rate limit" in msg or "429" in msg
