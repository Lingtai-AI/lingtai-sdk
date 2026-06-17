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

from .runtime import (
    EventKind,
    Runtime,
    RuntimeEvent,
    RuntimeMessage,
    RuntimeOptions,
    RuntimeSession,
    RuntimeState,
)


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


class LingTaiSession:
    """Small public wrapper around a live :class:`RuntimeSession`.

    This facade is for multi-message / streaming-ish use cases where callers
    want to keep a session open and decide when to poll events or close it. It
    owns no backend behavior; it delegates to the supplied runtime session.
    """

    def __init__(self, session: RuntimeSession) -> None:
        self._session = session
        self._closed = False

    @property
    def raw_session(self) -> RuntimeSession:
        """The underlying runtime session for advanced callers."""
        return self._session

    @property
    def state(self) -> RuntimeState:
        return self._session.state

    @property
    def working_dir(self):
        return self._session.working_dir

    def send(
        self,
        message: RuntimeMessage | str,
        *,
        sender: str = "user",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "LingTaiSession":
        """Send one message and return ``self`` for simple chaining."""

        self._session.send(
            _coerce_message(
                message, sender=sender, subject=subject, metadata=metadata
            )
        )
        return self

    def events(self) -> tuple[RuntimeEvent, ...]:
        """Return the currently available runtime events as a tuple."""

        return tuple(self._session.events())

    def text(self) -> str:
        """Drain currently available events and concatenate their TEXT chunks."""

        return _collect_text(self.events())

    def close(self, timeout: float = 5.0) -> tuple[RuntimeEvent, ...]:
        """Stop the underlying session once and return any final events."""

        if not self._closed:
            self._session.stop(timeout=timeout)
            self._closed = True
        return self.events()

    def __enter__(self) -> "LingTaiSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


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


    def open_session(
        self, options: RuntimeOptions | None = None
    ) -> LingTaiSession:
        """Start and return a live session facade.

        Unlike :meth:`query`, this keeps the runtime session open so callers can
        send multiple messages, poll events, and close explicitly.
        """

        runtime_options = options or self.options
        if runtime_options is None:
            raise ValueError(
                "LingTaiClient.open_session() requires RuntimeOptions either "
                "on the client or this call"
            )
        session = self.runtime.create_session(runtime_options)
        session.start()
        return LingTaiSession(session)

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


def open_session(
    *,
    options: RuntimeOptions,
    runtime: Runtime | None = None,
) -> LingTaiSession:
    """One-shot convenience helper that starts and returns a live session."""

    return LingTaiClient(runtime=runtime, options=options).open_session()


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


__all__ = ["LingTaiClient", "LingTaiSession", "QueryResult", "open_session", "query"]
