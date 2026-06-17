"""NativeRuntime — the stage-1 live runtime skeleton.

A thin :class:`~lingtai_sdk.runtime.Runtime` / :class:`RuntimeSession`
implementation that wraps the existing wrapper ``Agent`` **unchanged**. It
translates a backend-neutral :class:`RuntimeOptions` into ``Agent`` constructor
kwargs, drives the agent's start/stop lifecycle, and surfaces lifecycle / error
/ notification events through the stage-0 contract.

Scope (intentionally small — see ``docs/sdk/architecture-foundation.md`` §8):

- This wraps ``Agent``; it does **not** change the kernel turn loop or implement
  a non-native backend.
- Stage 2 adds an **LLM-service translation** for the *default* agent factory:
  when ``provider`` and ``model`` are set, ``start()`` lazily builds a wrapper
  ``LLMService`` from ``provider`` / ``model`` / ``base_url`` / ``api_key`` and
  passes it to ``Agent`` (which requires a ready service). Once applied, those
  LLM fields move from ``session.deferred['llm']`` to ``session.applied['llm']``
  **without** the secret — ``api_key`` is consumed into the service and never
  stored on the session, surfaced in events, or echoed in errors/reprs.
- If the default factory is used but ``provider``/``model`` are partial or
  absent, ``start()`` raises :class:`NativeRuntimeConfigurationError` *before*
  constructing any agent — no opaque missing-``service`` ``TypeError`` leaks and
  the session stays ``PENDING``.
- An injected ``agent_factory`` **bypasses** service building entirely: tests
  (and hosts that supply their own service) boot without a real ``LLMService``,
  keeping the runtime network-free.
- ``send()`` routes to ``Agent.send()`` — the existing fire-and-forget queue
  path. It does not block on a turn, so it is safe and deterministic in tests.

Import purity
-------------
``import lingtai_sdk.native`` imports only the pure contract module
(:mod:`lingtai_sdk.runtime`); the wrapper ``Agent`` is imported **lazily**, the
first time a session is actually started (or via the default agent factory).
Constructing a :class:`NativeRuntime` therefore stays free of the wrapper's
heavy provider SDKs — they load only when an agent boots. ``NativeRuntime`` and
``NativeRuntimeSession`` are exported from the package root via PEP 562 lazy
attributes (see ``lingtai_sdk.__getattr__``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import NativeRuntimeConfigurationError
from .runtime import (
    EventKind,
    Runtime,
    RuntimeEvent,
    RuntimeMessage,
    RuntimeOptions,
    RuntimeSession,
    RuntimeState,
)

#: A factory that builds the underlying agent from translated kwargs. The
#: default imports the wrapper ``Agent`` lazily; tests inject a fake.
AgentFactory = Callable[..., Any]

_SOURCE = "native"

#: Fields copied verbatim onto ``Agent`` constructor kwargs when present. These
#: are the options ``Agent`` accepts directly without changing runtime
#: semantics. ``working_dir`` is handled separately (it is required).
_SAFE_AGENT_FIELDS = ("agent_name", "capabilities", "addons", "streaming")

#: LLM/provider fields that cannot be applied without building an ``LLMService``
#: (a later stage). Collected into ``deferred['llm']`` instead of forced onto
#: the ``Agent`` constructor.
_LLM_FIELDS = ("provider", "model", "base_url", "api_key")


def _agent_kwargs_from_options(
    options: RuntimeOptions,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate ``RuntimeOptions`` into ``(agent_kwargs, deferred)``.

    ``agent_kwargs`` is what is safe to pass to ``Agent(**agent_kwargs)`` today.
    ``deferred`` records everything that is recognized but **not** applied in
    this stage (LLM/provider config, manifest, prompt overrides, adapter
    extras), so callers and tests can see exactly what was held back rather than
    silently dropped.
    """
    agent_kwargs: dict[str, Any] = {"working_dir": options.working_dir}

    for field in _SAFE_AGENT_FIELDS:
        value = getattr(options, field, None)
        # ``streaming`` is a plain bool (default False) and is always forwarded;
        # the rest are forwarded only when explicitly provided.
        if field == "streaming":
            if value:
                agent_kwargs[field] = value
        elif value is not None:
            agent_kwargs[field] = value

    # Public/deferred LLM fields intentionally omit api_key. The secret remains
    # only on RuntimeOptions until the lazy service builder consumes it.
    llm = {
        f: getattr(options, f)
        for f in _LLM_FIELDS
        if f != "api_key" and getattr(options, f) is not None
    }

    deferred: dict[str, Any] = {
        "llm": llm,
        "manifest": dict(options.manifest or {}),
        "system_prompt_overrides": dict(options.system_prompt_overrides or {}),
        "extra": dict(options.extra or {}),
    }
    return agent_kwargs, deferred


def _default_agent_factory(**kwargs: Any) -> Any:
    """Lazily import and construct the wrapper ``Agent``.

    Imported here (not at module top) so ``import lingtai_sdk.native`` and
    constructing a ``NativeRuntime`` stay free of the wrapper's provider SDKs.
    """
    from lingtai import Agent  # lazy: pulls the wrapper only on first boot

    return Agent(**kwargs)


def _public_llm_fields(llm: dict[str, Any]) -> dict[str, Any]:
    """Return the LLM config minus secrets.

    ``api_key`` is the only secret-bearing field in ``_LLM_FIELDS``; it is
    stripped so the result is safe for ``session.applied``, events, reprs, and
    reports. Empty values are dropped for a tidy surface.
    """
    return {k: v for k, v in llm.items() if k != "api_key" and v}


def _llm_service_from_options(options: RuntimeOptions) -> Any:
    """Build a wrapper ``LLMService`` from ``RuntimeOptions`` (default factory).

    Lazily imports ``lingtai.llm`` — which registers the built-in adapters on
    import — so ``import lingtai_sdk.native`` and constructing a
    ``NativeRuntime`` stay provider-free; the providers load only here, when a
    session is actually started through the default factory.

    Requires both ``provider`` and ``model`` (non-empty). Raises
    :class:`NativeRuntimeConfigurationError` otherwise — never the raw missing-
    ``service`` ``TypeError`` from ``Agent``. The error message deliberately
    does not echo ``api_key``.
    """
    provider = (options.provider or "").strip()
    model = (options.model or "").strip()
    if not provider or not model:
        missing = [
            name
            for name, val in (("provider", provider), ("model", model))
            if not val
        ]
        raise NativeRuntimeConfigurationError(
            "NativeRuntime default factory requires RuntimeOptions.provider and "
            "model (or an injected agent_factory/service) until init.json "
            f"translation lands. Missing/empty: {', '.join(missing)}."
        )

    # Lazy: importing lingtai.llm registers the built-in adapters and pulls the
    # active provider's SDK. Kept out of module scope to preserve import purity.
    from lingtai.llm.service import LLMService

    return LLMService(
        provider=provider,
        model=model,
        api_key=options.api_key,
        base_url=options.base_url,
    )


class NativeRuntimeSession(RuntimeSession):
    """A single agent session backed by the wrapper ``Agent``.

    The agent is built lazily in :meth:`start` via the runtime's factory, so a
    freshly created (but unstarted) session holds no agent and imports no
    wrapper code.
    """

    source = _SOURCE

    def __init__(
        self, options: RuntimeOptions, *, agent_factory: AgentFactory | None = None
    ) -> None:
        self._options = options
        # Track whether we're on the default (service-building) path. An
        # injected factory supplies its own agent/service and bypasses service
        # building entirely — so it must not require provider/model.
        self._uses_default_factory = agent_factory is None
        self._agent_factory = agent_factory or _default_agent_factory
        self._agent: Any | None = None
        self._state = RuntimeState.PENDING
        self._events: list[RuntimeEvent] = []
        self._agent_kwargs, self.deferred = _agent_kwargs_from_options(options)
        #: Fields that have actually been applied to the agent (secret-free).
        #: LLM config moves here from ``deferred`` once a service is built.
        self.applied: dict[str, Any] = {}

    # -- contract properties ------------------------------------------------
    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def working_dir(self) -> Path:
        return Path(self._options.working_dir)

    @property
    def agent(self) -> Any | None:
        """The underlying wrapper ``Agent``, or ``None`` before :meth:`start`."""
        return self._agent

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._state in (RuntimeState.ACTIVE, RuntimeState.STOPPED):
            return  # idempotent: never rebuild a running or stopped session
        kwargs = dict(self._agent_kwargs)
        # Default factory path: build the LLMService the wrapper Agent requires
        # and apply the (secret-free) LLM config. An injected factory supplies
        # its own agent/service, so it is left untouched. Service building runs
        # BEFORE any agent is constructed, so a bad LLM config raises a clear
        # SDK error and the session stays PENDING (no partial ACTIVE state).
        if self._uses_default_factory:
            service = _llm_service_from_options(self._options)
            kwargs["service"] = service
            self.applied["llm"] = _public_llm_fields(self.deferred.get("llm", {}))
            self.deferred["llm"] = {}  # no longer deferred — it's applied
        self._agent = self._agent_factory(**kwargs)
        self._agent.start()
        self._set_state(RuntimeState.ACTIVE)

    def send(self, message: RuntimeMessage | str) -> None:
        if self._state is not RuntimeState.ACTIVE or self._agent is None:
            self._emit(
                RuntimeEvent.error(
                    f"send() ignored: session is {self._state.value}, not active",
                    fatal=False,
                    source=self.source,
                )
            )
            return
        if isinstance(message, RuntimeMessage):
            content, sender = message.content, message.sender
        else:
            content, sender = message, "user"
        # Fire-and-forget enqueue onto the agent's inbox (no synchronous turn).
        self._agent.send(content, sender)
        self._emit(
            RuntimeEvent(
                EventKind.NOTIFICATION,
                {"queued": True, "sender": sender},
                source=self.source,
            )
        )

    def events(self) -> Iterator[RuntimeEvent]:
        # Stage 1: a non-blocking, re-iterable snapshot of the queue. A future
        # stage bridges the agent's live output stream onto these events.
        return iter(list(self._events))

    def stop(self, timeout: float = 5.0) -> None:
        if self._state is RuntimeState.STOPPED:
            return
        if self._agent is not None:
            self._agent.stop(timeout=timeout)
        self._set_state(RuntimeState.STOPPED)

    # -- internals ----------------------------------------------------------
    def _emit(self, event: RuntimeEvent) -> None:
        self._events.append(event)

    def _set_state(self, state: RuntimeState) -> None:
        self._state = state
        self._emit(RuntimeEvent.state(state, source=self.source))


class NativeRuntime(Runtime):
    """Factory for :class:`NativeRuntimeSession`s.

    ``agent_factory`` is injectable so tests can substitute a fake agent and
    avoid booting a real model / process.
    """

    id = _SOURCE

    def __init__(self, *, agent_factory: AgentFactory | None = None) -> None:
        self._agent_factory = agent_factory

    def create_session(self, options: RuntimeOptions) -> NativeRuntimeSession:
        return NativeRuntimeSession(options, agent_factory=self._agent_factory)


__all__ = [
    "NativeRuntime",
    "NativeRuntimeSession",
    "AgentFactory",
]
