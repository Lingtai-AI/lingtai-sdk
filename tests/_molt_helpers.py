"""Shared helpers for the molt-persistence test modules.

``VALID_SESSION_JOURNAL`` and ``write_session_journal`` were copy-pasted
verbatim across ``test_molt_task_persistence.py``,
``test_molt_notification_persistence.py`` and ``test_post_molt_notification.py``.
The molt session-journal gate requires a real journal on disk before context
shed, so every molt test needs to stage one — there is exactly one canonical
journal, so it lives here once.
"""
from __future__ import annotations


VALID_SESSION_JOURNAL = """\
---
name: 2026-06-19-molt-1-test
description: A test session journal entry for the molt gate.
date: 2026-06-19
molt_count: 1
type: session-journal
---

## What this segment was about
Testing.

## Accomplishments
Wrote a valid session journal.
"""


def write_session_journal(
    agent,
    rel: str = "knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md",
) -> str:
    """Write the canonical valid session journal under *agent*'s working dir.

    Returns the *rel* path the gate expects to be passed back into molt.
    """
    path = agent._working_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VALID_SESSION_JOURNAL, encoding="utf-8")
    return rel
