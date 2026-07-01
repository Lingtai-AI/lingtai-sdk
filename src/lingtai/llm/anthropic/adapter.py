"""Anthropic adapter — wraps the ``anthropic`` SDK for Claude models.

This is the **only** module that imports the ``anthropic`` package.

Key Anthropic API differences from OpenAI/Gemini:
- System prompt is a separate ``system`` parameter, not a message.
- Strict user/assistant alternation required — consecutive same-role messages
  must be merged.
- Tool results are sent inside a ``user`` message with ``tool_result`` blocks.
- Thinking/extended thinking is controlled via a ``thinking`` parameter with
  a token budget.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import anthropic
import httpx

from lingtai_kernel.logging import get_logger

logger = get_logger()


def _build_http_timeout(request_timeout: float | None):
    """Build explicit per-phase HTTP timeout for SDK calls."""
    if request_timeout is None:
        return None
    return httpx.Timeout(
        connect=min(float(request_timeout), 30.0),
        read=min(float(request_timeout), 60.0),
        write=min(float(request_timeout), 30.0),
        pool=10.0,
    )


from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
    UsageMetadata,
)
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai.llm.base import LLMAdapter
from lingtai_kernel.llm.interface import ChatInterface
from ..interface_converters import to_anthropic
from lingtai_kernel.llm.streaming import StreamingAccumulator
from lingtai.llm.identity_headers import merge_lingtai_identity_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tools(
    schemas: list[FunctionSchema] | None, *, cache_tools: bool = False
) -> list[dict] | None:
    """Convert FunctionSchema list to Anthropic tool format."""
    if not schemas:
        return None
    tools = [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters,
        }
        for s in schemas
    ]
    if cache_tools and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def _build_system_with_cache(system_prompt: str) -> list[dict]:
    """Build system prompt as cached content blocks for Anthropic.

    Single-block form: one cache breakpoint at the end of the prompt.
    Used by update_system_prompt(str). For the batched form with per-batch
    breakpoints, see _build_system_batches_with_cache.
    """
    if not system_prompt:
        return []
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _build_system_batches_with_cache(
    batches: list[str], *, max_breakpoints: int = 3
) -> list[dict]:
    """Build a multi-block system prompt with per-batch cache breakpoints.

    ``batches`` is the ordered output of build_system_prompt_batches:
    typically [immovable, rarely-mutated, per-idle]. Empty batches are
    dropped. A ``cache_control`` marker is placed on each batch boundary
    *except the last* — the last batch is volatile (e.g. grows every
    idle) and caching it would churn. This places up to
    ``max_breakpoints - 1`` in-system breakpoints; combined with the
    1 breakpoint on tools, that totals ``max_breakpoints`` and stays
    under Anthropic's 4-slot-per-request cap.

    When only a single non-empty batch exists, falls back to the
    single-block form (one breakpoint at the end).
    """
    non_empty = [b for b in batches if b]
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return _build_system_with_cache(non_empty[0])

    # Cap the number of cache markers we emit inside the system list.
    # The final batch gets no marker (too volatile to cache effectively).
    # Earlier batches get markers up to the cap.
    max_markers = max(0, max_breakpoints - 1)
    blocks: list[dict] = []
    num_marked = 0
    last_idx = len(non_empty) - 1
    for i, text in enumerate(non_empty):
        block: dict = {"type": "text", "text": text}
        if i < last_idx and num_marked < max_markers:
            block["cache_control"] = {"type": "ephemeral"}
            num_marked += 1
        blocks.append(block)
    return blocks


def _parse_response(raw) -> LLMResponse:
    """Parse an Anthropic Messages response into a provider-agnostic LLMResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    thoughts: list[str] = []

    for block in raw.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(
                    name=block.name,
                    args=block.input if isinstance(block.input, dict) else {},
                    id=block.id,
                )
            )
        elif block.type == "thinking":
            thinking_text = getattr(block, "thinking", None)
            if thinking_text:
                thoughts.append(thinking_text)

    # Token usage — includes cache metrics
    # Anthropic's input_tokens only counts tokens AFTER the last cache
    # breakpoint.  The true total is: input_tokens + cache_read + cache_write.
    # We normalise here so the rest of the system sees the same semantics as
    # OpenAI (prompt_tokens = total) and Gemini (prompt_token_count = total).
    usage = UsageMetadata()
    if raw.usage:
        cache_read = getattr(raw.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(raw.usage, "cache_creation_input_tokens", 0) or 0
        raw_input = getattr(raw.usage, "input_tokens", 0) or 0
        usage = UsageMetadata(
            input_tokens=raw_input + cache_read + cache_write,
            output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
            thinking_tokens=getattr(raw.usage, "thinking_tokens", 0) or 0,
            cached_tokens=cache_read,
        )
        if cache_read or cache_write:
            logger.debug(
                "Anthropic cache: read=%d write=%d uncached=%d total_input=%d",
                cache_read,
                cache_write,
                raw_input,
                usage.input_tokens,
            )

    return LLMResponse(
        text="\n".join(text_parts) if text_parts else "",
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


def _tool_result_to_dict(block: ToolResultBlock) -> dict:
    """Convert a canonical ToolResultBlock to Anthropic tool_result dict."""
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": block.content if isinstance(block.content, str) else json.dumps(block.content, default=str),
    }


def _ensure_alternation(messages: list[dict]) -> list[dict]:
    """Merge consecutive same-role messages to satisfy Anthropic's alternation rule.

    Anthropic requires strict user/assistant alternation. If two consecutive
    messages have the same role, merge their content.
    """
    if not messages:
        return messages

    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]
            # Merge content — both could be str or list
            prev_content = prev.get("content", "")
            new_content = msg.get("content", "")

            # Normalize to list form for merging
            if isinstance(prev_content, str):
                prev_list = (
                    [{"type": "text", "text": prev_content}] if prev_content else []
                )
            else:
                prev_list = list(prev_content)

            if isinstance(new_content, str):
                new_list = (
                    [{"type": "text", "text": new_content}] if new_content else []
                )
            else:
                new_list = list(new_content)

            combined = prev_list + new_list
            prev["content"] = combined
        else:
            merged.append(dict(msg))

    return merged


