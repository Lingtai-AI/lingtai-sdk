"""Soul consultation pipeline — mechanical past-self consultation.

Substrate + spark model: loads a ChatInterface from a past snapshot (or
the current chat) verbatim as substrate, sends the diary cue as the
spark, and runs LLM consultation with refusal-loop for tool-call
interception. Bundles voices into a synthetic pair for the main agent.
"""
from __future__ import annotations


def _build_consultation_tool_refusal(system_prompt: str) -> str:
    """ToolResultBlock content for intercepted consultation tool calls.

    Confirms receipt of the recommendation (so the model doesn't think it
    failed and retry the same call), then re-grounds with the same resolved
    soul-flow voice prompt used to create the consultation session.
    """
    return (
        "Your tool call has been recorded as a recommendation to your present self — "
        "the call name, arguments, and your adjacent reasoning will reach them. "
        "You may continue: more text, more tool-call recommendations, or stop "
        "when you have nothing further. (Reminder of your role:)\n\n"
        + system_prompt
    )

_CONSULTATION_MAX_ROUNDS = 3
_DIARY_CUE_TOKEN_CAP = 10_000
# Current-self insights keep the historical 70% consultation window;
# past-self snapshots are capped lower so routed memory experts are cheap.
_INSIGHTS_CONTEXT_RATIO = 0.7
_PAST_CONTEXT_RATIO = 0.3


def _send_with_timeout(agent, session, content: "str | list"):
    """Send with timeout using a daemon thread. Returns response or None.

    Uses a daemon thread so it dies with the process — no orphaned threads.
    """
    import threading
    timeout = agent._config.retry_timeout
    result_box: list = []
    error_box: list = []

    def _worker():
        try:
            result_box.append(session.send(content))
        except Exception as e:
            error_box.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Timed out — thread is daemon, will die with process
        agent._log("soul_whisper_error", error=f"LLM call timed out after {timeout}s")
        return None
    if error_box:
        agent._log("soul_whisper_error", error=str(error_box[0])[:200])
        return None
    return result_box[0] if result_box else None


_CUE_EVENT_TYPES = ("diary", "thinking")

# Substring tokens we look for in raw bytes before paying json.loads cost.
# Lines without any of these can be skipped without parsing — non-cue events
# dominate the log (~95% in practice) and JSON parsing is the hot cost.
_CUE_EVENT_TYPES_BYTES = tuple(f'"{t}"'.encode("utf-8") for t in _CUE_EVENT_TYPES)

# Reverse-seek chunk size for tail-reading events.jsonl. 64 KB is enough to
# hold ~150 typical events; we keep reading backward only until the cue
# token budget is satisfied.
_REVERSE_READ_CHUNK = 64 * 1024


