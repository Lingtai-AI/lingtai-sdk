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
from urllib.parse import urlsplit

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


# Sentinel for "auto-derive a default prompt_cache_key". The adapter accepts
# ``prompt_cache_key=None`` to mean "compute the stable default", an explicit
# string to override it, and ``False`` to disable cache-key emission entirely.
_AUTO_PROMPT_CACHE_KEY = object()


# Codex REST cache-affinity headers (issue #378). The official Codex client
# sends ``session_id`` / ``thread_id`` headers on its
# ``/backend-api/codex/responses`` calls; a probe showed they materially
# improve prompt-cache affinity for repeated full-history replays. The REST
# endpoint does NOT accept ``previous_response_id`` (``Unsupported
# parameter``), so stable headers — not delta chaining — are the near-term
# cache-affinity lever.
#
# The header keys MUST be the underscore names ``session_id`` / ``thread_id``
# exactly as the Codex backend/CLI sends them. Do NOT "normalise" them into
# hyphenated, HTTP-looking ``session-id`` / ``thread-id``: the Codex backend
# matches the literal underscore key, so a hyphenated spelling silently loses
# cache affinity — every request fragments to a cold slot, exploding cache
# misses and token cost. (This comment block uses the spelling the code must
# emit; keep prose and code in sync.)
#
# For the normal/root main Codex session, the three cache-affinity values are
# byte-identical and stable for the lifetime of the agent's durable identity:
#
#     session_id == thread_id == prompt_cache_key == <8-char agent-path hash>
#
# The shared value is a deterministic 8-character lowercase-hex digest of the
# agent's durable identity anchor (the resolved ``init.json`` / agent path).
# Ordinary LLM calls, ``api_call_id`` rotation, refresh/rebuild, molt, and
# clear (same agent path) all leave it unchanged — only a different agent path
# changes it. We do NOT use the latest ``api_call_id``, molt time, or generated
# UUIDs for these defaults.
#
# IMPORTANT — these identifiers MUST be per-agent. The value anchors on the
# agent's durable identity (the resolved ``init.json`` path). It must NOT be
# derived from a global, model-only anchor (e.g.
# ``prompt_cache_key=lingtai-codex:{model}:v1``): every agent on the same model
# shares that string, which would collapse all of them onto one
# session/thread and is exactly the wrong behavior.
#
# The adapter layer has no per-agent identity of its own, so the host wiring
# (``lingtai/llm/service.py:build_provider_defaults_from_manifest_llm``) passes
# the agent path down by default as ``codex_session_anchor``: a normal Codex
# agent gets a stable per-agent hash used identically for all three values. The
# constructor kwargs below are the seam those defaults flow through (and an
# internal override / testing escape hatch).


def _codex_session_id(anchor: str) -> str:
    """Derive the stable 8-char Codex cache-affinity id from ``anchor``.

    ``anchor`` MUST be a per-agent identity string (e.g. the agent's resolved
    ``init.json`` / agent-dir path), NOT a global model-only key. The result is
    a deterministic 8-character lowercase-hex sha256 prefix: the same anchor
    always yields the same id; distinct anchors differ. The same value is used
    byte-identically for ``session_id``, ``thread_id``, and the default
    ``prompt_cache_key`` on the normal/root path.
    """
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:8]

# Codex cache-affinity identity is a SINGLE STABLE per-agent value: a pure
# deterministic hash of the agent's durable identity anchor (the resolved
# ``init.json`` / agent-dir path), used byte-identically for ``session_id`` /
# ``thread_id`` / ``prompt_cache_key`` and NEVER changed for the life of the
# agent's identity. See :func:`_codex_session_id`.
#
# Historically there were two churn mechanisms here, both REMOVED because they
# were empirically counterproductive (the backend routes the prompt cache to a
# sticky-warm replica off a STABLE session id; changing the id re-rolls the
# routing and discards the warm slot):
#   - epoch-stamping the id on every adapter (re)build, and
#   - a "stalled-cache" rotation that changed the id when the cache rate dipped.
# Both are gone. The id depends only on the agent path — no time, no epoch, no
# rotation. The ``codex-cache-key`` request header (first chars of the prompt
# key) was part of the same churn apparatus and is no longer sent; Codex CLI
# never sends it either.