def _response_to_messages(raw) -> list[dict]:
    """Convert an Anthropic response into message dicts for the history."""
    result: dict[str, Any] = {"role": "assistant", "content": []}

    for block in raw.content:
        if block.type == "text":
            result["content"].append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result["content"].append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input if isinstance(block.input, dict) else {},
                }
            )
        elif block.type == "thinking":
            # Include thinking blocks so history round-trips correctly
            result["content"].append(
                {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    # Anthropic requires a signature for thinking blocks in history
                    "signature": getattr(block, "signature", ""),
                }
            )

    if not result["content"]:
        result["content"] = [{"type": "text", "text": ""}]

    return [result]


# ---------------------------------------------------------------------------
# AnthropicChatSession
# ---------------------------------------------------------------------------


class AnthropicChatSession(ChatSession):
    """Client-managed chat session for the Anthropic Messages API.

    Uses ChatInterface as the single source of truth. Rebuilds Anthropic
    message format from the interface on each API call.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        system_prompt: str | list[dict],
        interface: ChatInterface,
        tools: list[dict] | None,
        tool_choice: dict | None,
        extra_kwargs: dict,
        client_kwargs: dict | None = None,
        context_window: int = 0,
    ):
        self._client = client
        self._model = model
        self._system = system_prompt
        self._interface = interface
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._client_kwargs = client_kwargs or {}
        self._context_window = context_window
        # Per-request HTTP timeout (seconds). Set by send_with_timeout before
        # dispatching the worker so the HTTP client aborts at the same moment
        # the main-thread watchdog gives up. See OpenAI adapter for the race
        # this prevents.
        self._request_timeout: float | None = None

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    # -- Context-overflow detection (Anthropic-specific) -------------------

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Detect Anthropic context-length-exceeded errors."""
        if not isinstance(exc, anthropic.BadRequestError):
            return False
        msg = (str(exc) or "").lower()
        return any(
            needle in msg
            for needle in (
                "too many tokens",
                "context length",
                "context window",
                "prompt is too long",
                "input is too long",
                "maximum context",
                "request too large",
            )
        )

    def _build_request_kwargs(self, messages: list[dict]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._extra_kwargs.get("max_tokens", 8192),
            **self._extra_kwargs,
        }
        if self._system:
            kwargs["system"] = self._system
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        if self._request_timeout is not None:
            # Per-call HTTP timeout overrides the client-level timeout. Use
            # explicit per-phase values so read waits are bounded.
            kwargs["timeout"] = _build_http_timeout(self._request_timeout)
        return kwargs

    def send(self, message) -> LLMResponse:
        """Send a user message (str), tool results (list of ToolResultBlock),
        or drive the existing wire forward (``None``).

        For tool results, ``message`` is a list of ToolResultBlock (canonical).
        The message is committed to the interface before the API call; if the
        tail has unanswered tool_calls, add_user_message raises
        PendingToolCallsError (recovery paths must close first). On API error
        the last user entry is reverted via drop_trailing.

        ``None`` is the "continue from wire" signal — caller has already
        appended whatever needs to land (e.g. a synthesized
        ``(ToolCallBlock, ToolResultBlock)`` pair from
        ``_inject_notification_pair``); no input append happens here.
        """
        # Commit to interface first; enforce_tool_pairing() below handles any
        # earlier-history orphans before the API call.
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # Pre-request hook — safe boundary for kernel-side splices (mid-turn
        # tc_inbox drain). Tail is now user[tool_results] or user[text], so
        # has_pending_tool_calls() returns False; any (call, result) pair
        # the hook splices in will be included in this same API request.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # Build kwargs from current interface state — re-runs inside the
        # overflow-recovery loop so each retry sees the post-trim interface.
        def _do_call():
            self._interface.enforce_tool_pairing()
            candidate_msgs = to_anthropic(self._interface)
            clean_messages = _ensure_alternation(candidate_msgs)
            kwargs = self._build_request_kwargs(clean_messages)
            return self._client.messages.create(**kwargs)

        try:
            raw, total_dropped, rounds = self._run_with_overflow_recovery(_do_call)
        except Exception:
            # Revert the interface on error - drop the last user entry,
            # but only when this call appended one.  ``message is None``
            # means the caller pre-staged the wire and we must not
            # corrupt it on failure.
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # If recovery fired (entries were dropped), inject the molt notice.
        if rounds > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

        # Parse response and add to interface
        response = _parse_response(raw)
        # Record assistant response from raw API object (preserves thinking signatures)
        from lingtai_kernel.llm.interface import TextBlock, ThinkingBlock, ToolCallBlock
        assistant_blocks = []
        for block in raw.content:
            if block.type == "thinking":
                sig = getattr(block, "signature", None)
                pd = {"anthropic": {"signature": sig}} if sig else {}
                assistant_blocks.append(ThinkingBlock(
                    text=getattr(block, "thinking", ""),
                    provider_data=pd,
                ))
            elif block.type == "text":
                assistant_blocks.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                assistant_blocks.append(ToolCallBlock(
                    id=block.id,
                    name=block.name,
                    args=block.input if isinstance(block.input, dict) else {},
                ))
        if assistant_blocks:
            self._interface.add_assistant_message(
                assistant_blocks,
                model=self._model,
                provider="anthropic",
                usage={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "thinking_tokens": response.usage.thinking_tokens,
                },
            )

        return response

    def send_stream(
        self,
        message,
        on_chunk: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Streaming send. User message committed to history only after success."""
        from lingtai_kernel.llm.interface import TextBlock, ThinkingBlock, ToolCallBlock

        # Record user input into interface first.  ``None`` means the
        # caller pre-staged the wire (see send() docstring).
        if message is None:
            pass
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            # list of ToolResultBlock
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # Pre-request hook — see send() above for the contract.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # Build ephemeral Anthropic messages from interface — re-runs inside
        # the overflow-recovery loop so each retry sees the post-trim interface.
        acc = StreamingAccumulator()
        final_message = None

        def _do_stream():
            nonlocal final_message, acc
            acc = StreamingAccumulator()
            self._interface.enforce_tool_pairing()
            candidate_msgs = to_anthropic(self._interface)
            clean_messages = _ensure_alternation(candidate_msgs)
            kwargs = self._build_request_kwargs(clean_messages)

            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block and getattr(block, "type", None) == "tool_use":
                            acc.start_tool(id=block.id, name=block.name)
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            t = getattr(delta, "text", "")
                            if t:
                                acc.add_text(t)
                                if on_chunk:
                                    on_chunk(t)
                        elif dtype == "thinking_delta":
                            t = getattr(delta, "thinking", "")
                            if t:
                                acc.add_thought(t)
                        elif dtype == "input_json_delta":
                            partial = getattr(delta, "partial_json", "")
                            if partial:
                                acc.add_tool_args(partial)
                    elif etype == "content_block_stop":
                        acc.finish_thought()
                        acc.finish_tool()

                final_message = stream.get_final_message()
            return final_message

        try:
            raw_result, total_dropped, rounds = self._run_with_overflow_recovery(_do_stream)
        except Exception:
            # Revert the interface on error — drop the last user entry
            # only if this call appended one.
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # If recovery fired (entries were dropped), inject the molt notice.
        if rounds > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

        # Extract usage from final message (includes cache metrics)
        # Same normalisation as _parse_response — see comment there.
        usage = UsageMetadata()
        if final_message and final_message.usage:
            u = final_message.usage
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            raw_input = getattr(u, "input_tokens", 0) or 0
            usage = UsageMetadata(
                input_tokens=raw_input + cache_read + cache_write,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                thinking_tokens=getattr(u, "thinking_tokens", 0) or 0,
                cached_tokens=cache_read,
            )
            if cache_read or cache_write:
                logger.debug(
                    "Anthropic cache (stream): read=%d write=%d uncached=%d total_input=%d",
                    cache_read,
                    cache_write,
                    raw_input,
                    usage.input_tokens,
                )

        # Record assistant response into interface
        if final_message:
            assistant_blocks: list = []
            for block in final_message.content:
                if block.type == "thinking":
                    thinking_text = getattr(block, "thinking", "")
                    sig = getattr(block, "signature", None)
                    provider_data = {"anthropic": {"signature": sig}} if sig else {}
                    assistant_blocks.append(ThinkingBlock(text=thinking_text, provider_data=provider_data))
                elif block.type == "text":
                    assistant_blocks.append(TextBlock(text=block.text))
                elif block.type == "tool_use":
                    assistant_blocks.append(ToolCallBlock(
                        id=block.id,
                        name=block.name,
                        args=block.input if isinstance(block.input, dict) else {},
                    ))
            if assistant_blocks:
                self._interface.add_assistant_message(
                    assistant_blocks,
                    model=self._model,
                    provider="anthropic",
                    usage={
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "thinking_tokens": usage.thinking_tokens,
                    },
                )

        return acc.finalize(usage=usage, raw=final_message)

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to interface without an API call."""
        if tool_results:
            # tool_results is a list of ToolResultBlock
            self._interface.add_tool_results(tool_results)

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session."""
        self._tools = _build_tools(tools, cache_tools=True) if tools else None
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        self._interface.add_system(
            self._interface.current_system_prompt or "", tools=tool_dicts,
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session."""
        self._system = _build_system_with_cache(system_prompt)
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)

    def update_system_prompt_batches(self, batches: list[str]) -> None:
        """Replace the system prompt using mutation-frequency batches.

        Each batch becomes its own text block in the Anthropic ``system``
        list, with a ``cache_control`` breakpoint between batches (the
        final batch is left un-marked because it is the most volatile
        chunk). The interface-tracked system string is the concatenation
        of all batches — byte-identical to what a single-string caller
        would see — so token accounting stays consistent.
        """
        self._system = _build_system_batches_with_cache(batches)
        joined = "\n\n".join(b for b in batches if b)
        self._interface.add_system(joined, tools=self._interface.current_tools)

    def reset(self) -> None:
        """Create a truly fresh session instance while preserving state.

        Reconstructs a new AnthropicChatSession with a fresh HTTP client
        and copies all attributes onto self, giving a clean connection and
        fresh internal state.
        """
        if self._client_kwargs:
            new_client = anthropic.Anthropic(**self._client_kwargs)
            new_session = AnthropicChatSession(
                client=new_client,
                model=self._model,
                system_prompt=self._system,
                interface=self._interface,
                tools=self._tools,
                tool_choice=self._tool_choice,
                extra_kwargs=self._extra_kwargs,
                client_kwargs=self._client_kwargs,
            )
            self.__dict__.update(new_session.__dict__)

    # -- Context window -------------------------------------------------------

    def context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# AnthropicAdapter
