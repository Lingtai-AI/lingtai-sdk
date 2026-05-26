"""Pad (working notes) management — edit, load, and append-file pinning."""
from __future__ import annotations

import json
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Pad append-file management
# ---------------------------------------------------------------------------

_APPEND_LIST_PATH = "system/pad_append.json"
_APPEND_TOKEN_LIMIT = 100_000


def _append_list_file(agent) -> Path:
    return agent._working_dir / _APPEND_LIST_PATH


def _load_append_list(agent) -> list[str]:
    """Read the persisted append file list (empty list if missing)."""
    path = _append_list_file(agent)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(p) for p in data]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_append_list(agent, files: list[str]) -> None:
    """Persist the append file list to disk."""
    path = _append_list_file(agent)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(files, ensure_ascii=False))


def _resolve_path(agent, fpath: str) -> Path:
    if os.path.isabs(fpath):
        return Path(fpath)
    return agent._working_dir / fpath


def _read_append_content(agent, files: list[str]) -> tuple[str, list[str]]:
    """Read all append files. Returns (combined content, not_found list)."""
    parts: list[str] = []
    not_found: list[str] = []
    for fpath in files:
        resolved = _resolve_path(agent, fpath)
        if not resolved.is_file():
            not_found.append(fpath)
            continue
        parts.append(f"[append: {fpath}]\n{resolved.read_text(encoding='utf-8')}")
    return "\n\n".join(parts), not_found


def _is_text_file(path: Path, sample_size: int = 8192) -> bool:
    """Check if a file is a text file by reading the first chunk."""
    try:
        chunk = path.read_bytes()[:sample_size]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ---------------------------------------------------------------------------
# Pad actions
# ---------------------------------------------------------------------------


def _pad_edit(agent, args: dict) -> dict:
    """Write content + optional file imports to pad.md and reload prompt.

    To clear the pad explicitly, pass content="" (i.e. include the key).
    Calling edit with no content key and no files is rejected — that's
    almost always an LLM mistake.
    """
    if "content" not in args and not args.get("files"):
        return {"error": "Provide content (use empty string to clear), files, or both."}

    content = args.get("content", "")
    files = args.get("files") or []

    parts = [content] if content else []

    not_found: list[str] = []
    for i, fpath in enumerate(files, start=1):
        if os.path.isabs(fpath):
            resolved = Path(fpath)
        else:
            resolved = agent._working_dir / fpath
        if not resolved.is_file():
            not_found.append(fpath)
            continue
        file_content = resolved.read_text(encoding='utf-8')
        parts.append(f"[file-{i}]\n{file_content}")

    if not_found:
        return {"error": f"Files not found: {', '.join(not_found)}"}

    combined = "\n\n".join(parts)

    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    pad_path = system_dir / "pad.md"
    pad_path.write_text(combined)

    agent._log("psyche_pad_edit", length=len(combined), files=len(files))

    _pad_load(agent, {})

    return {"status": "ok", "path": str(pad_path), "size_bytes": len(combined.encode("utf-8"))}


def _pad_load(agent, args: dict) -> dict:
    """Load system/pad.md + appended reference files into the prompt."""
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    pad_path = system_dir / "pad.md"
    if not pad_path.is_file():
        pad_path.write_text("")

    content = pad_path.read_text(encoding="utf-8")
    size_bytes = len(content.encode("utf-8"))

    if content.strip():
        agent._prompt_manager.write_section("pad", content)
    else:
        agent._prompt_manager.delete_section("pad")

    # Append-files layering — pinned read-only reference appended to pad section
    append_files = _load_append_list(agent)
    append_meta: dict = {}
    if append_files:
        append_content, not_found = _read_append_content(agent, append_files)
        if append_content:
            existing = agent._prompt_manager.read_section("pad") or ""
            combined = existing + "\n\n---\n# 📎 Reference (read-only)\n\n" + append_content
            agent._prompt_manager.write_section("pad", combined)
        if not_found:
            append_meta["append_not_found"] = not_found
        append_meta["append_files"] = append_files
        append_meta["append_count"] = len(append_files)

    agent._token_decomp_dirty = True
    agent._flush_system_prompt()

    agent._log("psyche_pad_load", size_bytes=size_bytes)

    result: dict = {
        "status": "ok",
        "path": str(pad_path),
        "size_bytes": size_bytes,
        "content_preview": content[:200],
    }
    result.update(append_meta)
    return result


def _pad_append(agent, args: dict) -> dict:
    """Set the list of files pinned as read-only pad reference.

    Pass files=[] to clear. Persisted to system/pad_append.json.
    Automatically reloads pad after updating the list. Only text files
    are accepted.
    """
    files = args.get("files")
    if files is None:
        # No files param — return current list
        current = _load_append_list(agent)
        return {"status": "ok", "files": current, "count": len(current)}

    not_found: list[str] = []
    not_text: list[str] = []
    for fpath in files:
        resolved = _resolve_path(agent, fpath)
        if not resolved.is_file():
            not_found.append(fpath)
        elif not _is_text_file(resolved):
            not_text.append(fpath)
    if not_found:
        return {"error": f"Files not found: {', '.join(not_found)}"}
    if not_text:
        return {"error": f"Only text files are accepted. Binary files: {', '.join(not_text)}"}

    if files:
        from ...token_counter import count_tokens
        combined, _ = _read_append_content(agent, files)
        tokens = count_tokens(combined)
        if tokens > _APPEND_TOKEN_LIMIT:
            return {
                "error": f"Append files total {tokens:,} tokens, "
                         f"exceeding the {_APPEND_TOKEN_LIMIT:,} token limit. "
                         f"Reduce the number or size of files.",
            }

    _save_append_list(agent, files)
    _pad_load(agent, {})

    action = "cleared" if not files else "set"
    return {"status": "ok", "action": action, "files": files, "count": len(files)}
