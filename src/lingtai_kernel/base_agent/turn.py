"""Turn engine — main loop, message dispatch, LLM send, tool-call processing.

The core message lifecycle: receive → route → LLM → process → persist.
"""
from __future__ import annotations

import json
import queue
import threading
import time

from ..message import Message, _make_message, MSG_REQUEST, MSG_USER_INPUT, MSG_TC_WAKE
from ..i18n import t as _t
from ..logging import get_logger
from ..loop_guard import LoopGuard
from ..tool_executor import ToolExecutor
from ..meta_block import build_meta, render_meta
from ..sent_message_tracker import SEND_TOOLS, SEND_ACTIONS, CHECK_ACTIONS
from ..time_veil import now_iso

logger = get_logger()

# LLM hang watchdog threshold (seconds). If session.send() blocks for
# this long, the agent transitions to STUCK and a signal file is written.
_LLM_HANG_THRESHOLD_SECONDS = 120.0
_LLM_SLOW_THRESHOLD_SECONDS = 60.0

# TTL for the .llm_hang sentinel (seconds). Once written, the sentinel is
# considered stale after this long and is auto-cleared at the wake-refusal
# check so the agent can recover without manual filesystem surgery. See
# issue #35.
_LLM_HANG_SENTINEL_TTL_SECONDS = 300.0


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


def _on_llm_hang(agent) -> None:
    """Watchdog callback: LLM has been unresponsive for too long."""
    from ..state import AgentState

    agent._log("llm_hang_detected",
               seconds=_LLM_HANG_THRESHOLD_SECONDS,
               state=agent._state.value)

    # Write signal file for TUI/supervisor visibility.
    _write_llm_hang_signal(agent)

    # Transition to STUCK if not already in a terminal state
    if agent._state not in (AgentState.STUCK, AgentState.ASLEEP, AgentState.SUSPENDED):
        agent._set_state(AgentState.STUCK, reason="LLM API unresponsive")




def _write_llm_hang_signal(agent, **extra) -> None:
    """Write/update the .llm_hang signal file for TUI/supervisor visibility."""
    try:
        hang_file = agent._working_dir / ".llm_hang"
        payload = {
            "detected_at": time.time(),
            "threshold_seconds": _LLM_HANG_THRESHOLD_SECONDS,
            **extra,
        }
        hang_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _llm_hang_signal_exists(agent) -> bool:
    try:
        return (agent._working_dir / ".llm_hang").exists()
    except OSError:
        return False


def _llm_hang_signal_age(agent) -> float | None:
    """Return seconds since the sentinel's recorded ``detected_at``.

    Falls back to file mtime when the JSON is unreadable or missing the key,
    so a sentinel written by an older format never traps the agent. Returns
    ``None`` only if the file is gone or both reads fail.
    """
    hang_file = agent._working_dir / ".llm_hang"
    try:
        raw = hang_file.read_text(encoding="utf-8")
    except OSError:
        raw = None
    if raw:
        try:
            payload = json.loads(raw)
            detected_at = payload.get("detected_at")
            if isinstance(detected_at, (int, float)):
                return max(0.0, time.time() - float(detected_at))
        except (ValueError, TypeError):
            pass
    try:
        return max(0.0, time.time() - hang_file.stat().st_mtime)
    except OSError:
        return None


def _remove_llm_hang_signal(agent) -> None:
    try:
        (agent._working_dir / ".llm_hang").unlink(missing_ok=True)
    except OSError:
        pass


def _mark_worker_still_running(agent, err) -> None:
    """Record that the provider worker survived timeout+grace."""
    _write_llm_hang_signal(
        agent,
        worker_still_running_at=time.time(),
        error=str(err),
    )


def _handle_worker_still_running(agent, err) -> None:
    """Fail closed after a provider worker outlives timeout+grace.

    The adapter may still mutate the shared ChatInterface, so do not run AED
    repair or retry in this process. Leave a .llm_hang signal requiring an
    explicit refresh before mail can wake the agent into ACTIVE processing.
    """
    from ..state import AgentState

    err_desc = str(err) or repr(err)
    agent._log("llm_worker_still_running", error=err_desc)
    _mark_worker_still_running(agent, err)
    agent._set_state(AgentState.STUCK, reason=err_desc)
    agent._asleep.set()
    agent._set_state(AgentState.ASLEEP, reason="LLM worker still running; refresh required")