# ---------------------------------------------------------------------------


class AnthropicAdapter(LLMAdapter):
    """Adapter that wraps the ``anthropic`` SDK for Claude models."""


    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        max_rpm: int = 0,
        default_headers: dict | None = None,
    ):
        self._base_url = base_url
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_ms / 1000.0,
            "default_headers": merge_lingtai_identity_headers(default_headers),
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client_kwargs = dict(kwargs)  # store for session reset
        self._client = anthropic.Anthropic(**kwargs)
        self._setup_gate(max_rpm)

    @staticmethod
    def _resolve_thinking_budget(thinking: str) -> int | None:
        """Resolve thinking tier to budget tokens using config."""
        if thinking == "default" or thinking is None:
            return None

        if thinking == "high":
            tier = "high"
        elif thinking == "low":
            tier = "low"
        else:
            return None

        if tier == "high":
            return 16384
        elif tier in ("low", "medium"):
            return 2048
        return None

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
        interaction_id: str | None = None,  # ignored — Gemini-specific
        context_window: int = 0,
    ) -> AnthropicChatSession:
        # Create interface from scratch or from history
        if interface is not None:
            iface = interface
        else:
            iface = ChatInterface()
            tool_dicts = (
                [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]
                if tools else None
            )
            iface.add_system(system_prompt, tools=tool_dicts)

        anthropic_tools = _build_tools(tools, cache_tools=True)
        tool_choice: dict | None = None
        if force_tool_call and anthropic_tools:
            tool_choice = {"type": "any"}

        # JSON schema enforcement via tool-based structured output
        if json_schema is not None and anthropic_tools is None:
            anthropic_tools = []
        if json_schema is not None:
            schema_tool_name = json_schema.get("title", "structured_output")
            anthropic_tools.append(
                {
                    "name": schema_tool_name,
                    "description": "Return the structured response matching the required schema.",
                    "input_schema": json_schema,
                }
            )
            tool_choice = {"type": "tool", "name": schema_tool_name}

        extra_kwargs: dict[str, Any] = {}

        # Thinking/extended thinking
        thinking_budget = self._resolve_thinking_budget(thinking)
        if thinking_budget is not None:
            extra_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            extra_kwargs["max_tokens"] = max(
                thinking_budget * 2, thinking_budget + 8192
            )

        session = AnthropicChatSession(
            client=self._client,
            model=model,
            system_prompt=_build_system_with_cache(system_prompt),
            interface=iface,
            tools=anthropic_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            client_kwargs=self._client_kwargs,
            context_window=context_window,
        )
        return self._wrap_with_gate(session)

    def generate(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        messages: list[dict] = []
        if isinstance(contents, str):
            messages.append({"role": "user", "content": contents})
        elif isinstance(contents, list):
            messages.append({"role": "user", "content": contents})
        else:
            messages.append({"role": "user", "content": str(contents)})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_output_tokens or 8192,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if temperature is not None:
            kwargs["temperature"] = temperature

        if json_schema is not None:
            tools = [
                {
                    "name": json_schema.get("title", "structured_output"),
                    "description": "Return the structured response.",
                    "input_schema": json_schema,
                }
            ]
            kwargs["tools"] = tools
            kwargs["tool_choice"] = {
                "type": "tool",
                "name": json_schema.get("title", "structured_output"),
            }

        raw = self._gated_call(lambda: self._client.messages.create(**kwargs))
        return _parse_response(raw)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock."""
        return ToolResultBlock(
            id=tool_call_id or f"toolu_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        """Check if the exception is an Anthropic rate-limit error."""
        return isinstance(exc, anthropic.RateLimitError)

    # -- Convenience properties ------------------------------------------------

    @property
    def client(self):
        """Escape hatch — the underlying ``anthropic.Anthropic`` client."""
        return self._client
