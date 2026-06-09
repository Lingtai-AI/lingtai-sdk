"""ToolExecutor — sequential and parallel tool call execution."""
from __future__ import annotations

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

    def _build_result_message(
        self,
        tool_name: str,
        result: Any,
        *,
        tool_call_id: str | None,
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
        if capped is not result and self._logger_fn is not None:
            try:
                self._logger_fn(
                    "tool_result_spilled",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    original_char_count=capped.get("original_char_count"),
                    spill_path=capped.get("spill_path"),
                )
            except Exception:
                pass
        return self._make_tool_result_fn(tool_name, capped, tool_call_id=tool_call_id)

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
        deprecated_secondary = args.pop("secondary", None)
        if deprecated_secondary is not None:
            self._log(
                "deprecated_secondary_ignored",
                tool_name=tc.name,
                tool_call_id=tc_id,
            )

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
            msg = self._build_result_message(tc.name, result, tool_call_id=tc_id)
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
                result_msg = self._build_result_message(tc.name, result, tool_call_id=tc_id)
                return result_msg, True, intercept_text

            result_msg = self._build_result_message(tc.name, result, tool_call_id=tc_id)

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
            stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
            result_msg = self._build_result_message(tc.name, err_result, tool_call_id=tc_id)
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
        to_execute: list[tuple[int, ToolCall, dict]] = []
        tool_results: list[tuple[int, Any]] = []

        for i, tc in enumerate(tool_calls):
            tc_id = getattr(tc, "id", None)
            args = dict(tc.args) if tc.args else {}
            reasoning = args.pop("reasoning", None)
            args.pop("commentary", None)
            args.pop("_sync", None)
            deprecated_secondary = args.pop("secondary", None)
            if deprecated_secondary is not None:
                self._log(
                    "deprecated_secondary_ignored",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                )

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
                tool_results.append((i, self._build_result_message(
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
                self._guard.record_invalid_tool(tc.name)
                result = {"status": "error", "message": str(UnknownToolError(tc.name))}
                stamp_meta(result, self._meta_fn(), 0)
                tool_results.append((i, self._build_result_message(
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
                to_execute.append((i, tc, args))

        if not to_execute:
            tool_results.sort(key=lambda x: x[0])
            return [r for _, r in tool_results], False, ""

        # Phase 2: Execute in parallel
        results_map: dict[int, Any] = {}
        errors_map: dict[int, str] = {}

        def _run_one(index: int, tc: ToolCall, args: dict):
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
                pool.submit(_run_one, i, tc, args): i
                for i, tc, args in to_execute
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
                    tc_entry = next(((tc, args) for i, tc, args in to_execute if i == idx), None)
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
                    tc_entry = next(((tc, args) for i, tc, args in to_execute if i == idx), None)
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
        for i, tc, args in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                tool_results.append((i, self._build_result_message(
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
                stamp_meta(err_result, self._meta_fn(), 0)
                tool_results.append((i, self._build_result_message(
                    tc.name, err_result, tool_call_id=tc_id,
                )))
                collected_errors.append(f"{tc.name}: {err_msg}")

        tool_results.sort(key=lambda x: x[0])
        return [r for _, r in tool_results], False, ""