# Honest client-identity headers for the Codex ``/backend-api/codex/responses``
# path. The official Codex CLI sends ``originator`` + a Codex ``User-Agent`` to
# identify itself; LingTai is NOT the Codex CLI, so we identify HONESTLY as
# LingTai rather than impersonating ``codex_exec`` (impersonating a first-party
# client risks an OpenAI ToS violation). These are pure identity hints — they do
# NOT affect prompt caching (verified empirically: cache rate is identical with
# or without them; only the stable ``session_id``/``thread_id`` header routes the
# cache slot). The point is account hygiene: hitting the endpoint with a
# ChatGPT-OAuth token and no recognizable client identity is exactly the traffic
# anomaly/abuse detection flags. Mirrors the existing honest-User-Agent policy
# for Kimi in ``LLMService._default_headers_for``. See issue #436.
_CODEX_ORIGINATOR = "lingtai"


def _lingtai_user_agent() -> str:
    """Return an honest LingTai ``User-Agent`` string, e.g. ``LingTai/0.12.4``.

    Falls back to an unversioned token if the installed package version cannot
    be resolved (e.g. running from a source tree without metadata).
    """
    try:
        from importlib.metadata import version

        return f"LingTai/{version('lingtai')}"
    except Exception:
        return "LingTai"


def _codex_identity_headers() -> dict[str, str]:
    """Honest client-identity headers sent on every Codex request (see #436)."""
    return {"originator": _CODEX_ORIGINATOR, "User-Agent": _lingtai_user_agent()}


def _base_url_namespace(base_url: str | None) -> str:
    """Return a stable namespace token for an OpenAI-compatible ``base_url``.

    Prefers the URL host (e.g. ``api.vendor.example``) so distinct endpoints
    never share a prompt-cache namespace. Falls back to a short hash of the
    full URL when no host can be parsed, so the result is always deterministic
    and non-empty.
    """
    if not base_url:
        return ""
    host = urlsplit(base_url).hostname or ""
    if host:
        return host
    return "h" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:12]


