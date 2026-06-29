"""Lifecycle — start, stop, heartbeat, signal-file detection, refresh, preset fallback.

The agent's life support: starting, stopping, breathing, detecting signal
files (.sleep, .suspend, .refresh, .prompt, .clear, .inquiry, .rules,
.interrupt), enforcing stamina, managing AED timeout, and running periodic
snapshots.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path


# Key source files to hash for the runtime fingerprint.  A small curated
# list keeps startup cost low (<5ms) while still catching the most common
# source-level drift vectors.
_FP_KEY_FILES: list[str] = [
    "base_agent/__init__.py",
    "base_agent/lifecycle.py",
    "base_agent/turn.py",
    "nudge/__init__.py",
    "nudge/kernel_version.py",
    "intrinsics/system/__init__.py",
    "meta_block.py",
    "notifications.py",
    "workdir.py",
]


def _capture_runtime_fingerprint() -> dict:
    """Capture a dual fingerprint of the running lingtai_kernel source.

    Returns a dict with:
      - ``git_rev``: short git HEAD hash, or ``None`` if unavailable
      - ``source_digest``: SHA-256 hex prefix (12 chars) of key source files
      - ``captured_at``: ISO-8601 timestamp
    """
    # Resolve the lingtai_kernel package source directory
    try:
        import lingtai_kernel
        pkg_dir = Path(lingtai_kernel.__file__).resolve().parent  # type: ignore[arg-type]
    except Exception:
        pkg_dir = None

    # git rev-parse --short HEAD
    git_rev: str | None = None
    if pkg_dir is not None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(pkg_dir),
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                git_rev = result.stdout.strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # Hash key source files
    source_digest: str | None = None
    if pkg_dir is not None:
        h = hashlib.sha256()
        for rel in _FP_KEY_FILES:
            fp = pkg_dir / rel
            try:
                h.update(fp.read_bytes())
            except OSError:
                h.update(b"\x00")  # missing file marker
        source_digest = h.hexdigest()[:12]

    return {
        "git_rev": git_rev,
        "source_digest": source_digest,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _active_stuck_threshold_s() -> float:
    """Issue #164 — seconds of no-progress ACTIVE before the watchdog fires.

    Defaults to 600s (~10 min). Overridable via
    ``LINGTAI_ACTIVE_STUCK_THRESHOLD_S`` so operators can tune for noisy
    LLM providers without changing kernel code.
    """
    try:
        return max(30.0, float(os.environ.get("LINGTAI_ACTIVE_STUCK_THRESHOLD_S", "600")))
    except (TypeError, ValueError):
        return 600.0


def _start(agent) -> None:
    """Start the agent's main loop thread."""
    from ..intrinsics.soul.flow import _start_soul_timer, _rehydrate_appendix_tracking
    from ..token_ledger import sum_token_ledger

    agent._sealed = True
    if agent._thread and agent._thread.is_alive():
        return
    agent._shutdown.clear()

    # Initialize git repo in working directory (only if snapshots enabled)
    if agent._config.snapshot_interval is not None:
        agent._workdir.init_git()

    # Capture startup time for uptime tracking
    from datetime import datetime, timezone
    agent._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent._uptime_anchor = time.monotonic()

    # Export assembled system prompt to system/system.md
    agent._flush_system_prompt()

    # Publish process liveness before the heavier restore/listener startup
    # work. During this phase the heartbeat loop only emits runtime metadata;
    # signal and notification handling starts after the main loop exists.
    agent._heartbeat_runtime_ready = False
    _start_heartbeat(agent)

    # Restore chat session and token state from filesystem if available
    chat_history_file = agent._working_dir / "history" / "chat_history.jsonl"
    if chat_history_file.is_file():
        try:
            # Mark stale spill manifests before the LLM sees them so
            # expired sidecar files are flagged honestly.
            from ..tool_result_artifacts import mark_expired_spill_manifests
            try:
                expired = mark_expired_spill_manifests(agent._working_dir)
                if expired:
                    agent._log("spill_manifests_expired_on_restore", count=expired)
            except Exception:
                pass  # best-effort; don't block startup

            messages = [
                json.loads(line)
                for line in chat_history_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            agent.restore_chat({"messages": messages})
            agent._log("session_restored")
            _rehydrate_appendix_tracking(agent)
        except Exception as e:
            from ..logging import get_logger
            get_logger().warning(f"[{agent.agent_name}] Failed to restore chat history: {e}")

    # Rehydrate any still-open WorkerStillRunning recovery artifacts into a
    # high-priority notification so the next process re-surfaces the unfinished
    # turn after refresh/relaunch.
    try:
        from .worker_recovery import rehydrate_worker_hang_recovery

        rehydrated = rehydrate_worker_hang_recovery(agent)
        if rehydrated:
            agent._log("worker_hang_recovery_rehydrated", count=rehydrated)
    except Exception as e:
        try:
            agent._log("worker_hang_recovery_rehydrate_failed", error=str(e)[:300])
        except Exception:
            pass
    # Restore token state from ledger (lifetime accumulator)
    try:
        ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
        totals = sum_token_ledger(ledger_path)
        agent.restore_token_state(totals)
    except Exception as e:
        from ..logging import get_logger
        get_logger().warning(f"[{agent.agent_name}] Failed to restore token state from ledger: {e}")

    # Start MailService listener if configured
    if agent._mail_service is not None:
        try:
            agent._mail_service.listen(on_message=lambda payload: agent._on_mail_received(payload))
        except RuntimeError:
            pass  # Already listening — that's fine

    # Capture runtime fingerprint for drift detection
    try:
        agent._runtime_fingerprint = _capture_runtime_fingerprint()
    except Exception:
        agent._runtime_fingerprint = None

    agent._thread = threading.Thread(
        target=agent._run_loop,
        daemon=True,
        name=f"agent-{agent.agent_name or agent._working_dir.name}",
    )
    agent._thread.start()
    agent._heartbeat_runtime_ready = True
    # Boot state is IDLE (fire-eligible) — start the timer here.
    _start_soul_timer(agent)


def _reset_uptime(agent) -> None:
    """Reset the uptime anchor for stamina tracking (used on wake from asleep)."""
    agent._uptime_anchor = time.monotonic()


def _stop(agent, timeout: float = 5.0) -> None:
    """Signal shutdown and wait for the agent thread to exit.

    Heartbeat is stopped LAST (just before `release_lock`) so external
    observers — TUI launcher, `lingtai-tui list`, `lingtai-tui purge` — see
    `.agent.heartbeat` as fresh and present for the entire teardown window.
    Otherwise the file vanishes seconds before the Python process actually
    exits, and a quick relaunch races a still-living interpreter into the
    same workdir. See workdir-race investigation 2026-05-09.

    Daemon resources are also reclaimed before liveness is withdrawn: daemon
    ThreadPoolExecutor workers and external CLI process groups can otherwise
    keep this interpreter visible in `ps` after heartbeat/lock are gone, which
    makes refresh watchers race the duplicate-process guard.
    """
    from ..intrinsics.soul.flow import _cancel_soul_timer

    agent._log("agent_stop")
    _cancel_soul_timer(agent)
    agent._shutdown.set()
    if agent._thread:
        agent._thread.join(timeout=timeout)
    _shutdown_daemon_runtime(agent, reason="agent_stop")
    agent._session.close()

    # Stop MailService if configured
    if agent._mail_service is not None:
        try:
            agent._mail_service.stop()
        except Exception:
            pass

    # Close LoggingService if configured
    if agent._log_service is not None:
        try:
            agent._log_service.close()
        except Exception:
            pass

    # Persist final state, stop heartbeat, release lock — order matters.
    # See docstring above; heartbeat must remain fresh until this point.
    agent._workdir.write_manifest(agent._build_manifest())
    _stop_heartbeat(agent)
    agent._workdir.release_lock()


def _shutdown_daemon_runtime(agent, *, reason: str) -> None:
    """Best-effort daemon cleanup before parent liveness is released."""
    mgr = None
    try:
        get_capability = getattr(agent, "get_capability", None)
        if callable(get_capability):
            mgr = get_capability("daemon")
        if mgr is None:
            mgr = getattr(agent, "_capability_managers", {}).get("daemon")
    except Exception as e:
        try:
            agent._log("daemon_lifecycle_lookup_failed", reason=reason, error=str(e))
        except Exception:
            pass
        return

    shutdown = getattr(mgr, "shutdown_for_agent_stop", None)
    if not callable(shutdown):
        return
    try:
        shutdown(reason=reason)
    except Exception as e:
        # Stop/refresh teardown must continue even if daemon cleanup races with
        # already-finished workers. Keep heartbeat/lock alive until this point,
        # log the failure, then proceed to the rest of stop.
        try:
            agent._log("daemon_lifecycle_shutdown_failed", reason=reason, error=str(e))
        except Exception:
            pass


def _start_heartbeat(agent) -> None:
    """Start the heartbeat daemon thread."""
    if agent._heartbeat_thread is not None:
        return
    agent._heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(agent,),
        daemon=True,
        name=f"heartbeat-{agent.agent_name or agent._working_dir.name}",
    )
    agent._heartbeat_thread.start()
    agent._log("heartbeat_start")


