# Patch — Pad as Living Index (progressive disclosure)

**Date:** 2026-05-01
**Status:** Awaiting human review and application
**Author:** Claude (Opus 4.7), at user's direction
**Files touched:** `src/lingtai_kernel/i18n/{en,zh,wen}.json` — keys `psyche.description`, `psyche.object_description`, `psyche.content_description`

## Why

Pad's current voice in the tool description casts it as a "sketchboard" — a free-form scratch surface. The user wants pad to become a **living index** of what the agent is actively working on, with self-references pointing at where the substance lives (codex IDs, library SKILL.md paths, email message IDs, file paths in workdir, URLs). The purpose is **progressive disclosure for the agent's future self**: pad stays shallow and direct; the things it references are deep and structured.

The full *behavioral teaching* (what belongs in pad, what doesn't, when to update, how to archive completed pads) is going into `tui/internal/preset/procedures/procedures.md` in the lingtai repo. The kernel-shipped tool description should stay **lean and mechanical** — what the tool does, how to call it — and defer to procedures.md for the practice.

## Change shape

For each of the three locales, three keys are touched:

1. **`psyche.description`** — pad section becomes mechanic-only with a pointer to procedures.md.
2. **`psyche.object_description`** — pad line shortened to "working notes ... see procedures.md".
3. **`psyche.content_description`** — pad-edit clause shortened to "see procedures.md for what belongs in pad".

The other parts of these three keys (lingtai, context, name, summary, files, etc.) are **unchanged**.

---

## en.json — proposed values

### `psyche.description` — pad section only

The pad paragraph (currently the third paragraph in the description, after `lingtai:` and before `context:`) is replaced.

**Old text (the pad paragraph, between the lingtai RULE and the context paragraph):**

```
pad: your sketchboard — a free-form workspace that lives in your system prompt (system/pad.md). Use it liberally. Jot down plans, track tasks, sketch ideas, dump reference material, leave notes to yourself — anything that helps you think. RULE: edit pad once per task, as the last action before you go idle — accumulate updates in your head during the task, then commit them in a single edit at the end. Do not edit pad mid-task. edit to write (overwrites previous content; auto-loads into prompt). You can optionally import files via the files param — each is appended with [file-1], [file-2] dividers. append to pin text files as persistent read-only reference — pinned files are re-read on every load (including after molt), surviving context resets without being baked into pad.md. Pass files=[] to clear pins. load to re-inject pad into your prompt.
```

**New text:**

```
pad: your working notes — a free-form workspace in your system prompt (system/pad.md). edit to write (overwrites previous content; auto-loads into prompt). You can optionally import files via the files param — each is appended with [file-1], [file-2] dividers. append to pin text files as persistent read-only reference — pinned files are re-read on every load (including after molt), surviving context resets without being baked into pad.md. Pass files=[] to clear pins. load to re-inject pad into your prompt. **See procedures.md for how to use pad well — it is a living index, not a sketchpad.**
```

### `psyche.object_description` — pad line only

**Old:**
```
pad: your sketchboard — a free-form workspace in your system prompt (system/pad.md). Use it liberally.
```

**New:**
```
pad: your working notes — a free-form workspace in your system prompt (system/pad.md). See procedures.md for the practice.
```

### `psyche.content_description` — pad edit clause only

**Old:**
```
For pad edit: written as-is to pad.md. Don't be precious — rewrite freely, as often as you need.
```

**New:**
```
For pad edit: written as-is to pad.md. See procedures.md for what belongs in pad.
```

---

## zh.json — proposed values

### `psyche.description` — pad section only

**Old:**

```
pad：你的草稿板——一个自由形式的工作空间，直接嵌在你的系统提示中（system/pad.md）。随便用。记计划、追踪任务、勾画想法、存参考材料、给自己留便条——怎么有用怎么来。规则：草稿板每项任务只编辑一次，作为进入空闲前的最后一步——任务过程中在脑子里累积要写的内容，临近空闲时一次性写入。不要在任务中途编辑 pad。edit 写入（覆盖之前的内容，自动载入提示）。可选通过 files 参数导入文件——每个文件以 [file-1]、[file-2] 分隔符附加。append 将文本文件钉为持久只读参考——钉住的文件在每次 load（包括凝蜕后）时重新读取，不写入 pad.md。传 files=[] 清除钉住文件。load 将手记重新注入提示。
```

**New:**

```
pad：你的工作手记——一个自由形式的工作空间，嵌在你的系统提示中（system/pad.md）。edit 写入（覆盖之前的内容，自动载入提示）。可选通过 files 参数导入文件——每个文件以 [file-1]、[file-2] 分隔符附加。append 将文本文件钉为持久只读参考——钉住的文件在每次 load（包括凝蜕后）时重新读取，不写入 pad.md。传 files=[] 清除钉住文件。load 将手记重新注入提示。**详见 procedures.md ——pad 是活索引，非草稿板。**
```

### `psyche.object_description` — pad line only

**Old:**
```
pad：你的草稿板——自由形式的工作空间，嵌在系统提示中（system/pad.md）。随便用。
```

**New:**
```
pad：你的工作手记——自由形式的工作空间，嵌在系统提示中（system/pad.md）。详见 procedures.md。
```

### `psyche.content_description` — pad edit clause only

**Old:**
```
用于 pad edit：直接写入 pad.md。别太珍惜——想改就改，随时重写。
```

**New:**
```
用于 pad edit：直接写入 pad.md。pad 中应写什么、不应写什么，详见 procedures.md。
```

---

## wen.json — proposed values

### `psyche.description` — pad section only

**Old:**

```
pad：汝之草案板——自由之工作台，居于系统提示之中（system/pad.md）。随意用之。记谋划、追诸务、勾画意、存参考、留便条于己——凡有益于思者皆可书。律：草案板每务只修一次，临入息之前为之——务中于心累其意，临入息之时一并书之。勿于务中重修草案板。edit 写入（覆前文，自载入提示）。可选以 files 参数导入文卷——每卷以 [file-1]、[file-2] 分隔符附加。append 钉文本文卷为持久只读参考——钉住之文卷于每次 load（含凝蜕后）重读，不写入 pad.md。传 files=[] 以清钉。load 将简重载入提示。
```

**New:**

```
pad：汝之手记——自由之工作台，居于系统提示之中（system/pad.md）。edit 写入（覆前文，自载入提示）。可选以 files 参数导入文卷——每卷以 [file-1]、[file-2] 分隔符附加。append 钉文本文卷为持久只读参考——钉住之文卷于每次 load（含凝蜕后）重读，不写入 pad.md。传 files=[] 以清钉。load 将简重载入提示。**详见 procedures.md ——pad 乃活索引，非草案板也。**
```

### `psyche.object_description` — pad line only

**Old:**
```
pad：汝之草案板——自由之工作台，居于系统提示之中（system/pad.md）。随意用之。
```

**New:**
```
pad：汝之手记——自由之工作台，居于系统提示之中（system/pad.md）。详见 procedures.md。
```

### `psyche.content_description` — pad edit clause only

**Old:**
```
用于 pad edit：直书入 pad.md。勿太珍——可改即改，可重则重。
```

**New:**
```
用于 pad edit：直书入 pad.md。pad 中宜书何、不宜书何，详见 procedures.md。
```

---

## Verification checklist

Before applying:

1. The three keys above (`psyche.description`, `psyche.object_description`, `psyche.content_description`) exist in all three locale files.
2. The replaced sections are exactly the pad-related portions; the surrounding `lingtai:`, `context:`, `name:` content is untouched.
3. JSON validity preserved: only the string values change; quoting and escapes unchanged.

After applying, a quick smoke test:

```bash
~/.lingtai-tui/runtime/venv/bin/python -c "
from lingtai_kernel.i18n import t
for lang in ('en', 'zh', 'wen'):
    desc = t(lang, 'psyche.description')
    assert 'procedures.md' in desc, f'{lang}: pointer to procedures.md missing'
    assert 'sketchboard' not in desc.lower() and '草稿板' not in desc and '草案板' not in desc, f'{lang}: old vocabulary still present'
    print(f'{lang}: ok')
"
```

(The wen.json `草案板` was the old word; new wen text uses `手记` and `活索引`.)

## Companion change in lingtai repo

The behavioral teaching (purpose, what belongs/doesn't belong, when to update, archiving completed pads) lives in `tui/internal/preset/procedures/procedures.md` — applied separately in the lingtai repo by Claude. The kernel patch and the procedures.md edit are co-dependent: the tool description points at procedures.md, so procedures.md must contain the new "Tending the Pad" section before agents see the new tool description.
