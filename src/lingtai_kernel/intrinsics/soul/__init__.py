"""Soul intrinsic — the agent's inner voice.

Three actions:
    flow    — past-self consultation appendix. Every ``_soul_delay`` seconds,
              fires M=1+K parallel LLM calls (1 stepped-back read of the
              current chat as "insights", K random past-snapshot
              consultations sampled from history/snapshots/). Voices are
              written to ``.notification/soul.json`` via
              ``publish_notification``; the kernel's ``_sync_notifications``
              picks up the fingerprint change and surfaces them inside the
              single-slot synthesized ``notification(action="check")`` wire
              pair. Mechanical — agent cannot invoke manually.
    inquiry — sync mirror session. Clones conversation (text+thinking only),
              sends question, returns answer in tool result. On-demand.
    config  — adjust soul flow knobs. Accepts any subset of two optional
              fields: delay_seconds (wall-clock cadence), consultation_past_count
              (K, number of past-self voices per fire). Updates live state,
              restarts the wall-clock timer if delay changed, persists to
              init.json.
"""
from __future__ import annotations

# Re-export constants from config.py
from ...config import DEFAULT_SOUL_DELAY_SECONDS
from .config import (
    SOUL_DELAY_MIN_SECONDS,
    CONSULTATION_PAST_COUNT_MIN,
    CONSULTATION_PAST_COUNT_MAX,
    SOUL_VOICE_BUILTINS,
    SOUL_VOICE_PROMPT_MAX,
)

# Re-export private helpers consumed by base_agent.py and tests
from .config import (
    _handle_config,
    _handle_voice,
    _persist_soul_config,
    _persist_soul_voice,
    _atomic_write_init,
    _build_soul_system_prompt,
)

# Re-export consultation pipeline
from .consultation import (
    _build_consultation_tool_refusal,
    _CONSULTATION_MAX_ROUNDS,
    _DIARY_CUE_TOKEN_CAP,
    _send_with_timeout,
    _render_current_diary,
    _write_soul_tokens,
    _load_snapshot_interface,
    _fit_interface_to_window,
    _kind_for_source,
    _build_consultation_cue,
    _run_consultation,
    _list_snapshot_paths,
    _run_consultation_batch,
    build_consultation_pair,
)

# Re-export inquiry
from .inquiry import soul_inquiry, _run_inquiry

# Re-export flow (soul cadence, fire, persistence, appendix tracking)
from .flow import (
    _start_soul_timer,
    _cancel_soul_timer,
    _soul_whisper,
    _persist_soul_entry,
    _run_consultation_fire,
    _rehydrate_appendix_tracking,
)


def get_description(lang: str = "en") -> str:
    from ...i18n import t
    return t(lang, "soul.description")


def get_schema(lang: str = "en") -> dict:
    from ...i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inquiry", "flow", "config", "voice", "dismiss"],
                "description": t(lang, "soul.action_description"),
            },
            "inquiry": {
                "type": "string",
                "description": t(lang, "soul.inquiry_description"),
            },
            "delay_seconds": {
                "type": "number",
                "minimum": SOUL_DELAY_MIN_SECONDS,
                "description": t(lang, "soul.delay_seconds_description"),
            },
            "consultation_past_count": {
                "type": "integer",
                "minimum": CONSULTATION_PAST_COUNT_MIN,
                "maximum": CONSULTATION_PAST_COUNT_MAX,
                "description": t(lang, "soul.consultation_past_count_description"),
            },
            "set": {
                "type": "string",
                "description": t(lang, "soul.voice_set_description"),
            },
            "prompt": {
                "type": "string",
                "maxLength": SOUL_VOICE_PROMPT_MAX,
                "description": t(lang, "soul.voice_prompt_description"),
            },
        },
        "required": ["action"],
    }


def handle(agent, args: dict) -> dict:
    """Handle soul tool — inquiry, config, voice are agent-invocable.
    Flow is invocable too, but the call returns immediately with a
    synthesized success result; the actual voices arrive shortly by
    writing ``.notification/soul.json``, which the kernel's notification
    sync surfaces inside the synthesized ``notification(action="check")``
    wire pair.
    """
    action = args.get("action", "")

    if action == "flow":
        # Voluntary trigger: try-acquire the fire lock non-blocking. If
        # held, another fire is already in flight (timer-fired or a prior
        # voluntary call) — refuse so the agent isn't surprised by a
        # silent no-op. If free, release immediately and kick off the
        # real fire on a daemon thread; _run_consultation_fire will
        # re-acquire under the same gate.
        lock = getattr(agent, "_soul_fire_lock", None)
        if lock is not None:
            if not lock.acquire(blocking=False):
                agent._log("soul_flow_voluntary_rejected", reason="ongoing")
                return {"error": "soul flow ongoing, request rejected"}
            lock.release()

        import threading
        from .flow import _run_consultation_fire

        def _fire():
            try:
                # Wait for IDLE before firing — voluntary flow is triggered
                # while ACTIVE (inside a tool call), but _run_consultation_fire
                # gates on IDLE.  _idle is a threading.Event set on every
                # non-ACTIVE transition (see base_agent._set_state).
                idle_event = getattr(agent, "_idle", None)
                if idle_event is not None:
                    agent._log("soul_flow_voluntary_waiting_idle")
                    # Wait up to soul_delay seconds; if the agent never goes
                    # IDLE (stuck in ACTIVE), give up rather than hang.
                    timeout = getattr(agent, "_soul_delay", DEFAULT_SOUL_DELAY_SECONDS)
                    if not idle_event.wait(timeout=timeout):
                        agent._log("soul_flow_voluntary_timeout",
                                   timeout=timeout)
                        return
                _run_consultation_fire(agent)
            except Exception as e:
                try:
                    agent._log("soul_flow_voluntary_error", error=str(e)[:200])
                except Exception:
                    pass

        t = threading.Thread(target=_fire, daemon=True, name="soul-flow-voluntary")
        t.start()
        agent._log("soul_flow_voluntary_triggered")
        return {
            "status": "ok",
            "message": (
                "Soul flow triggered. Voices will arrive shortly as a "
                "separate soul(action='flow') tool-call pair appended to "
                "your chat history (replacing any prior soul-flow pair)."
            ),
        }

    if action == "inquiry":
        inquiry = args.get("inquiry", "")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return {"error": "inquiry is required — what do you want to reflect on?"}

        agent._log("soul_inquiry", inquiry=inquiry.strip()[:200])

        result = soul_inquiry(agent, inquiry.strip())

        if result:
            agent._persist_soul_entry(result, mode="inquiry")
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": result["voice"]}
        else:
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": "(silence)"}

    if action == "config":
        return _handle_config(agent, args)

    if action == "voice":
        return _handle_voice(agent, args)

    if action == "dismiss":
        from ...notifications import dismiss_channel
        result = dismiss_channel(agent, "soul", invoked_by="soul")
        if result.get("status") == "ok":
            result.setdefault("message", "Soul flow notification dismissed.")
        return result

    return {
        "error": (
            f"Unknown soul action: {action}. Use inquiry, config, voice, dismiss, "
            "or wait for flow (mechanical)."
        )
    }
