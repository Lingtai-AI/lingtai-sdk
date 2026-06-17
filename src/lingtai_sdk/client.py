"""Thin public client facade over the runtime contract.

This module is intentionally small and runtime-agnostic. It does not implement a
new backend and it does not import the wrapper ``lingtai`` package. Instead it
wraps the stage-0 :mod:`lingtai_sdk.runtime` contract with a convenient
``LingTaiClient.query(...)`` call that works with any supplied
:class:`~lingtai_sdk.runtime.Runtime`.

If no runtime is supplied, the default native runtime is imported lazily at
client construction time. Even then the wrapper ``Agent`` is not imported until a
native session is started.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .runtime import EventKind, Runtime, RuntimeEvent, RuntimeMessage, RuntimeOptions


@dataclass(frozen=True)
class QueryResult:
    """Result returned by :meth:`LingTaiClient.query`.

    ``text`` is the concatenation of text events emitted during the immediate
    runtime interaction. ``events`` preserves the full event snapshot so callers
    can inspect state transitions, tool events, usage, or backend-specific data.
    """

    text: str
    events: tuple[RuntimeEvent, ...]


def _default_runtime() -> Runtime:
    """Build the default native runtime lazily."""

    from .native import NativeRuntime

    return NativeRuntime()


def _coerce_message(
    message: RuntimeMessage | str,
    *,
    sender: str,
    subject: str,
    metadata: dict[str, Any] | None,
) -> RuntimeMessage:
    if isinstance(message, RuntimeMessage):
        return message
    return RuntimeMessage(
        content=message,
        sender=sender,
        subject=subject,
        metadata=dict(metadata or {}),
    )


def _collect_text(events: Iterable[RuntimeEvent]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.kind is EventKind.TEXT:
            value = event.data.get("text", "")
            if value:
                chunks.append(str(value))
    return "".join(chunks)


class LingTaiClient:
    """Convenience facade for running one message through a runtime.

    The client owns no kernel behavior. It creates a runtime session, starts it,
    sends one :class:`RuntimeMessage`, drains the immediately available events,
    and stops the session by default. Tests and embedding hosts can inject any
    :class:`Runtime`; absent injection, the native runtime is imported lazily.
    """

    def __init__(
        self,
        *,
        runtime: Runtime | None = None,
        options: RuntimeOptions | None = None,
    ) -> None:
        self.runtime = runtime if runtime is not None else _default_runtime()
        self.options = options

    def query(
        self,
        message: RuntimeMessage | str,
        *,
        options: RuntimeOptions | None = None,
        sender: str = "user",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
        stop: bool = True,
    ) -> QueryResult:
        """Send one message through a fresh runtime session.

        ``options`` may be supplied per call or stored on the client. A missing
        options object is a caller error because the runtime contract requires at
        least a working directory.
        """

        runtime_options = options or self.options
        if runtime_options is None:
            raise ValueError(
                "LingTaiClient.query() requires RuntimeOptions either on the "
                "client or this call"
            )

        session = self.runtime.create_session(runtime_options)
        events: list[RuntimeEvent] = []
        started = False
        try:
            session.start()
            started = True
            session.send(
                _coerce_message(
                    message, sender=sender, subject=subject, metadata=metadata
                )
            )
            events.extend(session.events())
        finally:
            if stop and started:
                session.stop()
                events.extend(session.events())

        return QueryResult(text=_collect_text(events), events=tuple(events))


def query(
    message: RuntimeMessage | str,
    *,
    options: RuntimeOptions,
    runtime: Runtime | None = None,
    sender: str = "user",
    subject: str = "",
    metadata: dict[str, Any] | None = None,
    stop: bool = True,
) -> QueryResult:
    """One-shot convenience wrapper around :class:`LingTaiClient`."""

    return LingTaiClient(runtime=runtime, options=options).query(
        message,
        sender=sender,
        subject=subject,
        metadata=metadata,
        stop=stop,
    )


__all__ = ["LingTaiClient", "QueryResult", "query"]
