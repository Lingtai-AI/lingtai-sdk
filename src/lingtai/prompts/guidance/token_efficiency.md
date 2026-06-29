---
id: token_efficiency
title: Token efficiency state
kind: meta-guidance-section
summary: >
  Resident guidance for interpreting the unified `_meta.tool_meta.token_usage` block as both
  per-provider-round and current-session token and cache economy.
why: >
  This fragment exists because token/caching numbers are dynamic runtime scalars; agents need a
  stable interpretation hook without repeating the full token-efficiency procedure in each tool
  result.
related_files:
  - "src/lingtai/prompts/principle.md"
  - "src/lingtai/prompts/guidance/INDEX.md"
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
Read `_meta.tool_meta.token_usage` as the single home for all token diagnostics — there is no `token_efficiency` block on `agent_meta` or `tool_meta`. It is one flat dict with two halves. The provider-round half (that tool result's own request) carries `input`, `cache_miss`, `cache_rate` (cached/input as a 0-1 fraction), `context_usage`, `window`, `output`, and `thinking`. The current-session aggregate half carries `session_cache_rate` (cached/input as a 0-1 fraction), `api_calls`, `input_tokens`, `cached_tokens`, and `avg_input_tokens_per_api_call`. These session-stat fields are CURRENT runtime-session deltas — counted since this process started or last refreshed/restored — NOT restored lifetime/cumulative totals, so they stay small and meaningful across a restart. The block also carries a short `ref` field whose value is the sentence `See meta_guidance.token_efficiency for details.`, a short hook back to this guidance subsection. A half appears only when its data is available, and missing values are omitted rather than invented. Because the block is permanent on every tool result, you can scan it across history to see repeated high-context summarize/reconstruction costs after the latest state has moved on; rising `input`/`context_usage` means the current session is carrying more into each provider request. Apply the token-efficiency principle from the system prompt prefix: summarize already-consumed tool results when continuing, use daemons before bulky work enters main context, and treat task-boundary molt as a costed decision. At a completed task boundary, default to proactive molt only when current-session `_meta.tool_meta.token_usage.api_calls > 100`; below that threshold, go idle unless context pressure, explicit human request, or conversation confusion makes the molt worth its cost.
