"""Session-journal gate for agent-initiated molt (issue #350).

Before an agent sheds context, it must point at the durable session-journal
entry it wrote for the just-finished segment. This module is the pure,
fail-closed validator: given the agent workdir and a candidate path, it
returns ``(ok, error, resolved_relpath)``.

The gate is intentionally simple — a structural signpost, not a semantic
grader (per the issue's non-goals). It checks, in order:

  1. Path present and non-empty.
  2. Resolves to a location INSIDE the agent workdir (no traversal,
     no absolute escape).
  3. Lives in the canonical session-journal area as a sub-entry
     ``knowledge/session-journal/<entry>/KNOWLEDGE.md`` — NOT the parent
     index ``knowledge/session-journal/KNOWLEDGE.md`` and not a scratch doc.
  4. Exists, is readable UTF-8, and is non-empty.
  5. Has valid YAML frontmatter with at least ``name`` and ``description``.
  6. Carries a standardized session-journal marker
     (``type: session-journal`` OR ``session_journal: true``).

On any failure it returns a clear, actionable recovery message and a
``None`` resolved path. Callers MUST treat a falsy ``ok`` as "do not shed
context" — the molt is refused with the message before any mutation.
"""
from __future__ import annotations

from pathlib import Path


# Canonical session-journal area, relative to the agent workdir.
_JOURNAL_ROOT = ("knowledge", "session-journal")
_ENTRY_FILENAME = "KNOWLEDGE.md"

# Standardized markers that prove a knowledge file is a session journal.
# Preferred: ``type: session-journal`` or ``session_journal: true``.
_MARKER_RECOVERY = (
    "Add a session-journal marker to the frontmatter — "
    "either `type: session-journal` or `session_journal: true`."
)

_RECOVERY_HINT = (
    "Before molting, write a session journal and pass its path as "
    "session_journal_path. Expected location: "
    "knowledge/session-journal/<entry>/KNOWLEDGE.md "
    "(a per-segment sub-entry, NOT the parent index). "
    "It must exist, be non-empty UTF-8, have valid YAML frontmatter with "
    "`name` and `description`, and identify itself as session knowledge via "
    "`type: session-journal` or `session_journal: true`."
)


def _fail(msg: str) -> tuple[bool, str, None]:
    return (False, f"{msg} {_RECOVERY_HINT}", None)


def _parse_frontmatter(text: str):
    """Return (frontmatter_dict, error). Exactly one of the two is None.

    Accepts the standard ``---\\n...\\n---`` leading block. ``yaml`` is a
    declared dependency (pyproject), so we use ``safe_load`` directly.
    """
    if not text.startswith("---"):
        return None, "missing YAML frontmatter (file must start with `---`)"
    # Split off the leading block: ---\n<body>\n---
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing YAML frontmatter (file must start with `---`)"
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, "frontmatter block is not terminated by a closing `---`"

    block = "\n".join(lines[1:end])
    try:
        import yaml

        data = yaml.safe_load(block)
    except Exception as e:  # yaml.YAMLError and anything it raises
        return None, f"invalid YAML frontmatter: {e}"
    if data is None:
        return None, "frontmatter is empty"
    if not isinstance(data, dict):
        return None, "frontmatter must be a YAML mapping"
    return data, None


def _has_session_marker(fm: dict) -> bool:
    if str(fm.get("type", "")).strip().lower() == "session-journal":
        return True
    sj = fm.get("session_journal")
    if sj is True:
        return True
    if isinstance(sj, str) and sj.strip().lower() in {"true", "yes", "1"}:
        return True
    return False


def validate_session_journal_path(
    workdir: Path, raw_path: object
) -> tuple[bool, str | None, str | None]:
    """Validate the agent-supplied session-journal path.

    ``workdir`` is the agent's working directory (``agent._working_dir``).
    ``raw_path`` is the value the agent passed as ``session_journal_path``.

    Returns ``(ok, error, resolved_relpath)``:
      - on success: ``(True, None, "<relative/path/to/KNOWLEDGE.md>")``
      - on failure: ``(False, "<actionable message>", None)``

    The returned path on success is normalized to a forward-slash relative
    path under the workdir, suitable for recording in molt metadata.
    """
    workdir = Path(workdir)

    # 1. Present and non-empty.
    if raw_path is None or not isinstance(raw_path, str) or not raw_path.strip():
        return _fail("session_journal_path is required for agent-initiated molt.")
    candidate = raw_path.strip()

    # 2. Resolve INSIDE the workdir. Reject traversal / absolute escape.
    try:
        wd_resolved = workdir.resolve()
        joined = (workdir / candidate) if not Path(candidate).is_absolute() else Path(candidate)
        resolved = joined.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        return _fail(f"session_journal_path could not be resolved ({e}).")

    try:
        rel = resolved.relative_to(wd_resolved)
    except ValueError:
        return _fail(
            "session_journal_path must resolve to a location inside the agent "
            "workdir (no path traversal, no absolute escape)."
        )

    rel_parts = rel.parts

    # 3. Canonical session-journal area, sub-entry KNOWLEDGE.md.
    if rel_parts[: len(_JOURNAL_ROOT)] != _JOURNAL_ROOT:
        return _fail(
            "session_journal_path must live under "
            "knowledge/session-journal/ (not a scratch note or another "
            "knowledge area)."
        )
    if rel.name != _ENTRY_FILENAME:
        return _fail(
            f"session_journal_path must target a {_ENTRY_FILENAME} file."
        )
    # Must be a sub-entry: knowledge/session-journal/<entry>/KNOWLEDGE.md
    # (exactly 4 parts). The parent index knowledge/session-journal/KNOWLEDGE.md
    # (3 parts) is routing-only and rejected.
    if len(rel_parts) == len(_JOURNAL_ROOT) + 1:
        return _fail(
            "session_journal_path points at the parent index "
            "knowledge/session-journal/KNOWLEDGE.md, which is routing-only. "
            "Pass the per-segment sub-entry "
            "knowledge/session-journal/<entry>/KNOWLEDGE.md instead."
        )
    if len(rel_parts) != len(_JOURNAL_ROOT) + 2:
        return _fail(
            "session_journal_path must be a per-segment sub-entry "
            "knowledge/session-journal/<entry>/KNOWLEDGE.md."
        )

    # 4. Exists, readable UTF-8, non-empty.
    if not resolved.is_file():
        return _fail("session_journal_path does not exist (write the journal first).")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _fail("session_journal_path is not valid UTF-8 text.")
    except OSError as e:
        return _fail(f"session_journal_path could not be read ({e}).")
    if not text.strip():
        return _fail("session_journal_path is empty.")

    # 5. Valid YAML frontmatter with name + description.
    fm, fm_err = _parse_frontmatter(text)
    if fm_err is not None:
        return _fail(f"session_journal_path has {fm_err}.")
    missing = [k for k in ("name", "description") if not str(fm.get(k, "")).strip()]
    if missing:
        return _fail(
            "session_journal_path frontmatter is missing required field(s): "
            f"{', '.join(missing)}."
        )

    # 6. Standardized session-journal marker.
    if not _has_session_marker(fm):
        return _fail(
            "session_journal_path does not identify itself as session knowledge. "
            + _MARKER_RECOVERY
        )

    return (True, None, rel.as_posix())
