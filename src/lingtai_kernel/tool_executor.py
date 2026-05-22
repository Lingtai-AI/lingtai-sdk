"""ToolExecutor — sequential and parallel tool call execution."""
from __future__ import annotations

import copy
import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .llm.base import ToolCall
from .loop_guard import LoopGuard
from .meta_block import stamp_meta
from .secondary_tools import (
    SECONDARY_ALLOWED_ACTIONS,
    SECONDARY_ALLOWED_TOOLS,
    SECONDARY_EXCLUDED_PRIMARY_TOOLS,
    SECONDARY_READ_RESULT_MAX_BYTES,
)
from .tool_timing import ToolTimer
from .types import UnknownToolError


def _short_message(message: Any, limit: int = 240) -> str:
    text = str(message)
    return text if len(text) <= limit else text[:limit] + "..."


def _secondary_summary(
    *,
    tool: str | None,
    action: str | None,
    status: str,
    message: str | None = None,
    result: Any | None = None,
) -> dict:
    summary = {"status": status}
    if tool:
        summary["tool"] = tool
    if action:
        summary["action"] = action
    if message:
        summary["message"] = _short_message(message)
    if result is not None:
        summary["result"] = _truncate_result(result, SECONDARY_READ_RESULT_MAX_BYTES)
    return summary


def _attach_secondary_result(result: Any, secondary: dict | None) -> Any:
    """Attach secondary outcome under the reserved top-level ``_secondary`` key.

    Kernel tool-result metadata is provider-visible content today: ``stamp_meta``
    already writes timing/context fields into dict-shaped tool results.  Follow
    that convention with a flat reserved key instead of inventing a separate
    ``_meta`` namespace.  Non-dict primary payloads are wrapped only when a
    secondary exists, because there is otherwise nowhere provider-visible to
    place the outcome.
    """
    if secondary is None:
        return result
    if isinstance(result, dict):
        result["_secondary"] = secondary
        return result
    return {"result": result, "_secondary": secondary}