def _stop_heartbeat(agent) -> None:
    """Stop the heartbeat (called only by stop/shutdown)."""
    thread = agent._heartbeat_thread
    agent._heartbeat_thread = None  # signals the loop to exit
    if thread is not None:
        thread.join(timeout=5.0)
    hb_file = agent._working_dir / ".agent.heartbeat"
    try:
        hb_file.unlink(missing_ok=True)
    except OSError:
        pass
    agent._log("heartbeat_stop", heartbeat=agent._heartbeat)


def _heartbeat_loop(agent) -> None:
    """Beat every 1 second. AED if agent is STUCK.

    Loop exit is governed solely by `agent._heartbeat_thread is None`, which
    `_stop_heartbeat` flips at the very end of `_stop`. The loop deliberately
    keeps writing fresh timestamps even after `agent._shutdown.is_set()` so
    the heartbeat file remains a faithful "this Python process is alive"
    signal across the entire teardown — preventing duplicate-launch races
    in the TUI. Signal-file detection IS gated on `_shutdown` below so we
    don't reprocess `.suspend`/`.refresh` mid-teardown.
    """
    from ..state import AgentState
    from ..intrinsics.soul.inquiry import _run_inquiry

    while agent._heartbeat_thread is not None:
        _write_heartbeat_tick(agent)

        # Once shutdown is signalled, keep beating the file (above) but stop
        # consuming signal files — the run loop is exiting and reprocessing
        # `.suspend`/`.refresh` here would emit spurious state-change events.
        if agent._shutdown.is_set() or not getattr(agent, "_heartbeat_runtime_ready", True):
            time.sleep(1.0)
            continue

        # --- signal file detection ---
        interrupt_file = agent._working_dir / ".interrupt"
        if interrupt_file.is_file():
            try:
                interrupt_file.unlink()
            except OSError:
                pass
            agent._cancel_event.set()
            agent._log("interrupt_received", source="signal_file")

        # .refresh = full refresh with relaunch (identical to system(action='refresh'))
        refresh_file = agent._working_dir / ".refresh"
        if refresh_file.is_file():
            taken_file = agent._working_dir / ".refresh.taken"
            try:
                refresh_file.rename(taken_file)
            except OSError:
                pass
            # Delegate to _perform_refresh which handles the full flow:
            # save chat history, spawn watcher process, deferred relaunch.
            _perform_refresh(agent)
            # Signal shutdown so the heartbeat loop exits and the watcher
            # can detect the lock release.  The _shutdown gate above
            # prevents the heartbeat from reprocessing .refresh on the
            # next tick.
            agent._shutdown.set()

        # .suspend = SUSPENDED (full process death, external only)
        suspend_file = agent._working_dir / ".suspend"
        if suspend_file.is_file():
            try:
                suspend_file.unlink()
            except OSError:
                pass
            agent._cancel_event.set()
            agent._set_state(AgentState.SUSPENDED, reason="suspend signal")
            agent._shutdown.set()
            agent._log("suspend_received", source="signal_file")

        # .sleep = ASLEEP (sleep, listeners stay alive)
        sleep_file = agent._working_dir / ".sleep"
        if sleep_file.is_file():
            try:
                sleep_file.unlink()
            except OSError:
                pass
            agent._cancel_event.set()
            agent._set_state(AgentState.ASLEEP, reason="sleep signal")
            agent._asleep.set()
            agent._log("sleep_received", source="signal_file")

        # .prompt = inject text input as [system] message
        prompt_file = agent._working_dir / ".prompt"
        if prompt_file.is_file():
            try:
                content = prompt_file.read_text(encoding="utf-8").strip()
            except OSError:
                content = ""
            try:
                prompt_file.unlink()
            except OSError:
                pass
            if content:
                agent.send(content, sender="system")
                agent._log("prompt_received", source="signal_file")

        # .clear = force a full molt (context wipe + recovery summary).
        clear_file = agent._working_dir / ".clear"
        if clear_file.is_file():
            try:
                source = clear_file.read_text(encoding="utf-8").strip() or "admin"
            except OSError:
                source = "admin"
            try:
                clear_file.unlink()
            except OSError:
                pass
            try:
                from ..intrinsics import psyche as _psyche
                _psyche.context_forget(agent, source=source)
                agent._log("clear_received", source=source)
            except Exception as clear_err:
                from ..logging import get_logger
                get_logger().error(
                    f"[{agent.agent_name}] .clear signal failed: {clear_err}",
                )

        # .inquiry = soul inquiry (from TUI /btw or auto-insight)
        inquiry_file = agent._working_dir / ".inquiry"
        taken_file = agent._working_dir / ".inquiry.taken"
        if inquiry_file.is_file() and not taken_file.is_file():
            try:
                inquiry_file.rename(taken_file)
            except OSError:
                pass
            else:
                try:
                    content = taken_file.read_text(encoding="utf-8").strip()
                except OSError:
                    content = ""
                if content:
                    lines = content.split("\n", 1)
                    if len(lines) == 2 and lines[0] in ("human", "insight", "agent"):
                        source, question = lines[0], lines[1].strip()
                    else:
                        source, question = "human", content.strip()
                    if question:
                        def _inquiry_done(q: str, s: str, tf) -> None:
                            _run_inquiry(agent, q, source=s)
                            try:
                                tf.unlink()
                            except OSError:
                                pass
                        threading.Thread(
                            target=_inquiry_done,
                            args=(question, source, taken_file),
                            daemon=True,
                        ).start()
                    else:
                        try:
                            taken_file.unlink()
                        except OSError:
                            pass
                else:
                    try:
                        taken_file.unlink()
                    except OSError:
                        pass

        # .rules = network rules signal
        _check_rules_file(agent)

        # --- Nudges ---
        # Per-agent periodic checks that publish to `.notification/nudge.json`
        # when something needs the agent's attention (e.g. a newer lingtai
        # wheel is installed on disk than the version this process imported).
        # Each check throttles itself; the dispatcher wraps individual calls
        # so a misbehaving check cannot block the heartbeat loop. See
        # `nudge/ANATOMY.md`.
        try:
            from ..nudge import run_checks as _run_nudge_checks
            _run_nudge_checks(agent)
        except Exception as nudge_err:
            from ..logging import get_logger
            get_logger().warning(
                f"[{agent.agent_name}] nudge dispatch failed: {nudge_err}"
            )

        # --- Notification sync ---
        # Poll the `.notification/` directory for changes.  The sync
        # method is a no-op when the fingerprint is unchanged, so this
        # call is cheap on the steady-state path.  On change it strips
        # the prior wire block and reinjects per current state (IDLE
        # pair / ACTIVE meta-stash / ASLEEP wake-then-pair).  See
        # base_agent/__init__.py:_sync_notifications and
        # the notification filesystem design rationale.
        try:
            agent._sync_notifications()
        except Exception as notif_err:
            from ..logging import get_logger
            get_logger().warning(
                f"[{agent.agent_name}] notification sync failed: {notif_err}"
            )

        # Stamina enforcement — asleep when stamina expires
        if agent._uptime_anchor is not None and agent._state not in (AgentState.ASLEEP, AgentState.SUSPENDED):
            elapsed = time.monotonic() - agent._uptime_anchor
            if elapsed >= agent._config.stamina:
                agent._log("stamina_expired", elapsed=round(elapsed, 1), stamina=agent._config.stamina)
                agent._cancel_event.set()
                agent._set_state(AgentState.ASLEEP, reason="stamina expired")
                agent._asleep.set()

        if agent._state == AgentState.STUCK:
            now = time.monotonic()
            if agent._aed_start is None:
                agent._aed_start = now
            if now - agent._aed_start > agent._config.aed_timeout:
                agent._log("aed_timeout", seconds=now - agent._aed_start)
                agent._set_state(AgentState.ASLEEP, reason="AED timeout")
                agent._save_chat_history()
                agent._asleep.set()
        else:
            agent._aed_start = None

        # Issue #164 — ACTIVE-without-progress watchdog.
        #
        # Fires once per stuck episode (latched by ``_active_stuck_logged``)
        # when the agent has been ACTIVE for longer than the configured
        # threshold without any progress event (wake, llm_call, llm_response,
        # tool_call, tool_result, notification_pair_injected, agent_state).
        # The companion symptom — a ``notification_deferred_active`` storm —
        # is included in the log fields so a single grep on
        # ``active_without_progress`` exposes both halves of the failure.
        #
        # We deliberately do NOT auto-recover here: the failure modes seen
        # in dev-2/dev-1/spiritualblisslingtaibot all benefited from human
        # inspection before .clear/refresh. Auto-restart could mask a
        # repeatable bug behind silent retries.
        if agent._state == AgentState.ACTIVE and not agent._active_stuck_logged:
            threshold = _active_stuck_threshold_s()
            no_progress_for = time.time() - agent._last_progress_at
            if no_progress_for > threshold:
                agent._log(
                    "active_without_progress",
                    no_progress_seconds=round(no_progress_for, 1),
                    threshold_seconds=threshold,
                    state_since=agent._state_changed_at,
                    active_turn_kind=agent._active_turn_kind,
                    active_turn_id=agent._active_turn_id,
                    deferred_notifications=agent._deferred_notifications_count,
                    deferred_oldest_at=agent._deferred_notifications_oldest_at,
                )
                agent._write_status_snapshot()
                agent._active_stuck_logged = True

        # Periodic snapshot (Time Machine) — off by default
        if agent._config.snapshot_interval is not None:
            now_mono = time.monotonic()
            if now_mono - agent._last_snapshot >= agent._config.snapshot_interval:
                agent._workdir.snapshot()
                agent._last_snapshot = now_mono

            # Periodic GC — every 24 hours
            if now_mono - agent._last_gc >= 86400:
                agent._workdir.gc()
                agent._last_gc = now_mono

        time.sleep(1.0)


