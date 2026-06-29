---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/i18n/__init__.py
  - src/lingtai/i18n/en.json
  - src/lingtai/i18n/wen.json
  - src/lingtai/i18n/zh.json
  - src/lingtai_kernel/i18n/ANATOMY.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# src/lingtai/i18n/

Wrapper-side i18n loader — language-aware string tables for lingtai capabilities, with kernel-level key injection.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|---|---|---|
| `__init__.py` | 66 | `t()` lookup function + kernel sync logic |

**JSON data files** alongside the module: `en.json`, `zh.json`, `wen.json`.

**Key functions:**
- `t(lang, key, **kwargs)` (L57) — main API. Returns localized string for dotted key; falls back to `en` then returns key itself. `format_map` with `defaultdict(str)` for safe template substitution.
- `_load(lang)` (L31) — lazy-loads JSON file into `_CACHE`, triggers `_sync_to_kernel()`.
- `_sync_to_kernel(lang)` (L44) — extracts keys matching `_KERNEL_PREFIXES` (`system.`, `soul.`, `mail.`, `eigen.`, `system_tool.`, `tool.`) and injects them into the kernel's i18n cache via `lingtai_kernel.i18n.register_strings()`.

## Connections

- **→ `lingtai_kernel.i18n`** (L46): `register_strings()` — pushes kernel-namespace keys from lingtai tables into kernel's cache. This is how lingtai ships `wen.json` translations for kernel-level strings.
- **← `lingtai.capabilities.vision`** (vision:21), **`lingtai.capabilities.web_search`** (web_search:14): both import `t` for i18n of tool descriptions/schemas.
- **← `lingtai.services.vision.*`**, **`lingtai.services.websearch.*`**: capabilities use `t()` for user-facing strings.

## Composition

Single module — no sub-packages, no classes. Stateless function API backed by module-level `_CACHE` dict.

## State

- `_CACHE: dict[str, dict[str, str]]` (L21) — module-global, lazily populated. Keys are language codes (`"en"`, `"zh"`, `"wen"`).

## Notes

- Kernel-sync is additive (never destructive) — existing kernel keys are overwritten only if lingtai's table has them (L49-52).
- `_KERNEL_PREFIXES` tuple (L26-28) defines which key namespaces belong to the kernel; everything else is lingtai-local.
- `defaultdict(str)` (L65) means missing template vars silently become empty strings rather than raising `KeyError`.
