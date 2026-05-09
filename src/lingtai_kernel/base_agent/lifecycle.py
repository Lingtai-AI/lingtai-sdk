"""Lifecycle — start, stop, heartbeat, signal-file detection, refresh, preset fallback.

The agent's life support: starting, stopping, breathing, detecting signal
files (.sleep, .suspend, .refresh, .prompt, .clear, .inquiry, .rules,
.interrupt), enforcing stamina, managing AED timeout, and running periodic
snapshots.
"""
from __future__ import annotations

import json
import time
import threading


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

    # Restore chat session and token state from filesystem if available
    chat_history_file = agent._working_dir / "history" / "chat_history.jsonl"
    if chat_history_file.is_file():
        try:
            messages = [
                json.loads(line)
                for line in chat_history_file.read_text().splitlines()
                if line.strip()
            ]
            agent.restore_chat({"messages": messages})
            agent._log("session_restored")
            _rehydrate_appendix_tracking(agent)
        except Exception as e:
            from ..logging import get_logger
            get_logger().warning(f"[{agent.agent_name}] Failed to restore chat history: {e}")
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

    agent._thread = threading.Thread(
        target=agent._run_loop,
        daemon=True,
        name=f"agent-{agent.agent_name or agent._working_dir.name}",
    )
    agent._thread.start()
    _start_heartbeat(agent)
    # Boot state is IDLE (fire-eligible) — start the timer here.
    _start_soul_timer(agent)


def _reset_uptime(agent) -> None:
    """Reset the uptime anchor for stamina tracking (used on wake from asleep)."""
    agent._uptime_anchor = time.monotonic()


def _stop(agent, timeout: float = 5.0) -> None:
    """Signal shutdown and wait for the agent thread to exit."""
    from ..intrinsics.soul.flow import _cancel_soul_timer

    agent._log("agent_stop")
    _stop_heartbeat(agent)
    _cancel_soul_timer(agent)
    agent._shutdown.set()
    if agent._thread:
        agent._thread.join(timeout=timeout)
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

    # Persist final state and release lock
    agent._workdir.write_manifest(agent._build_manifest())
    agent._workdir.release_lock()


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
    """Beat every 1 second. AED if agent is STUCK."""
    from ..state import AgentState
    from ..intrinsics.soul.inquiry import _run_inquiry

    while agent._heartbeat_thread is not None and not agent._shutdown.is_set():
        # time.time() (wall clock), not time.monotonic(). Deliberate:
        # heartbeat is written to a file and read by handshake.is_alive()
        # in a DIFFERENT process.
        agent._heartbeat = time.time()

        # Write heartbeat file in ALL living states (everything except SUSPENDED)
        try:
            hb_file = agent._working_dir / ".agent.heartbeat"
            hb_file.write_text(str(agent._heartbeat), encoding="utf-8")
        except OSError:
            pass

        # --- signal file detection ---
        interrupt_file = agent._working_dir / ".interrupt"
        if interrupt_file.is_file():
            try:
                interrupt_file.unlink()
            except OSError:
                pass
            agent._cancel_event.set()
            agent._log("interrupt_received", source="signal_file")

        # .refresh = self-initiated restart (handshake: rename to .refresh.taken)
        refresh_file = agent._working_dir / ".refresh"
        if refresh_file.is_file():
            # Delegate to _perform_refresh which handles the full flow:
            # .llm_hang clear, save chat history, spawn watcher process,
            # and deferred relaunch — identical to system(action='refresh').
            _perform_refresh(agent)

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
                content = prompt_file.read_text().strip()
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
                source = clear_file.read_text().strip() or "admin"
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
                    content = taken_file.read_text().strip()
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

        # --- Notification sync ---
        # Poll the `.notification/` directory for changes.  The sync
        # method is a no-op when the fingerprint is unchanged, so this
        # call is cheap on the steady-state path.  On change it strips
        # the prior wire block and reinjects per current state (IDLE
        # pair / ACTIVE meta-stash / ASLEEP wake-then-pair).  See
        # base_agent/__init__.py:_sync_notifications and
        # discussions/notification-filesystem-redesign.md.
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


def _perform_refresh(agent) -> None:
    """Refresh = .llm_hang clear + .refresh handshake + deferred relaunch."""
    import subprocess
    import sys

    agent._log("refresh_start")
    # Recovery path: refresh exists to unstick error states, so unconditionally
    # drop the .llm_hang sentinel here. Without this, a hung-LLM agent that
    # gets refreshed comes back up only to be re-stuck the moment it transitions
    # to ASLEEP and tries to wake. See issue #35.
    hang_file = agent._working_dir / ".llm_hang"
    if hang_file.exists():
        try:
            hang_file.unlink(missing_ok=True)
            agent._log("llm_hang_cleared", reason="refresh")
        except OSError:
            pass
    agent._save_chat_history()
    # Bound-method dispatch — _build_launch_cmd lives on BaseAgent (returns
    # None) and Agent (returns the real `lingtai run` cmd). A prior version
    # called a module-level _build_launch_cmd shadow that always returned
    # None, silently no-opping every user refresh on the Agent subclass —
    # see issue #7, confirmed in vivo against deepseek_pro 2026-05-05.
    cmd = agent._build_launch_cmd()
    if cmd is None:
        agent._log("refresh_no_launch_cmd")
        return

    working_dir = agent._working_dir
    (working_dir / ".refresh").touch()

    taken_path = str(working_dir / ".refresh.taken")
    lock_path = str(working_dir / ".agent.lock")
    events_path = str(working_dir / "logs" / "events.jsonl")
    agent_name = agent.agent_name
    address = agent._working_dir.name
    relaunch_script = (
        "import time, subprocess, os, sys, json\n"
        f"taken = {taken_path!r}\n"
        f"lock = {lock_path!r}\n"
        f"events = {events_path!r}\n"
        f"name = {agent_name!r}\n"
        f"addr = {address!r}\n"
        "def log(typ, **kw):\n"
        "    entry = {'type': typ, 'address': addr, 'agent_name': name, 'ts': time.time(), **kw}\n"
        "    try:\n"
        "        with open(events, 'a') as f:\n"
        "            f.write(json.dumps(entry) + '\\n')\n"
        "    except OSError:\n"
        "        pass\n"
        "deadline = time.time() + 60\n"
        "log('refresh_watcher_start')\n"
        "while not os.path.exists(taken) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "if not os.path.exists(taken):\n"
        "    log('refresh_watcher_timeout', phase='ack')\n"
        "    sys.exit(1)\n"
        "log('refresh_watcher_ack')\n"
        "while os.path.exists(lock) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "if os.path.exists(lock):\n"
        "    log('refresh_watcher_timeout', phase='lock')\n"
        "    sys.exit(1)\n"
        "time.sleep(0.5)\n"
        "log('refresh_watcher_relaunch')\n"
        f"subprocess.Popen({cmd!r},\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL, start_new_session=True)\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", relaunch_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    agent._log("refresh_deferred_relaunch", cmd=cmd[0])


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
        content = rules_file.read_text().strip()
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
            existing = canonical.read_text().strip()
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