def _contains_secondary_key(value: Any) -> bool:
    """Return True if a nested payload contains a key literally named secondary."""
    if isinstance(value, dict):
        return any(key == "secondary" or _contains_secondary_key(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secondary_key(item) for item in value)
    return False

# Default max tool result size: 50 KB.
_DEFAULT_MAX_RESULT_BYTES = 50_000


def _truncate_result(result: Any, max_bytes: int) -> Any:
    """Truncate a tool result if its serialized size exceeds max_bytes."""
    if max_bytes <= 0:
        return result
    if isinstance(result, str):
        if len(result) > max_bytes:
            return result[:max_bytes] + f"\n\n[truncated — showing first {max_bytes} of {len(result)} bytes]"
        return result
    if isinstance(result, dict):
        serialized = _json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) <= max_bytes:
            return result
        # Truncate the largest string/list values
        truncated = dict(result)
        patches = {}
        for key, val in truncated.items():
            if isinstance(val, str) and len(val) > max_bytes // 2:
                patches[key] = val[:max_bytes // 2] + f"\n[truncated — {len(val)} bytes total]"
            elif isinstance(val, list) and len(_json.dumps(val, ensure_ascii=False, default=str)) > max_bytes // 2:
                kept = []
                size = 0
                for item in val:
                    item_size = len(_json.dumps(item, ensure_ascii=False, default=str))
                    if size + item_size > max_bytes // 2:
                        break
                    kept.append(item)
                    size += item_size
                patches[key] = kept
                patches[f"_{key}_truncated"] = f"showing {len(kept)} of {len(val)} items"
        truncated.update(patches)
        return truncated
    return result


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
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._make_tool_result_fn = make_tool_result_fn
        self._guard = guard
        self._known_tools = known_tools or set()
        self._parallel_safe_tools = parallel_safe_tools or set()
        self._logger_fn = logger_fn
        self._max_result_bytes = max_result_bytes
        self._meta_fn = meta_fn or (lambda: {})

    def _validate_secondary(self, secondary: Any) -> tuple[str | None, dict | None, dict | None]:
        """Validate a nested secondary spec.

        Returns ``(tool_name, args, summary)``.  ``summary`` is non-None when
        validation failed and no dispatch should happen.
        """
        if secondary is None:
            return None, None, None
        if not isinstance(secondary, dict):
            return None, None, _secondary_summary(
                tool=None,
                action=None,
                status="error",
                message="secondary must be an object with tool and args",
            )
        tool_name = secondary.get("tool")
        args = secondary.get("args")
        if not isinstance(tool_name, str) or not tool_name:
            return None, None, _secondary_summary(
                tool=None,
                action=None,
                status="error",
                message="secondary.tool must be a non-empty string",
            )
        if tool_name not in SECONDARY_ALLOWED_TOOLS:
            return tool_name, None, _secondary_summary(
                tool=tool_name,
                action=None,
                status="error",
                message="secondary tool is not allowed",
            )
        if self._known_tools and tool_name not in self._known_tools:
            return tool_name, None, _secondary_summary(
                tool=tool_name,
                action=None,
                status="error",
                message="secondary tool is not registered for this agent",
            )
        if not isinstance(args, dict):
            return tool_name, None, _secondary_summary(
                tool=tool_name,
                action=None,
                status="error",
                message="secondary.args must be an object",
            )
        sanitized_args = copy.deepcopy(args)
        sanitized_args.pop("reasoning", None)
        sanitized_args.pop("commentary", None)
        sanitized_args.pop("_sync", None)
        action = sanitized_args.get("action")
        if not isinstance(action, str) or action not in SECONDARY_ALLOWED_ACTIONS[tool_name]:
            return tool_name, sanitized_args, _secondary_summary(
                tool=tool_name,
                action=action if isinstance(action, str) else None,
                status="error",
                message="secondary action is not allowed",
            )
        if _contains_secondary_key(sanitized_args):
            return tool_name, sanitized_args, _secondary_summary(
                tool=tool_name,
                action=action,
                status="error",
                message="recursive secondary calls are forbidden",
            )
        return tool_name, sanitized_args, None

    def _execute_secondary(
        self,
        secondary: Any,
        *,
        primary_tool: str,
        primary_tool_call_id: str | None,
    ) -> dict | None:
        """Run a validated secondary communication call before its primary.

        Secondary exists only for timely human replies during long primary work.
        It deliberately bypasses LoopGuard and never raises to the primary path;
        failures are summarized in the primary tool-result metadata.
        """
        if secondary is None:
            return None
        if primary_tool in SECONDARY_EXCLUDED_PRIMARY_TOOLS:
            summary = _secondary_summary(
                tool=None,
                action=None,
                status="error",
                message=f"primary tool {primary_tool!r} may not carry a secondary",
            )
            self._log(
                "tool_result",
                tool_name="secondary",
                tool_call_id=None,
                tool_args={},
                status="error",
                elapsed_ms=0,
                result=summary,
                secondary_for=primary_tool,
                primary_tool_call_id=primary_tool_call_id,
            )
            return summary
        tool_name, args, validation_summary = self._validate_secondary(secondary)
        if validation_summary is not None:
            self._log(
                "tool_result",
                tool_name=tool_name or "secondary",
                tool_call_id=None,
                tool_args=args or {},
                status="error",
                elapsed_ms=0,
                result=validation_summary,
                secondary_for=primary_tool,
                primary_tool_call_id=primary_tool_call_id,
            )
            return validation_summary

        assert tool_name is not None
        assert args is not None
        action = args.get("action") if isinstance(args.get("action"), str) else None
        self._log(
            "tool_call",
            tool_name=tool_name,
            tool_call_id=None,
            tool_args=args,
            secondary_for=primary_tool,
            primary_tool_call_id=primary_tool_call_id,
        )
        timer = ToolTimer()
        try:
            with timer:
                result = self._dispatch_fn(ToolCall(name=tool_name, args=args, id=None))
            status = result.get("status", "success") if isinstance(result, dict) else "success"
            if status == "error":
                summary = _secondary_summary(
                    tool=tool_name,
                    action=action,
                    status="error",
                    message=result.get("message", "secondary returned status=error") if isinstance(result, dict) else None,
                )
            elif action == "read":
                # ``read`` is the only secondary action whose payload is
                # interesting to the primary turn — send/reply only need
                # confirmation. Forward a bounded slice so the agent can
                # decide what to do without an extra round-trip.
                summary = _secondary_summary(
                    tool=tool_name,
                    action=action,
                    status="success",
                    result=result,
                )
            else:
                summary = _secondary_summary(tool=tool_name, action=action, status="success")
            self._log(
                "tool_result",
                tool_name=tool_name,
                tool_call_id=None,
                tool_args=args,
                status=summary["status"],
                elapsed_ms=timer.elapsed_ms,
                result=summary,
                secondary_for=primary_tool,
                primary_tool_call_id=primary_tool_call_id,
            )
            return summary
        except Exception as e:
            summary = _secondary_summary(
                tool=tool_name,
                action=action,
                status="error",
                message=str(e),
            )
            self._log(
                "tool_result",
                tool_name=tool_name,
                tool_call_id=None,
                tool_args=args,
                status="error",
                elapsed_ms=timer.elapsed_ms,
                result=summary,
                exception=type(e).__name__,
                exception_message=str(e),
                secondary_for=primary_tool,
                primary_tool_call_id=primary_tool_call_id,
            )
            return summary

    @property
    def guard(self) -> LoopGuard:
        return self._guard

    @guard.setter
    def guard(self, value: LoopGuard) -> None:
        self._guard = value

    def _log(self, event_type: str, **fields) -> None:
        if self._logger_fn:
            self._logger_fn(event_type, **fields)

    def execute(
        self,
        tool_calls: list[ToolCall],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
        collected_errors: list[str] | None = None,
    ) -> tuple[list, bool, str]:
        """Execute tool calls. Returns (results, intercepted, intercept_text)."""
        if collected_errors is None:
            collected_errors = []

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
        args = dict(tc.args) if tc.args else {}
        reasoning = args.pop("reasoning", None)
        args.pop("commentary", None)
        args.pop("_sync", None)
        secondary = args.pop("secondary", None)

        if reasoning:
            self._log("tool_reasoning", tool=tc.name, reasoning=reasoning)
            args["_reasoning"] = reasoning

        verdict = self._guard.record_tool_call(tc.name, args)
        if verdict.blocked:
            result = {
                "status": "blocked",
                "_duplicate_warning": verdict.warning,
                "message": f"Execution skipped — duplicate call #{verdict.count}",
            }
            msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status="blocked",
                elapsed_ms=0,
                result=result,
                duplicate_count=verdict.count,
            )
            return msg, False, ""

        secondary_summary = self._execute_secondary(
            secondary,
            primary_tool=tc.name,
            primary_tool_call_id=tc_id,
        )

        timer = ToolTimer()
        try:
            # Pre-check for unknown tool (records in guard for limit tracking)
            if self._known_tools and tc.name not in self._known_tools:
                self._guard.record_invalid_tool(tc.name)
                raise UnknownToolError(tc.name)

            self._log("tool_call", tool_name=tc.name, tool_call_id=tc_id, tool_args=args)
            with timer:
                result = self._dispatch_fn(
                    ToolCall(name=tc.name, args=args, id=tc_id)
                )

            result = _truncate_result(result, self._max_result_bytes)
            result = _attach_secondary_result(result, secondary_summary)

            if isinstance(result, dict):
                stamp_meta(result, self._meta_fn(), timer.elapsed_ms)

            status = result.get("status", "success") if isinstance(result, dict) else "success"
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )

            if verdict.warning and isinstance(result, dict):
                result["_duplicate_warning"] = verdict.warning

            if isinstance(result, dict) and result.get("intercept"):
                intercept_text = result.get("text", "")
                result_msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)
                return result_msg, True, intercept_text

            result_msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)

            if isinstance(result, dict) and result.get("status") == "error":
                err_msg = result.get("message", "unknown error")
                collected_errors.append(f"{tc.name}: {err_msg}")

            if on_result_hook is not None:
                intercept = on_result_hook(tc.name, args, result)
                if intercept is not None:
                    return result_msg, True, intercept

            return result_msg, False, ""

        except Exception as e:
            err_result = {"status": "error", "message": str(e)}
            if secondary_summary is not None:
                _attach_secondary_result(err_result, secondary_summary)
            stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
            result_msg = self._make_tool_result_fn(tc.name, err_result, tool_call_id=tc_id)
            collected_errors.append(f"{tc.name}: {e}")
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status="error",
                elapsed_ms=timer.elapsed_ms,
                result=err_result,
                exception=type(e).__name__,
                exception_message=str(e),
            )
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
        to_execute: list[tuple[int, ToolCall, dict, dict | None]] = []
        tool_results: list[tuple[int, Any]] = []

        for i, tc in enumerate(tool_calls):
            tc_id = getattr(tc, "id", None)
            args = dict(tc.args) if tc.args else {}
            reasoning = args.pop("reasoning", None)
            args.pop("commentary", None)
            args.pop("_sync", None)
            secondary = args.pop("secondary", None)

            if reasoning:
                self._log("tool_reasoning", tool=tc.name, reasoning=reasoning)
                args["_reasoning"] = reasoning

            verdict = self._guard.record_tool_call(tc.name, args)
            if verdict.blocked:
                result = {
                    "status": "blocked",
                    "_duplicate_warning": verdict.warning,
                    "message": f"Execution skipped — duplicate call #{verdict.count}",
                }
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, result, tool_call_id=tc_id,
                )))
                self._log(
                    "tool_result",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_args=args,
                    status="blocked",
                    elapsed_ms=0,
                    result=result,
                    duplicate_count=verdict.count,
                )
            elif self._known_tools and tc.name not in self._known_tools:
                secondary_summary = self._execute_secondary(
                    secondary,
                    primary_tool=tc.name,
                    primary_tool_call_id=tc_id,
                )
                self._guard.record_invalid_tool(tc.name)
                result = {"status": "error", "message": str(UnknownToolError(tc.name))}
                _attach_secondary_result(result, secondary_summary)
                stamp_meta(result, self._meta_fn(), 0)
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, result, tool_call_id=tc_id,
                )))
                collected_errors.append(f"{tc.name}: {result['message']}")
                self._log(
                    "tool_result",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_args=args,
                    status="error",
                    elapsed_ms=0,
                    result=result,
                    exception="UnknownToolError",
                    exception_message=result["message"],
                )
            else:
                secondary_summary = self._execute_secondary(
                    secondary,
                    primary_tool=tc.name,
                    primary_tool_call_id=tc_id,
                )
                to_execute.append((i, tc, args, secondary_summary))

        if not to_execute:
            tool_results.sort(key=lambda x: x[0])
            return [r for _, r in tool_results], False, ""

        # Phase 2: Execute in parallel
        results_map: dict[int, Any] = {}
        errors_map: dict[int, str] = {}

        def _run_one(index: int, tc: ToolCall, args: dict, secondary_summary: dict | None):
            tc_id = getattr(tc, "id", None)
            self._log("tool_call", tool_name=tc.name, tool_call_id=tc_id, tool_args=args)
            timer = ToolTimer()
            try:
                with timer:
                    result = self._dispatch_fn(
                        ToolCall(name=tc.name, args=args, id=tc.id)
                    )
            except Exception as e:
                err_result = {"status": "error", "message": str(e)}
                _attach_secondary_result(err_result, secondary_summary)
                stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
                self._log(
                    "tool_result",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_args=args,
                    status="error",
                    elapsed_ms=timer.elapsed_ms,
                    result=err_result,
                    exception=type(e).__name__,
                    exception_message=str(e),
                )
                return index, err_result
            result = _truncate_result(result, self._max_result_bytes)
            result = _attach_secondary_result(result, secondary_summary)
            if isinstance(result, dict):
                stamp_meta(result, self._meta_fn(), timer.elapsed_ms)
            status = result.get("status", "success") if isinstance(result, dict) else "success"
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )
            return index, result

        pool = ThreadPoolExecutor(max_workers=len(to_execute))
        try:
            futures = {
                pool.submit(_run_one, i, tc, args, secondary_summary): i
                for i, tc, args, secondary_summary in to_execute
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
                    errors_map[idx] = str(e)
                    tc_entry = next(((tc, args) for i, tc, args, _secondary_summary in to_execute if i == idx), None)
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args = tc_entry[1] if tc_entry else {}
                    self._log(
                        "tool_result",
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_args=tc_args,
                        status="error",
                        elapsed_ms=0,
                        result={"status": "error", "message": str(e)},
                        exception=type(e).__name__,
                        exception_message=str(e),
                    )
        except TimeoutError:
            for future, idx in futures.items():
                if idx not in results_map and idx not in errors_map:
                    errors_map[idx] = "Timed out"
                    tc_entry = next(((tc, args) for i, tc, args, _secondary_summary in to_execute if i == idx), None)
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id_t = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args_t = tc_entry[1] if tc_entry else {}
                    self._log(
                        "tool_result",
                        tool_name=tc_name,
                        tool_call_id=tc_id_t,
                        tool_args=tc_args_t,
                        status="error",
                        elapsed_ms=0,
                        result={"status": "error", "message": "Timed out"},
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Phase 3: Build result messages (sequential)
        for i, tc, args, secondary_summary in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, result, tool_call_id=tc_id,
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
                err_msg = errors_map[i]
                err_result = {"status": "error", "message": err_msg}
                _attach_secondary_result(err_result, secondary_summary)
                stamp_meta(err_result, self._meta_fn(), 0)
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, err_result, tool_call_id=tc_id,
                )))
                collected_errors.append(f"{tc.name}: {err_msg}")

        tool_results.sort(key=lambda x: x[0])
        return [r for _, r in tool_results], False, ""