def _send_with_watchdog(agent, content):
    """Wrap session.send with a hang watchdog.

    Used by both _handle_request and _handle_tc_wake. Arms a background
    timer; if session.send() blocks past the threshold, the timer fires
    and transitions the agent to STUCK with a signal file. The timer is
    cancelled in the finally block when send returns (whether success or
    failure).
    """
    hang_timer = threading.Timer(
        _LLM_HANG_THRESHOLD_SECONDS,
        _on_llm_hang,
        args=(agent,),
    )
    hang_timer.daemon = True
    hang_timer.start()
    from ..llm_utils import WorkerStillRunningError

    keep_hang_signal = False
    try:
        return agent._session.send(content)
    except WorkerStillRunningError as err:
        keep_hang_signal = True
        _mark_worker_still_running(agent, err)
        # When the orphaned worker future eventually settles (provider
        # finally returns, raises, or its HTTP client drops the socket),
        # clear the sentinel so the agent can wake without waiting out the
        # TTL. The callback runs on the worker thread; only filesystem ops
        # touch agent state, so this is safe. See issue #35.
        future = getattr(err, "future", None)
        if future is not None:
            def _on_worker_exit(_fut, _agent=agent):
                try:
                    _remove_llm_hang_signal(_agent)
                    _agent._log("llm_hang_cleared", reason="worker_exited")
                except Exception:
                    # Never let a cleanup callback raise into the pool.
                    pass
            try:
                future.add_done_callback(_on_worker_exit)
            except Exception:
                # If the pool is gone or callback registration fails, fall
                # back to TTL-based recovery at the wake-refusal site.
                pass
        raise
    finally:
        hang_timer.cancel()
        # Clean up signal file only when the send resolved or failed with an
        # ordinary, settled exception. If the worker is still alive, the
        # signal remains as a wake-time refresh requirement.
        if not keep_hang_signal:
            _remove_llm_hang_signal(agent)


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
                    try:
                        agent._chat.interface.close_pending_tool_calls(
                            reason="heal:going_asleep"
                        )
                        agent._log("heal_pending_tool_calls", reason="going_asleep")
                        agent._save_chat_history(ledger_source="heal")
                    except Exception as e:
                        agent._log(
                            "heal_pending_tool_calls_failed",
                            reason="going_asleep",
                            error=str(e)[:200],
                        )
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

                if _llm_hang_signal_exists(agent):
                    # TTL recovery: a sentinel older than the TTL is presumed
                    # stale (the orphaned worker is long gone but its done
                    # callback never fired, e.g. process restart in the
                    # interim). Clear it and proceed with the wake instead
                    # of leaving the agent stranded forever. See issue #35.
                    age = _llm_hang_signal_age(agent)
                    if age is not None and age > _LLM_HANG_SENTINEL_TTL_SECONDS:
                        _remove_llm_hang_signal(agent)
                        agent._log(
                            "llm_hang_cleared",
                            reason="ttl_expired",
                            age_seconds=round(age, 1),
                        )
                    else:
                        remaining = (
                            _LLM_HANG_SENTINEL_TTL_SECONDS - age
                            if age is not None
                            else _LLM_HANG_SENTINEL_TTL_SECONDS
                        )
                        reason = (
                            f"LLM hang detected. Sentinel auto-clears in "
                            f"{int(max(0, remaining))} seconds, or use "
                            f"system(action='refresh') to clear immediately."
                        )
                        agent._log(
                            "wake_refused_llm_hang",
                            trigger=msg.type,
                            age_seconds=(round(age, 1) if age is not None else None),
                            ttl_remaining_seconds=int(max(0, remaining)),
                        )
                        agent._asleep.set()
                        agent._set_state(AgentState.ASLEEP, reason=reason)
                        continue

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

                    if isinstance(e, WorkerStillRunningError):
                        _handle_worker_still_running(agent, e)
                        sleep_state = AgentState.ASLEEP
                        skip_post_turn_save = True
                        break

                    err_desc = str(e) or repr(e)

                    if _is_transient_provider_error(e):
                        if transient_attempts < _TRANSIENT_AED_RETRY_LIMIT:
                            transient_attempts += 1
                            backoff_s = min(2.0 ** (transient_attempts - 1), 8.0)
                            if agent._session.chat is not None:
                                agent._session.chat.interface.close_pending_tool_calls(
                                    reason=f"transient_retry: {err_desc[:200]}",
                                    tool_completed=True,
                                )
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


