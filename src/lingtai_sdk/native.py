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
``import lingtai_sdk.native`` imports only import-pure SDK siblings
(:mod:`lingtai_sdk.runtime`, capability-bundle schema/host helpers, and core
bundle manifest declarations); the wrapper ``Agent`` is imported **lazily**, the
first time a session is actually started (or via the default agent factory).
Constructing a :class:`NativeRuntime` therefore stays free of the wrapper's
heavy provider SDKs — they load only when an agent boots. ``NativeRuntime`` and
``NativeRuntimeSession`` are exported from the package root via PEP 562 lazy
attributes (see ``lingtai_sdk.__getattr__``).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .capabilities import BundleManifest
from .capability_host import NativeBundleHost, ToolHandler
from .core_bundles import core_bundle_manifests, native_core_hosts
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

#: Fields read from ``manifest['llm']`` to seed the merged LLM config. Explicit
#: ``RuntimeOptions`` fields of the same name take precedence; the manifest
#: only fills gaps. ``api_key`` is included here (so a manifest can carry the
#: secret) but is treated as a secret everywhere downstream — it is consumed
#: into the service and never echoed onto a public surface.
_MANIFEST_LLM_FIELDS = ("provider", "model", "base_url", "api_key")


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
    # only on RuntimeOptions (or inside manifest['llm']) until the lazy service
    # builder consumes it. Manifest-derived fields are surfaced here too so the
    # deferred view reflects what will be merged at start().
    merged = _llm_config_from_options(options)
    llm = {f: merged[f] for f in _LLM_FIELDS if f != "api_key" and merged.get(f)}

    deferred: dict[str, Any] = {
        "llm": llm,
        "manifest": _sanitized_manifest(options.manifest),
        "system_prompt_overrides": dict(options.system_prompt_overrides or {}),
        "extra": dict(options.extra or {}),
    }
    return agent_kwargs, deferred


def _sanitized_manifest(manifest: Any) -> dict[str, Any]:
    """Return a copy of ``manifest`` with any LLM ``api_key`` redacted.

    ``manifest['llm']['api_key']`` may carry a secret (init.json shape). The
    public ``session.deferred['manifest']`` mirror must not retain it verbatim,
    so it is dropped from the copied ``llm`` block. The original
    ``RuntimeOptions.manifest`` is left untouched — only this copy is sanitized.
    """
    out = dict(manifest or {})
    llm = out.get("llm")
    if isinstance(llm, dict):
        llm = dict(llm)
        llm.pop("api_key", None)
        # ``default_headers`` may legitimately include authorization-like
        # values for custom providers. The public deferred manifest is an
        # inspectable mirror, not the source of truth, so drop headers entirely
        # rather than trying to distinguish safe from secret header names.
        llm.pop("default_headers", None)
        out["llm"] = llm
    return out


def _manifest_llm(options: RuntimeOptions) -> dict[str, Any]:
    """Return ``manifest['llm']`` as a dict (empty if absent/ill-typed)."""
    manifest = options.manifest or {}
    llm = manifest.get("llm") if isinstance(manifest, Mapping) else None
    return dict(llm) if isinstance(llm, Mapping) else {}


def _llm_config_from_options(options: RuntimeOptions) -> dict[str, Any]:
    """Merge explicit ``RuntimeOptions`` LLM fields over ``manifest['llm']``.

    Precedence: an explicit, non-``None`` ``RuntimeOptions`` field wins; the
    manifest only fills fields the caller left unset. The result is a plain
    dict with the four ``_MANIFEST_LLM_FIELDS`` keys present only when a value
    was found (so callers can treat absence uniformly). Pure — never imports
    ``lingtai``.
    """
    manifest_llm = _manifest_llm(options)
    merged: dict[str, Any] = {}
    for field in _MANIFEST_LLM_FIELDS:
        explicit = getattr(options, field, None)
        if explicit is not None:
            merged[field] = explicit
        elif manifest_llm.get(field) is not None:
            merged[field] = manifest_llm[field]
    return merged


