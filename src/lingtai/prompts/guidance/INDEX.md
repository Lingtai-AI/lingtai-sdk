---
kind: meta-guidance-catalog
schema_version: 1
guidance_version: 0.7.0
priority: high
render_mode: latest_tool_result_only
summary: >
  Index for the resident `meta_guidance` runtime-guidance catalog. Carries the top-level guidance
  payload fields (schema_version, guidance_version, priority, render_mode) that used to live at
  the root of guidance.json. Each sibling `<id>.md` is one guidance section; the code-owned
  `GUIDANCE_SECTION_ORDER` controls order, and the kernel assembles them (plus the generated
  `_meta` readme and the active adapter's static rules) into the resident `meta_guidance`
  system-prompt section.
why: >
  guidance.json became a skill-style Markdown catalog so every guidance rule is a self-explaining
  frontmatter+Markdown file, like the prompt sections and skills. This frontmatter is
  developer-facing metadata; it never renders into the LLM prompt. The derived
  `system/guidance.json` is still emitted for TUI/Portal consumers.
related_files:
  - "src/lingtai/prompts/principle.md"
  - "src/lingtai/prompts/guidance/summarize_best_practice.md"
  - "src/lingtai/prompts/guidance/summarize_reconstruction_threshold.md"
  - "src/lingtai/prompts/guidance/token_efficiency.md"
  - "src/lingtai/prompts/guidance/review_delegation_instruction_check.md"
  - "src/lingtai/prompts/guidance/notification_handling.md"
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
