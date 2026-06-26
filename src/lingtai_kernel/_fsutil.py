"""Shared low-level filesystem / JSON / JSONL helpers for kernel state writes.

This module is dependency-light (stdlib only) on purpose: it sits *below* the
rest of the kernel so any module can import it without creating cycles.  It
centralises the small set of I/O decisions that were previously re-solved,
slightly differently, in dozens of call sites:

- crash-atomic replace via a temp file in the *same directory* + ``os.replace``
  (atomic only on the same filesystem, so the temp must be a sibling of the
  target, never ``/tmp``),
- one UTF-8 / ``ensure_ascii`` policy for model-visible JSON,
- a single ``read_json(default=...)`` exception policy,
- append-only JSONL with a returned byte offset for callers that index records.

The helpers intentionally match the *existing* dominant behaviour so callers
can migrate without changing public file formats:

- ``atomic_write_json`` writes ``json.dumps(obj, ensure_ascii=False, indent=2)``
  with **no** trailing newline (matches ``Workdir.write_manifest``).
- ``fsync`` is **opt-in** (default off) so migrating a non-fsync caller does not
  silently change durability behaviour.
- ``append_jsonl`` defaults to ``ensure_ascii=True`` to match the token ledger
  and other ASCII-escaped JSONL logs; pass ``ensure_ascii=False`` for
  UTF-8-preserving logs.

See ``docs/plans/2026-06-25-fsutil-migration.md`` for the issue #510 plan.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "read_json",
    "append_jsonl",
    "iter_jsonl_records",
    "tail_jsonl_records",
    "utc_now_iso",
]

PathLike = Union[str, "os.PathLike[str]"]

# Sentinel so ``read_json(path)`` can distinguish "no default given" (raise on
# error) from ``read_json(path, default=None)`` (return None on error).
_NO_DEFAULT = object()


def _unique_tmp(target: Path) -> Path:
    """Return a unique sibling temp path for ``target`` (same dir → atomic replace).

    The name embeds both the pid *and* a random uuid4 hex so two writers to the
    same target never share a temp path — including threads/tasks inside one
    process, which a pid-only suffix could not distinguish (the audit flagged
    pid-only/fixed temp names as a same-process collision risk: two writers
    would race on one temp file, so one could ``os.replace`` it out from under
    the other and the loser fails with ``FileNotFoundError`` or, worse, writes
    into an inode already renamed onto the target).

    A uuid4 sibling (rather than ``tempfile.mkstemp``) is used deliberately so
    the temp file is created by ``open(..., "x")`` and inherits the process
    umask, preserving the permission semantics of the plain-``open`` atomic
    writes these helpers replace. ``mkstemp`` forces mode ``0o600``, which
    ``os.replace`` would then carry onto the target, silently tightening the
    permissions of migrated state files.
    """
    return target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = False,
) -> Path:
    """Atomically write ``text`` to ``path``.

    Writes to a sibling temp file then ``os.replace``s it over the target, so a
    crash mid-write leaves either the old file or the new one, never a partial.
    The parent directory is created if missing.

    ``fsync`` is opt-in: when True the temp file's bytes are flushed to disk
    before the rename (stronger crash durability, extra I/O cost).  Leave it
    off to preserve the behaviour of callers that never fsynced.  Note this
    fsyncs the *file content* only, not the parent directory, so the rename
    metadata is not guaranteed durable across a power loss; that is stronger
    than the default and sufficient for the current opt-out callers.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(target)
    try:
        # "x" (exclusive create) guarantees we never write into a temp file
        # another writer already created; combined with the uuid4 name this
        # makes concurrent same-target writes collision-free.
        with open(tmp, "x", encoding=encoding) as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(str(tmp), str(target))
    except BaseException:
        # Best-effort cleanup so a failed write does not leave temp litter.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    ensure_ascii: bool = False,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
    default: Optional[Callable[[Any], Any]] = None,
    fsync: bool = False,
) -> Path:
    """Atomically write ``obj`` as JSON to ``path``.

    Defaults (``ensure_ascii=False``, ``indent=2``, no trailing newline) match
    the kernel's dominant model-visible JSON convention.  Serialization happens
    *before* the file is touched, so a non-serializable ``obj`` raises without
    leaving a temp file or clobbering the target.
    """
    text = json.dumps(
        obj,
        ensure_ascii=ensure_ascii,
        indent=indent,
        sort_keys=sort_keys,
        default=default,
    )
    return atomic_write_text(path, text, encoding="utf-8", fsync=fsync)


