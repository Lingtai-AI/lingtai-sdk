"""Shared mock LLM-service factories for tests.

Across the suite, dozens of test modules each defined an identical
``make_mock_service()`` (or ``_make_mock_service()``) returning a ``MagicMock``
service.  Two shapes dominated, so they live here as named factories and the
modules import whichever they need — no behaviour change, just one definition
instead of ~20 copies.

Modules with a genuinely different service stub (e.g. a ``create_session``
side-effect factory for the molt tests, a ``_key_resolver`` for vision, or the
Codex ``provider="p"`` shim) keep their own local definition on purpose.
"""
from __future__ import annotations

from unittest.mock import MagicMock


def make_gemini_mock_service() -> MagicMock:
    """The common adapter-backed stub: a Gemini provider/model with an adapter.

    This is the shape ~19 modules copied verbatim.
    """
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def make_tool_result_mock_service() -> MagicMock:
    """The messaging/heartbeat stub: a model plus a canned ``make_tool_result``.

    This is the shape the base-agent / heartbeat / watchdog / logging tests copied.
    """
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc
