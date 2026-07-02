---
id: summarize_best_practice
title: Summarize and molt deliberately
kind: meta-guidance-section
summary: >
  Resident guidance for when and how agents summarize consumed tool results and choose molt
  boundaries.
why: >
  This fragment exists because tool-result summarization is a high-attention runtime behavior: it
  keeps the current session efficient without losing recoverability, and it tells agents when molt
  supersedes summarize.
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
Use progressive disclosure for tool results. Raw output is for inspection; summarize a result only after you have consumed or digested it. When continuing in the same session, summarize completed tool results whose raw text no longer needs inspection, preserving key facts, evidence, paths or IDs, validation, risks, and next steps. Batch already-digested results when practical. Keep noisy or bulky work out of main context by delegating it to daemons before raw bulk lands here; use summarize for the bulk that already landed. Urgent cadence: when a long or noisy result ranks high in `_meta.agent_meta.current_tool_result_chars.top_results` (above its `threshold`), read it, keep the grain, then summarize the prior result once full inspection is no longer needed. Idle cleanup: when work quiets down, use `_meta.agent_meta.current_tool_result_chars` to find already-digested, irrelevant, or obsolete results worth summarizing. The original remains recoverable from logs by `tool_call_id`, so the summary should keep enough identifiers and evidence to recover it. Follow any adapter/provider static rules in resident `meta_guidance` in addition to these general rules. Summarize is a mini molt for consumed tool results; molt is the stronger whole-conversation boundary. If you have already decided to molt, skip pre-molt summarize and molt instead. When the current task is complete, necessary reporting and durable stores are tended, and no human reply or concrete next action remains, do not molt automatically. Default to proactive task-boundary molt only when `_meta.tool_meta.token_usage.session.api_calls > 100`; below that threshold, go idle unless context pressure is present, summarize/reconstruction cannot bring context below `0.6 * context_window`, the human explicitly asks for a reset, or conversation confusion makes the fresh briefing worth the molt cost.
