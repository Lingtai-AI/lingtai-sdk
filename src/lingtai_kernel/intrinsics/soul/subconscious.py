"""Subconscious engine — event-driven intra-turn fan-out with meta block delivery.

Architecture A (event-driven + meta block injection):
  - Trigger: fires after each tool-call batch completes in the turn loop
    (event-driven, not wall-clock timer).
  - Delivery: insights are stored on the agent and injected into the meta
    block (text-input prefix) on subsequent turns. No JSONL, no notification
    system — the agent sees insights directly in its prompt prefix.
  - Model: cheap model, ALL snapshots per fire (default: all available,
    configurable via subconscious_sample_n).
  - Confidence filtering: only insights with confidence >= threshold
    (default 0.6, configurable via subconscious_confidence_threshold)
    are stored.
  - Lifecycle: cleared at turn start, persisted until turn end.

Why event-driven instead of timer:
  The subconscious should fire when the agent is *doing things* — processing
  tool results, making decisions, hitting walls. A 60-second wall-clock timer
  fires regardless of whether anything meaningful happened. Event-driven
  ensures every fire has fresh material (the just-completed tool batch) and
  doesn't waste tokens during pauses.

Why meta block instead of notification/JSONL:
  The meta block (text-input prefix) is the agent's "always-on peripheral
  vision." It already carries context pressure, stamina, and time. Adding
  subconscious insights here means the agent sees them without needing to
  poll a notification channel or read a JSONL file. The insights are
  advisory — the agent can act on them or ignore them, but they're always
  visible until the turn ends.

Lifecycle:
  _clear_subconscious_state    — called from turn._handle_request (turn start)
  _fire_subconscious           — called from turn._process_response (after tool batch)
  _render_subconscious_insights — called from meta_block.build_meta (every turn)
"""
from __future__ import annotations

import json
import random
import threading
import time

# Maximum number of insights to retain per turn. Old insights are evicted
# FIFO when this limit is reached. Keeps the meta block lean.
_MAX_INSIGHTS_PER_TURN = 3

# Default confidence threshold — insights below this are silently dropped.
_DEFAULT_CONFIDENCE_THRESHOLD = 0.6

# Default number of snapshots to sample per fire.
# 9999 effectively means ALL available snapshots.
_DEFAULT_SAMPLE_N = 9999

# Subconscious system prompt — "does this remind you of something?"
_SUBCONSCIOUS_SYSTEM_PROMPT = (
    "The chat below is your context — your thoughts, your work, your tools, your memory.\n\n"
    "Your role: Does the current self's thinking and diary remind you of something?\n"
    "If so, write a brief insight to inform the current self. If not, stay silent.\n\n"
    "Format your insight as JSON:\n"
    '{"insight": "...", "confidence": 0.0-1.0, "source_memory": "..."}\n'
    '{"insight": null}\n'
    "You cannot execute tools. Do not attempt tool calls."
)


# ── State management ─────────────────────────────────────────────────────

def _clear_subconscious_state(agent) -> None:
    """Clear subconscious insights at turn start.

    Called from _handle_request at the top of every turn. Insights are
    ephemeral — they exist only for the current tool-call loop and are
    discarded at turn end.
    """
    agent._subconscious_insights = []


def _get_subconscious_insights(agent) -> list[dict]:
    """Return the current list of subconscious insights.

    Each insight is {"insight": str, "confidence": float, "source": str}.
    Returns empty list if no insights or subconscious is disabled.
    """
    if not getattr(agent._config, "subconscious_enabled", False):
        return []
    return list(getattr(agent, "_subconscious_insights", []) or [])


# ── Event-driven fire ────────────────────────────────────────────────────

