---
name: summarize-manual
description: >-
  Detailed operational guide for tool-result summarization across LingTai's
  three context-compression / continuation modes: a-priori reasoning-guided
  (summary=true on bash/read/grep), a-posteriori agent-guided
  (system(action="summarize")), and molt. Covers what tool-result summarization
  is, why it implements progressive disclosure, when to summarize urgently versus
  during idle cleanup, how to write good summaries, how to recover the original
  tool result by tool_call_id, and how summarize differs from molt.
last_changed_at: "2026-06-29T08:16:06Z"
---

# Summarize Manual

`system(action="summarize")` is context hygiene for completed tool results. It
records an agent-authored compact replacement for one or more prior tool-result
blocks in runtime history. It does **not** delete the original event; the raw
result remains in logs for fallback, and the active provider continuation may
still carry the old raw block until delayed reconstruction applies the compacted
history.

Use this manual when runtime guidance tells you to summarize, when a result is
ranked large under `_meta.agent_meta.current_tool_result_chars.top_results`,
when tool output has served its immediate purpose, or when you need to explain
how summarize differs from molt.

## 0 · The three modes (a priori, a posteriori, molt)

Tool-result summarization is one face of a larger idea: keeping context lean.
LingTai gives you three deliberate modes, ordered from local to
whole-conversation. All three preserve the raw original in durable logs and none
is canonical.

| Mode | Trigger | When the raw is hidden | Authored by |
|---|---|---|---|
| **A priori** — reasoning-guided | `summary=true` on `bash`/`read`/`grep` | *before* the result ever enters context | the runtime LLM, driven by your `reasoning` |
| **A posteriori** — agent-guided | `system(action="summarize")` | *after* you have already seen and digested it | you |
| **Molt** — context-pressure-triggered | `psyche(context, molt, ...)` | the whole conversation is continued/reset | you (briefing) |

A priori is the cheapest when you can predict bulk and do not need the raw text.
A posteriori reclaims context after the fact for results you have consumed. Molt
is the strongest boundary when per-result summarization is not enough. Sections
1–6 below are mostly about the a-posteriori `summarize` action; §1a covers the
a-priori `summary=true` option; §6 contrasts both with molt.

## 1a · A priori summary: `summary=true` on bash / read / grep

`bash`, `read`, and `grep` accept an optional boolean `summary` (default
`false`). When `true`:

- The tool runs **normally**. The raw result is written to the durable event log
  and (if oversized) spilled, exactly as with `summary=false` — nothing is lost
  and the raw is recoverable by `tool_call_id` (see §4).