def _iter_lines_reverse(path):
    """Yield decoded lines from a file in reverse order, tail-first.

    Reads fixed-size byte chunks from the end of the file. The append-only
    JSONL invariant (every record is one JSON object + ``\\n``) guarantees
    splitting on ``b'\\n'`` produces complete lines, with at most one
    partial line at the front of each chunk that carries over to the next
    earlier chunk.

    UTF-8 safe: ``\\n`` (0x0A) cannot appear inside a multi-byte UTF-8
    sequence, so byte-splitting on ``\\n`` and then decoding each piece is
    correct.

    Yields stripped, non-empty UTF-8 strings.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        carry = b""
        while pos > 0:
            read_size = min(_REVERSE_READ_CHUNK, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + carry
            lines = chunk.split(b"\n")
            # First element may be a partial line (chunk boundary inside a
            # JSON record); save it for the next earlier chunk. When pos==0
            # the partial leading element is actually the file's first
            # complete record (no earlier chunk to merge with), so emit it.
            if pos > 0:
                carry = lines[0]
                tail = lines[1:]
            else:
                carry = b""
                tail = lines
            for raw in reversed(tail):
                if not raw:
                    continue
                try:
                    yield raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue


def _render_current_diary(agent) -> str:
    """Build the cue: time-anchored recent diary AND thinking entries,
    tail-capped.

    The cue is the *spark* that triggers the past-self consultation — small
    relative to the chat substrate. We mix diary (externalized declarations)
    and thinking (inner monologue) because thinking entries explain the
    *why* behind diary entries that sit next to them in time, and the
    consultation voice benefits from both. Each entry carries an absolute
    [HH:MM:SS] timestamp and a type tag (``diary`` or ``thinking``); a
    [now: HH:MM:SS] header at the top lets the reader compute recency.
    Total cue is tail-trimmed to fit under ``_DIARY_CUE_TOKEN_CAP``
    tokens.

    Reads the log in reverse from the tail, stops once the token budget
    is satisfied. Cost is O(recent cue entries), not O(file size). See
    lingtai-kernel#6.

    Returns empty string if the log is missing/unreadable/empty.
    """
    import json
    from datetime import datetime
    from ...token_counter import count_tokens

    log_path = agent._working_dir / "logs" / "events.jsonl"
    if not log_path.is_file():
        return ""

    now_str = datetime.now().strftime("%H:%M:%S")
    header = f"[now: {now_str}]"

    kept_reverse: list[str] = []
    running = count_tokens(header) + 2

    try:
        for line in _iter_lines_reverse(log_path):
            if not line:
                continue
            raw = line.encode("utf-8")
            if not any(tok in raw for tok in _CUE_EVENT_TYPES_BYTES):
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("type")
            if kind not in _CUE_EVENT_TYPES:
                continue
            text = rec.get("text")
            ts = rec.get("ts")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(ts, (int, float)):
                continue
            ts_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
            entry = f"[{ts_str}] {kind}\n{text.strip()}"
            cost = count_tokens(entry) + 2
            if kept_reverse and running + cost > _DIARY_CUE_TOKEN_CAP:
                break
            kept_reverse.append(entry)
            running += cost
    except Exception:
        return ""

    if not kept_reverse:
        return ""

    kept = list(reversed(kept_reverse))
    return header + "\n\n" + "\n\n".join(kept)


def _write_soul_tokens(agent, response) -> None:
    """Append a soul-tagged token-ledger entry for a consultation or
    inquiry LLM call. Best-effort — failures are silently swallowed so
    a ledger hiccup does not break the cadence."""
    u = response.usage
    if not (u.input_tokens or u.output_tokens or u.thinking_tokens or u.cached_tokens):
        return
    try:
        from ...token_ledger import append_token_entry
        ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
        model = getattr(agent.service, "model", None)
        endpoint = getattr(agent.service, "_base_url", None)
        append_token_entry(
            ledger_path,
            input=u.input_tokens, output=u.output_tokens,
            thinking=u.thinking_tokens, cached=u.cached_tokens,
            model=model, endpoint=endpoint,
            extra={"source": "soul"},
        )
    except Exception:
        pass


def _load_snapshot_interface(path):
    """Load a snapshot file written by psyche._write_molt_snapshot and
    return its verbatim ``ChatInterface``, or None on any failure.

    The consultation substrate is the past self's canonical chat. Tool
    calls/results and frozen tool-schema metadata are intentionally preserved
    so the model can read its own past work in the same structure it was
    originally trained to consume. New tool calls in the consultation session
    are refused at the executor layer by ``_run_consultation``.
    """
    import json
    from pathlib import Path
    from ...llm.interface import ChatInterface

    try:
        p = Path(path)
        if not p.is_file():
            return None
        raw = p.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("schema_version"), int):
            return None
        entries = payload.get("interface")
        if not isinstance(entries, list):
            return None
        return ChatInterface.from_dict(entries)
    except Exception:
        return None


def _fit_interface_to_window(iface, target_tokens: int):
    """Tail-trim a ChatInterface to fit within ``target_tokens`` while
    preserving tool-call/tool-result pairing invariants.

    Strategy: walk entries from the end backward, accumulating until the
    next addition would exceed ``target_tokens``. The resulting "kept
    suffix" must start on a clean boundary — never with a user{tool_result}
    whose matching assistant{tool_call} has been dropped. If the natural
    cutoff falls mid-pair, walk one more step backward (or forward, if
    that would yield zero entries) until a clean start is reached.

    System entries (typically index 0) are *always* preserved at the head
    of the kept set when present, since they carry the frozen system
    prompt that the snapshot represents. They count toward the budget.

    Returns a fresh ChatInterface containing only the kept entries.
    """
    from ...llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock

    if target_tokens <= 0:
        return ChatInterface.from_dict([])

    entries = list(iface.entries)
    if not entries:
        return ChatInterface.from_dict([])

    # Already fits — return as-is (clone via to_dict round-trip so the
    # caller can mutate the trimmed copy without affecting the source).
    current = iface.estimate_context_tokens()
    if current <= target_tokens:
        return _heal_trailing_tool_calls(ChatInterface.from_dict(iface.to_dict()))

    # Identify a leading system entry (preserve it).
    head_system = []
    body_start = 0
    if entries[0].role == "system":
        head_system = [entries[0]]
        body_start = 1

    # Walk body from tail backward to find the largest suffix that fits.
    # Build a dict-list of (head_system + suffix) and ask the live
    # ChatInterface for an accurate token count each time.
    head_dicts = [e.to_dict() for e in head_system]
    body = entries[body_start:]
    body_dicts = [e.to_dict() for e in body]

    kept_suffix_start = len(body)  # start with empty suffix
    for i in range(len(body) - 1, -1, -1):
        candidate_dicts = head_dicts + body_dicts[i:]
        probe = ChatInterface.from_dict(candidate_dicts)
        if probe.estimate_context_tokens() > target_tokens:
            break
        kept_suffix_start = i

    # Adjust kept_suffix_start to land on a clean boundary: if the entry
    # at kept_suffix_start is a user-role with only ToolResultBlocks,
    # find the matching tool_call earlier in the body and either include
    # the call (drop kept_suffix_start by 1) or drop the result entry
    # (raise kept_suffix_start by 1). The simpler safe move is the
    # latter — drop the orphaned tool_result entry from the head of the
    # suffix.
    while kept_suffix_start < len(body):
        entry = body[kept_suffix_start]
        if entry.role != "user":
            break
        # If every block in this user entry is a ToolResultBlock, it's a
        # candidate orphan. Check whether its tool_call ids appear in the
        # already-kept suffix (they can only appear earlier than us — by
        # construction the matching call would be just before, so it's
        # been excluded by the cutoff).
        if all(isinstance(b, ToolResultBlock) for b in entry.content):
            # Orphan tool_result with no preceding tool_call in the kept
            # suffix. Drop it and keep walking forward in case the next
            # entry is also orphan.
            kept_suffix_start += 1
            continue
        break

    final_body = body[kept_suffix_start:]
    if not final_body and not head_system:
        # Nothing fits at all — return an empty interface rather than
        # something malformed. Caller treats empty interface as "skip
        # this consultation."
        return ChatInterface.from_dict([])

    final_dicts = head_dicts + [e.to_dict() for e in final_body]
    return _heal_trailing_tool_calls(ChatInterface.from_dict(final_dicts))


def _heal_trailing_tool_calls(iface):
    """Synthesize tool_result placeholders for any unanswered tool_calls
    on the fitted interface's tail.

    The consultation path appends a spark via ``add_user_message``, which
    refuses if the tail assistant turn has dangling ``tool_calls``. The
    fitter's boundary walk handles leading orphan tool_results but does
    not heal trailing orphan tool_calls — those arrive when the agent
    snapshot was taken mid-tool-flow (timeout, AED restart, daemon crash).
    Heals in place via ``close_pending_tool_calls`` so the spark append
    succeeds and the consultation sees the synthesized aborts in context.
    """
    if iface.has_pending_tool_calls():
        iface.close_pending_tool_calls(reason="consultation:fit_window")
    return iface


def _kind_for_source(source: str) -> str:
    """Map a consultation source label to its prompt kind."""
    if source == "insights":
        return "insights"
    return "past"


def _consultation_context_target(source: str, window: int) -> int:
    """Return the fitted context budget for one consultation source.

    The shared current-self (``insights``) keeps the historical large window.
    Past-self snapshots are routed memory experts; they should be relevant but
    cheaper, so they receive a smaller slice of the context window. This is a
    first step toward cache/budget-friendly soul flow: select fewer memories,
    then load less of each selected memory.
    """
    ratio = (
        _INSIGHTS_CONTEXT_RATIO
        if _kind_for_source(source) == "insights"
        else _PAST_CONTEXT_RATIO
    )
    return max(1, int(window * ratio))


def _build_consultation_cue(agent, kind: str, diary: str) -> str:
    """Localized cue prompt for a consultation voice.

    insights — current self stepping back to look at its own diary.
    past     — past self handed the future self's diary as context.

    Both kinds inject the diary at ``{diary}``. If the diary is empty
    (no diary entries logged yet), the cue still works — the placeholder
    becomes "(no diary yet)" for legibility.
    """
    from ...i18n import t
    key = (
        "soul.consultation_cue_insights"
        if kind == "insights"
        else "soul.consultation_cue_past"
    )
    template = t(agent._config.language, key)
    body = diary if diary else "(no diary yet)"
    try:
        return template.format(diary=body)
    except Exception:
        # If the i18n string lacks {diary} for some reason, append the
        # diary block manually rather than failing the whole consultation.
        return f"{template}\n\n{body}"


def _run_consultation(agent, iface, source: str) -> dict | None:
    """Run one substrate+spark consultation against a seeded ChatInterface.

    The seeded interface is cloned verbatim. The present diary cue is sent as
    the spark. Tool schemas are declared so historic tool calls/results remain
    structurally legible, but any new tool-call attempts are intercepted with
    synthetic refusal ``ToolResultBlock``s for up to
    ``_CONSULTATION_MAX_ROUNDS`` rounds.

    Returns ``{"source": source, "blocks": [...]}`` or None on failure/no cue.
    """
    if iface is None or not iface.entries:
        return None

    window = None
    if getattr(agent, "_chat", None) is not None:
        try:
            window = agent._chat.context_window()
        except Exception:
            window = None
    if window is None:
        window = int(getattr(agent._config, "context_limit", None) or 200_000)
    target = _consultation_context_target(source, window)
    fitted = _fit_interface_to_window(iface, target)
    if not fitted.entries:
        return None

    tool_schemas = None
    try:
        tool_schemas = agent._session._build_tool_schemas_fn() or None
    except Exception as e:
        try:
            agent._log("consultation_tool_schema_error", source=source, error=str(e)[:200])
        except Exception:
            pass
        tool_schemas = None

    kind = _kind_for_source(source)
    try:
        from .config import _build_soul_system_prompt
        system_prompt = _build_soul_system_prompt(agent, kind=kind)
    except Exception as e:
        try:
            agent._log("consultation_prompt_resolution_failed", source=source, error=str(e)[:200])
        except Exception:
            pass
        return None

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=tool_schemas,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=fitted,
            provider=agent._config.provider,
        )
    except Exception as e:
        try:
            agent._log("consultation_session_failed", source=source, error=str(e)[:200])
        except Exception:
            pass
        return None

    diary = _render_current_diary(agent)
    if not diary:
        # No spark = no consultation. Avoid sending an empty user message —
        # the model has no trigger to react to.
        return None
    spark = diary

    from ...llm.interface import ToolResultBlock

    blocks_collected: list = []
    next_input: "str | list[ToolResultBlock]" = spark

    for _round_idx in range(_CONSULTATION_MAX_ROUNDS):
        response = _send_with_timeout(agent, session, next_input)
        if response is None:
            break

        try:
            _write_soul_tokens(agent, response)
        except Exception:
            pass

        try:
            if session.interface.entries:
                tail = session.interface.entries[-1]
                if tail.role == "assistant":
                    blocks_collected.extend(tail.content)
        except Exception:
            pass

        if not getattr(response, "tool_calls", None):
            break

        refusal_blocks: list[ToolResultBlock] = []
        for tc in response.tool_calls:
            rb = ToolResultBlock(
                id=tc.id,
                name=tc.name,
                content=_build_consultation_tool_refusal(system_prompt),
            )
            refusal_blocks.append(rb)
        blocks_collected.extend(refusal_blocks)
        next_input = refusal_blocks

    if not blocks_collected:
        return None

    return {"source": source, "blocks": blocks_collected}


def _list_snapshot_paths(agent):
    """Return snapshot_*.json files under <workdir>/history/snapshots/."""
    snapshots_dir = agent._working_dir / "history" / "snapshots"
    if not snapshots_dir.is_dir():
        return []
    try:
        return sorted(snapshots_dir.glob("snapshot_*.json"))
    except Exception:
        return []


# --- MoE-inspired past-self selection -------------------------------------
#
# DeepSeek's MoE gate routes each token to a few *relevant* experts plus a
# shared expert, instead of running the whole pool. Soul flow borrows the
# shape: route to the past selves whose molt summary lexically overlaps the
# current diary cue (exploitation), then — with a small probability — let one
# unrelated past self in for serendipity (exploration). When a human is
# waiting (a non-soul notification is pending) or there is no diary signal,
# expensive/random past voices are suppressed; the shared current-self
# remains the only candidate and may still bail if it also lacks a diary spark.

# Probability of admitting one random ("serendipity") past self when budget
# remains after routing. Exploration knob — keeps the creative, non-linear
# past-self intrusions the pure-random design had, bounded so it never
# dominates the fire.
_SERENDIPITY_RATE = 0.25
# Hard cap on serendipity past selves per fire — one, and only when the
# routed selection left headroom under max_past_count.
_SERENDIPITY_MAX = 1

# Lightweight English/Chinese-agnostic stopword set for lexical routing.
# Tiny on purpose: the signal is summary↔diary token overlap, and these
# words carry no topical weight.
_ROUTE_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at",
    "for", "with", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "i", "you", "we", "they", "my", "me", "as", "by", "from",
})


def _route_tokens(text: str) -> set:
    """Lowercase word-token set for lexical routing, stopwords removed.

    Tokenizes on non-alphanumeric boundaries (keeps CJK runs intact since
    they are alphanumeric to ``str.isalnum``-style regex \\w). Empty/short
    tokens and stopwords are dropped. Returns a set for cheap overlap.
    """
    import re

    if not text:
        return set()
    raw = re.findall(r"\w+", text.lower())
    return {tok for tok in raw if len(tok) > 1 and tok not in _ROUTE_STOPWORDS}


def _snapshot_route_summary(path) -> str:
    """Read only a snapshot's cheap top-level ``molt_summary`` metadata.

    Avoids loading/parsing the full interface (the expensive part). Reads
    the JSON file and returns the ``molt_summary`` string, or "" on any
    failure or missing field. Used purely as a relevance signal.
    """
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    summary = payload.get("molt_summary")
    return summary if isinstance(summary, str) else ""


def _select_relevant_snapshots(diary: str, paths: list, k: int) -> list:
    """Route to the ``k`` past selves whose molt summary best overlaps the
    diary cue. Returns paths ordered by descending lexical relevance,
    keeping only snapshots with a non-zero overlap score.

    Returns an empty list when ``k <= 0``, the diary is empty, or no
    snapshot's summary shares any token with the diary — the caller then
    falls back to random sampling so the fire never silently loses its
    past voices for lack of a signal.
    """
    if k <= 0 or not paths:
        return []
    cue = _route_tokens(diary)
    if not cue:
        return []

    scored = []
    for path in paths:
        summary_tokens = _route_tokens(_snapshot_route_summary(path))
        score = len(cue & summary_tokens)
        if score > 0:
            scored.append((score, path))
    # Highest overlap first; ties broken by path name for determinism.
    scored.sort(key=lambda sp: (-sp[0], str(sp[1])))
    return [path for _score, path in scored[:k]]


def _select_serendipity_snapshot(paths: list, exclude: list, rng) -> list:
    """With probability ``_SERENDIPITY_RATE``, pick one random past self not
    already selected. Returns ``[path]`` or ``[]``.

    ``rng`` is the ``random`` module (or a stand-in exposing ``random`` and
    ``choice``) so the probability gate is patchable in tests. The roll
    happens before the candidate check so a suppressed roll is observable.
    """
    if rng.random() >= _SERENDIPITY_RATE:
        return []
    excluded = set(exclude)
    candidates = [p for p in paths if p not in excluded]
    if not candidates:
        return []
    return [rng.choice(candidates)]


def _has_pending_human(agent) -> bool:
    """True when a non-soul notification channel is pending — a human/urgent
    signal that should suppress expensive random/past reflection.

    Uses the existing ``collect_notifications`` API. The agent's own soul
    channel is ignored (it is not a human-facing signal). Any other channel
    (email, mcp.*, …) counts as "a human may be waiting." Best-effort: on
    any error, returns False so the guard never breaks the fire.
    """
    try:
        from ...notifications import collect_notifications
        notifications = collect_notifications(agent._working_dir)
    except Exception:
        return False
    return any(channel != "soul" for channel in notifications)


def _select_past_snapshots(agent, diary: str, paths: list, max_past: int) -> list:
    """Choose which past-self snapshots to consult this fire.

    Layers, in priority order (DeepSeek-MoE-inspired):
      1. Guard — if a human/urgent notification is pending, or there is no
         diary signal, consult NO past selves. The shared insights path
         remains separate and may still bail if no diary spark exists.
      2. Routed — top-N past selves by lexical overlap of molt_summary with
         the diary cue (exploitation).
      3. Serendipity — with small probability, one random non-selected past
         self if budget remains (exploration).
      4. Fallback — if routing found nothing (no lexical signal) and the
         guard did not suppress, sample randomly as before so the fire keeps
         its past voices.

    Returns the chosen paths (<= max_past). Emits a ``consultation_route``
    log describing the decision.
    """
    import random as _random

    if max_past <= 0 or not paths:
        return []

    if _has_pending_human(agent):
        try:
            agent._log("consultation_route", decision="guard_pending_human",
                       routed=0, serendipity=0, max_past=max_past)
        except Exception:
            pass
        return []

    if not diary:
        # No useful recent diary signal — skip the expensive/random past
        # voices, keep only the shared current-self insights.
        try:
            agent._log("consultation_route", decision="guard_no_diary",
                       routed=0, serendipity=0, max_past=max_past)
        except Exception:
            pass
        return []

    routed = _select_relevant_snapshots(diary, paths, max_past)
    decision = "routed"

    if not routed:
        # No lexical signal — graceful fallback to the legacy random sample
        # so a signal-less fire still surfaces past voices.
        routed = _random.sample(paths, min(max_past, len(paths)))
        decision = "fallback_random"

    selected = list(routed)
    serendipity = []
    if len(selected) < max_past:
        serendipity = _select_serendipity_snapshot(
            paths, exclude=selected, rng=_random,
        )[:_SERENDIPITY_MAX]
        selected.extend(serendipity)

    try:
        agent._log("consultation_route", decision=decision,
                   routed=len(routed), serendipity=len(serendipity),
                   max_past=max_past,
                   selected=[p.stem for p in selected])
    except Exception:
        pass
    return selected


def _run_consultation_batch(agent) -> list[dict]:
    """Run one full consultation fire: the shared current-self ("insights")
    plus a MoE-routed set of past-snapshot voices, all in parallel. Returns
    the list of surviving voices (failed/timed-out consultations filtered).

    Past-self selection is no longer pure random. ``consultation_past_count``
    is treated as a *maximum*; the actual past voices are chosen by
    ``_select_past_snapshots`` (routed-by-relevance + bounded serendipity,
    suppressed when a human is waiting or there is no diary signal).
    """
    import threading

    max_past = max(0, int(getattr(agent._config, "consultation_past_count", 2)))

    # Build work items.
    work: list[tuple[str, "ChatInterface"]] = []
    insights_iface = None
    if getattr(agent, "_chat", None) is not None:
        try:
            insights_iface = agent._chat.interface
            from ...llm.interface import ChatInterface
            insights_iface = ChatInterface.from_dict(insights_iface.to_dict())
        except Exception:
            insights_iface = None
    if insights_iface is not None and insights_iface.entries:
        work.append(("insights", insights_iface))

    # Route to the relevant + (maybe) serendipitous past selves; load each.
    paths = _list_snapshot_paths(agent)
    if paths and max_past > 0:
        diary = _render_current_diary(agent)
        selected = _select_past_snapshots(agent, diary, paths, max_past)
        for path in selected:
            iface = _load_snapshot_interface(path)
            if iface is None or not iface.entries:
                try:
                    agent._log("consultation_load_failed", path=str(path))
                except Exception:
                    pass
                continue
            # source label encodes molt_count + ts when parseable from filename
            source = f"snapshot:{path.stem}"
            work.append((source, iface))

    if not work:
        return []

    # Run all consultations in parallel daemon threads with a barrier.
    results: list[dict | None] = [None] * len(work)

    def worker(idx: int, source: str, iface) -> None:
        try:
            results[idx] = _run_consultation(agent, iface, source)
        except Exception as e:
            try:
                agent._log("consultation_thread_error",
                           source=source, error=str(e)[:200])
            except Exception:
                pass
            results[idx] = None

    threads: list[threading.Thread] = []
    for idx, (source, iface) in enumerate(work):
        t = threading.Thread(
            target=worker, args=(idx, source, iface),
            daemon=True,
            name=f"consult-w-{idx}-{source[:20]}",
        )
        threads.append(t)
        t.start()

    timeout = float(getattr(agent._config, "retry_timeout", 300.0)) * 2.0
    for t in threads:
        t.join(timeout=timeout)

    voices = [r for r in results if r is not None and r.get("blocks")]
    return voices


def build_consultation_pair(agent, voices: list[dict], tc_id: str | None = None):
    """Build a synthetic (ToolCallBlock, ToolResultBlock) pair carrying
    the bundled consultation voices. The result content includes an
    appendix_note framing the voices as advisory and ephemeral.

    ``tc_id`` may be supplied by the caller — useful when the fire layer
    wants the chat-history call_id to match the soul_flow.jsonl fire_id
    (cross-reference between logs and chat). If omitted, a fresh id is
    generated.
    """
    import secrets
    import time
    from ...llm.interface import ToolCallBlock, ToolResultBlock
    from ...i18n import t as _t

    if not tc_id:
        tc_id = f"tc_{int(time.time())}_{secrets.token_hex(2)}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})

    # Strip the thinking block from the wire payload — it inflates tokens
    # without adding readable signal at the consumption site (the agent
    # main turn). Keep the source label and the voice text only.
    rendered_voices = [
        {"source": v["source"], "voice": v["voice"]}
        for v in voices
        if v.get("voice")
    ]
    payload = {
        "appendix_note": _t(agent._config.language, "soul.appendix_note"),
        "voices": rendered_voices,
    }
    result = ToolResultBlock(id=tc_id, name="soul", content=payload)
    return call, result
