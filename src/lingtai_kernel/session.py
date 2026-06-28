"""SessionManager — LLM session lifecycle, token tracking, and compaction.

Extracted from BaseAgent to isolate LLM communication concerns.
BaseAgent delegates all session operations here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
import uuid
from typing import Any, Callable, TYPE_CHECKING

from .config import (
    AgentConfig,
    CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
    CONTEXT_PRESSURE_WARN_AFTER_ROUNDS,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
)
from .llm import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    LLMService,
)
from .llm_utils import (
    send_with_timeout,
    send_with_timeout_stream,
    track_llm_usage,
)
from .logging import get_logger
from .token_counter import count_tokens, count_tool_tokens

logger = get_logger()

# Sustained context-pressure (channel B) constants are kernel-fixed and live in
# config.py alongside the molt thresholds (imported above), giving one source of
# truth shared by SessionManager (the streak) and meta_block (the warning). See
# config.py for the full rationale.

if TYPE_CHECKING:
    from .llm.interface import ChatInterface


def _elapsed_ms(start: float) -> int:
    """Return non-negative elapsed milliseconds from a monotonic start."""
    return max(0, int((time.monotonic() - start) * 1000))


def _ensure_spill_manifest_fields(messages: list) -> None:
    """Backfill ``artifact_lifetime`` and ``artifact_state`` on legacy spill manifests.

    Walks a ``messages`` list (as produced by ``ChatInterface.to_dict``) and
    adds the ephemeral-metadata fields to any spill manifest that predates
    them. Mutates in place; idempotent.
    """
    from .tool_result_artifacts import ARTIFACT_MARKER

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            if (
                value.get("artifact") == ARTIFACT_MARKER
                and value.get("status") == "spilled"
            ):
                value.setdefault("artifact_lifetime", "ephemeral_tmp")
                value.setdefault("artifact_state", "available")
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    for msg in messages:
        _walk(msg)


class SessionManager:
    """Manages LLM session lifecycle, token tracking, and context compaction.

    Receives callback functions for building system prompts and tool schemas
    so it has no reference to BaseAgent.
    """

    def __init__(
        self,
        *,
        llm_service: LLMService,
        config: AgentConfig,
        agent_name: str | None = None,
        streaming: bool,
        build_system_prompt_fn: Callable[[], str],
        build_tool_schemas_fn: Callable[[], list[FunctionSchema]],
        logger_fn: Callable[..., None] | None,
        build_system_batches_fn: Callable[[], list[str]] | None = None,
        tool_result_recovery_lookup_fn: Callable[[Any], Any] | None = None,
    ):
        self._llm_service = llm_service
        self._config = config
        self._agent_name = agent_name
        self._display_name = agent_name or "agent"
        self._streaming = streaming
        self._build_system_prompt_fn = build_system_prompt_fn
        self._build_tool_schemas_fn = build_tool_schemas_fn
        self._logger_fn = logger_fn
        self._tool_result_recovery_lookup_fn = tool_result_recovery_lookup_fn
        # Optional batched system-prompt builder. When provided, adapters
        # that support per-block caching receive mutation-frequency batches
        # and can place cache breakpoints between them. When absent, the
        # string builder is used for everything.
        self._build_system_batches_fn = build_system_batches_fn
        # Persistent LLM session
        self._chat: ChatSession | None = None
        self._interaction_id: str | None = None

        # Token tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_thinking_tokens = 0
        self._total_cached_tokens = 0
        self._api_calls = 0
        # Current-runtime-session baselines. The ``_total_*``/``_api_calls``
        # counters above are CUMULATIVE/lifetime and survive restore; these
        # baselines mark where the live runtime session began so callers can
        # compute deltas (current_total - baseline). At a fresh init the live
        # counters are zero, so the baselines are zero too. On
        # ``restore_token_state`` they are re-anchored to the restored lifetime
        # totals so post-restart usage starts counting from zero again.
        self._session_baseline_input_tokens = 0
        self._session_baseline_cached_tokens = 0
        self._session_baseline_api_calls = 0
        self._last_tool_context = "send_message"
        self._system_prompt_tokens = 0
        self._tools_tokens = 0
        self._token_decomp_dirty = True
        self._token_fallback_warned = False
        self._latest_input_tokens = 0
        self._latest_token_usage_snapshot: dict[str, Any] | None = None

        # Sustained context-pressure streak (channel B). Transient runtime
        # state — not persisted, since a fresh/restored session has fresh
        # pressure. ``_context_pressure_streak`` counts consecutive fresh
        # provider rounds at/above CONTEXT_PRESSURE_RECONSTRUCTION_RATIO;
        # ``_context_pressure_last_round_id`` dedups multiple observations of
        # the same provider round (one provider response can be re-read by many
        # build_meta / tool-result stamps in a single batch).
        self._context_pressure_streak = 0
        self._context_pressure_last_round_id: int | None = None

        # Streaming state
        self._text_already_streamed = False
        self._intermediate_text_streamed = False
        self._message_seq = 0

        # Timeout pool for LLM calls
        self._timeout_pool = ThreadPoolExecutor(max_workers=1)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chat(self) -> ChatSession | None:
        """The current LLM chat session (or None if not yet created)."""
        return self._chat

    @chat.setter
    def chat(self, value: ChatSession | None) -> None:
        self._chat = value
        self._attach_tool_result_recovery_lookup(value)

    @property
    def token_decomp_dirty(self) -> bool:
        return self._token_decomp_dirty

    @token_decomp_dirty.setter
    def token_decomp_dirty(self, value: bool) -> None:
        self._token_decomp_dirty = value

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def interaction_id(self) -> str | None:
        return self._interaction_id

    @interaction_id.setter
    def interaction_id(self, value: str | None) -> None:
        self._interaction_id = value

    @property
    def intermediate_text_streamed(self) -> bool:
        return self._intermediate_text_streamed

    @intermediate_text_streamed.setter
    def intermediate_text_streamed(self, value: bool) -> None:
        self._intermediate_text_streamed = value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, event_type: str, **fields) -> None:
        """Delegate logging to the injected logger function."""
        if self._logger_fn is not None:
            self._logger_fn(event_type, **fields)

    # ------------------------------------------------------------------
    # LLM communication
    # ------------------------------------------------------------------

    def ensure_session(self) -> ChatSession:
        """Ensure a persistent LLM session exists, creating one if needed."""
        if self._chat is None:
            self._chat = self._llm_service.create_session(
                system_prompt=self._build_system_prompt_fn(),
                tools=self._build_tool_schemas_fn() or None,
                model=self._config.model or self._llm_service.model,
                thinking=self._config.thinking or "high",
                agent_type=self._display_name,
                tracked=True,
                interaction_id=self._interaction_id,
                provider=self._config.provider,
            )
            self._attach_tool_result_recovery_lookup(self._chat)
        return self._chat

    def _rebuild_session(
        self, interface: "ChatInterface", tracked: bool = True,
    ) -> None:
        """Create a new chat session with current config, preserving history."""
        self._chat = self._llm_service.create_session(
            system_prompt=self._build_system_prompt_fn(),
            tools=self._build_tool_schemas_fn() or None,
            model=self._config.model or self._llm_service.model,
            thinking=self._config.thinking or "high",
            agent_type=self._display_name,
            tracked=tracked,
            provider=self._config.provider,
            interface=interface,
        )
        self._attach_tool_result_recovery_lookup(self._chat)

    def _attach_tool_result_recovery_lookup(self, chat: ChatSession | None) -> None:
        if chat is None or self._tool_result_recovery_lookup_fn is None:
            return
        interface = getattr(chat, "interface", None)
        if interface is None:
            return
        try:
            interface.tool_result_recovery_lookup = self._tool_result_recovery_lookup_fn
        except Exception:
            pass

    def _health_check(self, message: Any) -> None:
        """Pre-send invariant checks on the canonical interface.

        Runs after system-prompt/tools refresh and before dispatch. Each
        check is a one-liner so future invariants land here, not scattered
        across adapters. Non-destructive: synthesizes placeholders rather
        than mutating committed history. Every heal logs a structured
        ``health_check`` event so operators can audit how often each
        invariant fires and trace the upstream callsite.
        """
        # Tail tool-call pairing: if we're about to append a user-text
        # message but the tail is assistant[tool_calls] (e.g. the prior
        # turn's tool_results never landed because of a timeout, daemon
        # crash, or partial AED recovery), close the dangling calls with
        # synthesized [aborted: ...] tool_results. The next add_user_message
        # will then succeed, and the model sees the abort reason on the
        # next turn.
        if isinstance(message, str) and self._chat.interface.has_pending_tool_calls():
            self._chat.interface.close_pending_tool_calls(
                reason="health_check:pre_send_pairing"
            )
            self._log("health_check", check="pre_send_pairing", action="auto_heal")

    def send(self, message: Any) -> LLMResponse:
        """Send a message to the LLM, reusing the persistent chat session.

        Single attempt — no retry. Raises on any failure; the caller
        (BaseAgent._run_loop AED loop) handles recovery.
        """
        self.ensure_session()

        if isinstance(message, str):
            reset_provider_turn_state = getattr(
                type(self._chat), "reset_provider_turn_state", None
            )
            if callable(reset_provider_turn_state):
                reset_provider_turn_state(self._chat)

        # Rebuild system prompt and tools every turn — they may have changed
        # (e.g. memory loaded, identity updated, capabilities added after refresh).
        # If the content is identical, this is a no-op at the LLM level.
        # Prefer the batched form so adapters can place per-block cache
        # breakpoints between mutation-frequency tiers. The default
        # update_system_prompt_batches concatenates and delegates to
        # update_system_prompt, so providers without per-block caching
        # see the exact same byte stream.
        prompt_start = time.monotonic()
        if self._build_system_batches_fn is not None:
            prompt_batches = self._build_system_batches_fn()
            prompt_build_ms = _elapsed_ms(prompt_start)
            self._chat.update_system_prompt_batches(prompt_batches)
        else:
            system_prompt = self._build_system_prompt_fn()
            prompt_build_ms = _elapsed_ms(prompt_start)
            self._chat.update_system_prompt(system_prompt)

        tool_schema_start = time.monotonic()
        tool_schemas = self._build_tool_schemas_fn() or None
        tool_schema_build_ms = _elapsed_ms(tool_schema_start)
        self._chat.update_tools(tool_schemas)

        self._health_check(message)

        api_call_id = f"api_{uuid.uuid4().hex[:12]}"
        self._log(
            "llm_call",
            model=self._config.model or self._llm_service.model or "unknown",
            api_call_id=api_call_id,
        )

        retry_timeout = self._config.retry_timeout

        provider_start = time.monotonic()
        if self._streaming:
            response = self._send_streaming(message, retry_timeout)
        else:
            response = send_with_timeout(
                chat=self._chat,
                message=message,
                timeout_pool=self._timeout_pool,
                retry_timeout=retry_timeout,
                agent_name=self._display_name,
                logger=logger,
            )
        provider_wait_ms = _elapsed_ms(provider_start)

        response.api_call_id = api_call_id
        if response.usage is not None:
            response.usage.extra.setdefault("api_call_id", api_call_id)
        self._track_usage(
            response,
            timing_fields={
                "prompt_build_ms": prompt_build_ms,
                "tool_schema_build_ms": tool_schema_build_ms,
                "provider_wait_ms": provider_wait_ms,
            },
        )
        # Preserve interaction ID for session reuse
        if hasattr(self._chat, "interaction_id") and self._chat.interaction_id:
            self._interaction_id = self._chat.interaction_id
        return response

    def _send_streaming(
        self, message: Any, retry_timeout: float
    ) -> LLMResponse:
        """Streaming LLM send via send_stream."""
        self._message_seq += 1

        response = send_with_timeout_stream(
            chat=self._chat,
            message=message,
            timeout_pool=self._timeout_pool,
            retry_timeout=retry_timeout,
            agent_name=self._display_name,
            logger=logger,
        )

        if response.text:
            if response.tool_calls:
                self._intermediate_text_streamed = True
            else:
                self._text_already_streamed = True

        return response

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def get_context_pressure(self) -> float:
        """Return context usage as fraction (0.0 to 1.0). Returns 0.0 if unknown."""
        if self._chat is None:
            return 0.0
        # Use configured context_limit if set, otherwise model default
        ctx_window = self._config.context_limit or self._chat.context_window()
        if ctx_window <= 0:
            return 0.0
        # Use local estimate (reflects current wire state including the
        # message about to be sent) as the primary source.  Fall back to
        # server-reported input tokens from the last response only when
        # the local estimate is unavailable (e.g. empty interface).
        tokens = self._chat.interface.estimate_context_tokens()
        if tokens <= 0:
            tokens = self._latest_input_tokens
        return tokens / ctx_window if tokens > 0 else 0.0

    # ------------------------------------------------------------------
    # Sustained context-pressure streak (channel B)
    # ------------------------------------------------------------------

    @property
    def context_pressure_streak(self) -> int:
        """Consecutive fresh provider rounds at/above the reconstruction ratio."""
        return self._context_pressure_streak

    @property
    def context_pressure_warning_active(self) -> bool:
        """True once context has been high for >= WARN_AFTER_ROUNDS fresh rounds.

        This drives the resident ``_meta.agent_meta.context.molt`` warning. It
        is *current state*, not a one-shot event: it stays True as long as the
        streak remains at/above the warn count and drops the moment context
        relaxes below the threshold (which resets the streak).
        """
        return self._context_pressure_streak >= CONTEXT_PRESSURE_WARN_AFTER_ROUNDS

    def note_context_pressure_round(self, usage: float, *, round_id: int) -> None:
        """Record one *fresh provider round*'s context usage for the streak.

        ``round_id`` identifies the provider round (the kernel passes the
        ``_api_calls`` counter, which increments exactly once per real provider
        response).  Re-observing the same ``round_id`` is a no-op so duplicate
        ``build_meta`` / tool-result stamps in a single batch cannot advance the
        streak — only genuinely new provider rounds do.

        ``usage`` is the fraction of the context window in use:
          * ``>= CONTEXT_PRESSURE_RECONSTRUCTION_RATIO`` → high round, advance.
          * ``0 <= usage < ratio``                       → relieved, reset to 0.
          * ``< 0`` (sentinel, decomposition not ready)  → leave streak untouched.
        """
        if round_id == self._context_pressure_last_round_id:
            return
        try:
            pressure = float(usage)
        except (TypeError, ValueError):
            return
        if pressure < 0:
            # Unknown/sentinel usage: neither a high round nor a real relief.
            # Don't advance and don't spuriously reset an existing streak.
            return
        self._context_pressure_last_round_id = round_id
        if pressure >= CONTEXT_PRESSURE_RECONSTRUCTION_RATIO:
            self._context_pressure_streak += 1
        else:
            self._context_pressure_streak = 0

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    def _update_token_decomposition(self) -> None:
        """Recompute cached system prompt and tools token counts."""
        self._system_prompt_tokens = count_tokens(self._build_system_prompt_fn())
        self._tools_tokens = count_tool_tokens(self._build_tool_schemas_fn())
        self._token_decomp_dirty = False

    def _track_usage(
        self,
        response: LLMResponse,
        *,
        timing_fields: dict[str, int | float] | None = None,
    ) -> None:
        """Accumulate token usage from an LLMResponse.

        If the provider returns all-zero usage, falls back to the local
        tokenizer (tiktoken / gemini / char estimate) and sets
        ``token_fallback_used`` so the TUI can warn the user.
        """
        usage_start = time.monotonic()
        if self._token_decomp_dirty:
            self._update_token_decomposition()

        # Detect zero-usage responses and estimate locally
        usage = response.usage
        fallback = False
        if usage and usage.input_tokens == 0 and usage.output_tokens == 0:
            estimated_output = count_tokens(response.text or "")
            # Estimate input from interface history (last user message + system prompt)
            estimated_input = self._system_prompt_tokens + self._tools_tokens
            if self._chat and self._chat.interface:
                last_entries = self._chat.interface.entries[-2:]  # last user + assistant
                for entry in last_entries:
                    for block in entry.content:
                        if hasattr(block, "text"):
                            estimated_input += count_tokens(block.text)
            from .llm.base import UsageMetadata
            usage = UsageMetadata(
                input_tokens=estimated_input,
                output_tokens=estimated_output,
                extra=dict(getattr(response.usage, "extra", {}) or {}),
            )
            if response.api_call_id:
                usage.extra.setdefault("api_call_id", response.api_call_id)
            response = LLMResponse(
                text=response.text,
                tool_calls=response.tool_calls,
                usage=usage,
                thoughts=response.thoughts,
                raw=response.raw,
                api_call_id=response.api_call_id,
            )
            fallback = True
            if not self._token_fallback_warned:
                self._token_fallback_warned = True
                logger.warning(
                    f"[{self._display_name}] Provider returned 0 tokens — "
                    f"using local tokenizer estimate"
                )
                self._log("token_fallback", reason="provider returned 0 tokens")

        token_state = {
            "input": self._total_input_tokens,
            "output": self._total_output_tokens,
            "thinking": self._total_thinking_tokens,
            "cached": self._total_cached_tokens,
            "api_calls": self._api_calls,
        }
        track_llm_usage(
            response=response,
            token_state=token_state,
            agent_name=self._display_name,
            last_tool_context=self._last_tool_context,
            system_tokens=self._system_prompt_tokens,
            tools_tokens=self._tools_tokens,
        )
        self._total_input_tokens = token_state["input"]
        self._total_output_tokens = token_state["output"]
        self._total_thinking_tokens = token_state["thinking"]
        self._total_cached_tokens = token_state["cached"]
        self._api_calls = token_state["api_calls"]
        self._latest_token_usage_snapshot = None
        if response.usage:
            self._latest_input_tokens = response.usage.input_tokens
            latest_context_window = -1
            latest_context_usage = -1.0
            # Record this fresh provider round's context pressure for the
            # sustained-warning streak (channel B). Keyed by _api_calls so it is
            # counted exactly once per real provider response, not once per
            # build_meta / tool-result stamp.
            #
            # Use the PROVIDER-reported input tokens (input_tokens / window), not
            # get_context_pressure()'s local-estimate-primary value: the delayed
            # reconstruction threshold (0.75) is itself provider-input based, so
            # the streak must observe the same ruler. This keeps channel A
            # (reconstruction event) and channel B (sustained warning) consistent
            # and avoids a low/stale local estimate masking real provider
            # pressure (PR #535 showed the local estimate can lag the wire).
            try:
                ctx_window = self._config.context_limit or (
                    self._chat.context_window() if self._chat is not None else 0
                )
                if ctx_window and ctx_window > 0:
                    latest_context_window = int(ctx_window)
                    provider_usage = self._latest_input_tokens / ctx_window
                    latest_context_usage = provider_usage
                else:
                    provider_usage = -1.0  # window unknown -> sentinel (no change)
                self.note_context_pressure_round(
                    provider_usage, round_id=self._api_calls
                )
            except Exception:
                pass
            usage_extra = getattr(response.usage, "extra", None)
            api_call_id = response.api_call_id
            if not api_call_id and isinstance(usage_extra, dict):
                api_call_id = usage_extra.get("api_call_id")
            input_tokens = int(getattr(response.usage, "input_tokens", 0) or 0)
            cached_tokens = int(getattr(response.usage, "cached_tokens", 0) or 0)
            cache_miss_tokens = max(input_tokens - cached_tokens, 0)
            snapshot = {
                "scope": "provider_round",
                "api_call_index": int(self._api_calls),
                "input_tokens": input_tokens,
                "cache_miss_tokens": cache_miss_tokens,
                "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
                "thinking_tokens": int(getattr(response.usage, "thinking_tokens", 0) or 0),
                "cached_tokens": cached_tokens,
                "cache_rate": (
                    round(min(cached_tokens / input_tokens, 1.0), 5)
                    if input_tokens > 0
                    else 0.0
                ),
                "context_tokens": input_tokens,
                "context_window": latest_context_window,
                "context_usage": (
                    round(latest_context_usage, 5)
                    if latest_context_usage >= 0
                    else -1.0
                ),
                "estimated": bool(fallback),
            }
            if api_call_id:
                snapshot["api_call_id"] = str(api_call_id)
            self._latest_token_usage_snapshot = snapshot
            usage_track_ms = _elapsed_ms(usage_start)
            telemetry_fields = dict(timing_fields or {})
            telemetry_fields["usage_track_ms"] = usage_track_ms
            self._log(
                "llm_response",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                thinking_tokens=response.usage.thinking_tokens,
                cached_tokens=response.usage.cached_tokens,
                estimated=fallback,
                api_call_id=response.api_call_id,
                **telemetry_fields,
            )

    def latest_token_usage_snapshot(self) -> dict | None:
        """Return the latest provider-round token/cache usage snapshot."""
        if isinstance(self._latest_token_usage_snapshot, dict):
            return dict(self._latest_token_usage_snapshot)
        return None

    def get_token_usage(self) -> dict:
        """Return token usage summary."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "thinking_tokens": self._total_thinking_tokens,
            "cached_tokens": self._total_cached_tokens,
            "total_tokens": (
                self._total_input_tokens
                + self._total_output_tokens
                + self._total_thinking_tokens
            ),
            "api_calls": self._api_calls,
            "ctx_system_tokens": self._system_prompt_tokens,
            "ctx_tools_tokens": self._tools_tokens,
            "ctx_history_tokens": max(
                0,
                self._latest_input_tokens
                - self._system_prompt_tokens
                - self._tools_tokens,
            ),
            "ctx_total_tokens": self._latest_input_tokens,
        }

    def get_current_session_token_usage(self) -> dict:
        """Return current-runtime-session token usage as DELTAS, not lifetime totals.

        Unlike :meth:`get_token_usage` (which reports the cumulative/persisted
        ``_total_*``/``_api_calls`` counters that survive ``restore_token_state``),
        this reports only what the *live* runtime session has accrued since it
        started — ``current_total - session_baseline`` for ``api_calls``,
        ``input_tokens`` and ``cached_tokens`` — so restored lifetime totals do
        not leak into the injected ``tool_meta.token_usage`` session stats.

        ``session_cache_rate`` and ``avg_input_tokens_per_api_call`` are derived
        from those deltas. All counters are clamped to ``>= 0`` so a restore to
        lower totals than the live counters can never produce a negative delta.
        """
        api_calls = max(0, self._api_calls - self._session_baseline_api_calls)
        input_tokens = max(
            0, self._total_input_tokens - self._session_baseline_input_tokens
        )
        cached_tokens = max(
            0, self._total_cached_tokens - self._session_baseline_cached_tokens
        )
        session_cache_rate = (
            round(min(cached_tokens / input_tokens, 1.0), 5)
            if input_tokens > 0
            else 0.0
        )
        avg_input = (
            int(round(input_tokens / api_calls)) if api_calls > 0 else 0
        )
        return {
            "api_calls": api_calls,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "session_cache_rate": session_cache_rate,
            "avg_input_tokens_per_api_call": avg_input,
        }

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def get_chat_state(self) -> dict:
        """Serialize current chat session for persistence."""
        if self._chat is None:
            return {}
        try:
            return {"messages": self._chat.interface.to_dict()}
        except Exception:
            return {}

    def restore_chat(self, state: dict) -> None:
        """Restore chat history with current system prompt and tools.

        Heals two classes of on-disk corruption before building the session:
        1. Set-level orphans (tool_call without result or vice versa) —
           handled by enforce_tool_pairing(), which strips them (except tail
           assistant[tool_calls], which is deferred).
        2. Positional violations (assistant[tool_calls] at tail with no
           matching tool_results) — handled by close_pending_tool_calls(),
           which replays durable real results when available, otherwise
           synthesizes placeholder error results so the next send is
           well-formed for strict providers (DeepSeek, OpenAI).

        Also ensures ephemeral manifest fields exist on spill manifests
        that predate the expired-spill-messaging feature (issue #192).
        Full stale-marking (file-existence checks) is handled upstream in
        lifecycle.py ``_start()``; this path only backfills missing fields
        for in-memory messages that bypassed that call.
        """
        from .llm.interface import ChatInterface
        messages = state.get("messages")
        if messages:
            _ensure_spill_manifest_fields(messages)
            try:
                interface = ChatInterface.from_dict(messages)
                interface.tool_result_recovery_lookup = self._tool_result_recovery_lookup_fn
                interface.enforce_tool_pairing()
                if interface.has_pending_tool_calls():
                    interface.close_pending_tool_calls(
                        reason="restored from disk — prior session ended mid-tool-loop"
                    )
                self._rebuild_session(interface)
                return
            except Exception as e:
                logger.warning(
                    f"[{self._display_name}] Failed to restore chat: {e}. Starting fresh.",
                    exc_info=True,
                )
        self.ensure_session()

    def restore_token_state(self, state: dict) -> None:
        """Restore cumulative token counters from a saved session.

        Re-anchors the current-runtime-session baselines to the restored lifetime
        totals so :meth:`get_current_session_token_usage` deltas start from zero
        after a refresh/restart — restored cumulative totals are lifetime facts
        and must not be reported as current-session activity.
        """
        self._total_input_tokens = state.get("input_tokens", 0)
        self._total_output_tokens = state.get("output_tokens", 0)
        self._total_thinking_tokens = state.get("thinking_tokens", 0)
        self._total_cached_tokens = state.get("cached_tokens", 0)
        self._api_calls = state.get("api_calls", 0)
        # Re-baseline so post-restore deltas begin at zero.
        self._session_baseline_input_tokens = self._total_input_tokens
        self._session_baseline_cached_tokens = self._total_cached_tokens
        self._session_baseline_api_calls = self._api_calls

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shut down the timeout pool."""
        self._timeout_pool.shutdown(wait=False)
