---
name: read-manual
description: "Complete guide for the read tool: continuation workflow, next_offset pagination, line_truncated handling, runtime tool-result spill vs read-level pagination, 50k read default / 200k runtime hard cap, and when to use bash/grep/sed for truncated lines. Use when implementing complete file-read workflows, handling large files, or understanding cap and truncation semantics."
version: 0.1.0
tags: [read, files, continuation, truncation, cap, pagination]
last_changed_at: "2026-06-27T02:45:37-07:00"
---

# Read Manual

This skill documents the complete workflow for reading files with the `read` tool.
For basic tool choice (read vs write vs edit vs grep vs glob), see the `file-manual` skill.

## Cap model: 100k read default, 200k runtime hard ceiling

Before using `read`, load this manual when you are dealing with large files,
complete-content workflows, truncation, or `line_truncated` results.

There are two limits:

- `read` default page budget: **100 000 characters per call**.
- Runtime tool-result hard ceiling: **200 000 characters**. This ceiling is not
  configurable by agents or prompts; it prevents provider-visible tool results
  from exploding.

`read` accepts an optional per-call `max_chars` parameter. Use it to request a
smaller or larger chunk for that call. Values above the runtime hard ceiling are
clamped to 200 000; the actual effective value appears as `cap_chars` when the
result is truncated. Do not assume the old 10 000-character or 8 000-character
limits from earlier versions.

## Recommended metadata/stats preflight

For unknown or large files, inspect cheap metadata before reading big chunks. This
replaces a dedicated `read(dry_run=true)` mode: use local tools to learn the shape
of the file, then choose `offset`, `limit`, and `max_chars`.

```bash
# File size in bytes
python - <<'PY'
from pathlib import Path
p = Path('/path/to/file')
print(p.stat().st_size)
PY

# Total lines and longest physical line
python - <<'PY'
from pathlib import Path
p = Path('/path/to/file')
max_len = 0
max_line = 0
count = 0
with p.open('r', encoding='utf-8', errors='replace') as f:
    for i, line in enumerate(f, 1):
        count = i
        n = len(line)
        if n > max_len:
            max_len = n
            max_line = i
print({'lines': count, 'longest_line': max_line, 'longest_chars': max_len})
PY

# Preview line lengths around a target region
python - <<'PY'
from pathlib import Path
p = Path('/path/to/file')
for i, line in enumerate(p.open('r', encoding='utf-8', errors='replace'), 1):
    if 100 <= i <= 120:
        print(i, len(line))
PY
```

Use the result to decide:

- `limit` = how many lines to request.
- `max_chars` = per-call character budget (default 100k, max 200k).
- `offset` = where to begin or resume.

## Complete-content workflow

For any file that may be larger than the cap, follow this loop:

1. Call `read` with the desired `offset` (default 1) and `limit`.
2. Check the result for `truncated=true`.
   - If absent or `false`: the entire requested range was returned. Done.
   - If `true`: proceed to step 3.
3. Note `next_offset` from the result metadata.
4. Call `read` again with `offset=next_offset` (keep the same `limit`).
5. Repeat until `truncated` is absent or `false`.

Minimal Python pseudocode:

```python
offset = 1
while True:
    r = read({"file_path": path, "offset": offset, "limit": 200})
    process(r["content"])
    if not r.get("truncated"):
        break
    offset = r["next_offset"]
```

## Continuation metadata fields

When `truncated=true` the result includes:

| Field | Meaning |
|---|---|
| `truncated` | `true` — content was cut |
| `cap_chars` | effective character cap used for this call |
| `returned_chars` | characters actually returned |
| `requested_offset` | 1-based start line you passed |
| `requested_limit` | line limit you passed |
| `last_returned_line` | 1-based line number of the last line shown |
| `next_offset` | pass this as `offset` on the next call to continue |
| `remaining_lines_estimate` | approximate lines still unread |
| `line_truncated` | `true` only when a single physical line exceeded the cap |

## When to reduce offset or limit

If you need a smaller window (to avoid transport spill or to target a region):

- Pass a smaller `limit` (e.g., `limit=50`) to get fewer lines per call.
- Pass a specific `offset` to jump directly to a region (e.g., `offset=500`).
- Combining a large `offset` with a small `limit` reads an arbitrary slice.

## Runtime tool-result spill vs read-level pagination

Two separate caps apply:

1. **Read-level pagination cap**: the effective per-call budget chosen by
   `max_chars` or the 100k default. When content exceeds this cap, the result is
   returned with `truncated=true` and continuation metadata. The agent reads the
   next chunk by calling `read` again with `next_offset`.

2. **Runtime preventive hard ceiling**: the non-configurable 200k cap applied by
   `ToolExecutor` to every tool result just before it reaches the LLM wire. If
   any result still exceeds this ceiling, the full content is written to
   `<workdir>/tmp/tool-results/<…>` and a compact manifest replaces the wire
   payload. The manifest contains `status="spilled"`, `spill_path`, `artifact`,
   `preview`, and `original_char_count`.

A well-formed `read` result should normally stay under the outer hard ceiling
because `max_chars` is clamped to 200k. If you still see a spill manifest from
`read`, inspect the `spill_path` artifact for the full payload; then re-call
`read` with a smaller `limit`/`max_chars` or process the artifact via
`bash`/`grep`/Python.

## Handling line_truncated=true

`line_truncated=true` appears when a single physical line in the file is longer than the
cap. In that case:

- The result contains only a **prefix** of that line (bounded by the cap).
- `next_offset` points to the **next line**, not to a mid-line continuation.
- The hidden tail of the long line is **not recoverable** through further `read` calls.

To inspect a long line fully, use targeted local processing instead of `read`:

```bash
# Print one specific line (e.g., line 42):
sed -n '42p' /path/to/file

# Count characters on each line:
awk '{print NR, length($0)}' /path/to/file | head -20

# Search within a long line:
grep -n "pattern" /path/to/file

# Extract a byte range from a long line with Python:
python - <<'PY'
with open('/path/to/file') as f:
    for i, line in enumerate(f, 1):
        if i == 42:
            print(line[0:2000])   # first 2000 chars
            print("...")
            print(line[-500:])    # last 500 chars
            break
PY
```

## Quick checklist

Before calling `read`:

- Is the file large? Start with `limit=100` or `limit=200` to probe.
- Do you need the whole file? Use the continuation loop above.
- Does the result show `line_truncated=true`? Switch to `bash`/`grep`/`sed`/Python.
- Does the result show `status=spilled`? Read the `spill_path` artifact or reduce `limit`.
- Do you need a specific region? Pass `offset` and a tight `limit`.