def _fire_subconscious(agent) -> None:
    """Fire one subconscious consultation after a tool-call batch.

    Event-driven: called from _process_response after tools complete.
    Runs in a daemon thread so it doesn't block the main turn loop.
    If an insight is produced, it's appended to agent._subconscious_insights
    and will appear in the meta block on the next turn.

    Uses the existing shared consultation engine with:
    - _SUBCONSCIOUS_SYSTEM_PROMPT (cheap "remind" prompt)
    - No tools, single round
    - Cheap model via session_overrides
    - ALL snapshots by default (configurable via subconscious_sample_n)
    """
    if not getattr(agent._config, "subconscious_enabled", False):
        return
    if agent._shutdown.is_set():
        return

    # Determine how many snapshots to sample this fire.
    sample_n = getattr(agent._config, "subconscious_sample_n", _DEFAULT_SAMPLE_N)
    if not isinstance(sample_n, int) or sample_n < 1:
        sample_n = _DEFAULT_SAMPLE_N

    # Fire in daemon threads — non-blocking, one per sampled snapshot.
    for i in range(sample_n):
        t = threading.Thread(
            target=_subconscious_fire_worker,
            args=(agent,),
            daemon=True,
            name=f"sub-{agent.agent_name or 'agent'}-{i}",
        )
        t.start()


def _subconscious_fire_worker(agent) -> None:
    """Worker thread for one subconscious fire.

    Loads a random snapshot, consults with the cheap model, and if an
    insight is produced, appends it to agent._subconscious_insights.
    """
    from .consultation import (
        _render_current_diary,
        _list_snapshot_paths,
        _load_snapshot_interface,
        _fit_interface_to_window,
        _send_with_timeout,
    )

    try:
        # Get the diary spark — no diary, no fire.
        diary = _render_current_diary(agent)
        if not diary:
            agent._log("subconscious_fire_empty")
            return

        # Select a random snapshot.
        paths = _list_snapshot_paths(agent)
        if not paths:
            agent._log("subconscious_fire_no_snapshots")
            return

        path = random.choice(paths)  # each worker picks one independently
        iface = _load_snapshot_interface(path)
        if iface is None or not iface.entries:
            agent._log("subconscious_fire_load_failed", path=str(path))
            return

        # Fit to window.
        window = getattr(agent._config, "subconscious_context_window", 128000) or 128000
        target = max(1, int(window * 0.7))
        fitted = _fit_interface_to_window(iface, target)
        if not fitted.entries:
            return

        # Build session with subconscious config.
        session_overrides = _build_session_overrides(agent)
        provider = session_overrides.get("provider", agent._config.provider)
        model = session_overrides.get("model", agent._config.model)
        base_url = session_overrides.get("base_url")

        try:
            session = agent.service.create_session(
                system_prompt=_SUBCONSCIOUS_SYSTEM_PROMPT,
                tools=None,  # subconscious doesn't use tools
                model=model,
                thinking=None,
                tracked=False,
                interface=fitted,
                provider=provider,
                base_url=base_url,
            )
        except Exception as e:
            agent._log("subconscious_session_failed", error=str(e)[:200])
            return

        # Send the diary as spark.
        response = _send_with_timeout(agent, session, diary)
        if response is None:
            return

        # Extract text from the response.
        text = ""
        if session.interface.entries:
            tail = session.interface.entries[-1]
            if tail.role == "assistant":
                for b in tail.content:
                    if hasattr(b, "text") and b.text:
                        text = b.text.strip()
                        break

        if not text:
            return

        # Parse the response.
        insight_data = _parse_subconscious_response(text)
        if insight_data is None:
            return

        # Confidence filtering — drop insights below threshold.
        threshold = getattr(
            agent._config, "subconscious_confidence_threshold",
            _DEFAULT_CONFIDENCE_THRESHOLD,
        )
        confidence = insight_data.get("confidence", 0.5)
        if confidence < threshold:
            agent._log(
                "subconscious_insight_filtered",
                confidence=confidence,
                threshold=threshold,
                insight=insight_data["insight"][:100],
            )
            return

        # Append to the agent's in-memory insights list.
        insight_record = {
            "insight": insight_data["insight"],
            "confidence": insight_data.get("confidence", 0.5),
            "source": f"snapshot:{path.stem}",
            "ts": time.time(),
        }

        insights = getattr(agent, "_subconscious_insights", None)
        if insights is None:
            agent._subconscious_insights = []
            insights = agent._subconscious_insights

        # Evict oldest if at capacity.
        while len(insights) >= _MAX_INSIGHTS_PER_TURN:
            insights.pop(0)

        insights.append(insight_record)

        agent._log(
            "subconscious_insight",
            insight=insight_data["insight"][:200],
            source=f"snapshot:{path.stem}",
            confidence=insight_data.get("confidence", 0.5),
        )

    except Exception as e:
        try:
            agent._log("subconscious_fire_error", error=str(e)[:200])
        except Exception:
            pass


