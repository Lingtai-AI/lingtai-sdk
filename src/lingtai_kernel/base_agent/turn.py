"""Turn engine — main loop, message dispatch, LLM send, tool-call processing.

The core message lifecycle: receive → route → LLM → process → persist.
"""
from __future__ import annotations

import json
import queue
import time

from ..message import Message, _make_message, MSG_REQUEST, MSG_USER_INPUT, MSG_TC_WAKE
from ..i18n import t as _t
from ..logging import get_logger
from ..loop_guard import LoopGuard
from ..safety_limits import (
    ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT,
)
from ..tool_executor import ToolExecutor
from ..tool_result_artifacts import CompactionStats, compact_oversized_history
from ..meta_block import (
    attach_active_notifications,
    attach_active_runtime,
    build_meta,
    render_meta,
)
from ..sent_message_tracker import SEND_TOOLS, SEND_ACTIONS, CHECK_ACTIONS
from ..time_veil import now_iso

logger = get_logger()


class EmptyLLMResponseError(RuntimeError):
    """The LLM returned a response with no text, no tool_calls, and no thoughts.

    A degenerate response indistinguishable from "task complete" by structure
    but actually a model failure (heavy context, provider hiccup, mid-tool
    notification injection confusing the model, etc). Raising this routes the
    failure into the AED recovery loop in ``_run_loop`` instead of silently
    transitioning to IDLE and abandoning the in-progress task.
    """

    def __init__(self, *, ledger_source: str, in_tool_loop: bool):
        self.ledger_source = ledger_source
        self.in_tool_loop = in_tool_loop
        where = "after tool results" if in_tool_loop else "on initial send"
        super().__init__(
            f"LLM returned empty response (no text, no tool_calls, no thoughts) "
            f"{where}; ledger={ledger_source}"
        )


