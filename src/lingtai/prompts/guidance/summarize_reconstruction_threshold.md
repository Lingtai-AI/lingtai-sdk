---
id: summarize_reconstruction_threshold
title: Delayed summarization reconstruction threshold
kind: meta-guidance-section
summary: >
  Resident guidance explaining that summarize records compact history immediately but
  provider-context reconstruction happens later at the threshold.
why: >
  This fragment exists so agents do not waste calls trying to force summarize reconstruction, do
  not assume raw blocks vanished too early, and know when to molt instead.
related_files:
  - "src/lingtai/prompts/principle.md"
  - "src/lingtai/prompts/guidance/INDEX.md"
  - "reference/summarize-manual/SKILL.md"
maintenance: >
  When editing this file, treat related_files as maintained inner links for the prompt/guidance
  source graph. Before changing behavior or prose, crawl the listed files, update any affected
  reciprocal link on the other side (principle links to each prompt/guidance source; each such
  source links back to principle; guidance INDEX links to each guidance section and each section
  links back to INDEX), and keep this list generous enough for future maintainers to find adjacent
  prompt layers. Do not list tests merely because they validate the contract; add loaders,
  manifests, or package metadata only when this file actually discusses them or the prompt-source
  relation needs that link.
---
Summarize has two mechanisms agents must distinguish. First, a successful summarize records an agent-authored replacement in runtime history (and, for legacy compatibility, clears a matching `large_tool_result` reminder if one is still present from before such reminders were retired). That bookkeeping does not mean the active provider-side context the agent is continuing from has been rebuilt, and the agent should not assume the old raw block has disappeared from its current continuation. Below `0.95` of the context window, summarized history may remain pending at the provider layer while the session keeps appending to the existing conversation; this is normal. Do not call `refresh` just to apply a summarize. Once context is at/above `0.75`, `_meta.tool_meta.context.rebuild` permits a deliberate one-shot provider-context rebuild with `system(action="summarize", rebuild_only=true)` when paying for an earlier rebuild is worth it; call it with no items and do not loop it. When summarized history is pending and context reaches `0.95` of the window, the runtime automatically reconstructs context with the compacted history on the next request; that reconstruction is when provider-context replacement becomes real for the agent. The automatic 95% event carries `reconstruction.proactive_hint`, noting that one manual rebuild-only call after the 75% hint should have reduced pressure before the emergency rebuild. If no summarize has been recorded, there is no compacted history to apply, though rebuild-only can still force a fresh replay of current history on adapters that support it. If summarize or rebuild still cannot bring context below `0.6 * context_window`, tend durable stores and molt deliberately. If you have already decided to molt, skip pre-molt summarize and molt instead. The resident rule is the operational mechanism; the rationale and edge cases live in `system-manual` → `reference/summarize-manual/SKILL.md`.
