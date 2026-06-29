---
name: [SKILL_NAME]          # REQUIRED: lowercase-kebab-case, unique
description: [ONE_LINE_DESCRIPTION]  # REQUIRED: What this skill does + when NOT to use it. Shown in catalog.
version: 1.0.0              # Semantic versioning
tags: [[optional, tags]]      # Optional: search/categorization tags
# last_changed_at: "YYYY-MM-DDTHH:MM:SSZ"  # Required only for LingTai-maintained skills
---

# [SKILL NAME]

[3–5 line overview: What problem does this solve? Who is it for?]

> **Reference-class vs. code-class:**  
> If you are writing a reference skill (e.g., a manual), skip the **Procedure** section and focus on **When this applies** + **What to expect**.  
> If you are writing a code/executable skill, use all sections.

## When this applies

- Use this when: [specific trigger conditions]
- Do NOT use this when: [clear exclusion cases]

## Procedure

[For code/executable skills: numbered steps, with copy-pasteable code blocks where useful. For reference skills: delete this section entirely.]

## What to expect

[Describe the output structure or behavior]

## Constraints

- [Known constraints, failure modes, edge cases]

## Scripts

| File | Purpose |
|------|---------|
| `scripts/[name].py` | [What it does] |

## Assets

| File | Purpose |
|------|---------|
| `assets/[name].json` | [What it contains] |