def _write_heartbeat_tick(agent) -> None:
    """Write one real runtime heartbeat and best-effort status snapshot."""
    # time.time() (wall clock), not time.monotonic(). Deliberate:
    # heartbeat is written to a file and read by handshake.is_alive()
    # in a DIFFERENT process.
    agent._heartbeat = time.time()

    try:
        hb_file = agent._working_dir / ".agent.heartbeat"
        hb_file.write_text(str(agent._heartbeat), encoding="utf-8")
    except OSError:
        pass

    try:
        agent._write_status_snapshot()
    except Exception:
        pass


def _perform_refresh(
    agent,
    *,
    skip_chat_history_save: bool = False,
    skip_save_reason: str | None = None,
) -> None:
    """Refresh = .refresh handshake + deferred relaunch.

    Self-sufficient across all call sites — heartbeat, tool-call (intrinsic
    ``system(action='refresh')``), and AED preset-fallback in ``turn.py`` all
    call directly. Two filesystem signals drive the watcher subprocess:

      1. ``.refresh.taken`` must exist before the watcher's ack deadline.
      2. ``.agent.lock`` must clear before the watcher's lock deadline.

    The heartbeat path renames ``.refresh`` → ``.refresh.taken`` before
    invoking us and sets ``agent._shutdown`` immediately after. Direct
    callers do neither — so we normalize the handshake here and then set
    ``_shutdown`` / ``_cancel_event`` ourselves so the watcher's second
    phase can complete.
    """
    import os
    import subprocess
    import sys

    # When the worker interface is poisoned, the in-memory ChatInterface may
    # still be mutated by a stuck worker thread — saving it would serialize
    # unsafe state. Fail closed: skip the save and rebuild from disk.
    poisoned = bool(getattr(agent, "_llm_worker_interface_poisoned", False))
    effective_skip_save = skip_chat_history_save or poisoned
    effective_skip_reason = (
        skip_save_reason
        or ("worker_still_running_interface_unsafe" if poisoned else None)
    )
    agent._log(
        "refresh_start",
        skip_chat_history_save=effective_skip_save,
        skip_save_reason=effective_skip_reason,
    )
    if effective_skip_save:
        agent._log("refresh_chat_history_save_skipped", reason=effective_skip_reason)
    else:
        agent._save_chat_history()
    # Bound-method dispatch — _build_launch_cmd lives on BaseAgent (returns
    # None) and Agent (returns the real `lingtai-agent run` cmd). A prior version
    # called a module-level _build_launch_cmd shadow that always returned
    # None, silently no-opping every user refresh on the Agent subclass —
    # see issue #7, confirmed in vivo against deepseek_pro 2026-05-05.
    cmd = agent._build_launch_cmd()
    if cmd is None:
        agent._log("refresh_no_launch_cmd")
        return

    working_dir = agent._working_dir
    refresh_path = working_dir / ".refresh"
    taken_path_obj = working_dir / ".refresh.taken"
    # Handshake normalization — make the on-disk state look the same
    # regardless of caller. The watcher polls for `.refresh.taken`; we
    # guarantee it exists before spawning the watcher, then remove any
    # remaining `.refresh` so the heartbeat doesn't fire a duplicate
    # watcher on its next tick.
    handshake_source = None
    if taken_path_obj.exists():
        handshake_source = "preexisting_taken"
    elif refresh_path.exists():
        try:
            refresh_path.rename(taken_path_obj)
            handshake_source = "renamed_refresh"
        except OSError:
            # Rename failed (e.g. cross-device, race). Fall back to a
            # synthesized ack so the watcher can still proceed.
            try:
                taken_path_obj.touch()
                handshake_source = "synthesized_after_rename_failed"
            except OSError:
                handshake_source = "ack_write_failed"
    else:
        try:
            taken_path_obj.touch()
            handshake_source = "synthesized_direct_call"
        except OSError:
            handshake_source = "ack_write_failed"
    if not taken_path_obj.exists():
        # Do not spawn a watcher or shut the agent down unless the ack
        # invariant is actually established. Otherwise an unusual
        # filesystem failure could turn a failed refresh into a dead
        # agent with no relaunch. If .refresh still exists, leave it for
        # the heartbeat path or a later retry rather than consuming it.
        agent._log("refresh_ack_failed", handshake=handshake_source)
        return

    # If both files happen to exist (heartbeat renamed but a later
    # consumer rewrote .refresh), remove the stale .refresh so the
    # heartbeat does not spawn a second watcher.
    try:
        refresh_path.unlink(missing_ok=True)
    except OSError:
        pass

    taken_path = str(taken_path_obj)
    lock_path = str(working_dir / ".agent.lock")
    events_path = str(working_dir / "logs" / "events.jsonl")
    agent_name = agent.agent_name
    address = agent._working_dir.name
    working_dir_str = str(working_dir)
    stderr_log = str(working_dir / "logs" / "refresh_relaunch.log")
    relaunch_script = (
        "import time, subprocess, os, sys, json, signal\n"
        "from datetime import datetime, timezone\n"
        f"taken = {taken_path!r}\n"
        f"lock = {lock_path!r}\n"
        f"events = {events_path!r}\n"
        f"stderr_log = {stderr_log!r}\n"
        f"wd = {working_dir_str!r}\n"
        f"cmd = {cmd!r}\n"
        f"name = {agent_name!r}\n"
        f"addr = {address!r}\n"
        "MAX_ATTEMPTS = 12\n"
        "HEALTH_CHECK_WAIT = 10\n"
        # The watcher writes events.jsonl through its own log() below, bypassing
        # the in-process CompositeLoggingService.redact_for_trajectory. Secret-
        # shaped values reach these events via stderr_tail (relaunched-process
        # stderr, e.g. a config traceback echoing a token), cmdline, and error
        # strings, so redact the whole event dict here before persisting. Use the
        # kernel's redact_for_trajectory (not just redact_text value-walking) so
        # the watcher gets the same key-aware redaction as normal trajectory
        # logging: values under secret-named keys are removed even when they do
        # not match a known token shape. The kernel redactor is the single source
        # of truth; fail open to identity if it cannot be imported so the watcher
        # never crashes over redaction, but record a non-secret marker so the
        # degradation is diagnosable rather than silent.
        "try:\n"
        "    from lingtai_kernel.trace_redaction import redact_for_trajectory as _redact_for_trajectory\n"
        "    _REDACTOR_IMPORT_OK = True\n"
        "except Exception:\n"
        "    def _redact_for_trajectory(value):\n"
        "        return value\n"
        "    _REDACTOR_IMPORT_OK = False\n"
        "try:\n"
        "    from lingtai_kernel.runtime_identity import runtime_identity_event_fields as _runtime_identity_event_fields\n"
        "except Exception:\n"
        "    def _runtime_identity_event_fields():\n"
        "        return {}\n"
        # Terminal-failure visibility (PR #292): when all relaunch attempts are
        # exhausted the watcher writes logs/refresh_failed_permanent.json and a
        # high-priority system notification carrying this failure_state so the
        # dead agent is diagnosable rather than silently gone. failure_state is
        # mutated in place across attempts by the relaunch loop and cleanup
        # helpers below.
        "STDERR_TAIL_CHARS = 1200\n"
        "failure_artifact = os.path.join(wd, 'logs', 'refresh_failed_permanent.json')\n"
        "RECOVERY_GUIDANCE = [\n"
        "    'Inspect logs/refresh_relaunch.log and logs/events.jsonl for the relaunch failure.',\n"
        "    'Run system(action=\"cpr\") or manually restart the agent after resolving the blocker.',\n"
        "    'If a duplicate PID is listed, verify it is this same agent before terminating it.',\n"
        "    'Do not delete .agent.lock by path; the kernel lock is advisory fd-based.',\n"
        "]\n"
        "failure_state = {\n"
        "    'attempts': MAX_ATTEMPTS,\n"
        "    'last_pid': None,\n"
        "    'last_duplicate_pid': None,\n"
        "    'last_relaunch_pid': None,\n"
        "    'last_heartbeat_age': None,\n"
        "    'last_heartbeat_status': 'unknown',\n"
        "    'last_stderr_tail': '',\n"
        "    'last_cleanup_action': 'not_attempted',\n"
        "    'last_cleanup_result': 'not_attempted',\n"
        "    'last_cleanup_error': None,\n"
        "    'last_relaunch_error': None,\n"
        "    'stderr_log': stderr_log,\n"
        "    'recovery_guidance': RECOVERY_GUIDANCE,\n"
        "}\n"
        "def log(typ, **kw):\n"
        "    entry = {'type': typ, 'address': addr, 'agent_name': name, 'ts': time.time(), **_runtime_identity_event_fields(), **kw}\n"
        "    if not _REDACTOR_IMPORT_OK:\n"
        "        entry['redaction_unavailable'] = True\n"
        "    else:\n"
        "        try:\n"
        "            entry = _redact_for_trajectory(entry)\n"
        "        except Exception:\n"
        "            entry = {'type': typ, 'address': addr, 'agent_name': name,\n"
        "                     'ts': entry.get('ts'), 'redaction_unavailable': True,\n"
        "                     'redaction_error': True}\n"
        "    try:\n"
        "        with open(events, 'a') as f:\n"
        "            f.write(json.dumps(entry) + '\\n')\n"
        "    except OSError:\n"
        "        pass\n"
        "def _now_iso():\n"
        "    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')\n"
        "def _bounded(text, limit=STDERR_TAIL_CHARS):\n"
        "    if text is None:\n"
        "        return ''\n"
        "    text = str(text)\n"
        "    return text[-limit:]\n"
        "def _write_json_atomic(path, payload):\n"
        "    os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "    tmp = f'{path}.tmp.{os.getpid()}'\n"
        "    with open(tmp, 'w', encoding='utf-8') as f:\n"
        "        json.dump(payload, f, ensure_ascii=False)\n"
        "        f.write('\\n')\n"
        "    os.replace(tmp, path)\n"
        "def _heartbeat_snapshot():\n"
        "    age = heartbeat_age()\n"
        "    if age is None:\n"
        "        return None, 'missing'\n"
        "    if age < 30:\n"
        "        return age, 'fresh'\n"
        "    return age, 'stale'\n"
        "def _read_stderr_tail():\n"
        "    try:\n"
        "        with open(stderr_log, encoding='utf-8', errors='replace') as f:\n"
        "            return _bounded(f.read())\n"
        "    except OSError:\n"
        "        return ''\n"
        "def _failure_metadata():\n"
        "    meta = dict(failure_state)\n"
        "    meta['attempts'] = MAX_ATTEMPTS\n"
        "    stderr_tail = _bounded(meta.get('last_stderr_tail'))\n"
        "    if stderr_tail and _REDACTOR_IMPORT_OK:\n"
        "        try:\n"
        "            meta['last_stderr_tail'] = _redact_for_trajectory(stderr_tail)\n"
        "        except Exception:\n"
        "            meta['last_stderr_tail'] = '<REDACTED:redaction-error>'\n"
        "            meta['redaction_unavailable'] = True\n"
        "    elif stderr_tail:\n"
        "        meta['last_stderr_tail'] = '<REDACTED:redaction-unavailable>'\n"
        "        meta['redaction_unavailable'] = True\n"
        "    else:\n"
        "        meta['last_stderr_tail'] = ''\n"
        "    meta['artifact_path'] = failure_artifact\n"
        "    return meta\n"
        "def _append_system_notification(meta):\n"
        "    notif_dir = os.path.join(wd, '.notification')\n"
        "    target = os.path.join(notif_dir, 'system.json')\n"
        "    current = {}\n"
        "    try:\n"
        "        with open(target, encoding='utf-8') as f:\n"
        "            current = json.load(f)\n"
        "    except (OSError, ValueError, TypeError):\n"
        "        current = {}\n"
        "    if not isinstance(current, dict):\n"
        "        current = {}\n"
        "    events_list = current.get('data', {}).get('events', [])\n"
        "    if not isinstance(events_list, list):\n"
        "        events_list = []\n"
        "    event_id = f'evt_refresh_{int(time.time()*1000):x}_{os.getpid()}'\n"
        "    body = (\n"
        "        f'Refresh failed permanently after {MAX_ATTEMPTS} attempts. '\n"
        "        'Inspect logs/refresh_relaunch.log and restart with system(action=\"cpr\") '\n"
        "        'or a manual launch after resolving the blocker.'\n"
        "    )\n"
        "    events_list.append({\n"
        "        'event_id': event_id,\n"
        "        'source': 'refresh',\n"
        "        'ref_id': 'refresh_failed_permanent',\n"
        "        'body': body,\n"
        "        'at': _now_iso(),\n"
        "        'metadata': meta,\n"
        "    })\n"
        "    events_list = events_list[-20:]\n"
        "    payload = {\n"
        "        'header': f'{len(events_list)} system notification' + ('' if len(events_list) == 1 else 's'),\n"
        "        'icon': '!',\n"
        "        'priority': 'high',\n"
        "        'published_at': _now_iso(),\n"
        "        'instructions': 'Read the refresh event metadata, inspect the relaunch log, then recover with cpr or a manual restart.',\n"
        "        'data': {'events': events_list},\n"
        "    }\n"
        "    _write_json_atomic(target, payload)\n"
        "    return event_id\n"
        "def _publish_refresh_failed_permanent():\n"
        "    meta = _failure_metadata()\n"
        "    artifact = {\n"
        "        'type': 'refresh_failed_permanent',\n"
        "        'address': addr,\n"
        "        'agent_name': name,\n"
        "        'created_at': _now_iso(),\n"
        "        'metadata': meta,\n"
        "    }\n"
        "    alert_id = None\n"
        "    alert_error = None\n"
        "    try:\n"
        "        _write_json_atomic(failure_artifact, artifact)\n"
        "    except Exception as e:\n"
        "        alert_error = str(e)\n"
        "    try:\n"
        "        alert_id = _append_system_notification(meta)\n"
        "    except Exception as e:\n"
        "        alert_error = str(e) if alert_error is None else alert_error + '; ' + str(e)\n"
        "    if alert_error:\n"
        "        log('refresh_failed_permanent_alert_error', error=_bounded(alert_error, 500),\n"
        "            artifact_path=failure_artifact)\n"
        "    else:\n"
        "        log('refresh_failed_permanent_alert_published', alert_id=alert_id,\n"
        "            artifact_path=failure_artifact)\n"
        "    return alert_id, meta\n"
        "deadline = time.time() + 60\n"
        "log('refresh_watcher_start')\n"
        "# Phase 1: wait for .refresh.taken\n"
        "while not os.path.exists(taken) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "if not os.path.exists(taken):\n"
        "    log('refresh_watcher_timeout', phase='ack')\n"
        "    sys.exit(1)\n"
        "log('refresh_watcher_ack')\n"
        "# Phase 2: wait for .agent.lock to clear\n"
        "while os.path.exists(lock) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "if os.path.exists(lock):\n"
        "    log('refresh_watcher_timeout', phase='lock')\n"
        "    sys.exit(1)\n"
        "# Phase 3: relaunch with health check and retry\n"
        "def heartbeat_age():\n"
        "    hb = os.path.join(wd, '.agent.heartbeat')\n"
        "    try:\n"
        "        hb_ts = float(open(hb).read().strip())\n"
        "        return time.time() - hb_ts\n"
        "    except (ValueError, OSError):\n"
        "        return None\n"
        "def is_alive():\n"
        "    age = heartbeat_age()\n"
        "    return age is not None and age < 30\n"
        "def _pid_cmd(pid):\n"
        "    try:\n"
        "        return subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='],\n"
        "            stderr=subprocess.DEVNULL, text=True).strip()\n"
        "    except Exception:\n"
        "        return ''\n"
        "def _extract_duplicate_pid(stderr_tail):\n"
        "    for line in stderr_tail.splitlines():\n"
        "        line = line.strip()\n"
        "        if not line.startswith('PID '):\n"
        "            continue\n"
        "        parts = line.split(None, 2)\n"
        "        if len(parts) >= 2 and parts[1].rstrip(':').isdigit():\n"
        "            return int(parts[1].rstrip(':'))\n"
        "    return None\n"
        "def match_agent_run(cmdline, working_dir):\n"
        "    target = os.path.normpath(working_dir)\n"
        "    for token, label, program_anchored in (\n"
        "        (' -m lingtai run ', 'module', False),\n"
        "        ('lingtai-agent run ', 'console', True),\n"
        "        ('lingtai run ', 'legacy', True),\n"
        "    ):\n"
        "        idx = cmdline.find(token)\n"
        "        while idx != -1:\n"
        "            if (not program_anchored) or idx == 0 or cmdline[idx - 1] == '/':\n"
        "                tail = cmdline[idx + len(token):].strip()\n"
        "                if tail and os.path.normpath(tail) == target:\n"
        "                    return label\n"
        "            idx = cmdline.find(token, idx + 1)\n"
        "    return None\n"
        "def _is_same_agent_run(pid):\n"
        "    if not pid or pid == os.getpid():\n"
        "        return False\n"
        "    try:\n"
        "        os.kill(pid, 0)\n"
        "    except OSError:\n"
        "        return False\n"
        "    cmdline = _pid_cmd(pid)\n"
        "    return match_agent_run(cmdline, wd) is not None\n"
        "def _cleanup_stale_duplicate(stderr_tail, attempt):\n"
        "    pid = _extract_duplicate_pid(stderr_tail)\n"
        "    failure_state['last_pid'] = pid\n"
        "    failure_state['last_duplicate_pid'] = pid\n"
        "    failure_state['last_cleanup_action'] = 'inspect_duplicate_guard'\n"
        "    if not _is_same_agent_run(pid):\n"
        "        failure_state['last_cleanup_result'] = 'skipped_not_same_agent'\n"
        "        return False\n"
        "    age = heartbeat_age()\n"
        "    failure_state['last_heartbeat_age'] = age\n"
        "    failure_state['last_heartbeat_status'] = 'fresh' if age is not None and age < 30 else ('stale' if age is not None else 'missing')\n"
        "    if age is not None and age < 60:\n"
        "        log('refresh_watcher_duplicate_alive', attempt=attempt, pid=pid, heartbeat_age=age)\n"
        "        failure_state['last_cleanup_result'] = 'skipped_fresh_heartbeat'\n"
        "        return False\n"
        "    log('refresh_watcher_stale_duplicate_terminate', attempt=attempt, pid=pid,\n"
        "        heartbeat_age=age, cmdline=_pid_cmd(pid)[-300:])\n"
        "    failure_state['last_cleanup_action'] = 'terminate_stale_duplicate'\n"
        "    try:\n"
        "        os.kill(pid, signal.SIGTERM)\n"
        "    except OSError as e:\n"
        "        log('refresh_watcher_stale_duplicate_term_error', attempt=attempt,\n"
        "            pid=pid, error=str(e))\n"
        "        failure_state['last_cleanup_result'] = 'sigterm_error'\n"
        "        failure_state['last_cleanup_error'] = str(e)\n"
        "        return False\n"
        "    deadline = time.time() + 5\n"
        "    while time.time() < deadline:\n"
        "        try:\n"
        "            os.kill(pid, 0)\n"
        "        except OSError:\n"
        "            log('refresh_watcher_stale_duplicate_gone', attempt=attempt, pid=pid)\n"
        "            failure_state['last_cleanup_result'] = 'terminated'\n"
        "            return True\n"
        "        time.sleep(0.2)\n"
        "    try:\n"
        "        os.kill(pid, signal.SIGKILL)\n"
        "        log('refresh_watcher_stale_duplicate_killed', attempt=attempt, pid=pid)\n"
        "        failure_state['last_cleanup_result'] = 'sigkill_sent'\n"
        "        return True\n"
        "    except OSError as e:\n"
        "        log('refresh_watcher_stale_duplicate_kill_error', attempt=attempt,\n"
        "            pid=pid, error=str(e))\n"
        "        failure_state['last_cleanup_result'] = 'sigkill_error'\n"
        "        failure_state['last_cleanup_error'] = str(e)\n"
        "        return False\n"
        "for attempt in range(1, MAX_ATTEMPTS + 1):\n"
        "    # Check if already alive before relaunching\n"
        "    if is_alive():\n"
        "        log('refresh_watcher_already_alive', attempt=attempt)\n"
        "        sys.exit(0)\n"
        "    # Clean signal files so the new process boots cleanly (like CPR)\n"
        "    for sig in ('.suspend', '.sleep', '.interrupt'):\n"
        "        try:\n"
        "            os.unlink(os.path.join(wd, sig))\n"
        "        except OSError:\n"
        "            pass\n"
        "    log('refresh_watcher_relaunch', attempt=attempt)\n"
        "    try:\n"
        "        with open(stderr_log, 'a') as serr:\n"
        "            serr.write(f'--- relaunch attempt {attempt} ---\\n')\n"
        "            serr.flush()\n"
        "            proc = subprocess.Popen(cmd,\n"
        "                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                stderr=serr, start_new_session=True)\n"
        "    except Exception as e:\n"
        "        log('refresh_watcher_relaunch_error', attempt=attempt, error=str(e))\n"
        "        hb_age, hb_status = _heartbeat_snapshot()\n"
        "        failure_state['last_heartbeat_age'] = hb_age\n"
        "        failure_state['last_heartbeat_status'] = hb_status\n"
        "        failure_state['last_stderr_tail'] = _read_stderr_tail()\n"
        "        failure_state['last_cleanup_action'] = 'not_applicable'\n"
        "        failure_state['last_cleanup_result'] = 'launch_error'\n"
        "        failure_state['last_cleanup_error'] = None\n"
        "        failure_state['last_relaunch_error'] = str(e)\n"
        "        if attempt < MAX_ATTEMPTS:\n"
        "            time.sleep(HEALTH_CHECK_WAIT)\n"
        "        continue\n"
        "    log('refresh_watcher_relaunched', attempt=attempt, pid=proc.pid)\n"
        "    failure_state['last_relaunch_pid'] = proc.pid\n"
        "    failure_state['last_relaunch_error'] = None\n"
        "    # Wait for the new process to start writing heartbeat\n"
        "    time.sleep(HEALTH_CHECK_WAIT)\n"
        "    hb = os.path.join(wd, '.agent.heartbeat')\n"
        "    if os.path.exists(hb):\n"
        "        try:\n"
        "            hb_ts = float(open(hb).read().strip())\n"
        "            if time.time() - hb_ts < HEALTH_CHECK_WAIT + 10:\n"
        "                log('refresh_watcher_success', attempt=attempt, pid=proc.pid)\n"
        "                sys.exit(0)\n"
        "        except (ValueError, OSError):\n"
        "            pass\n"
        "    # Process not alive — log failure and retry\n"
        "    stderr_tail = ''\n"
        "    try:\n"
        "        with open(stderr_log, encoding='utf-8', errors='replace') as f:\n"
        "            lines = f.readlines()\n"
        "            stderr_tail = ''.join(lines[-20:])\n"
        "    except OSError:\n"
        "        pass\n"
        "    hb_age, hb_status = _heartbeat_snapshot()\n"
        "    failure_state['last_heartbeat_age'] = hb_age\n"
        "    failure_state['last_heartbeat_status'] = hb_status\n"
        "    failure_state['last_stderr_tail'] = _bounded(stderr_tail)\n"
        "    failure_state['last_cleanup_action'] = 'not_applicable'\n"
        "    failure_state['last_cleanup_result'] = 'no_duplicate_guard'\n"
        "    failure_state['last_cleanup_error'] = None\n"
        "    log('refresh_watcher_relaunch_dead', attempt=attempt, pid=proc.pid,\n"
        "        stderr_tail=stderr_tail[-500:])\n"
        "    if 'another lingtai agent is already running' in stderr_tail:\n"
        "        _cleanup_stale_duplicate(stderr_tail, attempt)\n"
        "alert_id, meta = _publish_refresh_failed_permanent()\n"
        "log('refresh_failed_permanent', alert_id=alert_id, **meta)\n"
    )
    watcher_env = {**os.environ, "LINGTAI_REFRESH_ENV_OVERWRITE": "1"}
    subprocess.Popen(
        [sys.executable, "-c", relaunch_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=watcher_env,
    )
    agent._log("refresh_deferred_relaunch",
               cmd=cmd[0], handshake=handshake_source)
    # Lock-clear signaling — direct callers (intrinsic system tool call,
    # AED preset fallback) reach this function without going through the
    # heartbeat's `_shutdown.set()` step at lifecycle.py:212. Without
    # `_shutdown` set the run loop never exits and `.agent.lock` never
    # releases, so the watcher times out at phase='lock'. Setting these
    # events here makes the watcher's second phase complete uniformly
    # regardless of caller; the heartbeat path's redundant `_shutdown.set()`
    # is idempotent.
    cancel_event = getattr(agent, "_cancel_event", None)
    if cancel_event is not None:
        try:
            cancel_event.set()
        except Exception:
            pass
    shutdown_event = getattr(agent, "_shutdown", None)
    if shutdown_event is not None:
        try:
            shutdown_event.set()
        except Exception:
            pass


def _can_fallback_preset(agent) -> bool:
    """True if init.json has manifest.preset and active != default."""
    try:
        data = json.loads((agent._working_dir / "init.json").read_text(encoding="utf-8"))
        preset = data.get("manifest", {}).get("preset") or {}
        if not isinstance(preset, dict):
            return False
        active = preset.get("active")
        default = preset.get("default")
        return bool(active and default and active != default)
    except Exception:
        return False


def _check_rules_file(agent) -> None:
    """Consume .rules signal file, diff against system/rules.md, update if changed."""
    rules_file = agent._working_dir / ".rules"
    if not rules_file.is_file():
        return
    try:
        content = rules_file.read_text(encoding="utf-8").strip()
    except OSError:
        return
    # Always consume the signal file
    try:
        rules_file.unlink()
    except OSError:
        return
    if not content:
        return
    # Diff against canonical system/rules.md
    canonical = agent._working_dir / "system" / "rules.md"
    existing = ""
    if canonical.is_file():
        try:
            existing = canonical.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if content == existing:
        return
    # Content changed — persist and refresh
    try:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(content)
    except OSError:
        agent._log("rules_write_error", source="signal")
        return
    agent._prompt_manager.write_section("rules", content, protected=True)
    agent._flush_system_prompt()
    agent._log("rules_loaded", source="signal")
