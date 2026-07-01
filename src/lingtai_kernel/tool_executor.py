"""ToolExecutor — sequential and parallel tool call execution."""
from __future__ import annotations

import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .llm.base import ToolCall
from .loop_guard import LoopGuard
from .meta_block import (
    stamp_meta,
    now_iso_plain,
    build_tool_meta_overflow_comment,
    TOOL_META_COMMENT_KEY,
    TOOL_META_COMMENT_OVERFLOW_KEY,
    TOOL_META_CONTEXT_KEY,
    TOOL_META_CONTEXT_PENDING_KEY,
    TOOL_META_CONTEXT_EVENT_PENDING_KEY,
    TOOL_META_TOKEN_USAGE_KEY,
    TOOL_META_TOKEN_USAGE_PENDING_KEY,
    TOOL_META_CURRENT_TIME_KEY,
)
from .reminders.context_pressure import (
    RECONSTRUCTION_MOLT_EVENT,
    reconstruction_molt_emission_descriptor,
)
from .tool_result_artifacts import (
    PREVENTIVE_MAX_CHARS as _DEFAULT_MAX_RESULT_CHARS,
    spill_oversized_result as _spill_oversized_result,
)
from .tool_call_guard import GuardDecision, ToolCallGuard, ToolProposal
from .tool_result_summary import maybe_summarize_result as _maybe_summarize_result
from .tool_timing import ToolTimer
from .types import UnknownToolError


# Legacy constructor default retained for API compatibility.  Primary tool
# results are bounded by the character-based spill boundary in
# ``tool_result_artifacts.py``.
_DEFAULT_MAX_RESULT_BYTES = 50_000


def _resolve_max_result_chars(value: int) -> int:
    """Clamp executor result cap to the non-configurable hard ceiling.

    Callers may choose a smaller cap for tests or embedded runtimes, but cannot
    raise provider-visible tool results above PREVENTIVE_MAX_CHARS.
    """
    return value if type(value) is int and 0 < value <= _DEFAULT_MAX_RESULT_CHARS else _DEFAULT_MAX_RESULT_CHARS


def _resolve_hint_threshold(value: int | None) -> int:
    """Resolve the large-result hint threshold from an optional caller-supplied value.

    ``None`` → default (``DEFAULT_SUMMARIZE_NOTIFICATION_THRESHOLD`` from messaging).
    ``<= 0`` → 0, meaning the hint is disabled.
    Non-int or invalid → fall back conservatively to the default.
    """
    from .base_agent.messaging import DEFAULT_SUMMARIZE_NOTIFICATION_THRESHOLD
    if value is None:
        return DEFAULT_SUMMARIZE_NOTIFICATION_THRESHOLD
    if not (type(value) is int):
        return DEFAULT_SUMMARIZE_NOTIFICATION_THRESHOLD
    if value <= 0:
        return 0
    return value


