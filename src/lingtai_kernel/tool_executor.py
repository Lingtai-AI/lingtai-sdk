"""ToolExecutor — sequential and parallel tool call execution."""
from __future__ import annotations

import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .llm.base import ToolCall
from .loop_guard import LoopGuard
from .meta_block import stamp_meta
from .tool_result_artifacts import (
    PREVENTIVE_MAX_CHARS as _DEFAULT_MAX_RESULT_CHARS,
    spill_oversized_result as _spill_oversized_result,
)
from .tool_call_guard import GuardDecision, ToolCallGuard, ToolProposal
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
        """Attach ACTIVE-turn tool-call progress metadata when possible.

        Tool-result metadata is already dictionary-based in LingTai's main
        result path.  Preserve non-dict payloads to avoid changing legacy
        scalar tool semantics.
        """
        if isinstance(result, dict):
            result.update(self._guard.progress_metadata())
        return result

    @staticmethod
    def _append_advisory(result: Any, advisory: dict[str, Any] | None) -> Any:
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
            tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id, status="error"
        )

    def _build_result_message(
        self,
        tool_name: str,
        result: Any,
        *,
        tool_call_id: str | None,
        tool_trace_id: str,
        status: str | None = None,
    ) -> Any:
        """Final boundary before a result reaches the LLM wire.

        Adds ACTIVE-turn tool-call progress metadata, then applies the unified
        character cap (``PREVENTIVE_MAX_CHARS``): results that serialize
        beyond the cap are spilled to a sidecar artifact under
        ``<workdir>/tmp/tool-results/`` and replaced with a compact manifest
        pointing at the file.  The compact manifest also receives progress
        metadata so the model-visible result always carries the counter when the
        payload is dict-shaped.  Notification pairs do not pass through this
        method — they are synthesized directly by ``BaseAgent._inject_notifications``
        and bypass ``ToolExecutor``.
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
        if spilled:
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
                    status="error",
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
                intercept_text = result.get("text", "")
                result_msg = self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status=status,
                )
                return result_msg, True, intercept_text

            result_msg = self._build_result_message(
                tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                status=status,
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
                status="error",
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
                    status="error",
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
                return index, err_result
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
            return index, result

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
                    idx, result = future.result()
                    results_map[idx] = result
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

        # Phase 3: Build result messages (sequential) and invoke the result hook.
        # The hook sees results in input order (same as sequential execution) so
        # notification/intercept semantics are consistent across both paths.
        for i, tc, args, trace_id, verdict, decision, proposal in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                status = result.get("status", "success") if isinstance(result, dict) else "success"
                result_msg = self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status=status,
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
                tool_results.append((i, self._build_result_message(
                    tc.name, err_result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status="error",
                )))
                collected_errors.append(f"{tc.name}: {err_msg}")

        tool_results.sort(key=lambda x: x[0])
        return [r for _, r in tool_results], False, ""
