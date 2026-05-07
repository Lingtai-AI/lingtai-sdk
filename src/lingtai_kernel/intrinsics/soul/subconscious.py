"""Subconscious engine — intra-turn fan-out across past snapshots.

The subconscious fires ONLY while the agent is in a tool-call loop
(ACTIVE mid-turn). It runs a cheap model against ALL available snapshots
in parallel, looking for pattern matches. Insights are appended to an
append-only JSONL file that the agent sees via the notification system.

Lifecycle:
  _start_subconscious_timer  — called from turn._handle_request (turn start)
  _cancel_subconscious_timer — called from turn._handle_request (turn end)
  _clear_subconscious_jsonl  — called from turn._handle_request (turn start)

The timer is a 60-second wall-clock daemon; it fires _subconscious_tick
which fans out across ALL snapshots in parallel daemon threads.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

_SUBCONSCIOUS_FIRE_INTERVAL = 60.0
_SUBCONSCIOUS_DISPLAY_N = 10
_SUBCONSCIOUS_JSONL = "subconscious.jsonl"
_SUBCONSCIOUS_SAMPLE_N = 3
_SUBCONSCIOUS_CONFIDENCE_THRESHOLD = 0.6


# ── Timer management ────────────────────────────────────────────────────

def _start_subconscious_timer(agent) -> None:
    """Start the subconscious cadence timer (60s wall-clock).

    Runs only while the agent is in an active tool-call loop (ACTIVE state
    mid-turn). Cancelled by _cancel_subconscious_timer at turn end.
    """
    if not getattr(agent._config, "subconscious_enabled", False):
        return
    if agent._shutdown.is_set():
        return
    _cancel_subconscious_timer(agent)
    agent._subconscious_timer = threading.Timer(
        _SUBCONSCIOUS_FIRE_INTERVAL,
        _subconscious_tick,
        args=(agent,),
    )
    agent._subconscious_timer.daemon = True
    agent._subconscious_timer.name = (
        f"subconscious-{agent.agent_name or agent._working_dir.name}"
    )
    agent._subconscious_timer.start()


def _cancel_subconscious_timer(agent) -> None:
    """Cancel any pending subconscious timer."""
    timer = getattr(agent, "_subconscious_timer", None)
    if timer is not None:
        timer.cancel()
        agent._subconscious_timer = None


# ── Fire orchestration ──────────────────────────────────────────────────

def _subconscious_tick(agent) -> None:
    """Timer callback — fire the subconscious if still mid-turn.

    Only fires while agent state is ACTIVE (mid-tool-call-loop).
    Reschedules in the finally block if still in an active turn.
    """
    from ...state import AgentState

    agent._subconscious_timer = None
    try:
        if agent._state == AgentState.ACTIVE:
            _run_subconscious_fire(agent)
        else:
            agent._log("subconscious_tick_skipped", state=agent._state.value)
    except Exception as e:
        agent._log("subconscious_tick_error", error=str(e)[:200])
    finally:
        if agent._state == AgentState.ACTIVE:
            _start_subconscious_timer(agent)


def _run_subconscious_fire(agent) -> None:
    """Fire one subconscious batch — fan out across a RANDOM SAMPLE of snapshots.

    Uses _subconscious_fire_lock (try-acquire non-blocking) to prevent
    overlapping fires. Each snapshot gets its own daemon thread running
    _run_subconscious_snapshot. Non-null insights above the confidence
    threshold are appended directly to the JSONL by the per-snapshot thread.
    """
    from ...state import AgentState
    from .consultation import (
        _render_current_diary,
        _list_snapshot_paths,
        _SUBCONSCIOUS_SYSTEM_PROMPT,
    )

    # Fire lock — skip if a previous fire is still running.
    lock = getattr(agent, "_subconscious_fire_lock", None)
    if lock is not None and not lock.acquire(blocking=False):
        agent._log("subconscious_skipped_inflight")
        return

    fire_id = f"sub_{int(time.time())}_{_secrets_hex(4)}"

    try:
        # Pre-check.
        if agent._state != AgentState.ACTIVE:
            agent._log("subconscious_discarded_state",
                       fire_id=fire_id, state=agent._state.value)
            return

        # Diary gate — no spark = no fire.
        diary = _render_current_diary(agent)
        if not diary:
            agent._log("subconscious_fire_empty", fire_id=fire_id)
            return

        # Sample N snapshots instead of all.
        all_paths = _list_snapshot_paths(agent)
        if not all_paths:
            agent._log("subconscious_fire_no_snapshots", fire_id=fire_id)
            return

        sample_n = int(getattr(agent._config, "subconscious_sample_n", _SUBCONSCIOUS_SAMPLE_N))
        sample_n = max(1, sample_n)
        if sample_n >= len(all_paths):
            paths = all_paths
        else:
            paths = random.sample(all_paths, sample_n)

        agent._log("subconscious_fire_start",
                   fire_id=fire_id,
                   snapshot_count=len(paths),
                   total_snapshots=len(all_paths))

        # Session overrides from subconscious config.
        session_overrides = _build_session_overrides(agent)

        # Fire all snapshots in parallel.
        results: list[dict | None] = [None] * len(paths)

        def snapshot_worker(idx: int, path) -> None:
            try:
                results[idx] = _run_subconscious_snapshot(
                    agent, path, diary, fire_id, session_overrides,
                )
            except Exception as e:
                try:
                    agent._log("subconscious_snapshot_error",
                               fire_id=fire_id, path=str(path),
                               error=str(e)[:200])
                except Exception:
                    pass

        threads: list[threading.Thread] = []
        for idx, path in enumerate(paths):
            t = threading.Thread(
                target=snapshot_worker,
                args=(idx, path),
                daemon=True,
                name=f"sub-{idx}-{path.stem[:20]}",
            )
            threads.append(t)
            t.start()

        # Wait for all to complete.
        timeout = float(getattr(agent._config, "retry_timeout", 300.0))
        for t in threads:
            t.join(timeout=timeout)

        # Count results.
        insight_count = sum(1 for r in results if r is not None)
        agent._log("subconscious_fire_done",
                   fire_id=fire_id,
                   insight_count=insight_count,
                   total_snapshots=len(paths))

    except Exception as e:
        agent._log("subconscious_fire_error",
                   fire_id=fire_id, error=str(e)[:200])
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def _run_subconscious_snapshot(
    agent,
    path,
    diary: str,
    fire_id: str,
    session_overrides: dict,
) -> dict | None:
    """Run one subconscious snapshot. Returns insight dict if non-null, else None.

    Uses the shared consultation engine with allow_tool_recommendations=False.
    On non-null insight above the confidence threshold, appends directly to
    the JSONL (file-append is thread-safe on macOS/Linux for small records).
    """
    from .consultation import (
        _load_snapshot_interface,
        _run_consultation_voice,
        _SUBCONSCIOUS_SYSTEM_PROMPT,
    )

    iface = _load_snapshot_interface(path)
    if iface is None or not iface.entries:
        agent._log("subconscious_snapshot_load_failed",
                   fire_id=fire_id, path=str(path))
        return None

    source = f"snapshot:{path.stem}"
    result = _run_consultation_voice(
        agent, iface, source,
        system_prompt=_SUBCONSCIOUS_SYSTEM_PROMPT,
        spark=diary,
        session_overrides=session_overrides,
        allow_tool_recommendations=False,
        max_rounds=1,
    )

    if result is None:
        return None

    # Extract text from blocks.
    text = _extract_text_from_blocks(result.get("blocks", []))
    if not text:
        return None

    # Parse structured JSON response.
    insight_data = _parse_subconscious_response(text)
    if insight_data is None:
        return None  # Model said nothing relevant.

    # Confidence filtering — discard low-confidence insights.
    confidence = insight_data.get("confidence", 0.5)
    threshold = float(getattr(
        agent._config, "subconscious_confidence_threshold",
        _SUBCONSCIOUS_CONFIDENCE_THRESHOLD,
    ))
    if confidence < threshold:
        agent._log("subconscious_snapshot_below_threshold",
                   fire_id=fire_id, source=source,
                   confidence=confidence, threshold=threshold)
        return None

    # Append to JSONL.
    record = {
        "ts": time.time(),
        "fire_id": fire_id,
        "insight": insight_data["insight"],
        "confidence": confidence,
        "source_memory": insight_data.get("source_memory", "unstructured"),
        "source_snapshot": source,
        "model_used": session_overrides.get("model", "unknown"),
    }
    _append_subconscious_record(agent, record)

    return record


# ── JSONL persistence ───────────────────────────────────────────────────

def _subconscious_jsonl_path(agent):
    """Path to the subconscious insights JSONL file."""
    return agent._working_dir / "logs" / _SUBCONSCIOUS_JSONL


def _clear_subconscious_jsonl(agent) -> None:
    """Clear the subconscious JSONL file (called at turn start).

    Subconscious insights are ephemeral — they exist only for the current
    tool-call loop and are discarded at turn end.
    """
    path = _subconscious_jsonl_path(agent)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _append_subconscious_record(agent, record: dict) -> None:
    """Append one insight record to the subconscious JSONL.

    Thread-safe: file-append is atomic on macOS/Linux for small records.
    Called from per-snapshot daemon threads.
    """
    path = _subconscious_jsonl_path(agent)
    try:
        path.parent.mkdir(exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        try:
            agent._log("subconscious_jsonl_write_error", error=str(e)[:200])
        except Exception:
            pass


def _read_subconscious_tail(agent, n: int = _SUBCONSCIOUS_DISPLAY_N) -> str:
    """Read the last N lines of the subconscious JSONL, newest-last.

    Returns a formatted string for injection into the agent's prompt.
    Returns empty string if file is missing or empty.
    """
    from .consultation import _iter_lines_reverse

    path = _subconscious_jsonl_path(agent)
    if not path.is_file():
        return ""

    lines: list[str] = []
    try:
        for raw_line in _iter_lines_reverse(path):
            if not raw_line:
                continue
            try:
                rec = json.loads(raw_line)
                insight = rec.get("insight", "")
                confidence = rec.get("confidence", 0.5)
                source = rec.get("source_snapshot", "unknown")
                ts = rec.get("ts", 0)
                from datetime import datetime
                ts_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
                lines.append(
                    f"[{ts_str}] (confidence={confidence:.1f}, from {source})\n{insight}"
                )
            except (json.JSONDecodeError, ValueError):
                continue
            if len(lines) >= n:
                break
    except Exception:
        return ""

    if not lines:
        return ""

    # Reverse to get newest-last (chronological).
    lines.reverse()
    return "Subconscious insights:\n\n" + "\n---\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────────────

def _build_session_overrides(agent) -> dict:
    """Build session_overrides dict from agent config."""
    overrides: dict = {}
    provider = getattr(agent._config, "subconscious_provider", None)
    model = getattr(agent._config, "subconscious_model", None)
    base_url = getattr(agent._config, "subconscious_base_url", None)
    ctx_window = getattr(agent._config, "subconscious_context_window", None)
    if provider:
        overrides["provider"] = provider
    if model:
        overrides["model"] = model
    if base_url:
        overrides["base_url"] = base_url
    if ctx_window:
        overrides["context_window"] = ctx_window
    return overrides


def _extract_text_from_blocks(blocks: list) -> str:
    """Extract text content from a list of content blocks."""
    from ...llm.interface import TextBlock, ThinkingBlock

    parts: list[str] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            if b.text:
                parts.append(b.text)
        elif isinstance(b, ThinkingBlock):
            pass  # Skip thinking blocks.
        else:
            # Generic fallback for dict-like blocks.
            text = getattr(b, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _parse_subconscious_response(text: str) -> dict | None:
    """Parse the subconscious LLM response into structured insight data.

    Returns {"insight": str, "confidence": float, "source_memory": str}
    or None if insight is null/empty. Handles markdown-wrapped JSON.
    """
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


def _secrets_hex(n: int) -> str:
    """Generate n random hex characters."""
    import secrets
    return secrets.token_hex(n // 2 + 1)[:n]