def read_json(
    path: PathLike,
    *,
    default: Any = _NO_DEFAULT,
    expect: Optional[Union[type, tuple]] = None,
) -> Any:
    """Read JSON from ``path`` with one consistent exception policy.

    Returns the parsed object.  On a missing file, unreadable file, malformed
    JSON, or (when ``expect`` is given) a wrong top-level type:

    - if ``default`` was supplied, return ``default``;
    - otherwise re-raise the underlying error (``FileNotFoundError``,
      ``json.JSONDecodeError``, ``OSError``) or a ``TypeError`` for ``expect``.

    ``expect`` is an optional type or tuple of types the top-level value must be
    an instance of (e.g. ``dict`` for a manifest, ``list`` for an array file).
    """
    target = Path(path)
    try:
        obj = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        if default is not _NO_DEFAULT:
            return default
        raise
    if expect is not None and not isinstance(obj, expect):
        if default is not _NO_DEFAULT:
            return default
        raise TypeError(
            f"{target}: expected top-level {expect}, got {type(obj).__name__}"
        )
    return obj


def append_jsonl(
    path: PathLike,
    obj: Any,
    *,
    ensure_ascii: bool = True,
    default: Optional[Callable[[Any], Any]] = None,
    fsync: bool = False,
) -> int:
    """Append ``obj`` as a single JSONL record and return its byte offset.

    The returned offset is the position of the record's first byte (``f.tell()``
    before the write), matching the token ledger's ``source_offset`` contract so
    callers can index into the file later.  The parent directory is created if
    missing.  ``ensure_ascii`` defaults to True to match existing ASCII-escaped
    ledgers; pass False for UTF-8-preserving logs.

    Concurrency: the returned offset is durable only under a single writer (or
    an externally held lock). The ``O_APPEND`` write itself is atomic, but a
    second writer can append between this call's ``tell()`` and ``write``, so
    the offset is reliable record provenance only when one writer owns the file
    (matching the existing token-ledger pattern, which serializes via an
    in-process lock at the call site).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(obj, ensure_ascii=ensure_ascii, default=default) + "\n").encode(
        "utf-8"
    )
    with open(target, "ab") as f:
        offset = f.tell()
        f.write(payload)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    return offset


def iter_jsonl_records(
    path: PathLike,
    *,
    skip_invalid: bool = True,
) -> Iterator[Any]:
    """Yield parsed records from a JSONL file in file order.

    A missing file yields nothing.  Blank lines are skipped.  When
    ``skip_invalid`` is True (default) malformed lines are skipped silently,
    matching log-recovery paths that must tolerate a torn final write; pass
    False to surface ``json.JSONDecodeError``.
    """
    target = Path(path)
    try:
        handle = open(target, "r", encoding="utf-8")
    except FileNotFoundError:
        return
    with handle as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if skip_invalid:
                    continue
                raise


def tail_jsonl_records(
    path: PathLike,
    n: int,
    *,
    skip_invalid: bool = True,
) -> list:
    """Return the last ``n`` parsed records from a JSONL file, in file order.

    Convenience reverse-tail for recovery paths that only need the most recent
    entries.  Reads the whole file (callers with very large ledgers should use a
    seek-based tail); kept simple and dependency-light here.
    """
    if n <= 0:
        return []
    records = list(iter_jsonl_records(path, skip_invalid=skip_invalid))
    return records[-n:]


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    One canonical timestamp representation (timezone-aware UTC), matching the
    dominant ``datetime.now(timezone.utc).isoformat()`` usage across the kernel.
    """
    return datetime.now(timezone.utc).isoformat()