def _validate_compact_threshold(value: int | None) -> int | None:
    """Normalize the OpenAI Responses auto-compaction threshold.

    ``None`` intentionally disables Responses ``context_management``.  Any
    concrete value must be a positive integer; reject bool explicitly because
    it is an ``int`` subclass in Python but not a valid token threshold.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("compact_threshold must be a positive int or None")
    if value <= 0:
        raise ValueError("compact_threshold must be > 0 or None")
    return value


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


# Top-level JSON-Schema combinators the Responses API rejects on a
# function-tool `parameters` root. `enum` is only disallowed at the root —
# it is valid (and common) inside individual properties.
_RESPONSES_DISALLOWED_TOP_LEVEL = ("allOf", "oneOf", "anyOf", "not", "enum")


# Keys that, if present on a schema node, already establish its kind so it
# does not need a synthesized `type`. The Codex backend rejects a property
# schema that carries none of these (a "typeless" property, e.g. one with
# only a `description`), so such nodes are coerced to `{"type": "string"}`.
_SCHEMA_KIND_KEYS = (
    "type", "enum", "const", "$ref",
    "anyOf", "allOf", "oneOf", "not",
    "properties", "items",
)


def _scrub_responses_schema(node: Any) -> Any:
    """Recursively normalize a JSON schema for the Codex Responses backend.

    Empirically, `/backend-api/codex/responses` rejects three constructs even
    when nested inside a property, returning an opaque `server_error`:

      1. `oneOf` / `not` combinators  -> `oneOf` rewritten to the accepted
         `anyOf`; `not` dropped (no accepted equivalent).
      2. Typeless property schemas    -> a node with a `description` but no
         type-establishing key (see `_SCHEMA_KIND_KEYS`) gets `type:
         "string"`. This covers the nested `secondary.args.*` fields LingTai
         emits with only a description.
      3. `{"type": "object"}` with no `properties` key (a free-form object,
         e.g. `daemon`'s `tasks[].backend_options`) -> an empty
         `properties: {}` is added. An empty `properties` map is accepted.

    `enum`/`anyOf`/`allOf` are left untouched (the backend accepts them
    nested). Walks dicts and lists so all fixes apply at any depth.
    """
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            if key == "oneOf":
                out["anyOf"] = [_scrub_responses_schema(v) for v in value]
            elif key == "not":
                continue  # no accepted equivalent — drop it
            else:
                out[key] = _scrub_responses_schema(value)
        # Coerce typeless property schemas: a node describing a value (has a
        # description) but lacking any kind key. Skip bare containers like an
        # empty {} or {"required": [...]} that aren't value descriptors.
        if "description" in out and not any(k in out for k in _SCHEMA_KIND_KEYS):
            out["type"] = "string"
        # A typed object with no `properties` is rejected; give it an empty map.
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out
    if isinstance(node, list):
        return [_scrub_responses_schema(v) for v in node]
    return node


def _build_responses_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to Responses API tool format.

    Responses uses a flat shape (`type: function`, fields hoisted) instead
    of Chat Completions' nested `{type: function, function: {...}}`. Scrubs
    top-level combinators the Responses API rejects at the parameters root,
    then runs `_scrub_responses_schema` to fix the constructs the Codex
    backend rejects even nested in a property (`oneOf`/`not` combinators and
    typeless property schemas).
    """
    if not schemas:
        return None
    tools = []
    for s in schemas:
        params = dict(s.parameters or {})
        for key in _RESPONSES_DISALLOWED_TOP_LEVEL:
            params.pop(key, None)
        params = _scrub_responses_schema(params)
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
        prompt_cache_key: str | None = None,
    ):
        self._client = client
        self._model = model
        self._interface = interface
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._client_kwargs = client_kwargs or {}
        self._context_window = context_window
        # Stable ``prompt_cache_key`` for OpenAI-compatible Chat Completions
        # cross-request prompt caching. Sent only when set; ``None`` leaves it
        # off (so a directly-constructed session is opt-in). The adapter
        # supplies the namespaced default — see ``_default_prompt_cache_key``.
        # ``prompt_cache_retention`` is deliberately never sent (Codex rejects
        # it; we keep the whole OpenAI-compatible surface uniform).
        self._prompt_cache_key = prompt_cache_key
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
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
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
                context_window=self._context_window,
                prompt_cache_key=self._prompt_cache_key,
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
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
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
        prompt_cache_key: str | None = None,
    ):
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._response_id: str | None = previous_response_id
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        self._interface = interface or ChatInterface()
        # Optional OpenAI Responses ``prompt_cache_key`` — opts the request
        # into cross-request prompt caching keyed by a stable string. Sent
        # only when set; ``None`` leaves it off (default OpenAI behavior).
        # Note: ``prompt_cache_retention`` is deliberately never sent — the
        # Codex backend rejects it (``Unsupported parameter``).
        self._prompt_cache_key = prompt_cache_key

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
        if self._prompt_cache_key:
            kwargs["prompt_cache_key"] = self._prompt_cache_key

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
        if self._prompt_cache_key:
            kwargs["prompt_cache_key"] = self._prompt_cache_key

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
        compact_threshold: int | None = 100_000,
        prompt_cache_key: str | bool | None = None,
    ):
        self.base_url = base_url
        self._use_responses = use_responses
        self._force_responses = force_responses
        # Prompt-cache-key policy for this adapter's OpenAI-compatible sessions:
        #   None  -> auto-derive a stable, namespaced default per model
        #   str   -> use this exact key for every session (override)
        #   False -> disable; never send prompt_cache_key
        # Default-on is intentional: every OpenAI-compatible endpoint LingTai
        # talks to accepted the field in the compat probe, and a stable key is
        # what lets successive agent turns hit the cross-request prompt cache.
        if prompt_cache_key is False:
            self._prompt_cache_key_policy: object = False
        elif prompt_cache_key is None:
            self._prompt_cache_key_policy = _AUTO_PROMPT_CACHE_KEY
        else:
            self._prompt_cache_key_policy = prompt_cache_key
        # Responses-API auto-compaction threshold (input tokens). The host
        # injects its resolved config value via the adapter factory
        # (lingtai/llm/_register.py:_openai reads provider defaults); when
        # unset we fall back to the intended 100k default. ``None`` disables
        # compaction entirely. Config is injected at construction here, never
        # read from a global module — see lingtai_kernel.config's contract.
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        kwargs["timeout"] = timeout_ms / 1000.0  # openai SDK uses seconds
        if default_headers:
            kwargs["default_headers"] = dict(default_headers)
        self._client_kwargs = dict(kwargs)  # store for session reset
        self._client = openai.OpenAI(**kwargs)
        self._setup_gate(max_rpm)

    # -- Prompt cache key ------------------------------------------------------

    def _default_prompt_cache_key(self, model: str) -> str:
        """Return the auto-derived, namespaced default prompt_cache_key.

        Namespacing keeps incompatible endpoints from sharing a cache slot:
          * official OpenAI (no base_url) -> ``lingtai-openai:{model}:v1``
          * custom/compatible base_url    -> ``lingtai-openai-compat:{host}:{model}:v1``

        Subclasses with a fixed provider identity (DeepSeek, Zhipu, MiMo,
        Codex) override this to use a clean provider namespace instead of the
        base_url host.
        """
        if not self.base_url:
            return f"lingtai-openai:{model}:v1"
        return f"lingtai-openai-compat:{_base_url_namespace(self.base_url)}:{model}:v1"

    def _resolve_prompt_cache_key(self, model: str) -> str | None:
        """Resolve the effective prompt_cache_key for a session on ``model``.

        Honors the adapter's policy: ``False`` disables (returns ``None``), an
        explicit string overrides, and the auto sentinel derives the default.
        """
        policy = self._prompt_cache_key_policy
        if policy is False:
            return None
        if policy is _AUTO_PROMPT_CACHE_KEY:
            return self._default_prompt_cache_key(model)
        return policy  # explicit override string

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

        return OpenAIResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=self._compact_threshold,
            interface=interface,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
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
            prompt_cache_key=self._resolve_prompt_cache_key(model),
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
      * Optional stable ``session_id`` / ``thread_id`` request headers are
        sent for REST prompt-cache affinity (issue #378). They are HTTP
        headers (``extra_headers``), not request-body fields, and are
        independent of ``prompt_cache_key`` (both may be sent together). The
        keys are underscored (``session_id`` / ``thread_id``) to match the
        Codex backend literally — a hyphenated spelling loses cache affinity.
    """

    def __init__(
        self,
        *args,
        session_id: str | None = None,
        thread_id: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # Codex REST cache-affinity identity: ONE stable per-agent value used
        # byte-identically for ``prompt_cache_key`` / ``session_id`` /
        # ``thread_id`` on EVERY request, and NEVER changed for the life of the
        # session. There is no epoch-stamping and no rotation — the id is a pure
        # deterministic hash of the agent path (resolved upstream by the adapter
        # and passed in here). Priority for the single value: ``prompt_cache_key``
        # (explicit request-body cache key) > ``session_id`` > ``thread_id``.
        #
        # The header carve-out (issue #378): ``session_id`` / ``thread_id``
        # headers route the backend cache slot and MUST be per-agent. The
        # model-only fallback ``prompt_cache_key`` form
        # (``lingtai-codex:{model}:v1``) is shared by every agent on a model, so
        # it is NEVER promoted to headers (that would collapse all agents onto one
        # slot). Headers are emitted only when an explicit ``session_id`` /
        # ``thread_id`` was supplied (the per-agent path or a direct-construction
        # test); a cache-key-only construction (bare/no-anchor) keeps its body
        # ``prompt_cache_key`` and sends NO headers.
        self._current_id = self._prompt_cache_key or session_id or thread_id
        self._prompt_cache_key = self._current_id
        self._has_header_identity = bool(session_id or thread_id)
        if self._has_header_identity:
            self._session_id = self._current_id
            self._thread_id = self._current_id
        else:
            self._session_id = None
            self._thread_id = None

    def _cache_affinity_headers(self) -> dict[str, str]:
        """Return the stable ``session_id`` / ``thread_id`` headers, if any.

        The header names use UNDERSCORES (``session_id`` / ``thread_id``) to match
        what the official Codex CLI sends on its
        ``/backend-api/codex/responses`` calls (verified by capturing real Codex
        CLI traffic, 2026-06). This spelling is load-bearing: the Codex backend
        matches the literal underscore key, so emitting hyphenated
        ``session-id`` / ``thread-id`` would silently lose cache affinity and
        fragment every request onto a cold slot (cache/cost explosion). Do NOT
        rename these to HTTP-looking hyphenated forms. The backend routes the
        prompt-cache slot to a sticky-warm replica off a STABLE session id; we
        send one fixed per-agent value for the life of the session and never
        change it.
        """
        headers: dict[str, str] = {}
        if self._session_id:
            headers["session_id"] = self._session_id
        if self._thread_id:
            headers["thread_id"] = self._thread_id
        return headers

    def _effective_affinity(self) -> tuple[str | None, dict[str, str]]:
        """Resolve this request's (prompt_cache_key, headers) pair.

        Always the single stable per-agent id — fixed for the life of the
        session, used byte-identically for ``prompt_cache_key`` / ``session_id``
        / ``thread_id`` on every request. No rotation, no epoch, no time
        dependence.
        """
        return self._prompt_cache_key, self._cache_affinity_headers()

    @staticmethod
    def _usage_extra(
        affinity_headers: dict[str, str], cache_key: str | None
    ) -> dict[str, str]:
        """Build the token-ledger ``UsageMetadata.extra`` for this request.

        Surfaces the ACTUAL current ids used so a stalled-cache rotation is
        visible in ``token_ledger.jsonl`` alongside the pre-rotation requests.
        Only the short non-secret affinity ids ride here — no prompt body, no
        tokens, no OAuth secret.
        """
        extra: dict[str, str] = {}
        if affinity_headers.get("session_id"):
            extra["codex_session_id"] = affinity_headers["session_id"]
        if affinity_headers.get("thread_id"):
            extra["codex_thread_id"] = affinity_headers["thread_id"]
        if cache_key:
            extra["codex_prompt_cache_key"] = cache_key
        return extra

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
            # Ensure reasoning.encrypted_content is requested so the raw
            # reasoning item can be preserved for prompt-cache-stable replay.
            existing_include = kwargs.get("include") or []
            if isinstance(existing_include, str):
                existing_include = [existing_include]
            else:
                try:
                    existing_include = list(existing_include)
                except TypeError:
                    existing_include = [existing_include]
            if "reasoning.encrypted_content" not in existing_include:
                kwargs["include"] = existing_include + ["reasoning.encrypted_content"]
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
            # Resolve this request's cache-affinity values — the single stable
            # per-agent id (a pure hash of the agent path). All three levers
            # (prompt_cache_key / session_id / thread_id) carry the same value on
            # every request and never change for the life of the session.
            effective_cache_key, affinity_headers = self._effective_affinity()
            # Opt into Codex prompt caching with the resolved key. We send only
            # `prompt_cache_key`; the Codex backend rejects `prompt_cache_retention`
            # (Unsupported parameter), so it is deliberately never sent.
            if effective_cache_key:
                kwargs["prompt_cache_key"] = effective_cache_key
            # REST cache-affinity headers (issue #378). Sent as HTTP headers via
            # the SDK's per-request ``extra_headers``, never as request-body
            # fields. ``session_id`` / ``thread_id`` route the per-agent cache
            # slot and are a single stable per-agent value (never rotated).
            # Honest client identity (#436) forms the base; cache-affinity and
            # caller-supplied headers layer on top so they always win.
            extra_headers = {
                **_codex_identity_headers(),
                **affinity_headers,
            }
            if extra_headers:
                kwargs["extra_headers"] = {
                    **extra_headers,
                    **kwargs.get("extra_headers", {}),
                }
            acc = StreamingAccumulator()
            response_id = None
            usage = UsageMetadata()
            seen_reasoning_summary_items: set[str] = set()
            # Raw reasoning item dicts for replay, in provider output order.
            raw_reasoning_items: list[dict[str, Any]] = []
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
                    # Capture raw reasoning item when output_item.done carries
                    # encrypted_content, so it can be replayed verbatim next turn.
                    if (
                        event.type == "response.output_item.done"
                        and getattr(event.item, "type", None) == "reasoning"
                    ):
                        enc = getattr(event.item, "encrypted_content", None)
                        if enc:
                            item_id = getattr(event.item, "id", None) or ""
                            summaries = []
                            for s in getattr(event.item, "summary", None) or []:
                                summaries.append({
                                    "type": getattr(s, "type", None),
                                    "text": getattr(s, "text", None),
                                })
                            content = []
                            for c in getattr(event.item, "content", None) or []:
                                if hasattr(c, "model_dump"):
                                    content.append(c.model_dump(exclude_none=True))
                                elif isinstance(c, dict):
                                    content.append(c)
                                else:
                                    logger.warning(
                                        "codex.responses.reasoning_content_ignored",
                                        extra={
                                            "item_id": item_id,
                                            "content_type": type(c).__name__,
                                        },
                                    )
                            raw_reasoning_items.append({
                                "type": "reasoning",
                                "id": item_id,
                                "summary": summaries,
                                "content": content,
                                "encrypted_content": enc,
                            })
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
                        input_tokens = getattr(event.response.usage, "input_tokens", 0) or 0
                        usage = UsageMetadata(
                            input_tokens=input_tokens,
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
                            extra=self._usage_extra(
                                affinity_headers, effective_cache_key
                            ),
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
        raw_items = raw_reasoning_items
        if result.thoughts or raw_items:
            joined = "\n".join(t for t in result.thoughts if t)
            if raw_items:
                # Attach every raw reasoning item (with encrypted_content), even
                # when the provider returned no summary_text. Codex commonly
                # returns summary=[] with encrypted_content; dropping the block
                # in that case would lose the cache-stable replay state.
                for idx, raw_item in enumerate(raw_items):
                    item_summary_text = "\n".join(
                        str(s.get("text"))
                        for s in raw_item.get("summary", [])
                        if isinstance(s, dict)
                        and s.get("type") == "summary_text"
                        and s.get("text")
                    )
                    blocks.append(
                        ThinkingBlock(
                            text=item_summary_text or (joined if idx == 0 else ""),
                            provider_data={
                                "openai_responses_reasoning_item": raw_item,
                            },
                        )
                    )
            elif joined:
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

    def __init__(
        self,
        *args,
        codex_session_id: str | None = None,
        codex_session_anchor: str | None = None,
        codex_thread_salt: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # Codex REST cache-affinity identity: ONE stable per-agent value used
        # byte-identically for ``session_id``, ``thread_id``, and the default
        # ``prompt_cache_key``. It is a PURE deterministic hash of the agent's
        # durable identity anchor (the resolved ``init.json`` / agent-dir path) —
        # no epoch, no time, no rotation. The same agent path always yields the
        # same id across restarts/refresh/molt, so the agent keeps routing to the
        # same sticky-warm backend cache slot. The adapter has no per-agent
        # identity of its own; the host wiring passes the anchor down by default
        # via these kwargs (also an internal override / testing escape hatch):
        #
        #   codex_session_id=str     -> use this exact string verbatim for all
        #                               three (explicit operator override)
        #   codex_session_anchor=str -> hash the per-agent anchor (the resolved
        #                               init.json path) into the id for all three
        #   (neither set)            -> no session_id/thread_id (bare/test path)
        #
        # ``codex_thread_salt`` is accepted only as a legacy manifest
        # pass-through; it is intentionally NOT used to derive a separate thread
        # id. The root/main thread tracks the session id exactly so the three
        # values stay byte-identical.
        self._codex_session_anchor = (
            str(codex_session_anchor) if codex_session_anchor else None
        )
        if codex_session_id:
            # Explicit override: used verbatim.
            self._codex_id: str | None = str(codex_session_id)
        elif self._codex_session_anchor:
            self._codex_id = _codex_session_id(self._codex_session_anchor)
        else:
            self._codex_id = None  # no per-agent identity -> no headers
        self._codex_thread_salt = codex_thread_salt  # legacy pass-through; unused

    def _resolve_codex_ids(self, model: str) -> tuple[str | None, str | None]:
        """Resolve the (session_id, thread_id) headers for ``model``.

        Returns ``(None, None)`` only when no per-agent identity was passed in
        (e.g. a bare adapter built directly in a test). In the normal host path
        the agent path is always supplied, so both ids are the same stable
        per-agent hash — the thread id tracks the session id exactly.
        """
        return self._codex_id, self._codex_id

    def _default_prompt_cache_key(self, model: str) -> str:
        # On the normal/root path the cache key is the SAME per-agent value as
        # session_id / thread_id — byte-identical, so all three cache-affinity
        # levers point at one stable slot for the agent's durable identity (a
        # pure hash of the agent path, never time/epoch dependent). Never paired
        # with `prompt_cache_retention` (Codex rejects it).
        #
        # The model-keyed ``lingtai-codex:{model}:v1`` form survives only for the
        # truly bare/no-anchor path (e.g. a standalone unit test), where the
        # adapter has no per-agent identity to hash.
        if self._codex_id:
            return self._codex_id
        return f"lingtai-codex:{model}:v1"

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
        session_id, thread_id = self._resolve_codex_ids(model)
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
            # On the normal/root path this resolves to the SAME stable per-agent
            # hash as session_id / thread_id (see ``_default_prompt_cache_key``);
            # it honors an explicit override or a ``prompt_cache_key=False``
            # disable passed to the adapter. Only the bare/no-anchor path falls
            # back to ``lingtai-codex:{model}:v1``.
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            # Stable REST cache-affinity headers: both the per-agent hash,
            # byte-identical, passed down by the host; ``(None, None)`` only for
            # a bare/test adapter. Never rotated.
            session_id=session_id,
            thread_id=thread_id,
        )
