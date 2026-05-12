"""OpenAI adapter — wraps the ``openai`` SDK for OpenAI and compatible APIs.

Covers: OpenAI, DeepSeek, Together AI, Groq, Fireworks, Ollama, vLLM,
and any other provider exposing an OpenAI-compatible ``/chat/completions``
endpoint.

This is the **only** module that imports the ``openai`` package.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import openai

from lingtai_kernel.logging import get_logger

from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
    UsageMetadata,
)
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai.llm.base import LLMAdapter
from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ThinkingBlock, ToolCallBlock
from ..interface_converters import to_openai, to_responses_input
from lingtai_kernel.llm.streaming import StreamingAccumulator

logger = get_logger()


_CODEX_RESPONSES_TRACE_ENV = "LINGTAI_CODEX_RESPONSES_TRACE"
_CODEX_RESPONSES_TRACE_PATH_ENV = "LINGTAI_CODEX_RESPONSES_TRACE_PATH"
_CODEX_RESPONSES_TRACE_FILE = "codex_responses_trace.jsonl"


def _codex_responses_trace_path() -> Path | None:
    """Return the opt-in Codex Responses stream trace path, if enabled."""
    enabled = os.environ.get(_CODEX_RESPONSES_TRACE_ENV, "")
    if enabled.lower() not in {"1", "true", "yes", "on"}:
        return None

    explicit_path = os.environ.get(_CODEX_RESPONSES_TRACE_PATH_ENV)
    if explicit_path:
        return Path(explicit_path).expanduser()

    base_dir = Path(os.environ.get("LINGTAI_AGENT_DIR", ".")).expanduser()
    return base_dir / "logs" / _CODEX_RESPONSES_TRACE_FILE


def _text_fingerprint(text: str | None) -> dict[str, Any]:
    """Return safe metadata for text-like stream deltas without storing content."""
    if text is None:
        return {"present": False, "length": 0}
    encoded = text.encode("utf-8", errors="replace")
    return {
        "present": True,
        "length": len(text),
        "sha256_12": hashlib.sha256(encoded).hexdigest()[:12],
    }


def _codex_responses_trace_record(
    *,
    event: Any,
    accepted_reasoning: bool,
    thoughts_before: list[str],
    thoughts_after: list[str],
    pending_thought_chars_before: int,
    pending_thought_chars_after: int,
    trace_path: Path | None,
) -> None:
    """Append safe diagnostic metadata for one Codex Responses stream event.

    This intentionally records event/item shapes, text lengths, and hashes only;
    it must not store prompt text, raw response text, raw reasoning text, tool
    result content, or API credentials.
    """
    if trace_path is None:
        return

    item = getattr(event, "item", None)
    response = getattr(event, "response", None)
    usage = getattr(response, "usage", None) if response is not None else None
    input_details = getattr(usage, "input_tokens_details", None) if usage is not None else None
    output_details = getattr(usage, "output_tokens_details", None) if usage is not None else None

    summaries = []
    for summary in getattr(item, "summary", None) or []:
        summaries.append({
            "type": getattr(summary, "type", None),
            "text": _text_fingerprint(getattr(summary, "text", None)),
        })

    record = {
        "ts": time.time(),
        "event_type": getattr(event, "type", None),
        "accepted_reasoning": accepted_reasoning,
        "item": None if item is None else {
            "type": getattr(item, "type", None),
            "id": getattr(item, "id", None),
            "call_id": getattr(item, "call_id", None),
            "name": getattr(item, "name", None),
            "summary": summaries,
        },
        "item_id": getattr(event, "item_id", None),
        "delta": _text_fingerprint(getattr(event, "delta", None)),
        "summary_text": _text_fingerprint(getattr(event, "text", None)),
        "thoughts": {
            "before_count": len(thoughts_before),
            "after_count": len(thoughts_after),
            "before_lengths": [len(t) for t in thoughts_before],
            "after_lengths": [len(t) for t in thoughts_after],
            "pending_chars_before": pending_thought_chars_before,
            "pending_chars_after": pending_thought_chars_after,
        },
    }
    if usage is not None:
        record["usage"] = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cached_tokens": getattr(input_details, "cached_tokens", None),
            "reasoning_tokens": getattr(output_details, "reasoning_tokens", None),
        }

    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - diagnostics must not break send
        logger.warning("Codex Responses trace write failed: %s", exc)


def _build_http_timeout(request_timeout: float | None):
    """Build explicit per-phase HTTP timeout for SDK calls.

    The main-thread watchdog controls total wall-clock time. SDK/httpx
    timeout values are per phase, so cap read waits to keep wedged sockets
    from occupying the worker indefinitely.
    """
    if request_timeout is None:
        return None
    return httpx.Timeout(
        connect=min(float(request_timeout), 30.0),
        read=min(float(request_timeout), 60.0),
        write=min(float(request_timeout), 30.0),
        pool=10.0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to OpenAI tool format."""
    if not schemas:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in schemas
    ]


