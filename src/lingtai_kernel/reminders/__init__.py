"""Runtime reminder abstractions.

Currently home to the single molt/context-pressure reminder
(:class:`ContextPressureReminder`).  This is intentionally *not* a global
reminder registry — it is one owned abstraction for one reminder.  See
``context_pressure.py`` and this package's ``ANATOMY.md``.
"""
from __future__ import annotations

from .context_pressure import (
    CURRENT_MOLT_EVENT,
    CURRENT_MOLT_TARGET_PATH,
    RECONSTRUCTION_MOLT_EVENT,
    RECONSTRUCTION_MOLT_TARGET_PATH,
    ContextPressureReminder,
    current_molt_emission_descriptor,
    reconstruction_molt_emission_descriptor,
    reminder_message_hash,
    render_current_molt_context,
    render_reconstruction_molt,
)

__all__ = [
    "ContextPressureReminder",
    "CURRENT_MOLT_EVENT",
    "CURRENT_MOLT_TARGET_PATH",
    "RECONSTRUCTION_MOLT_EVENT",
    "RECONSTRUCTION_MOLT_TARGET_PATH",
    "current_molt_emission_descriptor",
    "reconstruction_molt_emission_descriptor",
    "reminder_message_hash",
    "render_current_molt_context",
    "render_reconstruction_molt",
]
