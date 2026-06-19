"""Shared pytest fixtures for LingTai kernel tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_notification_dismiss_guards():
    """Keep generic notification-dismiss guard registration test-local."""

    from lingtai_kernel.notifications import _GENERIC_DISMISS_GUARDED

    snapshot = dict(_GENERIC_DISMISS_GUARDED)
    yield
    _GENERIC_DISMISS_GUARDED.clear()
    _GENERIC_DISMISS_GUARDED.update(snapshot)
