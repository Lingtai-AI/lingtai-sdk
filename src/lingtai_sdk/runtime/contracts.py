"""Runtime contract seed.

Provider-agnostic shapes describing how a *future* live runtime is driven:
options in, messages in, a stream of events out. This PR ships the contract
only — there is no live runtime here. A thin ``NativeRuntime`` (wrapping the
existing ``Agent``) and any non-native backend (e.g. an Anthropic backend) land
in later PRs, once these shapes have stabilized. Keeping the contract as pure
dataclasses/ABCs with no kernel import means ``import lingtai_sdk.runtime`` is
free of provider deps and safe in tooling.

See ``docs/sdk/architecture-foundation.md`` for the staged roadmap.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


class RuntimeState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    IDLE = "idle"
    ASLEEP = "asleep"
    STUCK = "stuck"
    SUSPENDED = "suspended"
    STOPPED = "stopped"


class EventKind(str, enum.Enum):
    STATE = "state"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    NOTIFICATION = "notification"
    ERROR = "error"
    RAW = "raw"


@dataclass
class RuntimeOptions:
    """Declarative inputs for constructing a runtime session.

    A backend-neutral superset of what ``Agent`` / ``init.json`` consume today.
    A future ``NativeRuntime`` translates these into a kernel ``Agent``; other
    backends translate them into their own client config.

    Mapping-typed fields are backed by ordinary ``dict`` default factories for
    ergonomic construction in this seed contract. Runtime adapters should copy
    or freeze them if they need immutability.
    """

    working_dir: str | Path
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    capabilities: list[str] | Mapping[str, dict] | None = None
    addons: list[str] | None = None
    system_prompt_overrides: Mapping[str, str] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)
    streaming: bool = False

    def for_adapter(self, adapter_id: str) -> Mapping[str, Any]:
        """Adapter-scoped extras, e.g. ``extra['adapters']['anthropic']``."""
        adapters = self.extra.get("adapters", {}) if self.extra else {}
        return adapters.get(adapter_id, {})


@dataclass
class RuntimeMessage:
    """An inbound message handed to a running session."""

    content: str | Mapping[str, Any]
    sender: str = "user"
    subject: str = ""
    id: str = field(default_factory=lambda: f"rtmsg_{uuid4().hex[:12]}")
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeEvent:
    """An outbound event emitted by a running session."""

    kind: EventKind
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    id: str = field(default_factory=lambda: f"rtevt_{uuid4().hex[:12]}")

    @classmethod
    def state(cls, state: RuntimeState, *, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.STATE, {"state": state.value}, source=source)

    @classmethod
    def text(cls, text: str, *, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.TEXT, {"text": text}, source=source)

    @classmethod
    def error(
        cls, error: str, *, fatal: bool = False, source: str = ""
    ) -> "RuntimeEvent":
        return cls(EventKind.ERROR, {"error": error, "fatal": fatal}, source=source)

    @classmethod
    def tool_call(
        cls, name: str, args: Mapping[str, Any], *, source: str = ""
    ) -> "RuntimeEvent":
        return cls(
            EventKind.TOOL_CALL, {"name": name, "args": dict(args)}, source=source
        )

    @classmethod
    def tool_result(
        cls, name: str, result: Any, *, source: str = ""
    ) -> "RuntimeEvent":
        return cls(
            EventKind.TOOL_RESULT, {"name": name, "result": result}, source=source
        )

    @classmethod
    def usage(cls, usage: Mapping[str, Any], *, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.USAGE, dict(usage), source=source)


class RuntimeSession(ABC):
    """A single live agent session: send messages in, iterate events out.

    **Event semantics (the ``events()`` contract).** ``events()`` returns a
    *non-draining, re-iterable cumulative snapshot*: every call yields **all**
    events the session has emitted so far, in order, and reading them does not
    consume or clear them. Two back-to-back calls with no intervening activity
    therefore return equal sequences. A backend MUST NOT drain on read — callers
    that want an incremental "only what's new" view track their own cursor over
    the snapshot (see :class:`lingtai_sdk.client.LingTaiSession`). This is what
    keeps a single ``events()`` read after ``stop()`` complete and free of
    double counting; a draining implementation silently breaks
    :meth:`lingtai_sdk.client.LingTaiClient.query`.

    **State events.** State transitions are surfaced as ``EventKind.STATE``
    events whose ``data['state']`` is a :class:`RuntimeState` *value* (a plain
    string). Backends map their own life-state onto :class:`RuntimeState` so the
    taxonomy stays backend-neutral.
    """

    source: str = ""

    @property
    @abstractmethod
    def state(self) -> RuntimeState: ...

    @property
    @abstractmethod
    def working_dir(self) -> Path: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def send(self, message: "RuntimeMessage | str") -> None: ...

    @abstractmethod
    def events(self) -> Iterator[RuntimeEvent]:
        """Return a non-draining cumulative snapshot of all events so far.

        See the class docstring: re-iterable, never consumes, ordered. Backends
        that drain on read violate the contract and break ``query()``.
        """
        ...

    @abstractmethod
    def stop(self, timeout: float = 5.0) -> None: ...

    def __enter__(self) -> "RuntimeSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class Runtime(ABC):
    """A factory for runtime sessions. Backends subclass this."""

    id: str = ""

    @abstractmethod
    def create_session(self, options: RuntimeOptions) -> RuntimeSession: ...

    def supports(self, options: RuntimeOptions) -> bool:
        return True

    def run(self, options: RuntimeOptions) -> RuntimeSession:
        session = self.create_session(options)
        session.start()
        return session


__all__ = [
    "RuntimeState",
    "EventKind",
    "RuntimeOptions",
    "RuntimeMessage",
    "RuntimeEvent",
    "RuntimeSession",
    "Runtime",
]