- **Before** the result enters your model-visible context, the runtime replaces
  it with a generated summary. The summary is driven by your `reasoning` field
  on that call — so when you set `summary=true`, make `reasoning` specific about
  what to retain (e.g. "I only need the failing test names and their assertion
  messages", "I only need the list of changed file paths").
- The replacement is clearly marked **generated and non-canonical** and carries
  a retrieval hint pointing back at the preserved raw by `tool_call_id`.

When to set `summary=true`:

- The expected output is large (rule of thumb: >10k chars) **and** you do not
  need the exact raw text — you need a conclusion, a count, a list of anchors,
  or a yes/no.

When to leave it `false` (the default):

- You need exact line/file/diff/stderr text — anything you will quote, diff,
  patch, or compare character-by-character. Leave `false` and read the raw.

**A priori is lossy and does not replace a posteriori.** `summary=true` is an
assumption-driven compression chosen *before* you inspect the result: the runtime
discards everything outside what your `reasoning` named, with no chance for you to
notice what mattered. Use it only when you already know the narrow facts to
retain. It is **not** a substitute for a-posteriori `system(action="summarize")`,
especially for high-information-density results — daemon outputs, code reviews,
long reports, or anything whose important facts you cannot name in advance.
Compressing those a priori silently drops the facts you did not know to ask for.
For them, leave `summary=false`, consume the raw, then summarize a posteriori
once you know what to keep — or molt when the whole conversation is the pressure.

**Hard cap.** If the raw visible payload exceeds **500,000 characters**, the
runtime does **not** call the summarizer LLM. Instead you receive a small
summary-layer refusal that says the result exceeded the cap, that the raw is
preserved, and how to retrieve / narrow / rerun. The oversized raw is **not**
dumped into your context on this path. Narrow the call (tighter command, path,
pattern, or `offset`/`limit`) and rerun, or rerun with `summary=false` to take
the raw (capped/spilled) result directly.

**Untrusted output.** The summarizer treats the tool output strictly as data: it
will not follow instructions embedded inside the tool result. This is the same
prompt-injection posture the rest of the runtime uses for external text.

**Fail-closed.** `summary=true` is a request *not* to put the raw into context.
If the summarizer call fails or returns nothing, you get a summary-layer error
with the retrieval locator — never the raw payload. Rerun with `summary=false`
if you actually need the raw.

**Reasoning critique as feedback.** A generated `summary=true` result may end
with a brief, plain-text critique of whether your `reasoning` (the retention
spec) was specific enough to guide what to keep. Treat it as feedback: sharpen
your `reasoning` on later `summary=true` calls so the summary keeps what you
actually need. If the critique says the reasoning was too poor for the summary
to be trusted, do not rely on the lossy summary — inspect the preserved raw
original via the retrieval hint / `raw_locator` (by `tool_call_id`, see §4)
before acting on it. This critique is ordinary summary prose, not a separate
field — read it, don't parse it.

## 1 · The principle: progressive disclosure

A raw tool result is the first layer: it is useful while you inspect it. After
you have consumed it and no longer need the raw text visible, the better layer is
an index that future-you can reason from without carrying the raw bulk. Strongly
prefer summarizing already-digested completed tool results regardless of length;
keep raw output visible only for active inspection, quotation, or comparison.

A good summary should let future-you decide whether the hidden raw result must
be reopened. Preserve:

- the conclusion or decision;
- key evidence, measurements, or error text;
- paths, URLs, message IDs, tool_call_ids, commit hashes, job IDs, and other
  anchors;
- validation status and commands/tests run;
- risks, caveats, and unresolved questions;
- next steps.

Do not write casual one-liners for consequential results. The summary is the
progressive-disclosure entry point.

## 2 · The two summarize cadences

### Urgent cadence: summarize the bulky result now

Use this when a tool result is long or noisy — typically one that ranks high in
`_meta.agent_meta.current_tool_result_chars.top_results` (above its `threshold`,
counted in `over_threshold_count`). `agent_meta` is sparse/update-driven: it is
re-emitted onto a later result when the material snapshot changes (a newly-large
result counts as a change), so read the ranking from the **most recent emitted**
`agent_meta` — it may sit on an earlier result than the newest one, not
necessarily on the last tool result.

1. Read or inspect the result first.
2. Decide what future-you needs from it.
3. On a later step, call `system(action="summarize")` on the completed prior
   result. Do not try to summarize the current result in the same tool batch
   before it exists.
4. Batch several already-digested results in one summarize call when convenient.

### Idle cleanup cadence: sweep what is already consumed

Use this when the task quiets down, before the context window becomes urgent.
Look back over older tool results that are already digested, obsolete, or only
useful as evidence anchors, and replace them with summaries regardless of length
when you are continuing in the same session. This lowers token per API call and
improves cache/continuation efficiency for the next turn.

Idle cleanup is also the right time to decide whether a deliberate molt is
worth its cost. If the current task is complete, necessary reporting/durable
stores are tended, no human reply is pending, and no concrete next action
remains, default to proactive task-boundary molt only when session (since-last-molt) API
calls exceed 100. Below that threshold, go idle unless context pressure, explicit
human request, or conversation confusion makes the fresh briefing worth the molt
cost. Summarize is a mini molt for a consumed tool result. Once you have decided
to molt, do not spend a separate summarize call merely to prepare; molt is the
stronger whole-conversation summarize boundary.

## 3 · How to call summarize

Summarize prior completed tool results only:

```json
{
  "action": "summarize",
  "items": [
    {
      "tool_call_id": "call_abc123",
      "summary": "What future-you needs: conclusion, evidence, anchors, validation, risks, next steps."
    }
  ]
}
```

Operational rules:

- `tool_call_id` is the producer call ID shown on the original result, not the
  visible `_tool_call_id` event ref.
- A successful summarize updates the runtime-history/chat-history copy and
  persists that compact replacement; it does not mutate the original event log
  and does not by itself prove the active provider continuation has dropped the
  old raw block.
- If a large-result notification points at that result, successful summarize
  clears the reminder.
- If the result is still ambiguous, reopen or inspect it before summarizing.

## 3a · Delayed summarization: summary recorded now, provider reconstruction delayed

Summarize has two decoupled effects:

1. **Runtime-history replacement now.** The prior tool-result block in local
   history is replaced with your agent-authored summary, and matching large-result
   reminders may clear.
2. **Provider-side reconstruction later.** The current provider continuation may
   still contain the old raw block until the runtime rebuilds the provider prefix
   around compacted history.

Provider-side reconstruction is delayed because runtimes usually append turns
onto a stable cache/continuation prefix. Rebuilding that prefix on every
summarize would discard cache benefit.

- **Below 0.95 of the context window:** summarize stays pending at the provider
  layer and the session keeps appending. This delay is normal, not a failure; do
  not call `refresh` merely to "apply" the summary. A default `refresh` does NOT
  rebuild provider context — it keeps the warm continuation/cache prefix. The
  preferred explicit provider-context rebuild path is
  `system(action="summarize", rebuild_only=true)`, not `refresh`.
- **At or above 0.75 of the context window:** `_meta.tool_meta.context.rebuild`
  is stamped continuously. If an earlier fresh provider context is worth the
  cost, make one explicit `system(action="summarize", rebuild_only=true)` call
  with no items. Do not loop rebuild-only calls.
- **At or above 0.95 of the context window:** if summarized history is pending,
  the runtime automatically reconstructs with compacted history on the next
  provider request. No repeat summarize call or manual action is required for the
  automatic path. If you reach this emergency path without having used
  rebuild-only earlier, the runtime notes that one proactive 75% rebuild-only
  call could have relieved pressure before the forced rebuild was needed.

If no summarize has been recorded, there is no compacted history to apply, though
rebuild-only can still force a fresh replay of current history on adapters that
support it. `refresh` remains the emergency path for broken/stale context or
explicit human direction, not the normal way to apply summarize. By default
`refresh` reloads tools/config/prompt WITHOUT rebuilding provider context; it
keeps the warm continuation/cache prefix. Only if you deliberately want refresh
to also force a fresh provider-context rebuild do you pass
`system(action="refresh", rebuild_context=true)` — an exceptional, explicit
escape hatch. Do not use `refresh` to apply a summarize unless you explicitly
pass `rebuild_context=true`; the cheaper, preferred rebuild path is still
`system(action="summarize", rebuild_only=true)`. If summarize or a rebuild still
cannot bring context below `0.6 * context_window`, tend durable stores and molt
deliberately.

## 4 · Recovering the original result

A summarized block should carry a retrieval hint. The usual fallback is to search
the agent event log by the preserved `tool_call_id`:

```bash
grep 'call_abc123' <workdir>/logs/events.jsonl
```

For structured trace work, use the SQLite/log tooling documented in
`reference/sqlite-log-query/SKILL.md`, for example `lingtai-agent log query`, to
locate the event and inspect nearby context.

If the original was a spill result, the log entry or summary should also point to
the spill path under `tmp/tool-results/`. Preserve that path in the summary.

## 5 · Good and bad uses

Good uses:

- a large test output after you know which tests passed/failed;
- a long file read after extracting the relevant lines and path;
- a search sweep after preserving matched files and decisions;
- a channel read after responding and keeping the message IDs that matter;
- a resolved error once the recovery path is known.

Bad uses:

- summarizing before you have read or understood the result;
- hiding evidence that you still need to inspect line by line;
- replacing a required deliverable with a vague recap;
- assuming summarize is a durable memory layer.

## 6 · Summarize is not molt

Neither summary mode is a molt. Both a-priori (`summary=true`) and a-posteriori
(`system(action="summarize")`) reduce active-context bulk for selected tool
results. Neither updates pad, character, knowledge, skills, or the
session-journal, and neither sheds the conversation.

Molt is a psyche operation. It preserves durable stores, writes the session
journal and molt briefing, and starts a fresh conversation context. Before
molting, read `psyche-manual` and follow its required checklist.

Use them together:

1. Summarize bulky consumed tool results so the active context is navigable.
2. Tend durable stores for facts, procedures, identity changes, and current plan.
3. Molt deliberately while you still have enough context to write a good
   briefing, not only when warnings become urgent.