def _max_rpm_from_options_or_manifest(options: RuntimeOptions) -> int:
    """Resolve ``max_rpm`` for provider-defaults, searching in precedence order.

    ``options.extra['native']['max_rpm']`` → ``options.extra['max_rpm']`` →
    ``manifest['max_rpm']`` → ``manifest['llm']['max_rpm']``. Returns ``0`` when
    unset — unlike the CLI (which defaults to 60), the SDK does not impose RPM
    gating unless a host opts in, so embedders are not surprised by throttling.
    Pure — never imports ``lingtai``.
    """
    extra = options.extra or {}
    native_extra = extra.get("native") if isinstance(extra, Mapping) else None
    manifest = options.manifest or {}
    for value in (
        native_extra.get("max_rpm") if isinstance(native_extra, Mapping) else None,
        extra.get("max_rpm") if isinstance(extra, Mapping) else None,
        manifest.get("max_rpm") if isinstance(manifest, Mapping) else None,
        _manifest_llm(options).get("max_rpm"),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _context_window_from_options_or_manifest(
    options: RuntimeOptions,
) -> int | None:
    """Resolve an optional ``context_window``, searching in precedence order.

    ``manifest['llm']['context_window']`` → ``manifest['context_limit']`` →
    ``options.extra['native']['context_window']`` →
    ``options.extra['context_window']``. Returns ``None`` when unset, so the
    ``LLMService`` keeps its own default. Pure — never imports ``lingtai``.
    """
    extra = options.extra or {}
    native_extra = extra.get("native") if isinstance(extra, Mapping) else None
    manifest = options.manifest or {}
    for value in (
        _manifest_llm(options).get("context_window"),
        manifest.get("context_limit") if isinstance(manifest, Mapping) else None,
        native_extra.get("context_window")
        if isinstance(native_extra, Mapping)
        else None,
        extra.get("context_window") if isinstance(extra, Mapping) else None,
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


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

    **Stage-3 manifest translation.** Provider/model/base_url/api_key are taken
    from the merged config (explicit ``RuntimeOptions`` fields override
    ``manifest['llm']``; the manifest fills gaps). When a ``manifest['llm']``
    block is present, recognized provider defaults (``api_compat``,
    ``default_headers``, ``compact_threshold``, ``max_rpm``) are plumbed through
    ``build_provider_defaults_from_manifest_llm`` scoped to the merged provider,
    and an optional ``context_window`` is passed too.

    Requires both ``provider`` and ``model`` (non-empty) after the merge. Raises
    :class:`NativeRuntimeConfigurationError` otherwise — never the raw missing-
    ``service`` ``TypeError`` from ``Agent``. The error message deliberately
    does not echo ``api_key``.
    """
    config = _llm_config_from_options(options)
    provider = str(config.get("provider") or "").strip()
    model = str(config.get("model") or "").strip()
    if not provider or not model:
        missing = [
            name
            for name, val in (("provider", provider), ("model", model))
            if not val
        ]
        raise NativeRuntimeConfigurationError(
            "NativeRuntime default factory requires a provider and model — set "
            "RuntimeOptions.provider/model or manifest['llm'] "
            "(or inject an agent_factory/service). "
            f"Missing/empty after manifest merge: {', '.join(missing)}."
        )

    # Lazy: importing lingtai.llm registers the built-in adapters and pulls the
    # active provider's SDK. Kept out of module scope to preserve import purity.
    from lingtai.llm.service import (
        LLMService,
        build_provider_defaults_from_manifest_llm,
    )

    # Provider defaults are derived from manifest['llm'], but scoped to the
    # *merged* provider so an explicit RuntimeOptions.provider override does not
    # leave the defaults stranded under the manifest's provider key.
    manifest_llm = _manifest_llm(options)
    provider_defaults = None
    if manifest_llm:
        scoped = dict(manifest_llm)
        scoped["provider"] = provider
        provider_defaults = build_provider_defaults_from_manifest_llm(
            scoped, max_rpm=_max_rpm_from_options_or_manifest(options)
        )
    else:
        max_rpm = _max_rpm_from_options_or_manifest(options)
        if max_rpm > 0:
            provider_defaults = {provider.lower(): {"max_rpm": max_rpm}}

    kwargs: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "api_key": config.get("api_key"),
        "base_url": config.get("base_url"),
    }
    if provider_defaults is not None:
        kwargs["provider_defaults"] = provider_defaults
    context_window = _context_window_from_options_or_manifest(options)
    if context_window is not None:
        kwargs["context_window"] = context_window

    return LLMService(**kwargs)


class NativeRuntimeSession(RuntimeSession):
    """A single agent session backed by the wrapper ``Agent``.

    The agent is built lazily in :meth:`start` via the runtime's factory, so a
    freshly created (but unstarted) session holds no agent and imports no
    wrapper code.
    """

    source = _SOURCE

    def __init__(
        self,
        options: RuntimeOptions,
        *,
        agent_factory: AgentFactory | None = None,
        bridge_events: bool = True,
        core_handlers: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        self._options = options
        # Track whether we're on the default (service-building) path. An
        # injected factory supplies its own agent/service and bypasses service
        # building entirely — so it must not require provider/model.
        self._uses_default_factory = agent_factory is None
        self._agent_factory = agent_factory or _default_agent_factory
        #: Whether to install the live event bridge in ``start()`` (stage 4).
        #: A host can disable it to fall back to the stage-1 lifecycle/
        #: notification/error snapshot only.
        self._bridge_events = bridge_events
        self._core_bundle_manifests = core_bundle_manifests()
        self._core_bundle_hosts: dict[str, NativeBundleHost] = (
            native_core_hosts(core_handlers) if core_handlers is not None else {}
        )
        self._agent: Any | None = None
        self._state = RuntimeState.PENDING
        self._events: list[RuntimeEvent] = []
        # The agent drives its hooks on its own loop thread, so event appends
        # and the last-sampled-state read/write are guarded by a lock.
        self._lock = threading.Lock()
        #: Last ``agent._state`` value mapped+emitted, so ``events()`` only
        #: emits a STATE event when the agent's life-state actually changed.
        self._last_agent_state: str | None = None
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
    def core_bundle_manifests(self) -> tuple[BundleManifest, ...]:
        """The native core bundle manifests visible to this session.

        Stage 9 exposes the contract only: these are the required/privileged/
        native-only ``system`` / ``psyche`` / ``soul`` manifests declared by
        :mod:`lingtai_sdk.core_bundles`. They do not imply that real core
        handlers were imported or migrated.
        """
        return self._core_bundle_manifests

    @property
    def core_bundle_hosts(self) -> dict[str, NativeBundleHost]:
        """Native core hosts built from injected handlers, if any.

        Returns a shallow copy so callers can inspect or invoke the injected
        dummy/native handlers without mutating session wiring. When no
        ``core_handlers`` were supplied, the session remains manifest-only and
        this mapping is empty.
        """
        return dict(self._core_bundle_hosts)

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
        if self._bridge_events:
            self._install_event_bridge(self._agent)
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
        # A non-blocking, re-iterable snapshot of the queue. Tool-call /
        # tool-result / text / usage / turn-error events are appended live by
        # the installed hook bridge (stage 4) as the agent's loop thread runs;
        # the agent's life-state runs on that thread too, so it is *sampled*
        # here (a STATE event is appended only when it changed) rather than
        # pushed — keeping the bridge lock-light and the loop untouched.
        self._sample_agent_state()
        with self._lock:
            return iter(list(self._events))

    def stop(self, timeout: float = 5.0) -> None:
        if self._state is RuntimeState.STOPPED:
            return
        if self._agent is not None:
            self._agent.stop(timeout=timeout)
        self._set_state(RuntimeState.STOPPED)

    # -- event bridge (stage 4) --------------------------------------------
    def _install_event_bridge(self, agent: Any) -> None:
        """Wrap the agent's overridable hooks so existing activity becomes events.

        Purely additive: each wrapper calls the original hook and emits the
        matching :class:`RuntimeEvent`, leaving the kernel turn loop and the
        hooks' own contracts intact. The wrappers are installed as *instance*
        attributes — they shadow the bound methods on this one agent only and
        never touch the kernel/wrapper source.

        - ``_on_tool_result_hook(name, args, result)`` → a ``TOOL_CALL`` event
          (name + args) plus a ``TOOL_RESULT`` event (name + result). The
          original's return value is passed through unchanged, so an
          intercepting host hook still short-circuits the turn as before.
        - ``_post_request(msg, result)`` → a ``TEXT`` event for non-empty
          ``result['text']``, a non-fatal ``ERROR`` event per
          ``result['errors']``, and a ``USAGE`` event sampled from
          ``agent.get_token_usage()``.

        Missing hooks/accessors are tolerated (``getattr`` guards) so a
        slimmer fake or future agent shape degrades gracefully rather than
        breaking the bridge.
        """
        original_tool_hook = getattr(agent, "_on_tool_result_hook", None)
        if callable(original_tool_hook):
            def _bridged_tool_hook(
                tool_name: str, tool_args: Any, result: Any
            ) -> Any:
                self._emit(
                    RuntimeEvent.tool_call(
                        tool_name, tool_args or {}, source=self.source
                    )
                )
                self._emit(
                    RuntimeEvent.tool_result(tool_name, result, source=self.source)
                )
                return original_tool_hook(tool_name, tool_args, result)

            agent._on_tool_result_hook = _bridged_tool_hook

        original_post_request = getattr(agent, "_post_request", None)
        if callable(original_post_request):
            def _bridged_post_request(msg: Any, result: Any) -> None:
                self._emit_turn_result(agent, result)
                return original_post_request(msg, result)

            agent._post_request = _bridged_post_request

    def _emit_turn_result(self, agent: Any, result: Any) -> None:
        """Translate a ``_process_response`` result dict into events."""
        if isinstance(result, Mapping):
            text = result.get("text")
            if isinstance(text, str) and text:
                self._emit(RuntimeEvent.text(text, source=self.source))
            for err in result.get("errors") or ():
                self._emit(
                    RuntimeEvent.error(str(err), fatal=False, source=self.source)
                )
        get_usage = getattr(agent, "get_token_usage", None)
        if callable(get_usage):
            try:
                usage = get_usage()
            except Exception:
                usage = None
            if isinstance(usage, Mapping):
                self._emit(RuntimeEvent.usage(usage, source=self.source))

    def _sample_agent_state(self) -> None:
        """Emit a STATE event if the agent's life-state changed since last read.

        The agent owns its life-state on its loop thread; rather than splice a
        callback into that thread, we read ``agent._state`` whenever a consumer
        polls ``events()``. ``AgentState`` is a ``str`` enum, so ``str(...)``
        yields its value; a plain string (test fakes) passes through.
        """
        agent = self._agent
        if agent is None:
            return
        raw = getattr(agent, "_state", None)
        if raw is None:
            return
        value = getattr(raw, "value", None) or str(raw)
        with self._lock:
            if value == self._last_agent_state:
                return
            self._last_agent_state = value
            self._events.append(
                RuntimeEvent(EventKind.STATE, {"state": value}, source=self.source)
            )

    # -- internals ----------------------------------------------------------
    def _emit(self, event: RuntimeEvent) -> None:
        with self._lock:
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

    def __init__(
        self,
        *,
        agent_factory: AgentFactory | None = None,
        bridge_events: bool = True,
        core_handlers: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        #: Default for sessions: install the live event bridge (stage 4). Set
        #: ``False`` for the stage-1 lifecycle/notification/error snapshot only.
        self._bridge_events = bridge_events
        #: Optional injected handlers for the required native core bundles.
        #: Copy the mapping, but do not validate/build hosts until a session is
        #: created; constructing ``NativeRuntime`` remains side-effect-free.
        self._core_handlers = (
            dict(core_handlers) if core_handlers is not None else None
        )

    def create_session(self, options: RuntimeOptions) -> NativeRuntimeSession:
        return NativeRuntimeSession(
            options,
            agent_factory=self._agent_factory,
            bridge_events=self._bridge_events,
            core_handlers=self._core_handlers,
        )


__all__ = [
    "NativeRuntime",
    "NativeRuntimeSession",
    "AgentFactory",
]