_TRANSIENT_AED_RETRY_LIMIT = 3
_TRANSIENT_EXC_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServerError",
    "ServiceUnavailableError",
    "ReadError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "IncompleteRead",
    "ConnectionResetError",
    "TimeoutError",
}
_TRANSIENT_MSG_FRAGMENTS = (
    "an error occurred while processing your request",
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "read timed out",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def _exception_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP-ish status extraction across SDK exception shapes."""
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _is_transient_provider_error(exc: Exception) -> bool:
    """Return True for provider/network blips that should not spend AED budget.

    The adapter zoo wraps HTTP failures through different SDK exception
    classes.  Prefer explicit status-code handling when present; otherwise
    fall back to stable class names and conservative message fragments.
    4xx errors (including quota/rate limit) are not treated as transient here.
    """
    if isinstance(exc, EmptyLLMResponseError):
        return True

    status_code = _exception_status_code(exc)
    if status_code is not None:
        return 500 <= status_code < 600

    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover - httpx is a runtime dependency today
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        return True

    name = type(exc).__name__
    if name in _TRANSIENT_EXC_NAMES:
        return True

    msg = (str(exc) or "").lower()
    return any(fragment in msg for fragment in _TRANSIENT_MSG_FRAGMENTS)


def _tool_call_summary(tool_calls) -> dict:
    calls = list(tool_calls or [])
    return {
        "call_count": len(calls),
        "call_ids": [getattr(call, "id", None) for call in calls],
        "tool_names": [getattr(call, "name", None) for call in calls],
    }


def _pending_tool_call_summary(iface) -> dict:
    entries = getattr(iface, "entries", None) or []
    tail = entries[-1] if entries else None
    calls = []
    if getattr(tail, "role", None) == "assistant":
        calls = [
            block
            for block in getattr(tail, "content", []) or []
            if hasattr(block, "id") and hasattr(block, "name") and hasattr(block, "args")
        ]
    return {
        "pending_tool_call_count": len(calls),
        "pending_tool_call_ids": [getattr(call, "id", None) for call in calls],
        "pending_tool_names": [getattr(call, "name", None) for call in calls],
    }


def _publish_tool_loop_guard_notification(
    agent,
    *,
    reason: str,
    detail: str,
    ledger_source: str,
    in_tool_loop: bool,
    tool_call_fields: dict,
    closed_count: int,
) -> None:
    workdir = getattr(agent, "_working_dir", None)
    try:
        from pathlib import Path
        from ..intrinsics.system import publish_notification

        publish_notification(
            Path(workdir),
            "tool_loop_guard",
            header="tool loop guard interrupted work",
            icon="!",
            priority="normal",
            instructions=(
                "The kernel stopped a tool-call loop before dispatch. The "
                "matching synthetic tool results are already visible in the "
                "conversation transcript and say no side effects occurred "
                "from those blocked calls. Do not re-issue the same blocked "
                "tool call(s) unchanged. Continue with a different approach, "
                "summarize the blocked/completed work, or ask the human for "
                "direction, then dismiss with notification(action='dismiss_channel', "
                "channel='tool_loop_guard', reason='handled')."
            ),
            data={
                "reason": reason,
                "detail": detail,
                "ledger_source": ledger_source,
                "in_tool_loop": in_tool_loop,
                "closed_tool_result_count": closed_count,
                **tool_call_fields,
                "message": (
                    "Tool loop guard stopped before dispatch. Synthetic tool "
                    "results were committed to the transcript for the blocked "
                    "calls; no side effects occurred from those calls. Do not "
                    "retry the same blocked calls unchanged; switch strategy, "
                    "summarize blocked/completed work, or ask the human."
                ),
            },
        )
    except Exception as e:
        agent._log(
            "tool_loop_guard_notification_failed",
            reason=reason,
            detail=detail,
            error=(str(e) or repr(e))[:300],
        )
        return

    agent._log(
        "tool_loop_guard_notification_published",
        reason=reason,
        detail=detail,
        closed_tool_result_count=closed_count,
    )



# Design note: this helper deliberately only closes the provider wire
# (the ChatInterface transcript sent to the LLM) and publishes
# .notification/tool_loop_guard.json. It does not post MSG_TC_WAKE.
# After the turn unwinds to IDLE, BaseAgent._sync_notifications detects the
# notification file, injects the synthetic notification call/result pair,
# and posts MSG_TC_WAKE. _handle_tc_wake then builds a fresh ToolExecutor and
# LoopGuard, so the tool-call limit counter resets for the follow-up turn.
def _handle_guarded_non_dispatch(
    agent,
    response_tool_calls,
    *,
    reason: str,
    detail: str,
    detail_field: str,
    ledger_source: str,
    in_tool_loop: bool,
    collected_text_parts: list[str],
) -> dict:
    tool_call_fields = _tool_call_summary(response_tool_calls)
    agent._log(
        "tool_calls_not_dispatched",
        ledger_source=ledger_source,
        in_tool_loop=in_tool_loop,
        reason=reason,
        detail_field=detail_field,
        **{detail_field: detail},
        **tool_call_fields,
    )

    closed_count = 0
    chat = getattr(agent, "_chat", None)
    iface = getattr(chat, "interface", None)
    if iface is not None and iface.has_pending_tool_calls():
        pending_fields = _pending_tool_call_summary(iface)
        closed_count = pending_fields["pending_tool_call_count"]
        iface.close_pending_tool_calls(
            reason=f"{reason}: {detail}",
            tool_not_dispatched=True,
        )
        agent._save_chat_history(ledger_source=ledger_source)
        agent._log(
            "guarded_tool_calls_closed",
            ledger_source=ledger_source,
            reason=reason,
            detail=detail,
            result_count=closed_count,
            pending_tool_call_ids=pending_fields["pending_tool_call_ids"],
            pending_tool_names=pending_fields["pending_tool_names"],
        )
    else:
        agent._log(
            "guarded_tool_calls_close_skipped",
            ledger_source=ledger_source,
            reason=reason,
            detail=detail,
            skipped_reason="no_chat_or_tool_calls",
        )

    _publish_tool_loop_guard_notification(
        agent,
        reason=reason,
        detail=detail,
        ledger_source=ledger_source,
        in_tool_loop=in_tool_loop,
        tool_call_fields=tool_call_fields,
        closed_count=closed_count,
    )
    return {
        "text": "\n".join(collected_text_parts),
        "failed": False,
        "errors": [],
    }


def _prepare_aed_retry_message(agent, err_desc: str) -> Message:
    """Build the system recovery prompt reused by transient and AED retries."""
    ts = now_iso(agent)
    aed_msg = _t(
        agent._config.language,
        "system.stuck_revive",
        ts=ts,
        tool_calls=err_desc,
    )
    return _make_message(MSG_REQUEST, "system", aed_msg)


# Over-window / context-pressure fragments — provider errors whose root cause
# is "the wire is too long" and whose only safe recovery is to shrink the
# transcript before retry.  Distinct from generic transient errors:
# retrying transiently on the same unchanged wire just repeats the failure.
# Matched case-insensitively against ``str(exc)``.
_OVER_WINDOW_MSG_FRAGMENTS = (
    "context window",
    "context_window",
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "exceeds the maximum",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input token count",
    "tokens in the input",
    "request too large",
    "too many tokens",
)


def _is_over_window_error(exc: Exception) -> bool:
    """Return True for provider errors whose cause is wire length.

    Routed to the deterministic AED branch (not transient) because the
    same wire will fail the same way no matter how many retries we burn.
    Retroactive compaction MUST run before the rebuilt session replays
    the transcript — otherwise we will send the AED recovery prompt into
    an unchanged over-window wire and trip the same error.
    """
    if isinstance(exc, EmptyLLMResponseError):
        return False
    msg = (str(exc) or "").lower()
    return any(fragment in msg for fragment in _OVER_WINDOW_MSG_FRAGMENTS)


def _compact_history_before_retry(agent, *, source: str) -> "CompactionStats | None":
    """Retroactively spill oversized tool results before an AED retry.

    Walks ``agent._session.chat.interface._entries`` and replaces any
    ``ToolResultBlock.content`` larger than the retroactive cap (default
    5K chars — tighter than the preventive 10K cap because we want to
    actually free provider tokens before retry) with a spill manifest.
    Entries, ordering, ids, and ``tool_call``/``tool_result`` pairing are
    untouched.  Already-compacted manifests are skipped.

    When at least one block is rewritten, calls
    ``agent._save_chat_history(ledger_source="retroactive_compaction")``
    so the persisted ``history/chat_history.jsonl`` matches the compacted
    wire before the session rebuild / retry replays it.

    Logs a single bounded ``aed_history_compacted`` event on every call
    (including the noop case, so operators can correlate AED firings with
    compaction activity).  The event name is intentionally distinct from
    the per-block ``tool_result_compacted_retroactively`` emitted by
    ``compact_oversized_history`` itself.

    Safe no-op if the agent has no working_dir, no live chat, or the
    interface is in an unexpected shape — AED is the recovery path and
    must never become the cause of further failures.  Any exception
    raised by attribute access or the underlying helper is swallowed and
    logged (best-effort) instead of propagating.  Returns the
    ``CompactionStats`` for the caller's convenience, or ``None`` on
    failure.
    """
    stats: CompactionStats | None = None
    try:
        chat = agent._session.chat if agent._session is not None else None
        if chat is None:
            return None
        interface = getattr(chat, "interface", None)
        working_dir = getattr(agent, "_working_dir", None)
        stats = compact_oversized_history(
            interface,
            working_dir=working_dir,
            logger_fn=getattr(agent, "_log", None),
        )
    except Exception as exc:  # noqa: BLE001 — recovery path, never re-raise
        try:
            agent._log(
                "tool_result_compaction_failed",
                source=source,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return None

    log_fn = getattr(agent, "_log", None)
    if log_fn is not None:
        try:
            log_fn(
                "aed_history_compacted",
                source=source,
                **stats.to_log_fields(),
            )
        except Exception:
            pass

    if stats.compacted_blocks > 0:
        # Persist the shrunk wire so the rebuilt session and any later
        # snapshot load see the same compacted history the LLM will see
        # on the retry replay.
        save_fn = getattr(agent, "_save_chat_history", None)
        if save_fn is not None:
            try:
                save_fn(ledger_source="retroactive_compaction")
            except Exception as exc:  # noqa: BLE001
                if log_fn is not None:
                    try:
                        log_fn(
                            "retroactive_compaction_save_failed",
                            source=source,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    except Exception:
                        pass
    return stats


def _restore_tool_results_after_continuation_failure(
    agent,
    tool_results,
    *,
    ledger_source: str,
) -> bool:
    """Persist real tool results after post-tool LLM continuation failure.

    The tools already executed locally before the continuation send. If an
    adapter rolled back the attempted user tool-result entry on provider error,
    the canonical interface tail still has pending assistant tool_calls. Restore
    the real results before AED / notification heal can synthesize a placeholder
    that lacks the actual result payload.
    """
    if (
        not tool_results
        or agent._chat is None
        or not agent._chat.interface.has_pending_tool_calls()
    ):
        return False
    agent._chat.commit_tool_results(tool_results)
    try:
        agent._save_chat_history(ledger_source=ledger_source)
    except Exception as e:
        agent._log(
            "tool_results_restored_after_continuation_failure",
            result_count=len(tool_results),
            ledger_source=ledger_source,
            failed_at="save_chat_history",
            save_error=(str(e) or repr(e))[:300],
            side_effect="memory_state_may_be_ahead_of_disk",
        )
        raise
    agent._log(
        "tool_results_restored_after_continuation_failure",
        result_count=len(tool_results),
    )
    return True


def _run_loop(agent) -> None:
    """Wait for messages, process them. Agent persists between messages."""
    from ..state import AgentState
    from ..intrinsics.soul.flow import _cancel_soul_timer

    while True:
        while not agent._shutdown.is_set():
            # --- Asleep: soul off, wait for inbox message ---
            if agent._asleep.is_set():
                _cancel_soul_timer(agent)
                # Heal any dangling tool_calls on the wire BEFORE going to
                # sleep. If we sleep with an unanswered tool_call, the next
                # mail's _inject_notification_pair refuses to append (would
                # violate alternation invariant) and the agent silently
                # fails to wake. The chat-saved snapshot must always be
                # appendable from a fresh wake. Common cause: cancel
                # mid-batch leaves the just-arrived assistant response
                # with tool_calls on the wire but no results yet.
                if (
                    agent._chat is not None
                    and agent._chat.interface.has_pending_tool_calls()
                ):
                    phase = "close_pending_tool_calls"
                    try:
                        agent._chat.interface.close_pending_tool_calls(
                            reason="heal:going_asleep"
                        )
                        phase = "save_chat_history"
                        agent._save_chat_history(ledger_source="heal")
                        agent._log("heal_pending_tool_calls", reason="going_asleep")
                    except Exception as e:
                        fields = {
                            "reason": "going_asleep",
                            "failed_at": phase,
                            "error": (str(e) or repr(e))[:200],
                        }
                        if phase == "save_chat_history":
                            fields["side_effect"] = "memory_state_may_be_ahead_of_disk"
                        agent._log("heal_pending_tool_calls_failed", **fields)
                agent._log("sleep")

                # Block until a message arrives or shutdown
                msg = None
                while not agent._shutdown.is_set():
                    try:
                        msg = agent.inbox.get(timeout=1.0)
                        break
                    except queue.Empty:
                        continue

                if msg is None:
                    break  # shutdown was set — exit inner loop

                # Wake up
                agent._asleep.clear()
                agent._cancel_event.clear()  # clear stale sleep/stamina signal
                agent._set_state(AgentState.ACTIVE, reason=f"woke from asleep: {msg.type}")
                agent._log("wake", trigger=msg.type)
                agent._reset_uptime()
                msg = _concat_queued_messages(agent, msg)
                # Fall through to handle the message below
            else:
                try:
                    msg = agent.inbox.get(timeout=agent._inbox_timeout)
                except queue.Empty:
                    continue
                msg = _concat_queued_messages(agent, msg)
                agent._set_state(AgentState.ACTIVE, reason=f"received {msg.type}")

            # --- Process with AED (Automatic Error Detection) ---
            sleep_state = AgentState.IDLE
            aed_attempts = 0
            transient_attempts = 0
            skip_post_turn_save = False
            while True:
                try:
                    _handle_message(agent, msg)
                    transient_attempts = 0
                    break  # success (chat saved after each session.send inside)
                except Exception as e:
                    from ..llm_utils import WorkerStillRunningError

                    err_desc = str(e) or repr(e)

                    if isinstance(e, WorkerStillRunningError):
                        # Worker future is still alive — ChatInterface is
                        # unsafe to mutate from this thread. Skip chat
                        # save and put the agent to sleep; a refresh will
                        # bring up a fresh interface.
                        agent._log("llm_worker_still_running", error=err_desc[:300])
                        agent._set_state(AgentState.STUCK, reason=err_desc)
                        agent._asleep.set()
                        agent._set_state(
                            AgentState.ASLEEP,
                            reason="LLM worker still running; refresh recommended",
                        )
                        sleep_state = AgentState.ASLEEP
                        skip_post_turn_save = True
                        break

                    # Issue #144: over-window / context-pressure errors must
                    # take the deterministic AED branch, not transient
                    # retry — the same wire will fail the same way under
                    # any number of retries.  Retroactive compaction below
                    # shrinks the transcript before _rebuild_session
                    # replays it.  This is the dedicated over-window
                    # recovery path; if we ever add a hard pre-send gate
                    # (compact *before* the first send rather than after
                    # the first failure), it would slot in at
                    # _handle_message — see TODO below.
                    over_window = _is_over_window_error(e)
                    if over_window:
                        agent._log(
                            "aed_over_window_detected",
                            error=err_desc[:300],
                            exception=type(e).__name__,
                        )

                    if not over_window and _is_transient_provider_error(e):
                        if transient_attempts < _TRANSIENT_AED_RETRY_LIMIT:
                            transient_attempts += 1
                            backoff_s = min(2.0 ** (transient_attempts - 1), 8.0)
                            if agent._session.chat is not None:
                                agent._session.chat.interface.close_pending_tool_calls(
                                    reason=f"transient_retry: {err_desc[:200]}",
                                    tool_completed=True,
                                )
                            # Issue #144: shrink oversized historical tool
                            # results to manifests before the retry so the
                            # next send doesn't ship the same too-big wire.
                            _compact_history_before_retry(agent, source="aed_transient")
                            agent._log(
                                "aed_transient_retry",
                                attempt=transient_attempts,
                                max_attempts=_TRANSIENT_AED_RETRY_LIMIT,
                                backoff_s=backoff_s,
                                error=err_desc[:300],
                            )
                            logger.warning(
                                f"[{agent.agent_name}] AED transient retry "
                                f"{transient_attempts}/{_TRANSIENT_AED_RETRY_LIMIT}: {err_desc}",
                            )
                            time.sleep(backoff_s)
                            msg = _prepare_aed_retry_message(agent, err_desc)
                            continue

                        agent._log(
                            "aed_transient_exhausted",
                            attempts=transient_attempts,
                            error=err_desc[:300],
                        )
                    # TODO(issue #144 follow-up): add a hard pre-send gate
                    # in _handle_message that runs retroactive compaction
                    # whenever ``_serialized_len(interface.entries) >
                    # context_limit_threshold``, so we don't need a failed
                    # send to discover over-window.  Out of scope for this
                    # PR — current behavior is "compact on first failure
                    # then rebuild" which is correct but reactive.

                    aed_attempts += 1

                    # Close any dangling tool_calls with synthetic error
                    # tool_results.  tool_completed=True because AED fires
                    # after the tool executor already ran — the real failure
                    # is the LLM continuation, not the tool itself.
                    if agent._session.chat is not None:
                        agent._session.chat.interface.close_pending_tool_calls(
                            reason=err_desc or "aed_recovery",
                            tool_completed=True,
                        )

                    agent._set_state(AgentState.STUCK, reason=f"AED attempt {aed_attempts}: {err_desc}")
                    agent._log("aed_attempt", attempt=aed_attempts, error=err_desc)
                    logger.warning(
                        f"[{agent.agent_name}] AED attempt {aed_attempts}/{agent._config.max_aed_attempts}: {err_desc}",
                    )

                    if aed_attempts == agent._config.max_aed_attempts:
                        if not agent._preset_fallback_attempted and agent._can_fallback_preset():
                            agent._preset_fallback_attempted = True
                            agent._log("preset_auto_fallback",
                                      reason=err_desc,
                                      failed_attempts=aed_attempts)
                            try:
                                agent._activate_default_preset()
                            except Exception as e:
                                agent._log("preset_auto_fallback_failed", error=str(e))
                                # fall through to ASLEEP
                            else:
                                agent._perform_refresh()
                                return

                        agent._log("aed_exhausted", attempts=aed_attempts, error=err_desc)
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    # Issue #144: compact oversized historical tool results
                    # before rebuilding the session so the replayed history
                    # fits.  Runs after close_pending_tool_calls (above) and
                    # before _rebuild_session so the rebuilt session sees
                    # the already-shrunk wire.  Over-window errors get a
                    # distinct source tag so AED logs make the cause
                    # auditable.
                    _compact_history_before_retry(
                        agent,
                        source="aed_over_window" if over_window else "aed_deterministic",
                    )

                    # Rebuild session with current config, preserving history
                    if agent._session.chat is not None:
                        agent._session._rebuild_session(agent._session.chat.interface)

                    # Inject recovery message
                    msg = _prepare_aed_retry_message(agent, err_desc)
                    agent._set_state(AgentState.ACTIVE, reason=f"AED recovery attempt {aed_attempts}")

            if not agent._asleep.is_set():
                agent._set_state(sleep_state)

            # Issue #83: check for pending notifications only after the
            # state is observably IDLE.  A check while still ACTIVE would
            # take the ACTIVE deferral path, leaving the fingerprint
            # uncommitted with no wake queued until a later heartbeat.
            # At the IDLE boundary, _sync_notifications uses the distinct
            # synthetic notification pair + MSG_TC_WAKE path.
            if sleep_state == AgentState.IDLE and not agent._asleep.is_set():
                try:
                    from ..notifications import notification_fingerprint, collect_notifications
                    fp = notification_fingerprint(agent._working_dir)
                    if fp != agent._notification_fp:
                        notifications = collect_notifications(agent._working_dir)
                        if notifications:
                            agent._log("idle_notification_check",
                                       sources=list(notifications.keys()))
                            agent._sync_notifications()
                except Exception as notif_err:
                    agent._log("idle_notification_check_error",
                               error=str(notif_err))
            if skip_post_turn_save:
                agent._log(
                    "chat_history_save_skipped",
                    reason="worker_still_running_interface_unsafe",
                )
            else:
                agent._save_chat_history()

            # Auto-insight: fire after N turns
            if agent._config.insights_interval > 0:
                agent._insight_turn_counter += 1
                if agent._insight_turn_counter >= agent._config.insights_interval:
                    agent._insight_turn_counter = 0
                    from ..i18n import t as _ti
                    from ..intrinsics.soul.inquiry import _run_inquiry
                    _run_inquiry(
                        agent,
                        _ti(agent._config.language, "insight.auto_question"),
                        source="auto",
                    )

        break


_TEXT_MSG_TYPES = (MSG_REQUEST, MSG_USER_INPUT)


def _concat_queued_messages(agent, msg: Message) -> Message:
    """Drain queued same-type text messages and concatenate into one.

    Only consumes additional messages of MSG_REQUEST or MSG_USER_INPUT
    (text-bearing types) — and only when ``msg`` itself is one of those.
    Other message types (notably MSG_TC_WAKE) are put back into the
    inbox so the run loop processes them in their own iteration with
    their own dispatch path. Without this filter, an empty-content
    MSG_TC_WAKE queued behind a MSG_REQUEST would be silently absorbed
    into the merged request, and the tc_inbox drain handler would never
    fire — mail notifications would stay queued indefinitely.

    If nothing same-type is queued, returns the original message
    unchanged. Otherwise, joins all same-type contents with blank lines
    and returns a new merged message.
    """
    if msg.type not in _TEXT_MSG_TYPES:
        return msg

    extra: list[Message] = []
    putback: list[Message] = []
    while True:
        try:
            queued = agent.inbox.get_nowait()
        except queue.Empty:
            break
        if queued.type in _TEXT_MSG_TYPES:
            extra.append(queued)
        else:
            putback.append(queued)

    for held in putback:
        agent.inbox.put_nowait(held)

    if not extra:
        return msg

    all_msgs = [msg] + extra
    parts = [m.content if isinstance(m.content, str) else str(m.content)
             for m in all_msgs]
    merged_content = "\n\n".join(parts)
    merged = _make_message(MSG_REQUEST, msg.sender, merged_content)
    agent._log("messages_concatenated", count=len(all_msgs))
    return merged


def _handle_message(agent, msg: Message) -> None:
    """Route message by type. Subclasses may override for routing."""
    if msg.type in (MSG_REQUEST, MSG_USER_INPUT):
        _handle_request(agent, msg)
    elif msg.type == MSG_TC_WAKE:
        _handle_tc_wake(agent, msg)
    else:
        logger.warning(f"[{agent.agent_name}] Unknown message type: {msg.type}")


# Context-pressure molt reminders are emitted as `_meta.agent_meta.context.molt`
# by meta_block.build_meta; the notification channel is kept only for
# post-molt continuation/event signals.
def _check_molt_pressure(agent) -> None:
    """Clear the legacy pressure-warning notification channel.

    Context pressure is current agent state and is now exposed under
    `_meta.agent_meta.context.molt` by ``meta_block.build_meta``. It should not
    be a dismissible notification. Post-molt continuation still uses the
    notification system and is handled separately.
    """
    if "psyche" not in agent._intrinsics:
        return
    from ..intrinsics.system import clear_notification

    clear_notification(agent._working_dir, "molt")

def _is_context_molt_call(tc) -> bool:
    """Return True when ``tc`` is ``psyche(context, molt, ...)``.

    A post-molt notification is published before the ``psyche.molt`` tool
    result returns.  If that same result batch were active-stamped with the
    notification and committed its fingerprint, the subsequent IDLE boundary
    would see no change and would not inject the synthesized notification +
    ``MSG_TC_WAKE`` continuation.  Only the molt batch needs this deferral:
    later ACTIVE batches may consume the post-molt notification normally, while
    an immediate IDLE boundary will wake from the still-uncommitted file state.
    """
    if getattr(tc, "name", None) != "psyche":
        return False
    args = getattr(tc, "args", None)
    if not isinstance(args, dict):
        return False
    return args.get("object") == "context" and args.get("action") == "molt"


def _batch_includes_context_molt(tool_calls) -> bool:
    return any(_is_context_molt_call(tc) for tc in tool_calls or [])


def _handle_request(agent, msg: Message) -> None:
    """Send request to LLM, process response with tool calls."""
    # Splice any queued involuntary tool-call pairs
    agent._drain_tc_inbox()

    max_calls, dup_free, dup_hard = _get_guard_limits(agent)
    guard = LoopGuard(
        max_total_calls=max_calls,
        dup_free_passes=dup_free,
        dup_hard_block=dup_hard,
    )
    agent._executor = ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=guard,
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        parallel_safe_tools=agent._PARALLEL_SAFE_TOOLS,
        logger_fn=agent._log,
        meta_fn=lambda: build_meta(agent),
        working_dir=agent._working_dir,
        summarize_notification_threshold=getattr(
            agent, "_summarize_notification_threshold", None
        ),
    )
    content = agent._pre_request(msg)
    meta = build_meta(agent)

    # Molt pressure — warn agent when context is getting full.
    _check_molt_pressure(agent)

    # Synchronous notification sync — record same-turn notification
    # changes.  While ACTIVE this deliberately defers without mutating
    # unrelated tool results; delivery happens at the next IDLE boundary.
    try:
        agent._sync_notifications()
    except Exception:
        pass

    # Rescan live chat history for large unsummarized tool results that were
    # already in context before this turn (e.g. after a refresh, notification
    # dismissed, or history migration).  Uses skip_if_ref_id_exists so no
    # duplicate notifications are published for results already tracked.
    try:
        agent._rescan_large_tool_results()
    except Exception:
        pass

    prefix = render_meta(agent, meta)
    if prefix:
        content = f"{prefix}\n\n{content}"
    agent._log("text_input", text=content)
    response = agent._session.send(content)
    agent._last_usage = response.usage
    agent._save_chat_history()
    result = _process_response(agent, response)
    agent._post_request(msg, result)


def _handle_tc_wake(agent, msg: Message) -> None:
    """Drive one inference round off the existing wire, no append.

    Post-`.notification/`-redesign contract: the run loop receives this
    message after ``_sync_notifications`` has already spliced a
    synthesized ``(ToolCallBlock, ToolResultBlock)`` pair into the
    canonical interface (impersonating a voluntary
    ``notification(action="check")`` call from the agent's
    perspective).  This handler's job is to drive the next inference
    round off that wire — no fake user message, no meta prefix.  From
    the LLM's viewpoint it is indistinguishable from the agent having
    voluntarily called the tool itself.

    The legacy ``tc_inbox`` queue is still drained at the top for
    back-compat (in case anything outside the kernel still enqueues),
    but the empty-queue path now routes to the wire-drive path instead
    of no-op-and-return — the previous "tc_inbox_empty" silent
    no-op was the bug that left spliced notification pairs unread.
    """
    if agent._chat is None:
        try:
            agent._session.ensure_session()
        except Exception as e:
            agent._log(
                "tc_wake_noop",
                reason="ensure_session_failed",
                error=str(e)[:300],
            )
            return

    iface = agent._chat.interface
    items = agent._tc_inbox.drain()

    # Mid-pair tail — defer.  Re-enqueue any drained legacy items so
    # the next wake retries them.
    if iface.has_pending_tool_calls():
        for item in items:
            agent._tc_inbox.enqueue(item)
        agent._log(
            "tc_wake_noop",
            reason="pending_tool_calls",
            **_pending_tool_call_summary(iface),
        )
        return

    agent._executor = ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=LoopGuard(
            max_total_calls=_get_guard_limits(agent)[0],
            dup_free_passes=2,
            dup_hard_block=8,
        ),
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        parallel_safe_tools=agent._PARALLEL_SAFE_TOOLS,
        logger_fn=agent._log,
        meta_fn=lambda: build_meta(agent),
        working_dir=agent._working_dir,
        summarize_notification_threshold=getattr(
            agent, "_summarize_notification_threshold", None
        ),
    )

    # Legacy tc_inbox path — drained items get spliced and driven the
    # old way (call appended here, result passed through send).  Empty
    # in production post-redesign; preserved for back-compat.
    for idx, item in enumerate(items):
        try:
            if getattr(item, "replace_in_history", False):
                prior_id = agent._appendix_ids_by_source.get(item.source)
                if prior_id is not None:
                    iface.remove_pair_by_call_id(prior_id)
                agent._appendix_ids_by_source.pop(item.source, None)
            iface.add_assistant_message(content=[item.call])
            if getattr(item, "replace_in_history", False):
                agent._appendix_ids_by_source[item.source] = item.call.id
            agent._save_chat_history()

            agent._log("tc_wake_dispatch", source=item.source, call_id=item.call.id)
            try:
                response = agent._session.send([item.result])
            except Exception:
                # The spliced tool result was passed into send() and the
                # adapter rolled the user entry back when the API call
                # failed. Restore the real result before the catch-all
                # below synthesizes a placeholder — without this, the
                # original notification payload is permanently replaced by
                # the kernel notice and the agent has no way to recover
                # the message that was on the wire (issue #170).
                _restore_tool_results_after_continuation_failure(
                    agent, [item.result], ledger_source="tc_wake",
                )
                raise
            agent._last_usage = response.usage
            agent._save_chat_history(ledger_source="tc_wake")
            _process_response(agent, response, ledger_source="tc_wake")
        except Exception as splice_err:
            if iface.has_pending_tool_calls():
                # tool_completed=True: the tool result was produced by the
                # notification system and passed in as item.result — the
                # failure is in the LLM round-trip that followed.
                iface.close_pending_tool_calls(
                    reason=f"tc_wake splice failed: {str(splice_err)[:200]}",
                    tool_completed=True,
                )
                agent._save_chat_history()
            agent._log(
                "tc_wake_send_error",
                source=item.source,
                call_id=item.call.id,
                error=str(splice_err)[:300],
            )
            for remaining in items[idx + 1:]:
                agent._tc_inbox.enqueue(remaining)
            raise

    # Wire-drive path: notification sync (or anything else that
    # appends a complete (call, result) pair before posting
    # MSG_TC_WAKE) leaves the wire ready for inference.  Drive one
    # round off the existing state — pass None as the message so the
    # adapter knows to skip the input-append step.
    #
    # Guard against stale wakes: only drive the wire when the tail is
    # a user entry carrying ToolResultBlock(s).  Anything else (empty
    # interface, tail is assistant text, etc.) means there's nothing
    # for the LLM to respond to — sending would either error or
    # produce a redundant continuation.
    from ..llm.interface import ToolResultBlock

    entries = iface.entries
    tail_is_tool_result = (
        bool(entries)
        and entries[-1].role == "user"
        and any(isinstance(b, ToolResultBlock) for b in entries[-1].content)
    )
    if not tail_is_tool_result:
        agent._log("tc_wake_noop", reason="wire_not_ready")
        return

    try:
        agent._log("tc_wake_continue")
        response = agent._session.send(None)
        agent._last_usage = response.usage
        agent._save_chat_history(ledger_source="tc_wake")
        _process_response(agent, response, ledger_source="tc_wake")
        # Notification-driven turns should also check pressure so
        # warnings fire even when the agent is woken by mail/soul.
        _check_molt_pressure(agent)
        # Synchronous notification sync — same ACTIVE deferral semantics as
        # _handle_request; delivery happens at the next IDLE boundary.
        try:
            agent._sync_notifications()
        except Exception:
            pass
        # Rescan for large unsummarized results in chat history.
        try:
            agent._rescan_large_tool_results()
        except Exception:
            pass
    except Exception as e:
        if iface.has_pending_tool_calls():
            # tool_completed=True: the wire-drive path only fires when the
            # tail is already user[ToolResultBlock] — the tool results
            # were committed and the adapter reverted them after the
            # LLM continuation failed.
            iface.close_pending_tool_calls(
                reason=f"tc_wake continue heal: {str(e)[:200]}",
                tool_completed=True,
            )
            agent._save_chat_history()
        agent._log("tc_wake_error", error=str(e)[:300])
        raise


def _get_guard_limits(agent) -> tuple[int, int, int]:
    """Return (max_total_calls, dup_free_passes, dup_hard_block).

    The total-call ceiling is a kernel-owned ACTIVE-turn emergency fuse. It is
    intentionally not read from ``manifest.max_turns`` / ``AgentConfig.max_turns``
    so stale init.json files cannot make the runtime harsher or looser.
    """
    return (ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT, 3, 8)


def _check_external_send(agent, tool_calls, tool_results=None) -> None:
    """Record external sends and warn on duplicates.

    Scans the just-executed batch for send/reply actions on external
    channel tools (telegram, imap, wechat, feishu). Records each send
    in the tracker for dedup. When a duplicate is detected, appends a
    warning to the corresponding tool result.
    """
    tracker = agent._sent_tracker
    for tc in tool_calls:
        if tc.name not in SEND_TOOLS:
            continue
        args = tc.args or {}
        action = args.get("action", "")
        if action in SEND_ACTIONS:
            content = args.get("message", "") or args.get("body", "") or args.get("text", "")
            recipient = args.get("to", "") or args.get("chat_id", "") or args.get("address", "")
            if content and recipient:
                if tracker.was_recently_sent(content, recipient):
                    agent._log(
                        "send_dedup_detected",
                        tool=tc.name,
                        recipient=recipient,
                    )
                    if tool_results:
                        warning = (
                            "Recently sent similar message to this recipient."
                            " This send already executed; avoid sending it again."
                        )
                        for tr in tool_results:
                            if tr.id == tc.id:
                                if isinstance(tr.content, dict):
                                    # ToolResultBlock.content is Any (str or dict);
                                    # dict + str raises TypeError. Attach as a
                                    # structured field instead — adapters render
                                    # the whole dict to the LLM as JSON.
                                    tr.content["_advisory"] = {
                                        "type": "duplicate_send",
                                        "severity": "warning",
                                        "allowed": True,
                                        "blocked": False,
                                        "advisory_only": True,
                                        "message": warning,
                                        "skill_refs": ["system-manual"],
                                    }
                                else:
                                    tr.content = (
                                        (tr.content or "")
                                        + f"\n⚠️ {warning}"
                                    )
                                break
                    continue
                tracker.record_sent(content, recipient, tc.name)
                agent._log(
                    "external_send_detected",
                    tool=tc.name,
                    action=action,
                    recipient=recipient,
                )


def _check_poll_backoff(agent, tool_calls, tool_results=None) -> bool:
    """Check if polling actions should trigger idle-after-backoff.

    Counts consecutive check/read calls on external channel tools within
    the same turn. After ``max_poll_retries`` consecutive checks on the
    same channel, returns True to signal the agent should go IDLE.

    The counter resets when a send action occurs, when new messages
    arrive via the notification system, or when a check/read action
    actually returns messages (found_new=True).
    """
    # Build a lookup from tool-call id to result for found-new detection.
    # ToolResultBlock stores the correlated tool-call id on `.id` (the same
    # field name as ToolCallBlock), not on a separate `.tool_call_id`.
    result_by_tc_id: dict = {}
    if tool_results:
        for tr in tool_results:
            tc_id = getattr(tr, "id", None)
            if tc_id:
                result_by_tc_id[tc_id] = tr

    tracker = agent._sent_tracker
    should_idle = False
    for tc in tool_calls:
        if tc.name not in SEND_TOOLS:
            continue
        args = tc.args or {}
        action = args.get("action", "")
        if action in SEND_ACTIONS:
            # Send resets the poll counter for this channel.
            tracker.reset_poll(tc.name)
            continue
        if action not in CHECK_ACTIONS:
            continue
        # Check if this read/check actually returned items. Different
        # providers use different keys: imap→"emails", telegram/wechat→
        # "messages", feishu→"conversations" (check) or "messages" (read).
        found_new = False
        tr = result_by_tc_id.get(tc.id)
        if tr:
            content = getattr(tr, "content", None)
            payload = None
            if isinstance(content, dict):
                payload = content
            elif isinstance(content, str):
                try:
                    payload = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    payload = None
            if isinstance(payload, dict):
                for key in ("messages", "emails", "conversations"):
                    if payload.get(key):
                        found_new = True
                        break
        # Record the poll attempt with found_new status.
        tracker.record_poll(tc.name, found_new=found_new)
        if tracker.should_stop_polling(tc.name):
            agent._log(
                "poll_backoff_exhausted",
                tool=tc.name,
                action=action,
                poll_count=tracker._poll_counts.get(tc.name, 0),
            )
            should_idle = True
    return should_idle


def _process_response(agent, response, *, ledger_source: str = "main") -> dict:
    """Handle tool calls and collect text output.

    Returns a result dict: {"text": ..., "failed": ..., "errors": [...]}.

    ``ledger_source`` propagates to ``_save_chat_history`` for any
    tool-loop continuation LLM round-trips.
    """
    agent._cancel_event.clear()

    guard = agent._executor.guard
    collected_text_parts: list[str] = []
    collected_errors: list[str] = []
    in_tool_loop = False

    while True:
        # Empty-response guard: text + tool_calls + thoughts all empty means
        # the LLM produced nothing useful. Without this check, the loop would
        # break on `not response.tool_calls` and return success, abandoning
        # any in-progress task. Route into AED instead — a session rebuild
        # plus stuck_revive injection is the right recovery for a degenerate
        # response (often caused by heavy context or mid-loop notification
        # injection confusing the model).
        if (
            not response.text
            and not response.tool_calls
            and not response.thoughts
        ):
            # Extract diagnostic metadata from provider response.
            raw = response.raw
            _diag: dict = {}
            if raw is not None:
                _diag["response_id"] = getattr(raw, "id", None)
                _diag["response_model"] = getattr(raw, "model", None)
                choices = getattr(raw, "choices", None)
                if choices:
                    _diag["finish_reason"] = getattr(
                        choices[0], "finish_reason", None
                    )
            agent._log(
                "empty_llm_response",
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                output_tokens=response.usage.output_tokens,
                thinking_tokens=response.usage.thinking_tokens,
                api_call_id=getattr(response, "api_call_id", None),
                **_diag,
            )
            raise EmptyLLMResponseError(
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
            )

        if response.text:
            collected_text_parts.append(response.text)
            agent._log("diary", text=response.text)
            if response.tool_calls:
                agent._intermediate_text_streamed = False

        if response.thoughts:
            for thought in response.thoughts:
                agent._log("thinking", text=thought)

        if not response.tool_calls:
            break

        tool_call_fields = _tool_call_summary(response.tool_calls)
        if agent._cancel_event.is_set():
            agent._cancel_event.clear()
            agent._log(
                "tool_calls_not_dispatched",
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="cancel_event",
                **tool_call_fields,
            )
            return {"text": "", "failed": False, "errors": []}

        stop_reason = guard.check_limit(len(response.tool_calls))
        if stop_reason:
            return _handle_guarded_non_dispatch(
                agent,
                response.tool_calls,
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="tool_loop_limit",
                detail=stop_reason,
                detail_field="stop_reason",
                collected_text_parts=collected_text_parts,
            )

        invalid_reason = guard.check_invalid_tool_limit()
        if invalid_reason:
            return _handle_guarded_non_dispatch(
                agent,
                response.tool_calls,
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="invalid_tool_limit",
                detail=invalid_reason,
                detail_field="invalid_reason",
                collected_text_parts=collected_text_parts,
            )

        # Count this batch before execution so every model-visible tool result
        # can carry the post-batch ACTIVE-turn progress meter.
        guard.record_calls(len(response.tool_calls))

        # Delegate to ToolExecutor.  The progress notice is batch-scoped: it is
        # visible on this batch's tool results, then cleared before the next LLM
        # response is processed.
        try:
            tool_results, intercepted, intercept_text = agent._executor.execute(
                response.tool_calls,
                api_call_id=getattr(response, "api_call_id", None),
                on_result_hook=agent._on_tool_result_hook,
                cancel_event=agent._cancel_event,
                collected_errors=collected_errors,
            )
        finally:
            guard.clear_progress_notice()

        # Move the live notification payload from the previous holder (if
        # any) to the latest tool-result dict from this batch.  The prior
        # holder is skeletonized in-place before the new one is registered,
        # maintaining the at-most-one-live-payload invariant.  If the prior
        # holder was a synthesized pair, its content becomes a skeleton
        # placeholder (kept in history); if it was a normal tool result,
        # its canonical notification payload keys are removed.
        if _batch_includes_context_molt(response.tool_calls):
            # ``psyche.molt`` publishes ``.notification/post-molt.json`` before
            # its own tool result returns.  Do not let that same result batch
            # consume the notification or commit its fingerprint; otherwise the
            # post-turn IDLE sync would see no change and skip the wake.  Leaving
            # the fingerprint untouched lets the next boundary choose naturally:
            # IDLE/ASLEEP injects synthesized notification + MSG_TC_WAKE; a later
            # ACTIVE tool-result batch stamps the notification normally.
            if agent._notification_live_holder is not None:
                from ..meta_block import skeletonize_notification_holder
                skeletonize_notification_holder(agent)
        else:
            _prior_holder = agent._notification_live_holder
            agent._notification_live_holder = attach_active_notifications(
                agent,
                tool_results,
                prior_holder=_prior_holder,
            )

        # Move the live `_meta.agent_meta` / `_meta.guidance` blocks (kernel
        # runtime state + guidance) to the latest tool-result dict from this
        # batch, stripping them from the prior holder.  This keeps them
        # latest-only — only the freshest provider-visible result carries live
        # agent state, so stale snapshots do not accumulate in history.  Mirrors
        # the notification holder above.  Unlike notifications there is no
        # molt-race special case: these are pure per-turn snapshots, not
        # kernel-synchronized channel state.
        #
        # MUST run before _log_notification_block_injected below: the durable
        # snapshot copies the holder's full ``_meta`` envelope, and
        # ``attach_active_runtime`` is what populates ``_meta.agent_meta`` /
        # ``_meta.guidance`` on that holder.  Logging before this ran would
        # persist rows missing those two blocks.
        try:
            agent._runtime_live_holder = attach_active_runtime(
                agent,
                tool_results,
                prior_holder=getattr(agent, "_runtime_live_holder", None),
            )
        except Exception:
            agent._log(
                "runtime_block_attach_failed",
                reason="attach_active_runtime raised",
            )

        # Log the actual canonical ``_meta`` envelope that was stamped onto the
        # tool result so the TUI /notification command can show real snapshots.
        # Only log when a genuinely new notification holder was established
        # (changed and not None), i.e. when notification stamping actually
        # happened this batch.  Runs after attach_active_runtime so the persisted
        # ``_meta`` carries the full envelope (tool_meta/agent_meta/guidance/
        # notifications/notification_guidance).
        if not _batch_includes_context_molt(response.tool_calls):
            _new_holder = agent._notification_live_holder
            _new_meta = _new_holder.get("_meta") if isinstance(_new_holder, dict) else None
            if (
                _new_holder is not None
                and _new_holder is not _prior_holder
                and isinstance(_new_meta, dict)
                and "notifications" in _new_meta
            ):
                try:
                    _carrier_call_id = ""
                    for _result in tool_results:
                        if getattr(_result, "content", None) is _new_holder:
                            _carrier_call_id = str(getattr(_result, "id", "") or "")
                            break
                    agent._log_notification_block_injected(
                        _new_meta,
                        mode="active_tool_result",
                        call_id=_carrier_call_id,
                    )
                except Exception:
                    pass

        if intercepted:
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
            return {
                "text": intercept_text,
                "failed": False,
                "errors": [],
            }

        # Mid-batch cancel: a tool we just ran (e.g. system(action="sleep"))
        # set _cancel_event, meaning the agent has decided to stop this
        # turn. Commit the tool_results to the wire so the assistant turn
        # we just sent has matching pairs (no dangling tool_calls), then
        # return without re-sending to the LLM. Without this, the loop
        # would call agent._session.send(tool_results) below, get back a
        # new assistant response with NEW tool_calls, save those to the
        # wire — and then the cancel check at the top of the next
        # iteration would return, leaving those new tool_calls dangling.
        # That broken wire then blocks all future notification injects.
        if agent._cancel_event.is_set():
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
            agent._cancel_event.clear()
            agent._log("turn_cancelled_post_tool",
                       reason="cancel_event_set_after_tool_execute")
            return {"text": "", "failed": False, "errors": []}

        # Issue #63: dedup check — warn agent if it just re-sent
        # a duplicate message to an external channel.
        _check_external_send(agent, response.tool_calls, tool_results)

        # Issue #63: poll backoff — if the agent is repeatedly checking
        # for new messages without finding any, go IDLE after max retries.
        if _check_poll_backoff(agent, response.tool_calls, tool_results):
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
                # Issue #126: save immediately so the in-memory and on-disk
                # interface agree that tool results are committed. Without
                # this, a notification heartbeat tick between the return
                # and the post-turn save in _run_loop can see a stale wire
                # and heal the (already-committed) tool calls.
                agent._save_chat_history(ledger_source=ledger_source)
            agent._log("idle_after_poll_backoff",
                       reason="poll_retries_exhausted")
            return {
                "text": "\n".join(collected_text_parts),
                "failed": False,
                "errors": [],
            }

        in_tool_loop = True
        try:
            response = agent._session.send(tool_results)
        except Exception:
            # The local tools have already executed and returned results; only
            # the post-tool LLM continuation failed. Some adapters append tool
            # results as part of send(tool_results) and roll that user entry
            # back on provider error. If AED / notification heal sees the tail
            # assistant tool_calls as unanswered, it can only synthesize a
            # completion notice and the real result payload is lost. Restore the
            # real results before re-raising so recovery paths preserve truthful
            # tool completion state.
            _restore_tool_results_after_continuation_failure(
                agent, tool_results, ledger_source=ledger_source,
            )
            raise
        agent._last_usage = response.usage
        agent._save_chat_history(ledger_source=ledger_source)

        # Mid-loop legacy molt-notification sweep. Context pressure is now
        # surfaced every tool result under _meta.agent_meta.context.molt
        # (meta_block.build_meta), so this only clears any stale legacy
        # molt.json left from older builds; it no longer publishes pressure.
        _check_molt_pressure(agent)
        # Synchronous notification sync — same ACTIVE deferral semantics as
        # _handle_request; delivery happens at the next IDLE boundary.
        try:
            agent._sync_notifications()
        except Exception:
            pass
        # Keep summarize reminders in sync across tool-loop LLM rounds too.
        # New tool results are handled by the executor hook, but old live
        # results (or dismissed reminders for still-unsummarized results) must
        # be rediscovered after each continuation round, not only at external
        # request / notification-wake boundaries.
        try:
            agent._rescan_large_tool_results()
        except Exception:
            pass

    final_text = "\n".join(collected_text_parts)
    has_errors = bool(collected_errors)
    no_useful_output = not final_text.strip()
    return {
        "text": final_text,
        "failed": has_errors and no_useful_output,
        "errors": collected_errors,
    }