def _check_molt_pressure(agent) -> None:
    """Check context pressure and publish/clear molt warnings.

    Extracted from ``_handle_request`` so it can be called from the tool
    loop (``_process_response``) and notification-wake path
    (``_handle_tc_wake``) as well — not just at turn start.

    Uses ``_molt_warning_counted`` (set at turn boundaries) to prevent
    incrementing the warning counter more than once per turn — mid-loop
    calls from ``_process_response`` publish updated notifications but
    don't double-count.
    """
    has_molt = "psyche" in agent._intrinsics
    if not has_molt:
        return

    pressure = agent._session.get_context_pressure()

    if pressure >= agent._config.molt_hard_ceiling:
        if not agent._molt_warning_counted:
            agent._session._compaction_warnings += 1
            agent._molt_warning_counted = True
        warnings = agent._session._compaction_warnings
        max_warnings = agent._config.molt_warnings
        remaining = max(0, max_warnings - warnings)
        lang = agent._config.language
        level = 3
        level_prompt = _t(
            lang,
            "system.molt_warning_level3",
            pressure=f"{pressure:.0%}",
            remaining=remaining,
        )
        level_prompt = level_prompt + "\n\n" + _t(lang, "system.molt_procedure")
        molt_prompt = agent._config.molt_prompt or level_prompt
        status = f"[context: {pressure:.0%} | CRITICAL]"
        from ..intrinsics.system import publish_notification
        publish_notification(
            agent._working_dir, "molt",
            header=f"context {pressure:.0%}, CRITICAL — molt now or overflow recovery will trim",
            icon="🚨",
            priority="high",
            data={
                "pressure": pressure,
                "level": level,
                "warnings": warnings,
                "remaining": remaining,
                "max_warnings": max_warnings,
                "warning_text": molt_prompt,
                "status": status,
            },
        )
        agent._log("molt_hard_ceiling_warning", pressure=pressure, ceiling=agent._config.molt_hard_ceiling)
    elif pressure >= agent._config.molt_pressure:
        max_warnings = agent._config.molt_warnings
        if not agent._molt_warning_counted:
            agent._session._compaction_warnings += 1
            agent._molt_warning_counted = True
        warnings = agent._session._compaction_warnings
        remaining = max(0, max_warnings - warnings)
        lang = agent._config.language
        from ..intrinsics.system import publish_notification
        level = min(warnings, 3)
        level_prompt = _t(
            lang,
            f"system.molt_warning_level{level}",
            pressure=f"{pressure:.0%}",
            remaining=remaining,
        )
        if level >= 2:
            level_prompt = level_prompt + "\n\n" + _t(lang, "system.molt_procedure")
        molt_prompt = agent._config.molt_prompt or level_prompt
        status = f"[context: {pressure:.0%} | {remaining}/{max_warnings}]"
        publish_notification(
            agent._working_dir, "molt",
            header=f"context {pressure:.0%}, {remaining}/{max_warnings} turns left",
            icon=("⚠️" if level >= 2 else "🪶"),
            priority=("high" if level >= 2 else "normal"),
            data={
                "pressure": pressure,
                "level": level,
                "warnings": warnings,
                "remaining": remaining,
                "max_warnings": max_warnings,
                "warning_text": molt_prompt,
                "status": status,
            },
        )
    else:
        # Pressure dropped below threshold — clear any stale molt notice
        # and reset the warning counter so it doesn't escalate faster
        # than it should across pressure oscillations.
        from ..intrinsics.system import clear_notification
        clear_notification(agent._working_dir, "molt")
        if agent._session._compaction_warnings > 0:
            agent._session._compaction_warnings = 0


