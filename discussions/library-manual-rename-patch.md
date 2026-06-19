# Patch — Rename `library-manual` → `skill-manual`

**Date:** 2026-05-01
**Status:** Awaiting human review and application
**Author:** Claude (Opus 4.7), at user's direction
**Files touched (kernel):**
- `src/lingtai/core/library/manual/SKILL.md` (frontmatter `name` field)
- `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md` (one prose mention)
- `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/reference/changelog.md` (one prose mention)
- `src/lingtai/i18n/en.json` — `library.description`, `library.action_info`
- `src/lingtai/i18n/zh.json` — `library.description`, `library.action_info`
- `src/lingtai/i18n/wen.json` — `library.description`, `library.action_info`
- `tests/test_library.py` (5 occurrences)

**Files touched (lingtai):** Already applied separately — `tui/internal/preset/procedures/procedures.md` (3 mentions). See companion section at the end.

**Apply order:** Apply this rename **before** `library-manual-improvements-patch.md` if both are pending — the improvements patch refers to the manual by its old name throughout its prose, but the actual on-disk content it edits doesn't depend on the name. Easier path: apply rename → apply improvements verbatim (the prose in the patch md being slightly stale doesn't break anything; only the actual *insertions* into SKILL.md matter).

## Why

The skill is named after the *capability* that ships it (`library-manual` = "manual for the library capability"), but its actual content is **how to author skills** — frontmatter schema, structural patterns, validation, publishing flow. The capability is the host; the skill authoring is the topic. "skill-manual" describes what the manual *is*, not where it lives.

This is a pure rename. No content moves, no directory changes, no loader changes — only the displayed name in the catalog and references.

### What is NOT changing

- The source directory `src/lingtai/core/library/manual/` stays as-is. It lives under `library/` because it's bundled by the `library` capability; renaming the directory would break symmetry with other capabilities (`<cap>/manual/`).
- The installed path `.library/intrinsic/capabilities/library/SKILL.md` stays as-is. The kernel locates the manual via the capability tree, not by skill name (`src/lingtai/core/library/__init__.py:264`).
- The bundled-manual installer logic, the catalog builder, and the health-check stay untouched.

## Change shape

Eight files, ~12 string substitutions. Every occurrence of the literal `library-manual` in the listed files is replaced with `skill-manual`. The replacements are listed below per file.

---

## `src/lingtai/core/library/manual/SKILL.md`

**Line 2:**

**Old:**
```yaml
name: library-manual
```

**New:**
```yaml
name: skill-manual
```

