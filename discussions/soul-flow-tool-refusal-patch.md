# Soul flow: tools-preserved consultation with refusal interception

## Problem

The current consultation path (`_run_consultation` in `intrinsics/soul.py:856`) makes two structural compromises that produce empty fires and confused output, especially on Codex / GPT-5.x models:

1. **Lossy chat clone.** `_clone_current_chat_for_insights` (line 945) strips every block that isn't `TextBlock` / `ThinkingBlock`. Entries whose content was entirely tool calls/results collapse to zero blocks and get dropped. For Codex agents — whose chat history is dominated by tool-call/result pairs — the cloned chat is mostly empty. The consultation's own logs confirm this: `outcome="empty"` with `sources: []` on every fire (work list empty before any LLM call).

2. **Tool channel suppressed by prompt, not structure.** `_run_consultation` passes `tools=None` and appends a system-prompt instruction "You have no tools. Respond with plain text only. Never output tool calls or XML tags." (line 896). Models like Codex compensate for the missing tool channel by emitting raw XML in their text output — a documented failure mode of stripped-tool reuse.

The deeper conceptual problem: the existing design treats consultation as **observation across a temporal gap** ("past self looking forward, advising present self"). In practice, the more useful framing is **memory association** — a past self is a dormant context window that, when reawakened with a present-moment trigger, surfaces instincts and associations the present self can use. The tool-stripped, persona-shifted clone breaks both the structural integrity and the associative semantics.

## Goal

Reframe consultation as **substrate + spark**:

- **Substrate**: the past self's chat, cloned **verbatim** — every block, every tool pair intact. This is the dormant context window.
- **Spark**: a short diary cue describing what the present self is grappling with. This is the trigger that reawakens the substrate.
- **Reaction**: the past self responds as if they are live now — what surfaces, what they would reach for, what they want to say. Tools are declared so they can read their own past calls in context, but any new call is intercepted with a refusal that redirects them to text.

Concretely:

- Tools are **declared** in the consultation session (full live tool decl list from the agent), not `None`.
- The chat is **verbatim** — `_clone_current_chat_for_insights` is deleted.
- A **single consultation system prompt** replaces the per-voice `soul.voice.<name>.prompt` machinery for the consultation path. (Inquiry — `soul_inquiry` — keeps its existing voice profile system unchanged.)
- The diary cue is sent as a user message verbatim — no per-kind wrapper (`_build_consultation_cue` is dropped from this path).
- A **bounded redirect loop** (max 3 rounds) handles tool-call attempts: each attempt gets a synthetic `ToolResultBlock` carrying a refusal message, then the loop continues until the model produces a text-only response (or the round budget is exhausted).
- The voice payload becomes `{"source": str, "blocks": [...]}` — the full sequence of assistant blocks plus refusal `ToolResultBlock`s across all rounds, in order. The present self reads the complete arc, refusals included.
- `outcome="ok"` if `blocks` is non-empty (any block of any kind counts); `"empty"` only if nothing came back at all.

## Design notes

- **Why verbatim clone + real tool decls.** The model is trained on `(call, result)` pairs. Stripping them or removing tool decls leaves the model with a maimed chat it tries to "fix" by hallucinating tool syntax. Keeping both intact lets the model parse its own past activity as it was originally trained to read it. Refusal happens at the **executor layer** (synthesized `ToolResultBlock`), not the prompt layer — same channel the model already understands.

- **Why a single consultation system prompt.** Soul-flow's per-voice `soul_voice` profiles (`inner` / `kind` / `strict` / `custom`) made sense when the consultation was an "advice from the past" persona. Under the spark-and-substrate framing, the persona lives in the **chat** (the past self's own thoughts), not in a system prompt overlay. One prompt — narrowly scoped to "this is consultation mode, tools are intercepted, react in text" — is enough. Inquiry keeps its profiles.

- **Why up to 3 rounds.** First round may attempt tools (model wants to act on the spark). Second round after refusal usually produces text. Third round is safety margin for stubborn models. Beyond that, diminishing returns and unbounded latency. If 3 rounds all attempt tools, return `blocks` anyway — refusal-only voices are still signal ("past self spent three rounds wanting to act and never spoke" tells the present self something).