def _handle_request(agent, msg: Message) -> None:
    """Send request to LLM, process response with tool calls."""
    # Reset per-turn molt warning flag so the counter increments at most
    # once per turn (mid-loop _check_molt_pressure calls update the
    # notification but don't double-count).
    agent._molt_warning_counted = False

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

    prefix = render_meta(agent, meta)
    if prefix:
        content = f"{prefix}\n\n{content}"
    agent._log("text_input", text=content)
    response = _send_with_watchdog(agent, content)
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
    ``system(action="notification")`` call from the agent's
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
    # Reset per-turn molt warning flag (same as _handle_request).
    agent._molt_warning_counted = False

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
        agent._log("tc_wake_noop", reason="pending_tool_calls")
        return

    agent._executor = ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=LoopGuard(
            max_total_calls=agent._config.max_turns,
            dup_free_passes=2,
            dup_hard_block=8,
        ),
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        parallel_safe_tools=agent._PARALLEL_SAFE_TOOLS,
        logger_fn=agent._log,
        meta_fn=lambda: build_meta(agent),
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
            response = _send_with_watchdog(agent, [item.result])
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
        response = _send_with_watchdog(agent, None)
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

    Uses config.max_turns as the basis.
    """
    max_turns = agent._config.max_turns
    return (max_turns, 2, 8)


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
                        for tr in tool_results:
                            if tr.id == tc.id:
                                tr.content = (
                                    (tr.content or "")
                                    + "\n⚠️ Recently sent similar message"
                                    " to this recipient. Skipping duplicate."
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


def _check_poll_backoff(agent, tool_calls) -> bool:
    """Check if polling actions should trigger idle-after-backoff.

    Counts consecutive check/read calls on external channel tools within
    the same turn. After ``max_poll_retries`` consecutive checks on the
    same channel, returns True to signal the agent should go IDLE.

    The counter resets when a send action occurs or when new messages
    arrive via the notification system.
    """
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
        # Record a poll attempt (assume no new messages — the conservative
        # default). If new messages actually arrived, the notification
        # system will wake the agent via a separate path.
        tracker.record_poll(tc.name, found_new=False)
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
            agent._log(
                "empty_llm_response",
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                output_tokens=response.usage.output_tokens,
                thinking_tokens=response.usage.thinking_tokens,
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

        if agent._cancel_event.is_set():
            agent._cancel_event.clear()
            return {"text": "", "failed": False, "errors": []}

        stop_reason = guard.check_limit(len(response.tool_calls))
        if stop_reason:
            break

        invalid_reason = guard.check_invalid_tool_limit()
        if invalid_reason:
            break

        # Delegate to ToolExecutor
        tool_results, intercepted, intercept_text = agent._executor.execute(
            response.tool_calls,
            on_result_hook=agent._on_tool_result_hook,
            cancel_event=agent._cancel_event,
            collected_errors=collected_errors,
        )

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
        # would call _send_with_watchdog(tool_results) below, get back a
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
        if _check_poll_backoff(agent, response.tool_calls):
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
            agent._log("idle_after_poll_backoff",
                       reason="poll_retries_exhausted")
            return {
                "text": "\n".join(collected_text_parts),
                "failed": False,
                "errors": [],
            }

        guard.record_calls(len(response.tool_calls))

        # Break on repeated identical errors
        if (
            len(collected_errors) >= 2
            and collected_errors[-1] == collected_errors[-2]
        ):
            logger.warning(
                "[%s] Same error repeated, breaking early: %s",
                agent.agent_name,
                collected_errors[-1],
            )
            break

        in_tool_loop = True
        response = _send_with_watchdog(agent, tool_results)
        agent._last_usage = response.usage
        agent._save_chat_history(ledger_source=ledger_source)

        # Mid-loop pressure check — fire warnings during extended tool
        # chains so the agent doesn't go from below-threshold to hard
        # ceiling in a single turn with zero warnings.
        _check_molt_pressure(agent)
        # Synchronous notification sync — same ACTIVE deferral semantics as
        # _handle_request; delivery happens at the next IDLE boundary.
        try:
            agent._sync_notifications()
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
