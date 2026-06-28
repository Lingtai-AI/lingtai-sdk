---
id: review_delegation_instruction_check
title: Review delegation instruction check
kind: meta-guidance-section
summary: >
  Resident guidance requiring agents to re-anchor recent human instructions before delegating reviews or implementation checks.
why: >
  This fragment exists because review daemons can amplify stale scope or authorization mistakes unless the parent frames them with the latest human contract.
related_files:
  - "src/lingtai/prompts/guidance/INDEX.md"
  - "src/lingtai_kernel/prompt_catalog.py"
  - "src/lingtai_kernel/meta_block.py"
  - "tests/test_prompt_catalog.py"
  - "tests/test_meta_block.py"
  - "src/lingtai_kernel/ANATOMY.md"
  - "src/lingtai/prompts/procedures.md"
maintenance: >
  When editing this file, update this related_files list and inspect the listed paths in the same change so source, runtime mirrors, tests, docs, and package metadata stay connected.
---
Before sending a PR, diff, or implementation to GLM, Claude, another reviewer, or any review daemon, re-check the recent human-channel instructions for missed scope, boundary, or authorization changes. Use the producer channel, not memory or a notification digest alone; if the human specified a window such as the last 30 Telegram messages, use that exact window. Then frame the reviewer with the latest contract: what changed, what is out of scope, what side effects are unauthorized, and which human instructions were checked. This is system/procedure discipline, not a personal standing rule file.