(That is the only `library-manual` occurrence in this file. Other prose mentions describing "the library capability" remain — they refer to the capability, not the manual's name.)

---

## `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`

**Line 69:**

**Old:**
```
  `mcp-manual`, `library-manual`) are how-to guides — operational steps,
```

**New:**
```
  `mcp-manual`, `skill-manual`) are how-to guides — operational steps,
```

---

## `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/reference/changelog.md`

**Line 341:**

**Old:**
```
See the `library-manual` capability manual for the full workflow.
```

**New:**
```
See the `skill-manual` capability manual for the full workflow.
```

(Optional editorial note: the phrase "capability manual" is now slightly misleading since the new name no longer encodes the capability. You may prefer `See the` `skill-manual` `skill for the full workflow.` — but a literal substitution preserves the patch's mechanical nature.)

---

## `src/lingtai/i18n/en.json`

**Line 58:**

**Old:**
```json
"library.description": "Your per-agent skill library. The <available_skills> catalog in your system prompt lists every skill reachable right now. Call info to read the full library workflow (authoring, publishing, loading via pad.append) and verify your library is healthy. See also the library-manual skill in .library/intrinsic/.",
```

**New:**
```json
"library.description": "Your per-agent skill library. The <available_skills> catalog in your system prompt lists every skill reachable right now. Call info to read the full library workflow (authoring, publishing, loading via pad.append) and verify your library is healthy. See also the skill-manual skill in .library/intrinsic/.",
```

**Line 59:**

**Old:**
```json
"library.action_info": "info: return the library-manual skill body plus a runtime health snapshot (catalog size, resolved paths, problems).",
```

**New:**
```json
"library.action_info": "info: return the skill-manual skill body plus a runtime health snapshot (catalog size, resolved paths, problems).",
```

---

## `src/lingtai/i18n/zh.json`

**Line 58:**

**Old:**
```json
"library.description": "你的器灵专属技能库。系统提示词中的 <available_skills> 目录列出了当前所有可用的技能。调用 info 可阅读完整的技能库工作流（编写、发布、通过 pad.append 加载），并验证技能库的健康状态。也可直接查阅 .library/intrinsic/ 中的 library-manual 技能。",
```

**New:**
```json
"library.description": "你的器灵专属技能库。系统提示词中的 <available_skills> 目录列出了当前所有可用的技能。调用 info 可阅读完整的技能库工作流（编写、发布、通过 pad.append 加载），并验证技能库的健康状态。也可直接查阅 .library/intrinsic/ 中的 skill-manual 技能。",
```

**Line 59:**

**Old:**
```json
"library.action_info": "info：返回 library-manual 技能正文，以及运行时健康快照（目录规模、解析后的路径、问题列表）。",
```

**New:**
```json
"library.action_info": "info：返回 skill-manual 技能正文，以及运行时健康快照（目录规模、解析后的路径、问题列表）。",
```

---

## `src/lingtai/i18n/wen.json`

**Line 58:**

**Old:**
```json
"library.description": "尔之器灵藏典。系统之中 <available_skills> 所列，皆此时可取之技。呼 info 以得 library-manual 全文（藏用、出新、以 pad.append 载入），兼验藏典康否。详参 .library/intrinsic/ 中 library-manual 一技。",
```

**New:**
```json
"library.description": "尔之器灵藏典。系统之中 <available_skills> 所列，皆此时可取之技。呼 info 以得 skill-manual 全文（藏用、出新、以 pad.append 载入），兼验藏典康否。详参 .library/intrinsic/ 中 skill-manual 一技。",
```

**Line 59:**

**Old:**
```json
"library.action_info": "info：还 library-manual 之文，并录当时之康状（技数、解径、患记）。",
```

**New:**
```json
"library.action_info": "info：还 skill-manual 之文，并录当时之康状（技数、解径、患记）。",
```

---

## `tests/test_library.py`

Five occurrences. Each is a literal `library-manual` → `skill-manual`:

**Line 70:**
```python
assert "name: library-manual" in skill_md.read_text()
```
→
```python
assert "name: skill-manual" in skill_md.read_text()
```

**Line 83:**
```python
stale.write_text("---\nname: library-manual\ndescription: STALE\n---\n")
```
→
```python
stale.write_text("---\nname: skill-manual\ndescription: STALE\n---\n")
```

**Line 99:**
```python
assert "The Library Capability" in body or "library-manual" in body
```
→
```python
assert "The Library Capability" in body or "skill-manual" in body
```

**Line 138:**
```python
assert result["catalog_size"] >= 2  # library-manual + shared-skill
```
→
```python
assert result["catalog_size"] >= 2  # skill-manual + shared-skill
```

**Line 194:**
```python
assert "name: library-manual" in result["library_manual"]
```
→
```python
assert "name: skill-manual" in result["library_manual"]
```

**Line 262:**
```python
assert "library-manual" in prompt
```
→
```python
assert "skill-manual" in prompt
```

(That's six occurrences in the file, listed above — the grep showed 5 unique lines but line 262 was the sixth. All are literal substitutions.)

---

## Verification

After applying, run:

```bash
cd ~/Documents/GitHub/lingtai-kernel

# 1. No stale references remain
grep -rn 'library-manual' src/ tests/ 2>/dev/null
# Expected output: empty (no matches)

# 2. New name is everywhere it should be
grep -rn 'skill-manual' src/ tests/ 2>/dev/null | wc -l
# Expected: 13 (1 frontmatter + 1 anatomy SKILL + 1 changelog + 6 i18n + 5 test usages — adjust if line 262 also counted)

# 3. Library tests still pass
~/.lingtai-tui/runtime/venv/bin/python -m pytest tests/test_library.py -x
# Expected: all pass

# 4. Smoke import (catches incidental breakage)
~/.lingtai-tui/runtime/venv/bin/python -c "from lingtai.core.library import library_info; print('ok')"
```

---

## Companion change in lingtai repo (already applied)

`tui/internal/preset/procedures/procedures.md` had three `library-manual` mentions (lines 7, 16, 84). These are renamed in the same commit that lands this kernel patch — they are documentation-only and live in the lingtai repo, not the kernel, so they don't depend on the kernel-fix workflow.