# Top-level JSON-Schema combinators that the Responses API rejects on
# function-tool `parameters`. Allowed inside individual properties, just
# not as a top-level key. Scrubbing is shallow on purpose — we only
# remove these when they appear at the schema root.
_RESPONSES_DISALLOWED_TOP_LEVEL = ("allOf", "oneOf", "anyOf", "not", "enum")


def _build_responses_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to Responses API tool format.

    Responses uses a flat shape (`type: function`, fields hoisted) instead
    of Chat Completions' nested `{type: function, function: {...}}`. Also
    scrubs top-level JSON-Schema combinators that the Responses API
    rejects on tool parameters; combinators inside individual properties
    are left alone.
    """
    if not schemas:
        return None
    tools = []
    for s in schemas:
        params = dict(s.parameters or {})
        for key in _RESPONSES_DISALLOWED_TOP_LEVEL:
            params.pop(key, None)
        tools.append(
            {
                "type": "function",
                "name": s.name,
                "description": s.description,
                "parameters": params,
            }
        )
    return tools


def _parse_tool_calls(raw_tool_calls) -> list[ToolCall]:
    """Parse OpenAI tool calls into our ToolCall dataclass."""
    if not raw_tool_calls:
        return []
    result = []
    for tc in raw_tool_calls:
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        result.append(
            ToolCall(
                name=tc.function.name,
                args=args,
                id=tc.id,
            )
        )
    return result


def _parse_response(raw) -> LLMResponse:
    """Parse a raw OpenAI ChatCompletion into a provider-agnostic LLMResponse."""
    if not raw.choices:
        return LLMResponse(raw=raw)

    choice = raw.choices[0]
    message = choice.message

    text = message.content or ""
    tool_calls = _parse_tool_calls(message.tool_calls)

    # Extract thinking/reasoning. Field name varies by provider:
    #   OpenAI o-series native        -> message.reasoning_content
    #   OpenRouter (any reasoning mdl) -> message.reasoning
    # We check both so the same parser works across providers. Native
    # providers that don't set either field just produce no thoughts.
    thoughts: list[str] = []
    reasoning = (
        getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
    )
    if reasoning:
        thoughts.append(reasoning)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "prompt_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=raw.usage.prompt_tokens or 0,
            output_tokens=raw.usage.completion_tokens or 0,
            thinking_tokens=getattr(raw.usage, "completion_tokens_details", None)
            and getattr(raw.usage.completion_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


def _add_responses_reasoning_done_text(
    acc: StreamingAccumulator,
    reasoning_item_id: str | None,
    seen_reasoning_summary_items: set[str],
    text: str | None,
) -> None:
    """Add final reasoning-summary text only when no delta was seen.

    Responses ``*.done`` events carry the complete text.  When the stream
    already accepted summary text for the same reasoning item from deltas or a
    done fallback, adding ``done.text`` would duplicate the thought.  If a
    provider emits only the done event, use it as a lossless fallback.
    """
    if text and (not reasoning_item_id or reasoning_item_id not in seen_reasoning_summary_items):
        acc.add_thought(text)
        if reasoning_item_id:
            seen_reasoning_summary_items.add(reasoning_item_id)
    acc.finish_thought()


def _handle_responses_reasoning_event(
    event: Any,
    acc: StreamingAccumulator,
    seen_reasoning_summary_items: set[str],
) -> bool:
    """Feed safe Responses reasoning-summary events into ``acc``.

    We persist summary text, not raw ``response.reasoning_text.*`` events, so
    stateless Codex replay can include documented ``summary_text`` reasoning
    items without storing hidden chain-of-thought.
    """
    event_type = getattr(event, "type", None)
    if event_type == "response.reasoning_summary_text.delta":
        delta = getattr(event, "delta", None)
        if delta:
            acc.add_thought(delta)
            item_id = getattr(event, "item_id", None)
            if item_id:
                seen_reasoning_summary_items.add(item_id)
        return True
    if event_type == "response.reasoning_summary_text.done":
        _add_responses_reasoning_done_text(
            acc,
            getattr(event, "item_id", None),
            seen_reasoning_summary_items,
            getattr(event, "text", None),
        )
        return True
    if event_type == "response.output_item.done" and getattr(event.item, "type", None) == "reasoning":
        summaries = getattr(event.item, "summary", None) or []
        added_fallback = False
        for summary in summaries:
            if getattr(summary, "type", None) == "summary_text":
                item_id = getattr(event.item, "id", None)
                text = getattr(summary, "text", None)
                if text and (not item_id or item_id not in seen_reasoning_summary_items):
                    acc.add_thought(text)
                    if item_id:
                        seen_reasoning_summary_items.add(item_id)
                    added_fallback = True
        if added_fallback or acc.thoughts:
            acc.finish_thought()
        return True
    return False


def _parse_responses_api_response(raw) -> LLMResponse:
    """Parse a raw OpenAI Responses API response into a provider-agnostic LLMResponse."""
    text_parts = []
    tool_calls = []
    thoughts = []

    for item in raw.output or []:
        if item.type == "message":
            for block in item.content or []:
                if block.type == "output_text":
                    text_parts.append(block.text)
        elif item.type == "function_call":
            try:
                args = json.loads(item.arguments) if item.arguments else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=item.name, args=args, id=item.call_id))
        elif item.type == "reasoning":
            for summary in getattr(item, "summary", None) or []:
                if getattr(summary, "type", None) == "summary_text":
                    thoughts.append(summary.text)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "input_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=getattr(raw.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
            thinking_tokens=getattr(raw.usage, "output_tokens_details", None)
            and getattr(raw.usage.output_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# OpenAIChatSession
# ---------------------------------------------------------------------------


class OpenAIChatSession(ChatSession):
    """Client-managed chat session for OpenAI-compatible APIs.

    Uses ChatInterface as the single source of truth.
    """

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        interface: ChatInterface,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        client_kwargs: dict | None = None,
        context_window: int = 0,
    ):
        self._client = client
        self._model = model
        self._interface = interface
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._client_kwargs = client_kwargs or {}
        self._context_window = context_window
        # Per-request HTTP timeout (seconds). Set by send_with_timeout before
        # dispatching the worker so the HTTP client aborts at the same moment
        # the main-thread watchdog gives up. Prevents a race where the worker
        # keeps mutating the shared ChatInterface after AED has already
        # declared a timeout and started recovering.
        self._request_timeout: float | None = None

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def _build_messages(self) -> list[dict]:
        """Return the message list to send to the API.

        Default: the canonical OpenAI serialization of the current
        interface. Subclasses override to mutate or wrap — e.g. the
        DeepSeek session injects ``reasoning_content`` onto assistant
        turns that carry tool calls, which DeepSeek V4 thinking mode
        requires for the round-trip.
        """
        return to_openai(self._interface)

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Detect provider 400-class context-length-exceeded errors.

        Covers OpenAI's canonical ``context_length_exceeded`` code plus the
        loose string-match heuristics used by compatible vendors (DeepSeek,
        Together, Groq, etc.) that often only signal via the message body.
        """
        if not isinstance(exc, openai.BadRequestError):
            return False
        # Canonical OpenAI code on the body's error object.
        code = None
        try:
            body = getattr(exc, "body", None) or {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
        except Exception:
            pass
        if code == "context_length_exceeded":
            return True
        msg = (str(exc) or "").lower()
        return any(
            needle in msg
            for needle in (
                "context length",
                "context_length_exceeded",
                "maximum context",
                "context window",
                "too many tokens",
                "input is too long",
                "prompt is too long",
            )
        )

    # _trim_context_one_round, _run_with_overflow_recovery, and
    # _inject_overflow_notice are inherited from ChatSession (base class).
    # Only _is_context_overflow_error needs to be provider-specific.

    def _pair_orphan_tool_calls(self, messages: list[dict]) -> list[dict]:
        """Final wire-layer guard: synthesize placeholder tool messages for
        any assistant[tool_calls] that are not immediately followed by
        matching role=tool messages. Does NOT mutate the canonical interface
        — synthesis is local to this serialization pass, re-runs from scratch
        next send.

        This catches several known pathologies:
        - An interleaved entry (e.g. a new system prompt appended because
          identity changed) slipping between an assistant[tool_calls] and
          its tool_results in the canonical interface.
        - A cancelled / partial tool batch where some tool_results never
          made it into the interface.
        - Any future drift we haven't anticipated.

        Once the real tool_result arrives in the interface later, the next
        serialization sees it naturally and no synthesis fires — implicit
        dedup without any stateful replace step.

        Each synthesis logs a warning with the tool_call_id and tool name
        so we can track how often this fires and fix the root cause if it
        becomes common.
        """
        patched: list[dict] = []
        for i, msg in enumerate(messages):
            patched.append(msg)
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                continue
            # Look ahead in the ORIGINAL input list for role=tool entries
            # immediately following this assistant turn. Synthesize
            # placeholders for any tool_call_id not covered.
            seen_ids: set[str] = set()
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tcid = messages[j].get("tool_call_id")
                if tcid:
                    seen_ids.add(tcid)
                j += 1
            # For each tool_call without a matching tool message, emit a
            # synthesized placeholder immediately after the assistant turn.
            for tc in tool_calls:
                tcid = tc.get("id")
                name = (tc.get("function") or {}).get("name", "?")
                if not tcid or tcid in seen_ids:
                    continue
                logger.warning(
                    "[wire-guard] synthesizing placeholder tool_result for "
                    "orphan tool_call id=%s name=%s — real result was not "
                    "in context at send time. Investigate if this recurs.",
                    tcid, name,
                )
                patched.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": "[synthesized placeholder — real result was not in context at send time]",
                })
                seen_ids.add(tcid)
        return patched

    def send(self, message) -> LLMResponse:
        """Send a user message (str), tool results (list of dicts), or
        drive the existing wire forward (``None``).

        For tool results, ``message`` is a list of ToolResultBlock instances
        built by :meth:`OpenAIAdapter.make_tool_result_message`.

        ``None`` is the "continue from wire" signal — the caller has
        already appended whatever needs to land (see
        ``base_agent/turn.py:_handle_tc_wake`` for the notification
        path).  No input append happens here; the existing wire is
        sent as-is.

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            # Tool results — list of ToolResultBlock instances
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — kernel-side splice point for mid-turn
        # tc_inbox drains. Wire tail is user[tool_results] or user[text],
        # so any (call, result) pair the hook splices is appended at a
        # safe boundary and rides along on this same API request.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard: synthesize placeholder tool messages for
            # any orphan assistant[tool_calls] that aren't immediately followed
            # by matching role=tool entries. Canonical interface untouched.
            candidate = self._pair_orphan_tool_calls(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        # 3. Make the API call (with auto-recovery on context overflow);
        #    revert interface on any other error.
        def _do_call():
            return self._client.chat.completions.create(**_build_kwargs())

        try:
            raw, total_dropped, rounds = self._run_with_overflow_recovery(_do_call)
        except Exception:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # 3b. If recovery fired (entries were dropped), inject the molt notice.
        if rounds > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

        # 4. Record assistant response into interface
        self._record_assistant_response(raw)

        return _parse_response(raw)

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to interface without an API call."""
        if tool_results:
            self._interface.add_tool_results(tool_results)

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session."""
        self._tools = _build_tools(tools) if tools else None
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        self._interface.add_system(
            self._interface.current_system_prompt or "", tools=tool_dicts,
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session."""
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)

    def reset(self) -> None:
        """Create a truly fresh session instance while preserving state.

        Reconstructs a new OpenAIChatSession with a fresh HTTP client
        and copies all attributes onto self, giving a clean connection and
        fresh internal state.
        """
        if self._client_kwargs:
            new_client = openai.OpenAI(**self._client_kwargs)
            new_session = OpenAIChatSession(
                client=new_client,
                model=self._model,
                interface=self._interface,
                tools=self._tools,
                tool_choice=self._tool_choice,
                extra_kwargs=self._extra_kwargs,
                client_kwargs=self._client_kwargs,
            )
            self.__dict__.update(new_session.__dict__)

    def _record_assistant_response(self, raw) -> None:
        """Parse a raw ChatCompletion and record the assistant response into the interface."""
        choice = raw.choices[0] if raw.choices else None
        blocks: list = []
        if choice and choice.message:
            msg = choice.message
            # Capture reasoning_content (DeepSeek/o-series) or reasoning
            # (OpenRouter) into a ThinkingBlock. Persisting it makes the
            # next request carry real reasoning back to the provider on
            # replay, instead of a constant placeholder. See issue #9.
            reasoning = (
                getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None)
            )
            if reasoning:
                blocks.append(ThinkingBlock(text=reasoning))
            if msg.content:
                blocks.append(TextBlock(text=msg.content))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append(ToolCallBlock(id=tc.id, name=tc.function.name, args=args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        usage_dict = {}
        if raw.usage:
            details = getattr(raw.usage, "completion_tokens_details", None)
            usage_dict = {
                "input_tokens": raw.usage.prompt_tokens or 0,
                "output_tokens": raw.usage.completion_tokens or 0,
                "thinking_tokens": getattr(details, "reasoning_tokens", 0) or 0 if details else 0,
            }
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage=usage_dict,
        )

    @staticmethod
    def _response_to_message(raw) -> dict:
        """Convert an OpenAI ChatCompletion response to a message dict for history."""
        choice = raw.choices[0] if raw.choices else None
        if not choice:
            return {"role": "assistant", "content": ""}
        msg = choice.message
        result: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            result["content"] = msg.content
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        if not msg.content and not msg.tool_calls:
            result["content"] = ""
        return result

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        """Send a streaming request.  Same shape as :meth:`send` —
        ``str`` / ``list`` / ``None`` (continue from wire).

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — see send() above for contract.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard — same as non-streaming send().
            candidate = self._pair_orphan_tool_calls(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                "stream": True,
                "stream_options": {"include_usage": True},
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        acc = StreamingAccumulator()
        usage = UsageMetadata()

        # Streaming overflow-recovery: most providers raise the 400 either
        # when ``create()`` returns or on the first iteration of the stream
        # — before any content has been emitted to ``on_chunk``. We open the
        # stream and pull the first chunk inside the recovery wrapper; once
        # that succeeds, we hand off to the regular streaming loop.
        def _open_and_first_chunk():
            stream = self._client.chat.completions.create(**_build_kwargs())
            it = iter(stream)
            try:
                first = next(it)
            except StopIteration:
                first = None
            return stream, it, first

        # 3. Stream; revert interface on error
        try:
            (stream, it, first_chunk), total_dropped, rounds = (
                self._run_with_overflow_recovery(_open_and_first_chunk)
            )
            if rounds > 0:
                self._inject_overflow_notice(
                    total_dropped=total_dropped, rounds=rounds,
                )
            # Re-stitch: first chunk + remaining iterator.
            def _chunks():
                if first_chunk is not None:
                    yield first_chunk
                for c in it:
                    yield c
            for chunk in _chunks():
                if not chunk.choices:
                    if chunk.usage:
                        cached = getattr(chunk.usage, "prompt_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        usage = UsageMetadata(
                            input_tokens=chunk.usage.prompt_tokens or 0,
                            output_tokens=chunk.usage.completion_tokens or 0,
                            thinking_tokens=(
                                getattr(
                                    getattr(chunk.usage, "completion_tokens_details", None),
                                    "reasoning_tokens",
                                    0,
                                )
                                or 0
                            ),
                            cached_tokens=cached_tokens,
                        )
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    acc.add_text(delta.content)
                    if on_chunk:
                        on_chunk(delta.content)
                # OpenRouter (and OpenAI o-series under some SDKs) streams
                # reasoning text deltas under `reasoning` / `reasoning_content`.
                # Capture into the thoughts channel, never into visible text.
                reasoning_delta = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "reasoning_content", None)
                )
                if reasoning_delta:
                    acc.add_thought(reasoning_delta)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc.add_tool_delta(
                            tc.index,
                            id=tc.id,
                            name=(tc.function.name if tc.function else None),
                            args_delta=(tc.function.arguments if tc.function else None),
                        )
        except Exception:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # 4. Finalize
        acc.finish_all_tools()
        result = acc.finalize(usage=usage)

        # 5. Record assistant response into interface
        blocks: list = []
        # Persist captured reasoning as a ThinkingBlock so the next request
        # can replay it via reasoning_content (see issue #9).
        if result.thoughts:
            joined = "\n".join(t for t in result.thoughts if t)
            if joined:
                blocks.append(ThinkingBlock(text=joined))
        if result.text:
            blocks.append(TextBlock(text=result.text))
        for tc in result.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
            },
        )

        return result

    # -- Context compaction ---------------------------------------------------

    def context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# OpenAIResponsesSession
# ---------------------------------------------------------------------------


class OpenAIResponsesSession(ChatSession):
    """Session backed by OpenAI's Responses API with server-side state."""

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        instructions: str,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        previous_response_id: str | None = None,
        compact_threshold: int | None = None,
        interface: ChatInterface | None = None,
    ):
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._response_id: str | None = previous_response_id
        self._compact_threshold = compact_threshold
        self._interface = interface or ChatInterface()

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def _convert_input(self, message) -> list[dict]:
        """Convert messages to Responses API input format.

        ``None`` yields ``[]`` — caller wants the existing
        ``previous_response_id`` chain to continue with no new input.
        """
        if message is None:
            return []
        if isinstance(message, str):
            return [{"role": "user", "content": message}]
        elif isinstance(message, dict):
            return [message]
        elif isinstance(message, list):
            items = []
            for item in message:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                ):
                    items.append(item)
                elif isinstance(item, dict) and item.get("role") == "tool":
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item["tool_call_id"],
                            "output": item["content"],
                        }
                    )
                else:
                    items.append(item)
            return items
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

    def send(self, message) -> LLMResponse:
        """Send a user message (str) or tool results (list of dicts)."""
        # Pre-request hook — fired for the kernel-side drain. NOTE: this
        # session uses server-side state (previous_response_id) and does
        # NOT commit message content to the canonical ChatInterface, so a
        # pair the hook splices is only visible to the LLM on the *next*
        # turn (when the interface re-syncs). This is acceptable because
        # the agent's local view is updated immediately for persistence
        # and inspection. Most agents using this session use it via Codex
        # OAuth (CodexResponsesSession), which DOES replay the full
        # interface and gets same-turn delivery.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        input_items = self._convert_input(message)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            **self._extra_kwargs,
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        if self._response_id:
            kwargs["previous_response_id"] = self._response_id
        if self._compact_threshold:
            kwargs["context_management"] = [
                {"type": "compaction", "compact_threshold": self._compact_threshold}
            ]

        raw = self._client.responses.create(**kwargs)
        self._response_id = raw.id
        return _parse_responses_api_response(raw)

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        """Send a streaming request."""
        # Pre-request hook — see send() above for contract + caveat.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        input_items = self._convert_input(message)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
            **self._extra_kwargs,
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        if self._response_id:
            kwargs["previous_response_id"] = self._response_id
        if self._compact_threshold:
            kwargs["context_management"] = [
                {"type": "compaction", "compact_threshold": self._compact_threshold}
            ]

        acc = StreamingAccumulator()
        response_id = None
        usage = UsageMetadata()
        seen_reasoning_summary_items: set[str] = set()

        stream = self._client.responses.create(**kwargs)
        for event in stream:
            if _handle_responses_reasoning_event(event, acc, seen_reasoning_summary_items):
                continue
            if event.type == "response.output_text.delta":
                acc.add_text(event.delta)
                if on_chunk:
                    on_chunk(event.delta)
            elif event.type == "response.function_call_arguments.delta":
                acc.add_tool_args(event.delta)
            elif event.type == "response.output_item.added":
                if getattr(event.item, "type", None) == "function_call":
                    acc.start_tool(id=event.item.call_id, name=event.item.name)
            elif event.type == "response.output_item.done":
                if getattr(event.item, "type", None) == "function_call":
                    acc.finish_tool()
            elif event.type == "response.completed":
                response_id = event.response.id
                if event.response.usage:
                    cached = getattr(event.response.usage, "input_tokens_details", None)
                    cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                    usage = UsageMetadata(
                        input_tokens=getattr(event.response.usage, "input_tokens", 0)
                        or 0,
                        output_tokens=getattr(event.response.usage, "output_tokens", 0)
                        or 0,
                        thinking_tokens=getattr(
                            event.response.usage, "output_tokens_details", None
                        )
                        and getattr(
                            event.response.usage.output_tokens_details,
                            "reasoning_tokens",
                            0,
                        )
                        or 0,
                        cached_tokens=cached_tokens,
                    )

        self._response_id = response_id
        return acc.finalize(usage=usage)

    def get_history(self) -> list[dict]:
        """Return minimal state for session persistence (server-side)."""
        return [{"_response_id": self._response_id}]

    @property
    def session_resume_id(self) -> str | None:
        """Return the response ID for session resumption."""
        return self._response_id


