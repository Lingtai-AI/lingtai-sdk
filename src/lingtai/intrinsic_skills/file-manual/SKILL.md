---
name: file-manual
description: "Operational guide for LingTai's built-in file tools: read, write, edit, glob, and grep. Use when working with local text files, deciding whether to use file tools versus bash, handling large files, avoiding binary/image misuse, or reading non-UTF-8 text via explicit bash/Python/iconv instead of complicating the core read tool. Covers UTF-8 policy, safe write/edit discipline, search workflows, and examples for GBK/Shift-JIS/Latin-1 conversion."
version: 0.1.0
tags: [files, read, write, edit, grep, glob, encoding, utf-8]
---

# File Manual

This skill is the working guide for LingTai's built-in file tools:

- `read` — read a text file with line numbers.
- `write` — create or fully overwrite a text file.
- `edit` — perform exact string replacement inside an existing text file.
- `glob` — find files by path pattern.
- `grep` — search file contents by regex.

Use these tools for ordinary project text: source code, Markdown, JSON/YAML/TOML, logs, prompts, skills, and notes.

Do **not** use these tools for binary/image/audio/video content. For images, use the `vision` skill/tool. For arbitrary binary inspection or transcoding, use `bash` with explicit commands.

## Encoding policy

LingTai's own text assets should be UTF-8:

- source code;
- prompts and system notes;
- skills and knowledge entries;
- JSON/YAML/TOML/Markdown config and documentation.

Do not rely on the host locale. In particular, Windows Chinese/Japanese/Korean locales may default Python text I/O to GBK/CP936/Shift-JIS-like encodings. Internal LingTai assets should not be decoded by guessing the locale; they should be read and written as UTF-8.

For external or user-provided non-UTF-8 files, keep the core `read` tool simple. Use `bash` with an explicit encoding instead.

### Read a GBK file with Python

```bash
python - <<'PY'
from pathlib import Path
print(Path('file.txt').read_text(encoding='gbk'))
PY
```

If the goal is to inspect imperfect text without crashing on bad bytes:

```bash
python - <<'PY'
from pathlib import Path
print(Path('file.txt').read_text(encoding='gbk', errors='replace'))
PY
```

For Shift-JIS or Latin-1, change the encoding:

```bash
python - <<'PY'
from pathlib import Path
print(Path('file.txt').read_text(encoding='shift_jis', errors='replace'))
print(Path('latin1.txt').read_text(encoding='latin-1'))
PY
```

### Convert a file to UTF-8

With Python:

```bash
python - <<'PY'
from pathlib import Path
src = Path('legacy-gbk.txt')
dst = Path('legacy-gbk.utf8.txt')
dst.write_text(src.read_text(encoding='gbk'), encoding='utf-8')
print(dst)
PY
```

With `iconv` when available:

```bash
iconv -f gbk -t utf-8 legacy-gbk.txt > legacy-gbk.utf8.txt
iconv -f shift_jis -t utf-8 legacy-sjis.txt > legacy-sjis.utf8.txt
```

Recommended rule: if a file will become part of the project, convert it to UTF-8 before committing or storing it as a durable LingTai asset.

## Choosing the right tool

| Need | Tool |
|---|---|
| Read a known text file | `read` |
| Read a large file section | `read` with `offset` / `limit` |
| Create a new text file or replace a whole file | `write` |
| Make a small exact change | `edit` |
| Find files by name/path | `glob` |
| Search text across files | `grep` |
| Decode non-UTF-8 text | `bash` + Python or `iconv` |
| Inspect binary format, archive, media | `bash` or a domain skill/tool |
| Analyze image content | `vision` |

## Reading files safely

Prefer `read` for known text files. It returns line numbers, which makes later edits and citations easier.

For large files, avoid loading everything at once:

```python
read({"file_path": "/abs/path/to/file.log", "offset": 1, "limit": 120})
read({"file_path": "/abs/path/to/file.log", "offset": 500, "limit": 120})
```

If a file may be generated, minified, huge, or noisy, search before reading:

```python
grep({"pattern": "class Agent|def handle", "path": "/abs/path/src", "glob": "*.py", "max_matches": 50})
```

Then read only the relevant region.

## Writing files safely

`write` is a full-file operation. Use it when:

- creating a new file;
- replacing a generated artifact;
- deliberately rewriting a small file you already understand.

Before overwriting an important existing file, read it first unless the human explicitly asked for a blind overwrite.

Avoid using `write` for tiny modifications to large files. Use `edit` instead.

## Editing files safely

`edit` replaces an exact string. It fails when the old string is absent or ambiguous, which is a feature: it prevents accidental broad changes.

Good pattern:

1. `read` the relevant lines.
2. Copy an exact old-string region with enough surrounding context to be unique.
3. Call `edit` once.
4. Re-read the changed region or run tests.

Use `replace_all=true` only when every occurrence is supposed to change and you have checked the match set with `grep` first.

## Search workflow

Start broad with `glob`, then narrow with `grep`, then inspect with `read`.

Examples:

```python
glob({"pattern": "**/*.py", "path": "/abs/path/project"})
grep({"pattern": "read_text\\(", "path": "/abs/path/project/src", "glob": "*.py", "max_matches": 100})
read({"file_path": "/abs/path/project/src/module.py", "offset": 40, "limit": 80})
```

Use `grep` for text content. Use `glob` for file names.

## File paths and privacy

Use absolute paths with file tools. Paths inside your working directory may be private to this agent. Do not send local private paths to other agents or humans unless they are useful and safe for that recipient.

When sharing file content, quote the relevant content or attach/export a file through the appropriate communication channel. Do not assume another agent can dereference your local path.

## Quick checklist

Before using file tools:

- Is this text, not binary/media?
- Is it expected to be UTF-8? If not, use bash with explicit encoding.
- Is the file large? If yes, search or read a slice.
- Am I about to overwrite? If yes, read first or confirm intent.
- Am I about to replace many occurrences? If yes, grep first.