class ToolExecutor:
    """Executes tool calls sequentially or in parallel."""

    def __init__(
        self,
        dispatch_fn: Callable[[ToolCall], Any],
        make_tool_result_fn: Callable,
        guard: LoopGuard,
        known_tools: set[str] | None = None,
        parallel_safe_tools: set[str] | None = None,
        logger_fn: Callable | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        meta_fn: Callable[[], dict] | None = None,
        working_dir: Path | str | None = None,
        max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS,
        tool_call_guard: ToolCallGuard | None = None,
        summarize_notification_threshold: int | None = None,
        reconstruction_event_fn: Callable[[], dict | None] | None = None,
        summarizer_fn: Callable[[str, str, str, str | None], str] | None = None,
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._make_tool_result_fn = make_tool_result_fn
        self._guard = guard
        self._known_tools = known_tools or set()
        self._parallel_safe_tools = parallel_safe_tools or set()
        self._logger_fn = logger_fn
        self._max_result_bytes = max_result_bytes
        self._meta_fn = meta_fn or (lambda: {})
        self._working_dir = Path(working_dir) if working_dir is not None else None
        self._max_result_chars = _resolve_max_result_chars(max_result_chars)
        self._tool_call_guard = tool_call_guard or ToolCallGuard()
        self._current_api_call_id: str | None = None
        self._summarize_notification_threshold = _resolve_hint_threshold(
            summarize_notification_threshold
        )
        # One-shot delayed-summarize reconstruction event source (channel A).
        # When set, ``_attach_tool_block`` pops a pending event and attaches it
        # to the first dict result it stamps; the source is one-shot, so only
        # one result per reconstruction carries the event.
        self._reconstruction_event_fn = reconstruction_event_fn
        # A-priori (reasoning-driven) summarizer. When a tool call sets
        # ``summary=true``, ``_maybe_apriori_summary`` replaces the raw visible
        # payload with an LLM-generated summary AFTER the raw is durably logged
        # and BEFORE it reaches the wire. ``summarizer_fn(system_prompt,
        # user_prompt, tool_name, tool_call_id) -> str``. When None,
        # ``summary=true`` fails closed to a summary-layer error (the raw is
        # never dumped into context); see ``maybe_summarize_result``.
        self._summarizer_fn = summarizer_fn
        # Dedup memory for the current sustained-pressure molt EMISSION EVENT.
        # The reminder text is permanent (restamped on every tool result while
        # active), so the ``context_pressure_current_molt_reminder_emitted`` event
        # is logged at most once per distinct provider round (payload
        # ``last_round_id``) within this executor's lifetime (one executor per
        # turn — see ``base_agent/turn._make_tool_executor``), counting genuine
        # emissions instead of every permanent restamp.  ``object()`` is a
        # sentinel distinct from any real round id (incl. ``None``).
        self._last_current_molt_event_round: Any = object()

    def _tool_trace_id(self, tc: ToolCall) -> str:
        """Return the stable trace id for one top-level tool-call execution.

        Provider tool-call ids are already the right trace identity: they tie the
        model proposal to the required provider tool-result id.  Some tests or
        hand-built calls do not provide an id; give those an ephemeral id so all
        lifecycle events from that execution remain joinable without changing the
        provider-visible result id (which remains ``None``).
        """
        tc_id = getattr(tc, "id", None)
        if isinstance(tc_id, str) and tc_id:
            return tc_id
        return f"tool-{uuid.uuid4().hex}"

    def _log_lifecycle(
        self,
        event_type: str,
        *,
        tool_name: str,
        tool_call_id: str | None,
        tool_trace_id: str,
        **fields,
    ) -> None:
        self._log(
            event_type,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_trace_id=tool_trace_id,
            **fields,
        )

    def _prepare_args(self, tc: ToolCall, tool_trace_id: str) -> dict:
        """Normalize model-supplied tool args without changing execution semantics."""
        tc_id = getattr(tc, "id", None)
        raw_args = dict(tc.args) if tc.args else {}
        self._log_lifecycle(
            "tool_call_received",
            tool_name=tc.name,
            tool_call_id=tc_id,
            tool_trace_id=tool_trace_id,
            raw_arg_keys=sorted(str(key) for key in raw_args.keys()),
            raw_arg_count=len(raw_args),
        )

        args = dict(raw_args)
        removed_args: list[str] = []

        reasoning = None
        if "reasoning" in args:
            reasoning = args.pop("reasoning")
            removed_args.append("reasoning")
        for hidden_key in ("commentary", "_sync"):
            if hidden_key in args:
                args.pop(hidden_key, None)
                removed_args.append(hidden_key)
        deprecated_secondary = None
        if "secondary" in args:
            deprecated_secondary = args.pop("secondary")
            removed_args.append("secondary")
            self._log(
                "deprecated_secondary_ignored",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=tool_trace_id,
            )

        if reasoning:
            self._log(
                "tool_reasoning",
                tool=tc.name,
                reasoning=reasoning,
                tool_call_id=tc_id,
                tool_trace_id=tool_trace_id,
            )
            args["_reasoning"] = reasoning

        self._log_lifecycle(
            "tool_call_normalized",
            tool_name=tc.name,
            tool_call_id=tc_id,
            tool_trace_id=tool_trace_id,
            tool_args=args,
            removed_args=removed_args,
            deprecated_secondary_ignored=deprecated_secondary is not None,
        )
        return args


    def _tool_error_meta(self, result: dict) -> dict:
        """Return the nested, model-visible recovery block for an error result.

        Keep the legacy flat fields for compatibility, but also group the
        failure cause and recovery advice in one obvious block so the next LLM
        turn does not receive a bare/ambiguous ``status=error`` payload.
        """
        message = str(result.get("message") or "unknown error")
        error_phase = str(result.get("error_phase") or "unknown")
        error_type = str(result.get("error_type") or "ToolError")
        tool_name = str(result.get("tool_name") or "unknown")
        retryable = result.get("retryable", "unknown")
        guidance = [
            "Do not blindly retry the same tool call unchanged.",
            "Use the error message, phase, and argument keys to correct parameters or switch strategy.",
            "If the failure depends on mutable external state, read the current state before retrying.",
            "If the cause cannot be corrected, report the failure and ask for direction instead of looping.",
        ]
        if retryable is True:
            guidance.insert(
                1,
                "This error is marked retryable, but retry only after addressing the likely cause or waiting for the transient condition to clear.",
            )
        elif retryable is False:
            guidance.insert(
                1,
                "This error is marked non-retryable; change the request or strategy before any retry.",
            )
        return {
            "version": 1,
            "summary": message,
            "reason": f"{tool_name} failed during {error_phase}: {message}",
            "error_type": error_type,
            "error_phase": error_phase,
            "retryable": retryable,
            "tool_name": result.get("tool_name"),
            "tool_call_id": result.get("tool_call_id"),
            "tool_trace_id": result.get("tool_trace_id"),
            "arg_keys": result.get("arg_keys", []),
            "guidance": guidance,
        }

    def _attach_tool_error_meta(self, result: dict) -> dict:
        result.setdefault("tool_error", self._tool_error_meta(result))
        return result

    def _error_payload(
        self,
        *,
        tool_name: str,
        tool_call_id: str | None,
        tool_trace_id: str,
        tool_args: dict,
        error_phase: str,
        error_type: str,
        message: str,
        elapsed_ms: int | float,
        hint: str | None = None,
        traceback_tail: str | None = None,
        **extra: Any,
    ) -> dict:
        """Build a model-visible, self-repair-friendly tool error payload.

        Durable lifecycle logs remain the forensic source of truth; this payload
        gives the next model turn enough local context to understand what failed
        without forcing it to query logs first.
        """
        payload = {
            "status": "error",
            "message": message,
            "error_type": error_type,
            "error_phase": error_phase,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_trace_id": tool_trace_id,
            "tool_args": tool_args,
            "arg_keys": sorted(str(key) for key in tool_args.keys()),
            "elapsed_ms": elapsed_ms,
            "retryable": "unknown",
            "_tool_error_payload_version": 1,
        }
        if hint:
            payload["hint"] = hint
        if traceback_tail:
            payload["traceback_tail"] = traceback_tail
        payload.update(extra)
        return self._attach_tool_error_meta(payload)

    def _enrich_error_payload(
        self,
        result: dict,
        *,
        tool_name: str,
        tool_call_id: str | None,
        tool_trace_id: str,
        tool_args: dict,
        error_phase: str,
        error_type: str,
        elapsed_ms: int | float,
    ) -> dict:
        """Add trace/argument context to a tool-returned error result in place."""
        result.setdefault("error_type", error_type)
        result.setdefault("error_phase", error_phase)
        result.setdefault("tool_name", tool_name)
        result.setdefault("tool_call_id", tool_call_id)
        result.setdefault("tool_trace_id", tool_trace_id)
        result.setdefault("tool_args", tool_args)
        result.setdefault("arg_keys", sorted(str(key) for key in tool_args.keys()))
        result.setdefault("elapsed_ms", elapsed_ms)
        result.setdefault("retryable", "unknown")
        result.setdefault("_tool_error_payload_version", 1)
        return self._attach_tool_error_meta(result)

    def _traceback_tail(self, exc: Exception, *, max_chars: int = 4000) -> str:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(formatted) <= max_chars:
            return formatted
        return formatted[-max_chars:]

    def _attach_tool_call_progress(self, result: Any) -> Any:
        """Attach the batch-scoped ACTIVE-turn progress *notice* when present.

        The running counter (``active_turn_tool_calls``) is intentionally NOT
        written here: it lives under the sparse/update-driven
        ``_meta.agent_meta.active_turn_tool_calls`` snapshot (stamped by
        ``attach_active_runtime`` at the tool-batch boundary when material
        agent_meta changes).  Repeating the counter on every result left stale
        snapshots in history.

        The ``active_turn_tool_call_notice`` is a transient soft self-check that
        the guard only emits when a notice interval is crossed and clears after
        the batch — it is genuine batch-scoped advisory text the model should see
        on the result that triggered it, so it is preserved here.

        Non-dict payloads are left unchanged to avoid mutating legacy scalar
        tool semantics.
        """
        if isinstance(result, dict):
            progress = self._guard.progress_metadata()
            notice = progress.get("active_turn_tool_call_notice")
            if notice:
                result["active_turn_tool_call_notice"] = notice
        return result

    # Kernel-injected auxiliary keys that are NOT part of the tool's own
    # substantive payload. Excluded from the result-intrinsic
    # ``_meta.tool_meta.char_count`` count so size reflects the tool output, not
    # metadata layered on after.  The unified ``_meta`` envelope holds
    # tool_meta/agent_meta/guidance/notifications/notification_guidance; the
    # remaining keys are transient top-level scaffolding/advisories.
    _AUX_RESULT_KEYS = (
        "_meta",
        "_runtime_pending",
        "_advisory",
        "active_turn_tool_calls",
        "active_turn_tool_call_notice",
    )

    def _intrinsic_char_count(self, result: dict) -> int:
        """Serialized size of the result excluding kernel auxiliary keys.

        Builds a shallow copy without the aux keys (see ``_AUX_RESULT_KEYS``) and
        measures that, so the count is stable regardless of which metadata blocks
        happen to be present when ``_meta.tool_meta`` is stamped.
        """
        import json as _json
        intrinsic = {k: v for k, v in result.items() if k not in self._AUX_RESULT_KEYS}
        try:
            return len(_json.dumps(intrinsic, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return 0

    def _attach_tool_block(
        self,
        result: Any,
        *,
        tool_call_id: str | None,
        elapsed_ms: int | float,
        spilled_char_count: int | None = None,
        status: str | None = None,
    ) -> Any:
        """Inject the permanent ``_meta.tool_meta`` identity block into dict results.

        This block is tiny, written once, and survives context history. It records
        facts intrinsic to this specific tool result invocation. The tool name is
        intentionally omitted because it already appears on the ToolCallBlock.

        Fields:
          id                  — tool_call_id (or "<unknown>")
          timestamp           — UTC ISO completion timestamp
          current_time        — optional agent-aware current time (local timezone
                                when timezone_awareness=True); absent when
                                time_awareness=False
          char_count          — current model-visible serialized size: the kernel
                                ``_meta`` envelope and transient top-level
                                scaffolding (``_runtime_pending``, ``_advisory``,
                                batch progress notice) are excluded from the count.
          elapsed_ms          — execution time in milliseconds
          token_usage         — optional compact provider-round token/cache snapshot
                                (input, cache_miss, cache_rate, context_usage, window,
                                output, thinking) copied from the transient runtime
                                snapshot so it remains permanent per-result evidence
          spilled_char_count  — original sidecar character count when a spill occurred;
                                omitted for ordinary non-spilled results
          status              — "error" when the result carries status=error; omitted otherwise
          comment.overflow    — machine-generated per-result hint added only when the
                                visible payload is capped/large (see
                                ``_overflow_comment_applies``): where the full original
                                is preserved (``logs/events.jsonl`` by ``tool_call_id``),
                                how to retrieve it, and the summarize cleanup action.
        """
        if not isinstance(result, dict):
            return result
        meta = result.get("_meta")
        if isinstance(meta, dict) and "tool_meta" in meta:
            return result

        pending = result.get("_runtime_pending")
        token_usage = None
        current_time = None
        context_block = None
        current_molt_event = None
        if isinstance(pending, dict):
            candidate_time = pending.pop(TOOL_META_CURRENT_TIME_KEY, None)
            if isinstance(candidate_time, str) and candidate_time:
                current_time = candidate_time
            candidate = pending.pop(TOOL_META_TOKEN_USAGE_PENDING_KEY, None)
            if isinstance(candidate, dict):
                token_usage = dict(candidate)
            # Current sustained-pressure molt reminder — permanent per-result
            # metadata at tool_meta.context.molt (build_meta stashes it under a
            # transit key so it lands on tool_meta, not the sparse agent_meta).
            candidate_context = pending.pop(TOOL_META_CONTEXT_PENDING_KEY, None)
            if isinstance(candidate_context, dict) and candidate_context.get("molt"):
                context_block = {"molt": candidate_context["molt"]}
            # The matching one-shot emission-event payload rides alongside; it is
            # only present when build_meta decided this is a NEW emission (deduped
            # against agent state), so logging it here counts genuine emissions
            # rather than every permanent restamp.
            candidate_event = pending.pop(TOOL_META_CONTEXT_EVENT_PENDING_KEY, None)
            if isinstance(candidate_event, dict):
                current_molt_event = candidate_event

        char_count = self._intrinsic_char_count(result)

        tool_block: dict = {
            "id": tool_call_id or "<unknown>",
            "timestamp": now_iso_plain(),
            "char_count": char_count,
            "elapsed_ms": int(elapsed_ms),
        }
        if current_time:
            tool_block[TOOL_META_CURRENT_TIME_KEY] = current_time
        if token_usage:
            tool_block[TOOL_META_TOKEN_USAGE_KEY] = token_usage
        if context_block:
            tool_block[TOOL_META_CONTEXT_KEY] = context_block
        if spilled_char_count is not None:
            tool_block["spilled_char_count"] = int(spilled_char_count)
        if status == "error":
            tool_block["status"] = "error"

        # Capped/large visible results get a single ``comment.overflow`` hint
        # pointing at the preserved original.  Small ordinary results do not, so
        # the common path stays as tiny as before.
        if self._overflow_comment_applies(
            char_count=char_count, spilled_char_count=spilled_char_count
        ):
            tool_block[TOOL_META_COMMENT_KEY] = {
                TOOL_META_COMMENT_OVERFLOW_KEY: build_tool_meta_overflow_comment(
                    tool_call_id
                ),
            }

        if not isinstance(meta, dict):
            meta = {}
            result["_meta"] = meta
        meta["tool_meta"] = tool_block

        # Channel B event: the current sustained-pressure molt reminder was just
        # attached to tool_meta.context.molt.  Emit the structured runtime event
        # ONLY on a real attach (context_block present), and dedup to at most once
        # per provider round (the reminder text is permanent — restamped on every
        # result while active — so per-result emission would flood the log).
        # Best-effort: a log failure never breaks the turn.
        if context_block and isinstance(current_molt_event, dict):
            self._maybe_emit_current_molt_event(current_molt_event)

        # Channel A: attach the one-shot delayed-summarize reconstruction event
        # (permanent per-result evidence) to the first stamped result. The
        # source is one-shot — once it returns an event it returns None — so
        # exactly one tool result per reconstruction carries it.
        if self._reconstruction_event_fn is not None:
            try:
                event = self._reconstruction_event_fn()
            except Exception:
                event = None
            if event:
                tool_block["reconstruction"] = event
                # Channel A event: the reconstruction molt reminder text was just
                # attached at tool_meta.reconstruction.molt.  Emit only when the
                # event actually carries that text (still >= recovery target).
                molt_text = event.get("molt") if isinstance(event, dict) else None
                if isinstance(molt_text, str) and molt_text:
                    try:
                        descriptor = reconstruction_molt_emission_descriptor(
                            event, message=molt_text
                        )
                        self._emit_reminder_event(descriptor)
                    except Exception:
                        pass
        return result

    def _maybe_emit_current_molt_event(self, descriptor: dict) -> None:
        """Emit the current-molt event, deduped to once per provider round.

        The reminder text is permanent (restamped every result while active), so
        we log the emission event at most once per distinct ``last_round_id``
        within this executor's lifetime (one executor per turn), counting genuine
        emissions rather than every restamp.  Best-effort throughout.
        """
        try:
            if not isinstance(descriptor, dict):
                return
            payload = descriptor.get("payload")
            round_id = payload.get("last_round_id") if isinstance(payload, dict) else None
            if round_id == self._last_current_molt_event_round:
                return
            self._last_current_molt_event_round = round_id
            self._emit_reminder_event(descriptor)
        except Exception:
            pass

    def _emit_reminder_event(self, descriptor: dict) -> None:
        """Log a reminder-emission event from a ``{event_name, payload}`` descriptor.

        Best-effort: reminder-emission accounting must never break a turn (the
        reminder text is already attached to the wire regardless).  Uses the
        standard agent event log via ``self._log`` — no parallel sink.
        """
        try:
            if not isinstance(descriptor, dict):
                return
            event_name = descriptor.get("event_name")
            payload = descriptor.get("payload")
            if not event_name or not isinstance(payload, dict):
                return
            self._log(event_name, **payload)
        except Exception:
            pass

    def _overflow_comment_applies(
        self, *, char_count: int, spilled_char_count: int | None
    ) -> bool:
        """Decide whether a result earns a ``tool_meta.comment.overflow`` hint.

        Two cases count as "capped/large" and earn the hint:

        * **Spilled** — ``spilled_char_count`` is set, so the visible payload is a
          compact manifest standing in for an oversized original.
        * **Large but inline** — the visible ``char_count`` exceeds the executor's
          large-result hint threshold (``summarize_notification_threshold``), the
          same threshold that gates the batched large-result reminder.  When that
          threshold is disabled (``0``) only the spill case applies, so ordinary
          small results never carry the hint.
        """
        if spilled_char_count is not None:
            return True
        threshold = self._summarize_notification_threshold
        return threshold > 0 and char_count > threshold

    def _append_advisory(self, result: Any, advisory: dict[str, Any] | None) -> Any:
        if not isinstance(result, dict) or not advisory:
            return result
        existing = result.get("_advisory")
        if not existing:
            result["_advisory"] = advisory
            return result
        if isinstance(existing, dict) and existing.get("type") == "multiple_tool_advisories":
            existing.setdefault("items", []).append(advisory)
            existing["message"] = "Multiple tool-call advisories are attached."
            return result
        result["_advisory"] = {
            "type": "multiple_tool_advisories",
            "message": "Multiple tool-call advisories are attached.",
            "items": [existing, advisory],
        }
        return result

    def _attach_duplicate_advisory(self, result: Any, verdict: Any) -> Any:
        """Attach duplicate-call advisory metadata to dict results.

        LoopGuard duplicate detection is advisory-only: the call has already run
        (or at least reached its normal validation/dispatch path). Keep the
        provider-visible shape centralized so success, returned-error, and
        dispatch-error results all expose the same ``_advisory`` payload.
        """
        if isinstance(result, dict) and getattr(verdict, "warning", None):
            self._append_advisory(result, self._guard.advisory_metadata(verdict))
        return result

    def _proposal(self, tc: ToolCall, args: dict, trace_id: str) -> ToolProposal:
        return ToolProposal(
            tool_name=tc.name,
            tool_args=dict(args),
            tool_call_id=getattr(tc, "id", None),
            tool_trace_id=trace_id,
        )

    def _log_guard_approval(
        self,
        *,
        tc: ToolCall,
        trace_id: str,
        decision: GuardDecision,
    ) -> None:
        fields = {
            "tool_name": tc.name,
            "tool_call_id": getattr(tc, "id", None),
            "tool_trace_id": trace_id,
            "approval_mode": decision.approval_mode,
            "policy": decision.check_name,
        }
        if decision.is_structured:
            fields["guard_decision"] = decision.to_payload()
        self._log_lifecycle("tool_call_approved", **fields)

    def _attach_guard_advisory(
        self,
        result: Any,
        *,
        proposal: ToolProposal,
        decision: GuardDecision,
    ) -> Any:
        if isinstance(result, dict):
            self._append_advisory(result, decision.advisory_metadata(proposal))
        return result

    def _guard_rejection_result(
        self,
        *,
        tc: ToolCall,
        args: dict,
        trace_id: str,
        proposal: ToolProposal,
        decision: GuardDecision,
        elapsed_ms: int | float,
    ) -> dict:
        reason = decision.reason or f"Tool call {tc.name!r} denied by {decision.check_name}"
        payload = self._error_payload(
            tool_name=tc.name,
            tool_call_id=getattr(tc, "id", None),
            tool_trace_id=trace_id,
            tool_args=args,
            error_phase="guard",
            error_type="ToolCallGuardDenied",
            message=reason,
            elapsed_ms=elapsed_ms,
            guard_decision=decision.to_payload(proposal),
            guard_check=decision.check_name,
            guard_action=decision.action,
            guard_severity=decision.severity,
        )
        payload["_advisory"] = decision.advisory_metadata(proposal)
        return payload

    def _deny_tool_call(
        self,
        *,
        tc: ToolCall,
        args: dict,
        trace_id: str,
        proposal: ToolProposal,
        decision: GuardDecision,
        timer: ToolTimer,
        collected_errors: list[str],
    ) -> Any:
        elapsed = timer.elapsed_ms
        result = self._guard_rejection_result(
            tc=tc,
            args=args,
            trace_id=trace_id,
            proposal=proposal,
            decision=decision,
            elapsed_ms=elapsed,
        )
        stamp_meta(result, self._meta_fn(), elapsed)
        tc_id = getattr(tc, "id", None)
        self._log_lifecycle(
            "tool_call_denied",
            tool_name=tc.name,
            tool_call_id=tc_id,
            tool_trace_id=trace_id,
            guard_decision=decision.to_payload(proposal),
            reason=result["message"],
        )
        self._log_tool_result(
            tool_name=tc.name,
            tool_call_id=tc_id,
            tool_trace_id=trace_id,
            tool_args=args,
            status="error",
            elapsed_ms=elapsed,
            result=result,
            exception="ToolCallGuardDenied",
            exception_message=result["message"],
        )
        collected_errors.append(f"{tc.name}: {result['message']}")
        return self._build_result_message(
            tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
            status="error", elapsed_ms=elapsed,
        )

    def _maybe_apriori_summary(
        self,
        result: Any,
        *,
        args: dict,
        tool_name: str,
        tool_call_id: str | None,
    ) -> Any:
        """Apply the a-priori (``summary=true``) summary replacement.

        Must be called AFTER the raw result is durably logged (so the raw is
        preserved by ``tool_call_id``) and BEFORE ``_build_result_message``
        turns it into the wire message. When ``summary`` is not true the raw
        result is returned unchanged — current behavior is preserved exactly.
        When ``summary=true`` but no summarizer is wired this **fails closed**:
        it returns a summary-layer error/refusal carrying the raw-retrieval
        locator and never places the raw payload into context (the raw stays
        retrievable by ``tool_call_id``). See ``maybe_summarize_result``.
        """
        return _maybe_summarize_result(
            result,
            args=args,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            summarizer_fn=self._summarizer_fn,
            logger_fn=self._log,
        )

    def _build_result_message(
        self,
        tool_name: str,
        result: Any,
        *,
        tool_call_id: str | None,
        tool_trace_id: str,
        status: str | None = None,
        elapsed_ms: int | float = 0,
    ) -> Any:
        """Final boundary before a result reaches the LLM wire.

        Attaches ACTIVE-turn progress metadata and the permanent
        ``_meta.tool_meta`` identity block, then applies the unified character
        cap: oversized results
        are spilled to a sidecar artifact and replaced with a compact manifest.
        The manifest also receives progress metadata when dict-shaped.

        Notification pairs bypass this method — they are synthesized directly
        by ``BaseAgent._inject_notifications``.
        """
        self._attach_tool_call_progress(result)
        capped = _spill_oversized_result(
            result,
            max_chars=self._max_result_chars,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            working_dir=self._working_dir,
        )
        spilled = capped is not result
        spilled_char_count = None
        if spilled:
            if isinstance(capped, dict) and isinstance(capped.get("original_char_count"), int):
                spilled_char_count = capped["original_char_count"]
            self._attach_tool_call_progress(capped)
        if spilled and self._logger_fn is not None:
            try:
                self._logger_fn(
                    "tool_result_spilled",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tool_trace_id=tool_trace_id,
                    original_char_count=capped.get("original_char_count"),
                    spill_path=capped.get("spill_path"),
                )
            except Exception:
                pass
        # Attach permanent _meta.tool_meta identity block to the final (possibly spilled) result.
        self._attach_tool_block(
            capped,
            tool_call_id=tool_call_id,
            elapsed_ms=elapsed_ms,
            spilled_char_count=spilled_char_count,
            status=status,
        )
        msg = self._make_tool_result_fn(tool_name, capped, tool_call_id=tool_call_id)
        self._log_lifecycle(
            "tool_result_model_visible",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_trace_id=tool_trace_id,
            status=status,
            spilled=spilled,
            result_type=type(capped).__name__,
        )
        return msg

    def _log_tool_result(
        self,
        *,
        tool_name: str,
        tool_call_id: str | None,
        tool_trace_id: str,
        tool_args: dict,
        status: str,
        elapsed_ms: int | float,
        result: Any,
        **fields,
    ) -> None:
        self._log(
            "tool_result",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_trace_id=tool_trace_id,
            tool_args=tool_args,
            status=status,
            elapsed_ms=elapsed_ms,
            result=result,
            **fields,
        )
        self._log_lifecycle(
            "tool_result_durable_log_visible",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_trace_id=tool_trace_id,
            status=status,
            elapsed_ms=elapsed_ms,
            result_type=type(result).__name__,
        )

    @property
    def guard(self) -> LoopGuard:
        return self._guard

    @guard.setter
    def guard(self, value: LoopGuard) -> None:
        self._guard = value

    def _log(self, event_type: str, **fields) -> None:
        if self._current_api_call_id and "api_call_id" not in fields:
            fields["api_call_id"] = self._current_api_call_id
        if self._logger_fn:
            self._logger_fn(event_type, **fields)

    def execute(
        self,
        tool_calls: list[ToolCall],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
        collected_errors: list[str] | None = None,
        api_call_id: str | None = None,
    ) -> tuple[list, bool, str]:
        """Execute tool calls. Returns (results, intercepted, intercept_text).

        ``api_call_id`` identifies the LLM API response that produced this
        batch. When present it is stamped onto every tool lifecycle/result
        event for UI grouping and trace reconstruction.
        """
        if collected_errors is None:
            collected_errors = []

        previous_api_call_id = self._current_api_call_id
        self._current_api_call_id = api_call_id
        try:
            return self._execute_with_current_api_call_id(
                tool_calls,
                collected_errors,
                on_result_hook=on_result_hook,
                cancel_event=cancel_event,
            )
        finally:
            self._current_api_call_id = previous_api_call_id

    def _execute_with_current_api_call_id(
        self,
        tool_calls: list[ToolCall],
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
    ) -> tuple[list, bool, str]:
        all_parallel_safe = (
            len(tool_calls) > 1
            and self._parallel_safe_tools
            and all(tc.name in self._parallel_safe_tools for tc in tool_calls)
        )

        if all_parallel_safe:
            return self._execute_parallel(
                tool_calls, collected_errors,
                on_result_hook=on_result_hook,
                cancel_event=cancel_event,
            )
        else:
            return self._execute_sequential(
                tool_calls, collected_errors,
                on_result_hook=on_result_hook,
                cancel_event=cancel_event,
            )

    def _execute_single(
        self,
        tc: ToolCall,
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
    ) -> tuple[Any, bool, str]:
        tc_id = getattr(tc, "id", None)
        trace_id = self._tool_trace_id(tc)
        args = self._prepare_args(tc, trace_id)

        verdict = self._guard.record_tool_call(tc.name, args)
        proposal = None
        decision = None

        timer = ToolTimer()
        try:
            # Pre-check for unknown tool (records in guard for limit tracking)
            if self._known_tools and tc.name not in self._known_tools:
                self._guard.record_invalid_tool(tc.name)
                self._log_lifecycle(
                    "tool_call_validation_failed",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    reason="unknown_tool",
                )
                err_result = self._error_payload(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    error_phase="validation",
                    error_type="UnknownToolError",
                    message=str(UnknownToolError(tc.name)),
                    elapsed_ms=timer.elapsed_ms,
                    validation_reason="unknown_tool",
                    available_tools=sorted(self._known_tools),
                )
                stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
                self._log_tool_result(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    status="error",
                    elapsed_ms=timer.elapsed_ms,
                    result=err_result,
                    exception="UnknownToolError",
                    exception_message=err_result["message"],
                )
                result_msg = self._build_result_message(
                    tc.name, err_result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status="error", elapsed_ms=timer.elapsed_ms,
                )
                collected_errors.append(f"{tc.name}: {err_result['message']}")
                return result_msg, False, ""

            proposal = self._proposal(tc, args, trace_id)
            decision = self._tool_call_guard.evaluate(proposal)
            if not decision.allowed:
                result_msg = self._deny_tool_call(
                    tc=tc,
                    args=args,
                    trace_id=trace_id,
                    proposal=proposal,
                    decision=decision,
                    timer=timer,
                    collected_errors=collected_errors,
                )
                return result_msg, False, ""

            self._log_guard_approval(tc=tc, trace_id=trace_id, decision=decision)
            self._log(
                "tool_call",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
            )
            self._log_lifecycle(
                "tool_call_dispatch_start",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
            )
            with timer:
                result = self._dispatch_fn(
                    ToolCall(name=tc.name, args=args, id=tc_id)
                )

            if isinstance(result, dict):
                stamp_meta(result, self._meta_fn(), timer.elapsed_ms)
                self._attach_duplicate_advisory(result, verdict)
                self._attach_guard_advisory(result, proposal=proposal, decision=decision)

            status = result.get("status", "success") if isinstance(result, dict) else "success"
            if status == "error" and isinstance(result, dict):
                self._enrich_error_payload(
                    result,
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    error_phase="tool_returned_error",
                    error_type=str(result.get("error_type") or result.get("tool") or "ToolReturnedError"),
                    elapsed_ms=timer.elapsed_ms,
                )
            self._log_lifecycle(
                "tool_call_dispatch_done",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                status=status,
                elapsed_ms=timer.elapsed_ms,
            )
            self._log_tool_result(
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )

            if isinstance(result, dict) and result.get("intercept"):
                # Intercept results carry kernel control flags; never summarize.
                intercept_text = result.get("text", "")
                result_msg = self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status=status, elapsed_ms=timer.elapsed_ms,
                )
                return result_msg, True, intercept_text

            # A-priori summary: the raw result is already durably logged above
            # (preserved by tool_call_id); replace the visible payload with the
            # generated summary before it reaches the wire. No-op unless the call
            # set summary=true and a summarizer is wired.
            visible = self._maybe_apriori_summary(
                result, args=args, tool_name=tc.name, tool_call_id=tc_id,
            )
            result_msg = self._build_result_message(
                tc.name, visible, tool_call_id=tc_id, tool_trace_id=trace_id,
                status=status, elapsed_ms=timer.elapsed_ms,
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err_msg = result.get("message", "unknown error")
                collected_errors.append(f"{tc.name}: {err_msg}")

            if on_result_hook is not None:
                hook_result = getattr(result_msg, "content", None)
                if hook_result is None:
                    hook_result = result_msg.get("result", result_msg) if isinstance(result_msg, dict) else result_msg
                intercept = on_result_hook(tc.name, args, hook_result, tool_call_id=tc_id)
                if intercept is not None:
                    return result_msg, True, intercept

            return result_msg, False, ""

        except Exception as e:
            self._log_lifecycle(
                "tool_call_dispatch_failed",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                elapsed_ms=timer.elapsed_ms,
                exception=type(e).__name__,
                exception_message=str(e),
            )
            err_result = self._error_payload(
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
                error_phase="dispatch",
                error_type=type(e).__name__,
                exception_type=type(e).__name__,
                message=str(e),
                elapsed_ms=timer.elapsed_ms,
                traceback_tail=self._traceback_tail(e),
            )
            stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
            self._attach_duplicate_advisory(err_result, verdict)
            if proposal is not None and decision is not None:
                self._attach_guard_advisory(err_result, proposal=proposal, decision=decision)
            self._log_tool_result(
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
                status="error",
                elapsed_ms=timer.elapsed_ms,
                result=err_result,
                exception=type(e).__name__,
                exception_message=str(e),
            )
            result_msg = self._build_result_message(
                tc.name, err_result, tool_call_id=tc_id, tool_trace_id=trace_id,
                status="error", elapsed_ms=timer.elapsed_ms,
            )
            collected_errors.append(f"{tc.name}: {e}")
            return result_msg, False, ""

    def _execute_sequential(
        self,
        tool_calls: list[ToolCall],
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
    ) -> tuple[list, bool, str]:
        tool_results = []
        for tc in tool_calls:
            if cancel_event is not None and cancel_event.is_set():
                return [], False, ""
            result_msg, intercepted, intercept_text = self._execute_single(
                tc, collected_errors, on_result_hook=on_result_hook,
            )
            if result_msg is not None:
                tool_results.append(result_msg)
            if intercepted:
                return tool_results, True, intercept_text
        return tool_results, False, ""

    def _execute_parallel(
        self,
        tool_calls: list[ToolCall],
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
    ) -> tuple[list, bool, str]:
        # Phase 1: Pre-check duplicates (sequential — guard not thread-safe)
        to_execute: list[tuple[int, ToolCall, dict, str, Any, GuardDecision, ToolProposal]] = []
        tool_results: list[tuple[int, Any]] = []

        for i, tc in enumerate(tool_calls):
            tc_id = getattr(tc, "id", None)
            trace_id = self._tool_trace_id(tc)
            args = self._prepare_args(tc, trace_id)

            verdict = self._guard.record_tool_call(tc.name, args)
            if self._known_tools and tc.name not in self._known_tools:
                self._guard.record_invalid_tool(tc.name)
                self._log_lifecycle(
                    "tool_call_validation_failed",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    reason="unknown_tool",
                )
                result = self._error_payload(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    error_phase="validation",
                    error_type="UnknownToolError",
                    message=str(UnknownToolError(tc.name)),
                    elapsed_ms=0,
                    validation_reason="unknown_tool",
                    available_tools=sorted(self._known_tools),
                )
                stamp_meta(result, self._meta_fn(), 0)
                self._log_tool_result(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    status="error",
                    elapsed_ms=0,
                    result=result,
                    exception="UnknownToolError",
                    exception_message=result["message"],
                )
                tool_results.append((i, self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status="error", elapsed_ms=0,
                )))
                collected_errors.append(f"{tc.name}: {result['message']}")
            else:
                proposal = self._proposal(tc, args, trace_id)
                decision = self._tool_call_guard.evaluate(proposal)
                if not decision.allowed:
                    result_msg = self._deny_tool_call(
                        tc=tc,
                        args=args,
                        trace_id=trace_id,
                        proposal=proposal,
                        decision=decision,
                        timer=ToolTimer(),
                        collected_errors=collected_errors,
                    )
                    tool_results.append((i, result_msg))
                    continue

                self._log_guard_approval(tc=tc, trace_id=trace_id, decision=decision)
                to_execute.append((i, tc, args, trace_id, verdict, decision, proposal))

        if not to_execute:
            tool_results.sort(key=lambda x: x[0])
            return [r for _, r in tool_results], False, ""

        # Phase 2: Execute in parallel
        results_map: dict[int, Any] = {}
        errors_map: dict[int, dict] = {}
        elapsed_map: dict[int, int] = {}

        def _run_one(
            index: int,
            tc: ToolCall,
            args: dict,
            trace_id: str,
            verdict,
            decision: GuardDecision,
            proposal: ToolProposal,
        ):
            tc_id = getattr(tc, "id", None)
            self._log(
                "tool_call",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
            )
            self._log_lifecycle(
                "tool_call_dispatch_start",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
            )
            timer = ToolTimer()
            try:
                with timer:
                    result = self._dispatch_fn(
                        ToolCall(name=tc.name, args=args, id=tc.id)
                    )
            except Exception as e:
                self._log_lifecycle(
                    "tool_call_dispatch_failed",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    elapsed_ms=timer.elapsed_ms,
                    exception=type(e).__name__,
                    exception_message=str(e),
                )
                err_result = self._error_payload(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    error_phase="dispatch",
                    error_type=type(e).__name__,
                    exception_type=type(e).__name__,
                    message=str(e),
                    elapsed_ms=timer.elapsed_ms,
                    traceback_tail=self._traceback_tail(e),
                )
                stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
                self._attach_duplicate_advisory(err_result, verdict)
                self._attach_guard_advisory(err_result, proposal=proposal, decision=decision)
                self._log_tool_result(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    status="error",
                    elapsed_ms=timer.elapsed_ms,
                    result=err_result,
                    exception=type(e).__name__,
                    exception_message=str(e),
                )
                return index, err_result, timer.elapsed_ms
            if isinstance(result, dict):
                stamp_meta(result, self._meta_fn(), timer.elapsed_ms)
                self._attach_duplicate_advisory(result, verdict)
                self._attach_guard_advisory(result, proposal=proposal, decision=decision)
            status = result.get("status", "success") if isinstance(result, dict) else "success"
            if status == "error" and isinstance(result, dict):
                self._enrich_error_payload(
                    result,
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    error_phase="tool_returned_error",
                    error_type=str(result.get("error_type") or result.get("tool") or "ToolReturnedError"),
                    elapsed_ms=timer.elapsed_ms,
                )
            self._log_lifecycle(
                "tool_call_dispatch_done",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                status=status,
                elapsed_ms=timer.elapsed_ms,
            )
            self._log_tool_result(
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )
            return index, result, timer.elapsed_ms

        pool = ThreadPoolExecutor(max_workers=len(to_execute))
        try:
            futures = {
                pool.submit(_run_one, i, tc, args, trace_id, verdict, decision, proposal): i
                for i, tc, args, trace_id, verdict, decision, proposal in to_execute
            }
            for future in as_completed(futures, timeout=300.0):
                if cancel_event is not None and cancel_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return [], False, ""
                try:
                    idx, result, elapsed_ms = future.result()
                    results_map[idx] = result
                    elapsed_map[idx] = elapsed_ms
                except Exception as e:
                    idx = futures[future]
                    tc_entry = next(
                        (
                            (tc, args, trace_id, _verdict, _decision, _proposal)
                            for i, tc, args, trace_id, _verdict, _decision, _proposal in to_execute
                            if i == idx
                        ),
                        None,
                    )
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args = tc_entry[1] if tc_entry else {}
                    trace_id = tc_entry[2] if tc_entry else f"tool-{uuid.uuid4().hex}"
                    verdict = tc_entry[3] if tc_entry else None
                    decision = tc_entry[4] if tc_entry else GuardDecision.allow()
                    proposal = tc_entry[5] if tc_entry else ToolProposal(
                        tool_name=tc_name,
                        tool_args=tc_args,
                        tool_call_id=tc_id,
                        tool_trace_id=trace_id,
                    )
                    err_result = self._error_payload(
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_trace_id=trace_id,
                        tool_args=tc_args,
                        error_phase="parallel_future",
                        error_type=type(e).__name__,
                        exception_type=type(e).__name__,
                        message=str(e),
                        elapsed_ms=0,
                        traceback_tail=self._traceback_tail(e),
                    )
                    stamp_meta(err_result, self._meta_fn(), 0)
                    self._attach_duplicate_advisory(err_result, verdict)
                    self._attach_guard_advisory(err_result, proposal=proposal, decision=decision)
                    errors_map[idx] = err_result
                    self._log_lifecycle(
                        "tool_call_dispatch_failed",
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_trace_id=trace_id,
                        elapsed_ms=0,
                        exception=type(e).__name__,
                        exception_message=str(e),
                    )
                    self._log_tool_result(
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_trace_id=trace_id,
                        tool_args=tc_args,
                        status="error",
                        elapsed_ms=0,
                        result=err_result,
                        exception=type(e).__name__,
                        exception_message=str(e),
                    )
        except TimeoutError:
            for future, idx in futures.items():
                if idx not in results_map and idx not in errors_map:
                    tc_entry = next(
                        (
                            (tc, args, trace_id, _verdict, _decision, _proposal)
                            for i, tc, args, trace_id, _verdict, _decision, _proposal in to_execute
                            if i == idx
                        ),
                        None,
                    )
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id_t = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args_t = tc_entry[1] if tc_entry else {}
                    trace_id_t = tc_entry[2] if tc_entry else f"tool-{uuid.uuid4().hex}"
                    verdict_t = tc_entry[3] if tc_entry else None
                    decision_t = tc_entry[4] if tc_entry else GuardDecision.allow()
                    proposal_t = tc_entry[5] if tc_entry else ToolProposal(
                        tool_name=tc_name,
                        tool_args=tc_args_t,
                        tool_call_id=tc_id_t,
                        tool_trace_id=trace_id_t,
                    )
                    err_result = self._error_payload(
                        tool_name=tc_name,
                        tool_call_id=tc_id_t,
                        tool_trace_id=trace_id_t,
                        tool_args=tc_args_t,
                        error_phase="timeout",
                        error_type="TimeoutError",
                        exception_type="TimeoutError",
                        message="Timed out",
                        elapsed_ms=0,
                        retryable=True,
                    )
                    stamp_meta(err_result, self._meta_fn(), 0)
                    self._attach_duplicate_advisory(err_result, verdict_t)
                    self._attach_guard_advisory(err_result, proposal=proposal_t, decision=decision_t)
                    errors_map[idx] = err_result
                    self._log_lifecycle(
                        "tool_call_dispatch_failed",
                        tool_name=tc_name,
                        tool_call_id=tc_id_t,
                        tool_trace_id=trace_id_t,
                        elapsed_ms=0,
                        exception="TimeoutError",
                        exception_message="Timed out",
                    )
                    self._log_tool_result(
                        tool_name=tc_name,
                        tool_call_id=tc_id_t,
                        tool_trace_id=trace_id_t,
                        tool_args=tc_args_t,
                        status="error",
                        elapsed_ms=0,
                        result=err_result,
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        def _elapsed_ms_from_result(result: Any) -> int:
            if not isinstance(result, dict):
                return 0
            pending = result.get("_runtime_pending")
            if isinstance(pending, dict):
                pending_elapsed = pending.get("elapsed_ms")
                if isinstance(pending_elapsed, (int, float)):
                    return int(pending_elapsed)
            elapsed = result.get("elapsed_ms", 0)
            return int(elapsed) if isinstance(elapsed, (int, float)) else 0

        # Phase 3: Build result messages (sequential) and invoke the result hook.
        # The hook sees results in input order (same as sequential execution) so
        # notification/intercept semantics are consistent across both paths.
        for i, tc, args, trace_id, verdict, decision, proposal in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                status = result.get("status", "success") if isinstance(result, dict) else "success"
                _elapsed = elapsed_map.get(i, _elapsed_ms_from_result(result))
                # A-priori summary: raw already durably logged in Phase 2; replace
                # the visible payload before the wire. Intercept results are
                # guarded out inside the summarizer, but keep error/intercept
                # control checks below against the RAW result, not the summary.
                visible = result
                if not (isinstance(result, dict) and result.get("intercept")):
                    visible = self._maybe_apriori_summary(
                        result, args=args, tool_name=tc.name, tool_call_id=tc_id,
                    )
                result_msg = self._build_result_message(
                    tc.name, visible, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status=status, elapsed_ms=_elapsed,
                )
                tool_results.append((i, result_msg))
                if isinstance(result, dict) and result.get("status") == "error":
                    err_msg = result.get("message", "unknown error")
                    collected_errors.append(f"{tc.name}: {err_msg}")
                if isinstance(result, dict) and result.get("intercept"):
                    tool_results.sort(key=lambda x: x[0])
                    return (
                        [r for _, r in tool_results],
                        True,
                        result.get("text", ""),
                    )
                if on_result_hook is not None:
                    hook_result = getattr(result_msg, "content", None)
                    if hook_result is None:
                        hook_result = result_msg.get("result", result_msg) if isinstance(result_msg, dict) else result_msg
                    intercept = on_result_hook(tc.name, args, hook_result, tool_call_id=tc_id)
                    if intercept is not None:
                        tool_results.sort(key=lambda x: x[0])
                        return [r for _, r in tool_results], True, intercept
            elif i in errors_map:
                err_result = errors_map[i]
                err_msg = str(err_result.get("message", "unknown error"))
                _elapsed = elapsed_map.get(i, _elapsed_ms_from_result(err_result))
                tool_results.append((i, self._build_result_message(
                    tc.name, err_result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status="error", elapsed_ms=_elapsed,
                )))
                collected_errors.append(f"{tc.name}: {err_msg}")

        tool_results.sort(key=lambda x: x[0])
        return [r for _, r in tool_results], False, ""