- **Why include refusals in `blocks`.** The present self reads a complete dialogue, not a curated highlight reel. They can distinguish "past self spoke immediately" (calm, certain) from "past self tried three tools before settling on words" (something pulling at them). Refusal text is consultation framing — when the present self reads it, they read the same instruction the past self read; reinforces the consultation-is-a-context philosophy.

- **Window-fit budget multiplier drops 0.8 → 0.7.** Tool decls now count toward the budget (5–15K tokens for a fully-loaded agent). Existing 0.8 leaves no room for system prompt + tool schemas + diary cue. 0.7 gives ~30% headroom which matches the rest of the kernel's window-management margins.

- **Diary cue capped at 10K tokens, with timestamps.** Today `_render_current_diary` walks every diary entry since the last molt and concatenates them — for a long-running agent that's huge, easily larger than the chat substrate it's meant to spark. The cue should be the *trigger*, not the substrate. Cap at 10K tokens (keep the tail, drop oldest entries). Each entry gets an absolute `[HH:MM:SS]` timestamp prefix; a single `[now: HH:MM:SS]` header at the top lets the past self compute recency themselves (cleaner than encoding "X min ago" per entry — entries stay immutable, only the "now" header varies).

- **`_send_with_timeout` generalized.** Today it accepts a string only. The refusal rounds need to send `list[ToolResultBlock]`. Widening the parameter type to `str | list[ToolResultBlock]` is a one-line change; the wrapper just passes through to `session.send` which already accepts both.

- **Schema bump v2 → v3.** Old `soul_flow.jsonl` voice records have separate `voice: str` + `thinking: list` fields. New records have a single `blocks: [...]` field. Bumping `schema_version` to 3 keeps consumers explicit; old records on disk stay v2; consumers branch on `schema_version`.

- **`build_consultation_pair` rendering — deferred.** How the synthetic tool-result content presents the new `blocks` to the present self (human-readable labels vs. structured vs. verbatim concat) is a separate decision. The patch keeps the existing rendering for now and flags it for follow-up. Behavior is unchanged for present-self-side reading until that decision lands.

---

## Files to change

### 1. `src/lingtai_kernel/intrinsics/soul.py`

#### 1a. Add module-level constants

Near the top of the file, alongside `SOUL_VOICE_PROMPT_MAX`:

```python
_CONSULTATION_SYSTEM_PROMPT = (
    "The chat below is your context — your thoughts, your work, your tools, your memory. "
    "A new diary cue from the present moment will arrive as the next message. "
    "React to it. What does it remind you of? What surfaces? What would you reach for? "
    "Tool schemas are preserved so you can read your own past calls in context, "
    "but any new tool call you attempt will be intercepted and refused. "
    "Respond in plain text — observations, instincts, things worth remembering. "
    "Speak briefly."
)

_CONSULTATION_TOOL_REFUSAL = (
    "Consultation mode: tool calls are intercepted, not executed. "
    "Respond in plain text — observations, instincts, things worth remembering."
)

_CONSULTATION_MAX_ROUNDS = 3
```

These are intentionally English-only. They are runtime mechanics, not persona voice — they don't go through i18n. Same convention as migration warnings and other internal log/control strings (per `CLAUDE.md`).

#### 1b. Rewrite `_run_consultation` (line 856)

Replace the body. Key changes:
- Drop `_clone_current_chat_for_insights` call — the caller (`_run_consultation_batch`) now passes the verbatim live chat or verbatim snapshot interface directly.
- Build session with **real tool decls** from `agent._session._build_tool_schemas_fn()`, not `tools=None`.
- Use the new single `_CONSULTATION_SYSTEM_PROMPT` — no `_build_soul_system_prompt(kind=...)` call, no `_build_consultation_cue(kind, diary)` wrapper.
- Send the raw diary cue as the first user message.
- Run a redirect loop of up to `_CONSULTATION_MAX_ROUNDS`: each round, if the response includes tool calls, build refusal `ToolResultBlock`s and feed them via `session.send([...])`; otherwise break.
- Return `{"source": source, "blocks": [...]}` — assistant turns + injected refusal results across all rounds.

