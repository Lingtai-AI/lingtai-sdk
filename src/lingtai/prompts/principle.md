---
name: principle
kind: prompt-section
section: principle
summary: >
  Kernel-owned top-level progressive-disclosure contract. The single source of truth for how the resident prompt layers (meta_guidance / procedures / substrate / reference manuals) divide responsibility, plus the token-efficiency principle. Rendered raw (no `## ` header) as the first system-prompt section.
why: >
  Self-explains why this fragment leads the system prompt: it tells the agent where each canonical rule lives so layers point to one source instead of restating it. This frontmatter is developer-facing metadata only — it is stripped before the body is rendered into the LLM prompt or system.md.
related_files:
  - "src/lingtai_kernel/prompt.py"
  - "src/lingtai/agent.py"
  - "src/lingtai_kernel/base_agent/__init__.py"
  - "tests/test_prompt_catalog.py"
  - "tests/test_agent_meta_guidance.py"
  - "tests/test_deep_refresh.py"
  - "tests/test_prompt.py"
  - "pyproject.toml"
  - "MANIFEST.in"
maintenance: >
  When editing this file, update this related_files list and inspect the listed paths in the same change so source, runtime mirrors, tests, docs, and package metadata stay connected.
---
Progressive disclosure principle: each resident prompt layer has one job and points to the next.

- `meta_guidance` is immediate runtime guidance and routing hooks: tell the agent where the canonical rule lives right now; do not duplicate long procedures here.
- `procedures` is how to act: operational triggers, checklists, routing steps, reporting discipline, and concrete tool-use rules.
- `substrate` is the working model and principles: why the agent has these bodies, memory layers, lifecycle states, and communication channels.
- Reference manuals are why, boundaries, examples, and troubleshooting: load them on demand when the concise resident layer is not enough.

Keep each layer concise. A rule should have one source of truth; other layers should point to that source instead of restating it.

Token efficiency principle: the current session's active context is carried into every provider request. When continuing, summarize consumed tool results whose raw text is no longer needed. At completed task boundaries, after reporting and durable stores are tended, do not molt automatically; default to a proactive task-boundary molt only once current-session API calls exceed 100, or when context pressure, explicit human request, or conversation confusion makes the molt worth its cost. Use daemons to keep bulky or noisy work out of the main context.