# ---------------------------------------------------------------------------
# OpenAIAdapter
# ---------------------------------------------------------------------------


class OpenAIAdapter(LLMAdapter):
    """Adapter that wraps the ``openai`` SDK for OpenAI and compatible APIs."""

    # Session class for the Chat Completions path. Subclasses override
    # this to inject provider-specific behavior (e.g. DeepSeek preserves
    # ``reasoning_content`` on tool-call turns for thinking-mode replay).
    # Responses-API sessions use OpenAIResponsesSession unconditionally
    # since that path is OpenAI-only.
    _session_class: type = OpenAIChatSession

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        use_responses: bool = False,
        force_responses: bool = False,
        max_rpm: int = 0,
        default_headers: dict | None = None,
    ):
        self.base_url = base_url
        self._use_responses = use_responses
        self._force_responses = force_responses
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        kwargs["timeout"] = timeout_ms / 1000.0  # openai SDK uses seconds
        if default_headers:
            kwargs["default_headers"] = dict(default_headers)
        self._client_kwargs = dict(kwargs)  # store for session reset
        self._client = openai.OpenAI(**kwargs)
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
        interaction_id: str | None = None,  # ignored — Gemini-specific
        context_window: int = 0,
    ) -> ChatSession:
        # Create interface if not provided
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=tool_dicts)

        use_responses = self._use_responses

        # Only use Responses API for actual OpenAI (not compatible providers)
        if use_responses and (not self.base_url or self._force_responses):
            session = self._create_responses_session(
                model,
                system_prompt,
                tools,
                json_schema,
                force_tool_call,
                interface,
                thinking,
            )
        else:
            # Fallback: Chat Completions for compatible providers
            session = self._create_completions_session(
                model, system_prompt, tools, json_schema, force_tool_call, interface, thinking,
                context_window=context_window,
            )
        return self._wrap_with_gate(session)

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
    ) -> OpenAIResponsesSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        if thinking != "default":
            # Responses API takes `reasoning: { effort: ... }`, not the
            # Chat Completions SDK's flat `reasoning_effort`. Sending the
            # wrong shape silently drops the field on the OpenAI Responses
            # endpoint and 400s on Codex's `/backend-api/codex/responses`.
            extra_kwargs["reasoning"] = {"effort": "high" if thinking == "high" else "low"}

        # Get compact threshold from config
        compact_threshold = None
        try:
            from config import get as config_get

            compact_threshold = config_get("providers.openai.compact_threshold", 100000)
        except ImportError:
            pass

        return OpenAIResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=compact_threshold,
            interface=interface,
        )

    def _create_completions_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> OpenAIChatSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        # Extra kwargs for the completions call
        extra_kwargs: dict[str, Any] = {}

        # JSON schema enforcement (OpenAI Structured Outputs)
        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Reasoning effort for o-series models
        if thinking != "default":
            extra_kwargs["reasoning_effort"] = "high" if thinking == "high" else "low"

        # Subclass-provided extra_body (e.g. OpenRouter's reasoning include).
        # Merge rather than overwrite so callers adding their own extra_body
        # via extra_kwargs aren't clobbered.
        sub_extra_body = self._adapter_extra_body()
        if sub_extra_body:
            existing = extra_kwargs.get("extra_body") or {}
            extra_kwargs["extra_body"] = {**sub_extra_body, **existing}

        return self._session_class(
            client=self._client,
            model=model,
            interface=interface,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            client_kwargs=self._client_kwargs,
            context_window=context_window,
        )

    def _adapter_extra_body(self) -> dict:
        """Return extra_body JSON fields to include on every request.

        Default is empty. Subclasses override to inject provider-specific
        kwargs (e.g. OpenRouter needs `reasoning: {include: true}` to
        surface reasoning text on reasoning-capable models).
        """
        return {}

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
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # contents can be a string or a list of content blocks
        if isinstance(contents, str):
            messages.append({"role": "user", "content": contents})
        elif isinstance(contents, list):
            messages.append({"role": "user", "content": contents})
        else:
            messages.append({"role": "user", "content": str(contents)})

        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens

        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        raw = self._gated_call(lambda: self._client.chat.completions.create(**kwargs))
        return _parse_response(raw)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock."""
        return ToolResultBlock(
            id=tool_call_id or f"call_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        """Check if the exception is an OpenAI rate-limit error."""
        return isinstance(exc, openai.RateLimitError)

    # -- Convenience properties ------------------------------------------------

    @property
    def client(self):
        """Escape hatch — the underlying ``openai.OpenAI`` client."""
        return self._client


# ---------------------------------------------------------------------------
# CodexResponsesSession — stateless variant for ChatGPT-OAuth backend
# ---------------------------------------------------------------------------


class CodexResponsesSession(OpenAIResponsesSession):
    """Stateless Responses session for Codex's `/backend-api/codex/responses`.

    Differences from the parent:
      * `previous_response_id` is never sent — Codex's backend doesn't
        persist turns server-side. The full input must be carried each
        request by the caller (interface layer accumulates messages).
      * `store=False` is forced — same reason.
      * Streaming is forced (`stream=True` on send/send_stream alike) —
        non-streaming Codex requests return data the SDK can't unmarshal.
    """

    def send(self, message) -> LLMResponse:
        # Force the streaming path — Codex doesn't serve non-streaming JSON.
        return self.send_stream(message, on_chunk=None)

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        # Codex's backend is stateless — no previous_response_id, so the full
        # conversation must ride along on every request. Record the new
        # message into the canonical interface, then build wire input from
        # the entire interface (mirrors OpenAIChatSession.send's contract).
        # ``message is None`` is the "continue from wire" signal — the
        # caller pre-staged the canonical interface (e.g. notification
        # sync), so we just replay it without any additional append.
        if message is None:
            pass
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            # ToolResultBlock list, the canonical kernel shape coming back
            # from ToolExecutor via _make_tool_result_fn.
            if message and all(isinstance(b, ToolResultBlock) for b in message):
                self._interface.add_tool_results(message)
            else:
                # Pre-built wire dicts (legacy / tests). Fall back to the
                # parent's converter so behavior matches what callers
                # passing dicts expect.
                pass
        elif isinstance(message, dict):
            pass
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # Pre-request hook — kernel-side splice point. The Codex stateless
        # path replays the full canonical interface on every request, so
        # any (call, result) pair the hook splices in is included in the
        # current request's input items. See OpenAIChatSession.send for
        # the contract.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        try:
            self._interface.enforce_tool_pairing()
            input_items = to_responses_input(self._interface)
            # If the caller passed pre-built wire dicts (not str / not
            # ToolResultBlock list), append them after the replay so the
            # behavior is additive rather than dropped.
            if isinstance(message, dict):
                input_items.append(message)
            elif isinstance(message, list) and not (
                message and all(isinstance(b, ToolResultBlock) for b in message)
            ):
                for item in self._convert_input(message):
                    input_items.append(item)

            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": input_items,
                "stream": True,
                "store": False,
                **self._extra_kwargs,
            }
            if self._instructions:
                kwargs["instructions"] = self._instructions
            if self._tools:
                kwargs["tools"] = self._tools
                if self._tool_choice:
                    kwargs["tool_choice"] = self._tool_choice
            # Deliberately omit previous_response_id — backend is stateless.
            if self._compact_threshold:
                kwargs["context_management"] = [
                    {"type": "compaction", "compact_threshold": self._compact_threshold}
                ]

            acc = StreamingAccumulator()
            response_id = None
            usage = UsageMetadata()
            seen_reasoning_summary_items: set[str] = set()
            trace_path = _codex_responses_trace_path()

            stream = self._client.responses.create(**kwargs)
            for event in stream:
                thoughts_before = acc.thoughts
                pending_thought_chars_before = len("".join(acc._thought_parts))
                accepted_reasoning = _handle_responses_reasoning_event(
                    event,
                    acc,
                    seen_reasoning_summary_items,
                )
                _codex_responses_trace_record(
                    event=event,
                    accepted_reasoning=accepted_reasoning,
                    thoughts_before=thoughts_before,
                    thoughts_after=acc.thoughts,
                    pending_thought_chars_before=pending_thought_chars_before,
                    pending_thought_chars_after=len("".join(acc._thought_parts)),
                    trace_path=trace_path,
                )
                if accepted_reasoning:
                    continue
                if event.type == "response.output_text.delta":
                    acc.add_text(event.delta)
                    if on_chunk:
                        on_chunk(event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    acc.add_tool_args(event.delta)
                elif event.type == "response.output_item.added":
                    if getattr(event.item, "type", None) == "function_call":
                        acc.start_tool(id=event.item.call_id, name=event.item.name)
                elif event.type == "response.output_item.done":
                    if getattr(event.item, "type", None) == "function_call":
                        acc.finish_tool()
                elif event.type == "response.completed":
                    response_id = event.response.id
                    if event.response.usage:
                        cached = getattr(event.response.usage, "input_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        usage = UsageMetadata(
                            input_tokens=getattr(event.response.usage, "input_tokens", 0) or 0,
                            output_tokens=getattr(event.response.usage, "output_tokens", 0) or 0,
                            thinking_tokens=getattr(
                                event.response.usage, "output_tokens_details", None
                            )
                            and getattr(
                                event.response.usage.output_tokens_details,
                                "reasoning_tokens",
                                0,
                            )
                            or 0,
                            cached_tokens=cached_tokens,
                        )
        except Exception:
            # Revert the trailing user entry we just added so the next retry
            # doesn't double-record it. Mirrors OpenAIChatSession.send's
            # error path. ToolResultBlock entries also revert — the executor
            # will re-supply them when AED rebuilds the loop.
            self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        result = acc.finalize(usage=usage)

        # Record assistant response into the interface so it rides along on
        # the next request. Without this, the stateless backend would never
        # see the assistant's own prior turns.
        blocks: list = []
        if result.thoughts:
            joined = "\n".join(t for t in result.thoughts if t)
            if joined:
                blocks.append(ThinkingBlock(text=joined))
        if result.text:
            blocks.append(TextBlock(text=result.text))
        for tc in result.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="codex",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
            },
        )

        # Stateless: don't persist the response_id beyond this single turn.
        # Stored only as a transient debug aid; never threaded into the next
        # request.
        self._response_id = response_id
        return result


class CodexOpenAIAdapter(OpenAIAdapter):
    """OpenAIAdapter variant that builds CodexResponsesSession instead of the
    standard server-stateful OpenAIResponsesSession.

    Use this with `provider=codex` only. Always set `use_responses=True,
    force_responses=True, base_url='https://chatgpt.com/backend-api/codex'`.
    """

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
    ) -> CodexResponsesSession:
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        if thinking != "default":
            extra_kwargs["reasoning"] = {"effort": "high" if thinking == "high" else "low"}

        # Codex's backend doesn't accept context_management compaction —
        # leave compact_threshold unset.
        return CodexResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=None,
            interface=interface,
        )