# ── Rendering for meta block ─────────────────────────────────────────────

def _render_subconscious_insights(agent) -> str:
    """Render subconscious insights for the meta block (text-input prefix).

    Returns a formatted string that will be appended to the meta block.
    Returns empty string if no insights or subconscious is disabled.

    Format:
        🧠 subconscious: <insight1> | <insight2>
    """
    insights = _get_subconscious_insights(agent)
    if not insights:
        return ""

    parts = []
    for ins in insights:
        text = ins.get("insight", "").strip()
        confidence = ins.get("confidence", 0.5)
        if text:
            # Truncate long insights for the meta block.
            if len(text) > 80:
                text = text[:77] + "..."
            parts.append(f"({confidence:.0%}) {text}")

    if not parts:
        return ""

    return "🧠 " + " | ".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_session_overrides(agent) -> dict:
    """Build session_overrides dict from agent config."""
    overrides: dict = {}
    provider = getattr(agent._config, "subconscious_provider", None)
    model = getattr(agent._config, "subconscious_model", None)
    base_url = getattr(agent._config, "subconscious_base_url", None)
    if provider:
        overrides["provider"] = provider
    if model:
        overrides["model"] = model
    if base_url:
        overrides["base_url"] = base_url
    return overrides


def _parse_subconscious_response(text: str) -> dict | None:
    """Parse the subconscious LLM response into structured insight data.

    Returns {"insight": str, "confidence": float, "source_memory": str}
    or None if insight is null/empty. Handles markdown-wrapped JSON.
    """
    import re

    # Strip markdown code fences.
    cleaned = re.sub(r'^```(?:json)?\s*\n?|\n?```\s*$', '', text.strip())

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        # Unstructured text — treat as an insight with default confidence.
        if text.strip():
            return {
                "insight": text.strip(),
                "confidence": 0.5,
                "source_memory": "unstructured",
            }
        return None

    if not isinstance(data, dict):
        return None

    insight = data.get("insight")
    if insight is None or (isinstance(insight, str) and not insight.strip()):
        return None

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    source_memory = data.get("source_memory", "")
    if not isinstance(source_memory, str):
        source_memory = str(source_memory)

    return {
        "insight": str(insight).strip(),
        "confidence": confidence,
        "source_memory": source_memory,
    }


def _read_subconscious_tail(agent, n: int = _MAX_INSIGHTS_PER_TURN) -> str:
    """Read the last N subconscious insights for logging/debugging.

    Returns a formatted string. Used for the soul tool's notification
    action response — so the agent can see its current subconscious state.
    """
    insights = _get_subconscious_insights(agent)
    if not insights:
        return ""

    from datetime import datetime

    lines = []
    for ins in insights[-n:]:
        ts = ins.get("ts", 0)
        ts_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S") if ts else "??:??:??"
        confidence = ins.get("confidence", 0.5)
        source = ins.get("source", "unknown")
        text = ins.get("insight", "")
        lines.append(f"[{ts_str}] (confidence={confidence:.1f}, from {source})\n{text}")

    if not lines:
        return ""

    return "Subconscious insights:\n\n" + "\n---\n".join(lines)
