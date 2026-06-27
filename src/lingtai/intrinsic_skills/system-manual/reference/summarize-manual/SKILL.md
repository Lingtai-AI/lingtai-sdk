---
name: summarize-manual
description: Detailed operational guide for system(action="summarize"): what tool-result summarization is, why it implements progressive disclosure, when to summarize urgently versus during idle cleanup, how to write good summaries, how to recover the original tool result by tool_call_id, and how summarize differs from molt.
---

# Summarize Manual

`system(action="summarize")` is context hygiene for completed tool results. It
replaces the context-visible copy of one or more prior tool-result blocks with
your own summary. It does **not** delete the original event; the raw result
remains in logs for fallback.

Use this manual when runtime guidance tells you to summarize, when a
`large_tool_result` reminder appears, when tool output has served its immediate
purpose, or when you need to explain how summarize differs from molt.

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

Use this when a tool result is long, noisy, or raises a `large_tool_result`
reminder.

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

Idle cleanup is also the right time to choose a deliberate molt. If the current
task is complete, necessary reporting/durable stores are tended, no human reply
is pending, and no concrete next action remains, molt regardless of context
size. Summarize is a mini molt for a consumed tool result. Once you have decided
to molt, do not spend a separate summarize call merely to prepare; molt is the
stronger whole-conversation summarize boundary. This is a
token-economy boundary, not an aesthetic reset: keeping a finished task segment
alive raises future token per API call and can reduce cache/continuation
efficiency.

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
- A successful summarize replaces only the active-context copy; it does not
  mutate the original event log.
- If a large-result notification points at that result, successful summarize
  clears the reminder.
- If the result is still ambiguous, reopen or inspect it before summarizing.

## 3a · Local effect now, reconstruction delayed

Summarize has two layers of effect, and they are deliberately decoupled.

**Local effect — immediate.** The moment the call succeeds, the visible
tool-result blocks are replaced with your summary and any matching large-result
reminders are cleared. The successful result carries a short reassurance to that
effect. You see the compacted context right away.

**Provider-side reconstruction — delayed.** Runtimes serve most requests by
*appending* new turns onto a stable cache/continuation prefix, not by
*reconstructing* that prefix from scratch each time. Rebuilding the prefix on
every summarize would throw away the cache/continuation benefit. So summarizing
does not immediately force the provider to rebuild context:

- **Below 0.75 of the context window:** the summarize stays "pending" at the
  provider layer. The session keeps appending; you can keep working and may issue
  more summarize calls safely. This delay is normal and is not a failure.
- **At or above 0.75 of the context window:** if summarized history is
  pending, the runtime automatically reconstructs context with that compacted
  history on the next provider request. You do not need to call summarize again
  or take manual action for this to happen. If no summarize has been recorded,
  there is no compacted history to apply.

`refresh` is an **emergency** reconstruction path — for context that is broken or
stale, or when an immediate rebuild is urgently needed. It is not a routine knob
for the normal summarize flow; do not reach for it just to "apply" a summarize.

If summarize and the automatic reconstruction still cannot bring context back
below `0.6 * context_window`, that is the signal to **molt** (see §6 and
`psyche-manual`).

Runtimes that already reconstruct on every request simply observe no delay; the
above is generic behavior, not a single provider's policy.

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

`system(action="summarize")` reduces active-context bulk for selected tool
results. It does not update pad, character, knowledge, skills, or the
session-journal, and it does not shed the conversation.

Molt is a psyche operation. It preserves durable stores, writes the session
journal and molt briefing, and starts a fresh conversation context. Before
molting, read `psyche-manual` and follow its required checklist.

Use them together:

1. Summarize bulky consumed tool results so the active context is navigable.
2. Tend durable stores for facts, procedures, identity changes, and current plan.
3. Molt deliberately while you still have enough context to write a good
   briefing, not only when warnings become urgent.
