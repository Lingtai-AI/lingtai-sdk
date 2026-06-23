"""Provider-agnostic types and session ABC for the LLM protocol layer.

All agent code should depend on these types, never on provider-specific SDKs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lingtai_kernel.logging import get_logger

from .interface import ChatInterface, ToolResultBlock

logger = get_logger()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single function/tool invocation extracted from the LLM response.

    Attributes:
        name: Tool/function name.
        args: Parsed arguments dict.
        id: Provider-assigned call ID (e.g. ``call_xxxxx`` for OpenAI,
            ``toolu_xxxxx`` for Anthropic).  None for Gemini which doesn't
            use explicit tool-call IDs.
    """

    name: str
    args: dict
    id: str | None = None


@dataclass
class UsageMetadata:
    """Normalized token counts plus optional per-call ledger metadata."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    # Optional safe, provider-specific metadata to merge into token_ledger.jsonl.
    # Do not place request bodies, API keys, or other secrets here.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Provider-agnostic response from an LLM call.

    Attributes:
        text: Concatenated text output (excludes thinking text).
        tool_calls: Extracted function/tool calls.
        usage: Token usage for this call.
        thoughts: List of thinking/reasoning text blocks (for verbose logging).
        raw: The original provider-specific response object. Use for escape
            hatches (e.g. Gemini grounding metadata, multimodal parts).
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: UsageMetadata = field(default_factory=UsageMetadata)
    thoughts: list[str] = field(default_factory=list)
    raw: Any = None
    # Stable identifier for this kernel-level LLM API round-trip.
    # SessionManager assigns it before logging llm_call/llm_response;
    # BaseAgent/ToolExecutor propagate it to every tool event produced from
    # the same assistant response so UI/replay code can group tool batches.
    api_call_id: str | None = None


@dataclass
class FunctionSchema:
    """Wraps a tool/function schema dict for type clarity.

    The ``parameters`` dict is already JSON-schema-shaped and provider-agnostic.
    """

    name: str
    description: str
    parameters: dict
    system_prompt: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    @staticmethod
    def list_to_dicts(schemas: list[FunctionSchema] | None) -> list[dict] | None:
        """Convert a list of FunctionSchema to dicts, or None if empty/None."""
        if not schemas:
            return None
        return [s.to_dict() for s in schemas]

    @classmethod
    def from_dicts(cls, dicts: list[dict] | None) -> list["FunctionSchema"] | None:
        """Convert tool dicts (as stored in ChatInterface) back to FunctionSchema objects."""
        if not dicts:
            return None
        return [
            cls(
                name=d["name"],
                description=d.get("description", ""),
                parameters=d.get("parameters", {}),
            )
            for d in dicts
        ]


# ---------------------------------------------------------------------------
# ChatSession ABC
# ---------------------------------------------------------------------------


class ChatSession(ABC):
    """Abstract multi-turn chat session."""

    # lingtai-assigned session ID, set by LLMService
    session_id: str = ""
    # Session metadata for get_state()
    _agent_type: str = ""
    _tracked: bool = True

    # Optional pre-request hook fired after the message is committed to the
    # canonical ChatInterface but before the API call is made. The kernel
    # installs ``_drain_tc_inbox`` here so involuntary tool-call pairs
    # (mail notifications, soul.flow voices) splice into the wire chat
    # mid-turn — between tool rounds within a single _handle_request —
    # rather than waiting for the outer turn to finish.
    #
    # Wire-state contract: at the moment the hook fires, the interface
    # tail must be ``user[tool_results]`` or ``user[text]`` — i.e.
    # ``has_pending_tool_calls()`` must return False, so the splicer can
    # safely append a new ``(call, result)`` pair without violating the
    # provider's strict pair-validation invariant.
    #
    # Sessions that don't use the canonical ChatInterface for wire
    # serialization (OpenAIResponsesSession, GeminiChatSession via
    # genai SDK) still call the hook for the agent-side drain, but the
    # spliced pair is only visible to the LLM on the *next* turn (when
    # the agent re-syncs from interface). For canonical-interface
    # adapters (anthropic, openai-CC, codex-Responses, deepseek), the
    # spliced pair is visible in the same API call as the triggering
    # tool_results.
    #
    # Default ``None`` — adapters that don't install a hook treat the
    # call as a no-op, preserving the legacy zero-hook behavior.
    pre_request_hook: "Callable[[ChatInterface], None] | None" = None

    def adapter_comment(self):
        """Optional adapter-authored, agent-facing runtime note for `_meta.agent_meta`.

        Adapters can override this to surface provider-specific state that the
        agent must reason about (for example remote state reuse semantics). The
        value must be small and JSON-serializable; falsy values are omitted.
        """

        return None

    def on_history_summarized(self, summarized_ids: list[str]) -> None:
        """Hook called after `system(action='summarize')` mutates chat history."""

        return None

    def on_notification_dismissed(self, channel: str | None = None) -> None:
        """Hook called after a notification dismiss/cleanup mutates the surface.

        A dismiss rewrites the resident notification meta on prior tool results,
        so — like ``on_history_summarized`` — adapters that reuse remote state
        (e.g. Codex WS) use this to start a fresh ws_full epoch. Default no-op.
        """

        return None

    @property
    @abstractmethod
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""

    @abstractmethod
    def send(self, message) -> LLMResponse:
        """Send a user message or tool results and return the model response.

        ``message`` can be:
        - A string (user text message)
        - A list of ToolResultBlock (canonical tool results)
        """

    def reset_provider_turn_state(self) -> None:
        """Reset transient provider turn state before a new user text turn.

        Most providers have no extra turn-scoped transport state. Adapters that
        do (for example Codex Responses-over-WebSocket turn-state headers) may
        override this hook. The kernel calls it only before string user-text
        messages, not before tool-result continuations.
        """

    def get_history(self) -> list[dict]:
        """Return serializable conversation history (canonical format)."""
        return self.interface.to_dict()

    def get_state(self) -> dict:
        """Return the full session state dict.

        Format: {"session_id": str, "messages": [...], "metadata": {...}}
        """
        return {
            "session_id": self.session_id,
            "messages": self.interface.to_dict(),
            "metadata": {
                "agent_type": self._agent_type,
                "created_at": self.interface.entries[0].timestamp if self.interface.entries else 0.0,
                "tracked": self._tracked,
            },
        }

    def total_usage(self) -> dict:
        """Sum tokens and count API calls across all messages."""
        return self.interface.total_usage()

    def usage_by_model(self) -> dict[str, dict]:
        """Breakdown of usage per model name."""
        return self.interface.usage_by_model()

    def send_stream(
        self,
        message,
        on_chunk: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Send a message with optional streaming callback for text chunks.

        If the session supports streaming, calls ``on_chunk(text_delta)``
        as text tokens arrive.  Always returns the complete ``LLMResponse``
        at the end.

        Default implementation falls back to non-streaming ``send()``.
        """
        response = self.send(message)
        if on_chunk and response.text:
            on_chunk(response.text)
        return response

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to history without an API call.

        Used when tool execution is intercepted (e.g., clarification_needed
        terminal tool) but the tool_use/tool_result pairing must be preserved
        in history for subsequent messages.

        Default is a no-op for adapters that don't need it (e.g., server-managed
        history).
        """

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session.

        Used by the tool-store pattern: the orchestrator starts with
        meta-tools only and dynamically loads more as the model requests.

        Default: no-op. Override in session types that support it.
        """

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session.

        Default: no-op. Override in session types that support it.
        """

    def update_system_prompt_batches(self, batches: list[str]) -> None:
        """Replace the system prompt using mutation-frequency batches.

        ``batches`` is the ordered output of
        ``build_system_prompt_batches``: each element is a contiguous
        chunk whose content tends to change at a different cadence
        (e.g. immovable / rarely-mutated / per-idle). Adapters that
        support per-block prompt caching (Anthropic's ``cache_control``)
        can place cache breakpoints at batch boundaries so only the
        volatile tail pays for re-caching.

        Default: concatenate to a string and delegate to
        ``update_system_prompt`` — providers without per-block caching
        see no behaviour change.
        """
        joined = "\n\n".join(b for b in batches if b)
        self.update_system_prompt(joined)

    def reset(self) -> None:
        """Reset the session's HTTP connection while preserving conversation state.

        Called after persistent API errors (e.g. 3+ consecutive 500s) to get a
        fresh connection.  History, tools, and system prompt are preserved —
        only the underlying HTTP client is recreated.

        Default: no-op.  Override in session types backed by a persistent
        HTTP client (Anthropic, OpenAI).  Gemini sessions with server-side
        state (Interactions API) cannot be meaningfully reset this way.
        """

    @property
    def interaction_id(self) -> str | None:
        """Return the current Interactions API interaction ID, or None.

        Only meaningful for Gemini ``InteractionsChatSession`` which chains
        calls via ``previous_interaction_id``.  Other session types return None.
        """
        return None

    def context_window(self) -> int:
        """Total context window in tokens for this session's model. 0 = unknown."""
        return 0

    # -----------------------------------------------------------------------
    # Context-overflow auto-recovery (shared across all providers)
    # -----------------------------------------------------------------------
    #
    # When the provider rejects a request because the context exceeds its
    # hard token limit, the session trims ~10% of the oldest non-system
    # entries from the canonical ChatInterface and retries — up to
    # ``_OVERFLOW_MAX_ROUNDS`` times.  Each provider only needs to
    # implement ``_is_context_overflow_error()`` to opt in.
    #
    # This lives on ChatSession (not LLMAdapter) because the trim
    # operates on ``self._interface._entries`` and the retry wraps the
    # provider-specific ``send()`` / ``send_stream()`` call.

    _OVERFLOW_MAX_ROUNDS: int = 10
    _OVERFLOW_DROP_FRACTION: float = 0.10

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Return True if *exc* is a provider context-length-exceeded error.

        Default returns False (no recovery).  Override in subclasses that
        want overflow auto-recovery.
        """
        return False

    def _trim_context_one_round(self) -> int:
        """Drop ~``_OVERFLOW_DROP_FRACTION`` of non-system entries from the
        **front** of the canonical interface.

        Snaps the cut point forward so we never split an
        ``assistant[ToolCallBlock]`` from its matching
        ``user[ToolResultBlock]`` — the resulting wire payload would be
        invalid for strict providers.

        Returns the number of entries dropped (0 if none could be dropped
        — caller should treat that as terminal).
        """
        from .interface import ToolCallBlock, ToolResultBlock

        entries = self._interface._entries  # canonical list, mutated in place
        if not entries:
            return 0
        # Index of first non-system entry.
        first_conv = 0
        if entries[0].role == "system":
            first_conv = 1
        conv_len = len(entries) - first_conv
        if conv_len <= 1:
            return 0
        drop_n = max(1, int(conv_len * self._OVERFLOW_DROP_FRACTION))
        cut = first_conv + drop_n  # entries[first_conv:cut] get dropped

        # Snap cut forward past any assistant[tool_calls] / user[tool_results]
        # boundary so we never strand a tool_call without its result.
        max_cut = len(entries)
        while cut < max_cut:
            # If the entry just *before* the cut is assistant[tool_calls],
            # advance until we're past its matching user[tool_results].
            if cut == 0:
                break
            prev = entries[cut - 1]
            if prev.role == "assistant" and any(
                isinstance(b, ToolCallBlock) for b in prev.content
            ):
                cut += 1
                continue
            # If the entry at the cut is a user[tool_results-only], advance
            # past it so we don't leave dangling results without their call.
            cur = entries[cut]
            if cur.role == "user" and cur.content and all(
                isinstance(b, ToolResultBlock) for b in cur.content
            ):
                cut += 1
                continue
            break

        if cut >= max_cut:
            # Snap consumed everything — refuse to drop the entire conversation.
            return 0

        dropped = cut - first_conv
        # Mutate in place: keep system + everything from cut onward.
        del entries[first_conv:cut]
        return dropped

    def _inject_overflow_notice(self, total_dropped: int, rounds: int) -> None:
        """Append a single user-role kernel notice after successful recovery.

        We use the user role (universally supported) with an explicit
        ``[kernel]`` prefix — same pattern as our synthesized tool aborts.
        The notice strongly recommends molting since context pressure is
        now demonstrably above the model's hard ceiling.
        """
        from .interface import TextBlock

        notice = (
            f"[kernel] Context exceeded the provider's hard token limit. "
            f"To recover, the kernel dropped {total_dropped} oldest entries "
            f"across {rounds} retry round(s). Detail from earlier turns may "
            f"be lost — re-read recent context before acting on it. "
            f"**Strongly recommend triggering a molt soon** — the conversation "
            f"is past the model's safe limit and further growth will overflow "
            f"again."
        )
        self._interface._append("user", [TextBlock(text=notice)])

    def _run_with_overflow_recovery(self, do_call):
        """Run an API call with context-overflow auto-recovery.

        ``do_call`` is a zero-arg callable performing one full attempt
        (build kwargs from current interface state + invoke the API). It
        is re-called after each trim so the request reflects the post-trim
        canonical interface.

        Returns ``(result, total_dropped, rounds)``. ``total_dropped`` is 0
        and ``rounds`` is 0 when no recovery was needed. On non-overflow
        errors, re-raises immediately. On terminal failure (cannot trim
        further, or max rounds hit), re-raises the original error.
        """
        total_dropped = 0
        rounds = 0
        while True:
            try:
                result = do_call()
                return result, total_dropped, rounds
            except Exception as exc:
                if not self._is_context_overflow_error(exc):
                    raise
                if rounds >= self._OVERFLOW_MAX_ROUNDS:
                    logger.warning(
                        "[overflow-recovery] giving up after %d rounds "
                        "(dropped %d entries total) — re-raising provider error.",
                        rounds, total_dropped,
                    )
                    raise
                dropped = self._trim_context_one_round()
                if dropped == 0:
                    logger.warning(
                        "[overflow-recovery] cannot trim further "
                        "(dropped %d entries across %d rounds) — re-raising.",
                        total_dropped, rounds,
                    )
                    raise
                total_dropped += dropped
                rounds += 1
                logger.warning(
                    "[overflow-recovery] round %d: dropped %d entries "
                    "(running total %d). Retrying.",
                    rounds, dropped, total_dropped,
                )
                continue