Sketch (final wording in the patch; this shows the structural shape):

```python
def _run_consultation(agent, iface, source: str) -> dict | None:
    if iface is None or not iface.entries:
        return None

    # Window-fit. 0.7 leaves headroom for system prompt + tool decls + cue.
    window = None
    if getattr(agent, "_chat", None) is not None:
        try:
            window = agent._chat.context_window()
        except Exception:
            window = None
    if window is None:
        window = int(getattr(agent._config, "context_limit", None) or 200_000)
    target = max(1, int(window * 0.7))
    fitted = _fit_interface_to_window(iface, target)
    if not fitted.entries:
        return None

    # Pull live tool decls from the agent's main session.
    tool_schemas = None
    try:
        tool_schemas = agent._session._build_tool_schemas_fn() or None
    except Exception as e:
        try:
            agent._log("consultation_tool_schema_error", source=source, error=str(e)[:200])
        except Exception:
            pass
        tool_schemas = None  # degrade gracefully — consultation still runs without tools

    try:
        session = agent.service.create_session(
            system_prompt=_CONSULTATION_SYSTEM_PROMPT,
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

    blocks_collected: list = []  # type: list[ContentBlock]
    next_input: "str | list[ToolResultBlock]" = diary

    for round_idx in range(_CONSULTATION_MAX_ROUNDS):
        response = _send_with_timeout(agent, session, next_input)
        if response is None:
            # timeout/error — _send_with_timeout already logged
            break

        # Persist tokens for this round.
        try:
            _write_soul_tokens(agent, response)
        except Exception:
            pass

        # The session's interface now contains the assistant turn we just
        # received as its tail entry. Capture those blocks.
        if session.interface.entries:
            tail = session.interface.entries[-1]
            if tail.role == "assistant":
                blocks_collected.extend(tail.content)

        # If the response had no tool calls, we're done.
        if not response.tool_calls:
            break

        # Build refusal results, one per call_id, and append to blocks_collected
        # so the present self sees the refusal turn-by-turn.
        from ..llm.interface import ToolResultBlock
        refusal_blocks: list[ToolResultBlock] = []
        for tc in response.tool_calls:
            rb = ToolResultBlock(
                id=tc.id,
                name=tc.name,
                content=_CONSULTATION_TOOL_REFUSAL,
            )
            refusal_blocks.append(rb)
        blocks_collected.extend(refusal_blocks)
        next_input = refusal_blocks

    if not blocks_collected:
        return None

    return {"source": source, "blocks": blocks_collected}
```

A couple of things this sketch implies, worth flagging for review:

- It uses `agent._session._build_tool_schemas_fn()` directly. That accessor is currently private; if you'd rather expose a public helper on the session (e.g. `agent._session.tool_schemas()`), that's a minor cleanup but not required.
- `next_input` alternates between `str` (initial diary cue) and `list[ToolResultBlock]` (refusal rounds). `_send_with_timeout` needs its parameter widened (see 1d).
- The function pulls blocks from `session.interface.entries[-1]` rather than reconstructing them from the `LLMResponse`. The interface is the source of truth for canonical blocks (provider-agnostic); `LLMResponse.text`/`thoughts`/`tool_calls` are the parsed view. Using the interface ensures we capture exactly what the canonical layer recorded, including any blocks the response object might have collapsed.

#### 1c. Delete `_clone_current_chat_for_insights` (line 945)

No longer called. Drop the function entirely. Its docstring explicitly references the strip-and-skip approach we're abandoning.

#### 1c-bis. Update `_render_current_diary` (line 509) — cap + timestamps

Add a module constant near `_CONSULTATION_MAX_ROUNDS`:

```python
_DIARY_CUE_TOKEN_CAP = 10_000
```

Rewrite the function. Behavior changes:

