---
id: token_efficiency
title: Token efficiency state
kind: meta-guidance-section
summary: >
  Resident guidance for interpreting `_meta.agent_meta.token_efficiency` as current-session context and cache economy.
why: >
  This fragment exists because token/caching numbers are dynamic runtime scalars; agents need a stable interpretation hook without repeating the full token-efficiency procedure in each tool result.
related_files:
  - "src/lingtai/prompts/guidance/INDEX.md"
  - "src/lingtai_kernel/prompt_catalog.py"
  - "src/lingtai_kernel/meta_block.py"
  - "tests/test_prompt_catalog.py"
  - "tests/test_meta_block.py"
  - "src/lingtai_kernel/ANATOMY.md"
  - "tests/test_agent_meta_guidance.py"
maintenance: >
  When editing this file, update this related_files list and inspect the listed paths in the same change so source, runtime mirrors, tests, docs, and package metadata stay connected.
---
Read `_meta.agent_meta.token_efficiency` as current-session token economy state, not lifetime or project accounting. It is explicitly scoped by `scope: current_session` and includes `api_calls`, `input_tokens`, `cached_tokens`, `cache_rate` (cached/input as a 0-1 fraction), `avg_input_tokens_per_api_call`, `context_tokens`, `context_window`, and `guidance_ref`. Use `context_tokens` and `context_window` directly: rising context means the current session is carrying more into each provider request. Apply the token-efficiency principle from the system prompt prefix: summarize already-consumed tool results when continuing, use daemons before bulky work enters main context, and treat task-boundary molt as a costed decision. At a completed task boundary, default to proactive molt only when current-session `api_calls > 100`; below that threshold, go idle unless context pressure, explicit human request, or conversation confusion makes the molt worth its cost.
