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
from .tool_timing import ToolTimer
from .types import UnknownToolError


# Legacy constructor default retained for API compatibility.  Primary tool
# results are bounded by the character-based spill boundary in
# ``tool_result_artifacts.py``.
_DEFAULT_MAX_RESULT_BYTES = 50_000


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
        self._max_result_chars = max_result_chars
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
        return payload

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
        return result

    def _traceback_tail(self, exc: Exception, *, max_chars: int = 4000) -> str:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(formatted) <= max_chars:
            return formatted
        return formatted[-max_chars:]

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

        Applies the unified character cap (``_DEFAULT_MAX_RESULT_CHARS``):
        results that serialize beyond the cap are spilled to a sidecar
        artifact under ``<workdir>/tmp/tool-results/`` and replaced with a
        compact manifest pointing at the file.  The artifact stores the full
        post-dispatch result.  Notification pairs do not pass through this
        method — they are synthesized directly by
        ``BaseAgent._inject_notifications`` and bypass ``ToolExecutor``.
        """
        capped = _spill_oversized_result(
            result,
            max_chars=self._max_result_chars,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            working_dir=self._working_dir,
        )
        spilled = capped is not result
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
        if verdict.blocked:
            self._log_lifecycle(
                "tool_call_validation_failed",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                reason="duplicate_call",
                duplicate_count=verdict.count,
            )
            result = {
                "status": "blocked",
                "_duplicate_warning": verdict.warning,
                "message": f"Execution skipped — duplicate call #{verdict.count}",
            }
            self._log_tool_result(
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                tool_args=args,
                status="blocked",
                elapsed_ms=0,
                result=result,
                duplicate_count=verdict.count,
            )
            msg = self._build_result_message(
                tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                status="blocked",
            )
            return msg, False, ""

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

            self._log_lifecycle(
                "tool_call_approved",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_trace_id=trace_id,
                approval_mode="pass_through",
                policy="default_allow",
            )
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

            if verdict.warning and isinstance(result, dict):
                result["_duplicate_warning"] = verdict.warning

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
                intercept = on_result_hook(tc.name, args, result)
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
        to_execute: list[tuple[int, ToolCall, dict, str]] = []
        tool_results: list[tuple[int, Any]] = []

        for i, tc in enumerate(tool_calls):
            tc_id = getattr(tc, "id", None)
            trace_id = self._tool_trace_id(tc)
            args = self._prepare_args(tc, trace_id)

            verdict = self._guard.record_tool_call(tc.name, args)
            if verdict.blocked:
                self._log_lifecycle(
                    "tool_call_validation_failed",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    reason="duplicate_call",
                    duplicate_count=verdict.count,
                )
                result = {
                    "status": "blocked",
                    "_duplicate_warning": verdict.warning,
                    "message": f"Execution skipped — duplicate call #{verdict.count}",
                }
                self._log_tool_result(
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    tool_args=args,
                    status="blocked",
                    elapsed_ms=0,
                    result=result,
                    duplicate_count=verdict.count,
                )
                tool_results.append((i, self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status="blocked",
                )))
            elif self._known_tools and tc.name not in self._known_tools:
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
                self._log_lifecycle(
                    "tool_call_approved",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_trace_id=trace_id,
                    approval_mode="pass_through",
                    policy="default_allow",
                )
                to_execute.append((i, tc, args, trace_id))

        if not to_execute:
            tool_results.sort(key=lambda x: x[0])
            return [r for _, r in tool_results], False, ""

        # Phase 2: Execute in parallel
        results_map: dict[int, Any] = {}
        errors_map: dict[int, dict] = {}

        def _run_one(index: int, tc: ToolCall, args: dict, trace_id: str):
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
                pool.submit(_run_one, i, tc, args, trace_id): i
                for i, tc, args, trace_id in to_execute
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
                            (tc, args, trace_id)
                            for i, tc, args, trace_id in to_execute
                            if i == idx
                        ),
                        None,
                    )
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args = tc_entry[1] if tc_entry else {}
                    trace_id = tc_entry[2] if tc_entry else f"tool-{uuid.uuid4().hex}"
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
                            (tc, args, trace_id)
                            for i, tc, args, trace_id in to_execute
                            if i == idx
                        ),
                        None,
                    )
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id_t = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args_t = tc_entry[1] if tc_entry else {}
                    trace_id_t = tc_entry[2] if tc_entry else f"tool-{uuid.uuid4().hex}"
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

        # Phase 3: Build result messages (sequential)
        for i, tc, args, trace_id in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                status = result.get("status", "success") if isinstance(result, dict) else "success"
                tool_results.append((i, self._build_result_message(
                    tc.name, result, tool_call_id=tc_id, tool_trace_id=trace_id,
                    status=status,
                )))
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