1. **Per-entry timestamp prefix.** Each entry's `ts` (Unix epoch float in `events.jsonl`) is converted to local-time `HH:MM:SS` and prefixed in square brackets. Timestamps sit on their own line above the text:

   ```
   [14:07:01]
   Replied to human via internal mail.
   ```

   Two-line format (timestamp on its own line) keeps the prose readable when entries are long; one-line format would leave the timestamp visually tied to only the first line of a multi-paragraph entry.

2. **`[now: HH:MM:SS]` header at the top.** Single header line, blank line below, then entries. Lets the model compute recency itself from the gap between `now` and each entry's timestamp; we don't encode "N min ago" per entry.

3. **Tail-cap at 10K tokens.** Walk all diary entries to build the formatted list, then trim from the **front** until the joined cue fits under `_DIARY_CUE_TOKEN_CAP`. If even the most recent entry exceeds the cap on its own (rare but possible — a giant single response), keep that one entry alone; don't truncate inside an entry.

Sketch:

```python
def _render_current_diary(agent) -> str:
    """Build the diary cue: time-anchored recent thoughts, tail-capped.

    The cue is the *spark* that triggers the past-self consultation — small
    relative to the chat substrate. Each entry carries an absolute
    [HH:MM:SS] timestamp; a [now: HH:MM:SS] header at the top lets the
    reader compute recency. Total cue is tail-trimmed to fit under
    ``_DIARY_CUE_TOKEN_CAP`` tokens.

    Returns empty string if the log is missing/unreadable/empty.
    """
    import json
    from datetime import datetime
    from ..token_counter import count_tokens

    log_path = agent._working_dir / "logs" / "events.jsonl"
    if not log_path.is_file():
        return ""

    # Collect (ts_str, text) for every diary entry.
    formatted: list[str] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "diary":
                    continue
                text = rec.get("text")
                ts = rec.get("ts")
                if not isinstance(text, str) or not text.strip():
                    continue
                if not isinstance(ts, (int, float)):
                    continue
                ts_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
                formatted.append(f"[{ts_str}]\n{text.strip()}")
    except Exception:
        return ""

    if not formatted:
        return ""

    now_str = datetime.now().strftime("%H:%M:%S")
    header = f"[now: {now_str}]"

    # Tail-trim: keep most recent entries that fit under the cap.
    # Walk backwards, accumulate until adding the next entry would exceed.
    kept: list[str] = []
    running = count_tokens(header) + 2  # +2 ≈ paragraph separators
    for entry in reversed(formatted):
        cost = count_tokens(entry) + 2
        if kept and running + cost > _DIARY_CUE_TOKEN_CAP:
            break
        kept.append(entry)
        running += cost
    kept.reverse()

    return header + "\n\n" + "\n\n".join(kept)
```

Notes:

- `count_tokens` lives at `lingtai_kernel/token_counter.py` and is already used by `interface.py` for window budgeting. Reuse — don't add a second token estimator.
- `datetime.fromtimestamp(...)` and `datetime.now()` use local time, matching how `.status.json`'s `current_time` field is rendered. Consistent with the rest of the kernel's user-facing timestamps.
- `if kept and running + cost > _CAP` — the `kept and` guard ensures we always keep at least one entry, even if it alone exceeds the cap. Better to send one oversized entry than an empty cue.
- `reversed(formatted)` then `kept.reverse()` keeps chronological order in the final output (oldest-kept first, newest last). Models read top-to-bottom; chronological order matches.

#### 1d. Widen `_send_with_timeout` parameter (line 445)

Change signature from `content: str` to `content: "str | list"` (or import `ToolResultBlock` for a tighter union type). Body is unchanged — `session.send` already accepts both.

#### 1e. Update `_run_consultation_batch` (line 980)

Two adjustments:

- The insights work item used to be `_clone_current_chat_for_insights(agent)`. Replace with a verbatim clone of the live chat interface. Today this is roughly:

  ```python
  insights_iface = agent._chat.interface.clone() if agent._chat else None
  if insights_iface and insights_iface.entries:
      work.append(("insights", insights_iface))
  ```

  (`ChatInterface.clone()` exists — see `llm/interface.py`. If it doesn't expose the right API, `ChatInterface()` + manual `add_*` walk works, same shape as the old function but without the strip.)

- Snapshot loading via `_load_snapshot_interface` is unchanged; it already produces a verbatim interface from the snapshot file.

#### 1f. Update voice record schema (line 1077-1086 in `base_agent.py`, see section 2)

Bump `schema_version` from 2 to 3, write a single `blocks` field in place of `voice` + `thinking`. See section 2 for the call-site change.

#### 1g. Drop `_build_consultation_cue` calls from the consultation path

`_build_consultation_cue` (line 830) wraps the diary in a per-kind framing. Under the new model the system prompt + chat carry framing; the cue is sent verbatim. Two options:

- **Conservative**: leave `_build_consultation_cue` defined, just stop calling it from `_run_consultation`. Future cleanup can drop it if no caller emerges.
- **Aggressive**: delete it now along with `_kind_for_source` if the latter has no other caller after the refactor.

Recommend **conservative** — minimizes blast radius of this patch; a follow-up sweep can remove dead helpers cleanly.

---

### 2. `src/lingtai_kernel/base_agent.py`

#### 2a. Update voice record write (line 1074-1086 in `_run_consultation_fire`)

Today:

```python
for v in voices:
    try:
        src = v.get("source", "unknown")
        self._append_soul_flow_record({
            "kind": "voice",
            "schema_version": 2,
            "ts": ...,
            "fire_id": fire_id,
            "source": src,
            "consultation_kind": _kind_for_source(src),
            "voice": v.get("voice", ""),
            "thinking": v.get("thinking", []),
        })
    except Exception as e:
        ...
```

After:

```python
for v in voices:
    try:
        src = v.get("source", "unknown")
        # Serialize blocks to dict form for JSONL persistence. ContentBlock
        # already has a to_dict / canonical-form helper used by the chat
        # interface — reuse it so the on-disk shape matches what the rest
        # of the kernel reads.
        blocks_serialized = [_block_to_dict(b) for b in v.get("blocks", [])]
        self._append_soul_flow_record({
            "kind": "voice",
            "schema_version": 3,
            "ts": ...,
            "fire_id": fire_id,
            "source": src,
            "blocks": blocks_serialized,
        })
    except Exception as e:
        ...
```

`_block_to_dict` should mirror whatever serialization `ChatInterface.to_dict()` uses for individual blocks. If the interface module exposes a `block_to_dict` helper, use it; otherwise add a small one in `intrinsics/soul.py` that switches on block type (`TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ThinkingBlock`).

The `consultation_kind` field is dropped — it was a derived label tied to the per-kind cue/prompt machinery we're removing. The `source` field still distinguishes insights vs. snapshots.

#### 2b. Update outcome computation (line 1054)

Today:

```python
sources = [v.get("source", "unknown") for v in voices]
outcome = "ok" if voices else "empty"
```

After (no change needed — `voices` is non-empty iff at least one consultation returned blocks; still correct semantics under v3). Keep as-is.

#### 2c. Verify `build_consultation_pair` signature (line 1047 in soul.py)

`build_consultation_pair(agent, voices, tc_id)` consumes the voice list. Today it reads `voice["voice"]` and `voice["thinking"]`. With v3, voices have `blocks` instead.

**Decision deferred (per spec scope):** keep `build_consultation_pair` reading the old fields by translating v3 voices in the caller — i.e., before passing to `build_consultation_pair`, flatten each voice's `blocks` into a synthetic `voice` (concatenated text) + `thinking` (concatenated thinking text) pair. This preserves present-self-side rendering exactly as today.

Concretely, in `_run_consultation_fire` before splicing, add:

```python
def _flatten_v3_for_pair(v):
    voice_text_parts = []
    thinking_parts = []
    tool_attempt_lines = []
    for b in v.get("blocks", []):
        # TextBlock → voice text
        # ThinkingBlock → thinking
        # ToolCallBlock → "Wanted to: name(args)" line
        # ToolResultBlock (refusal) → skip in flatten (already implied by "wanted to" line)
        ...
    return {
        "source": v["source"],
        "voice": "\n".join(voice_text_parts),
        "thinking": thinking_parts,
        # "tool_attempts" added for visibility in the spliced pair text
    }
```

Then pass `[_flatten_v3_for_pair(v) for v in voices]` to `build_consultation_pair`. This keeps the chat-splice rendering stable while the soul_flow.jsonl stores v3 natively. A follow-up patch can then redesign `build_consultation_pair` to consume v3 directly.

This is a known seam — flagged for follow-up rather than expanding the scope of this patch.

---

### 3. Tests

#### 3a. Update existing soul-flow tests

Any test that asserts `voice["voice"]` or `voice["thinking"]` shape needs updating to v3 (`voice["blocks"]`). Likely candidates: `tests/test_soul_flow.py`, `tests/test_soul_consultation.py` (grep for `_run_consultation`, `_clone_current_chat_for_insights`, `_run_consultation_batch`).

Tests for `_clone_current_chat_for_insights` should be deleted (function deleted).

#### 3b. New tests

Add coverage for the redirect loop:

- `test_consultation_redirects_tool_calls`: build a fake session that emits one tool call on round 1 then text on round 2; assert the loop sends a refusal `ToolResultBlock` between them and the final `blocks` contains [tool_call, refusal_result, text].
- `test_consultation_max_rounds_exhausted`: fake session emits tool calls on all 3 rounds; assert loop terminates, `blocks` contains all 3 attempts + refusals, `outcome="ok"`.
- `test_consultation_no_diary_cue`: empty `_render_current_diary` → return None (no spark, no fire).
- `test_consultation_verbatim_clone`: assert that a chat with tool-call/result pairs is cloned with all blocks intact (no stripping).
- `test_render_diary_format`: write a few `type: "diary"` records to a fake `events.jsonl` with known `ts` values; assert the rendered cue starts with `[now: HH:MM:SS]`, each entry has its own `[HH:MM:SS]` prefix on its own line, and entries are in chronological order.
- `test_render_diary_tail_cap`: write enough diary entries to exceed 10K tokens; assert the rendered cue is under the cap, the *most recent* entries are preserved, and the *oldest* entries are dropped.
- `test_render_diary_single_oversized_entry`: write one diary entry exceeding 10K tokens by itself; assert it is still returned (single oversized entry is preferred to empty cue).

---

## Estimated diff size

- `intrinsics/soul.py`: ~70 lines deleted (`_clone_current_chat_for_insights`, old `_run_consultation` body, old `_render_current_diary` body, `_build_consultation_cue` call sites), ~120 lines added (new `_run_consultation` with redirect loop, new `_render_current_diary` with timestamps + cap, three module constants, verbatim insights clone). Net ~+50.
- `base_agent.py`: ~10 lines changed (voice record schema, `_flatten_v3_for_pair` helper).
- `tests/`: ~3 deleted tests, ~7 new tests, ~2 updated tests. Net ~+80 lines.

Total: roughly +130 / -70 across the kernel, plus tests.

## Out of scope for this patch (follow-ups)

- **`build_consultation_pair` v3-native rendering.** Today's flattener is a bridge; a future pass can render `blocks` directly into the synthetic pair content with proper structure (e.g., labeled sections for thinking / said / wanted-to-do).
- **Soul voice profiles (`soul.voice.<name>.prompt`).** Still used by `soul_inquiry`. If consultation no longer uses them, the user-facing config + i18n can be reviewed for whether the inquiry-only surface justifies the complexity. Not changed here.
- **Per-tool serialization.** Earlier design discussion proposed per-tool serializers. The refusal-interception approach makes them unnecessary for consultation. They might still be useful for *other* contexts (e.g., compaction summaries), but that's a separate design.
- **Deleting `_kind_for_source` and `_build_consultation_cue`.** Conservative leave-in-place for this patch; clean up in a follow-up sweep.
