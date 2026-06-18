"""High-state ``soul`` inner-voice bundle declaration + native host seam.

The privileged, native-only mirror of :mod:`lingtai_sdk.lifecycle_tools` (the
``system`` lifecycle surface, stage 3C) and :mod:`lingtai_sdk.psyche_tools` (the
``psyche`` identity/context surface) for the **third** core surface — the agent's
reflective inner voice: periodic past-self consultation (``flow``), on-demand
self-inquiry (``inquiry``), flow/voice tuning (``config`` / ``voice``), and a
``dismiss`` alias that clears the soul notification surface.

Single source of manifest truth
--------------------------------
The ``soul`` :class:`~lingtai_sdk.capabilities.BundleManifest` is **not**
redeclared here. It already exists in :mod:`lingtai_sdk.core_bundles`
(:func:`~lingtai_sdk.core_bundles.soul_bundle`, stage 8) — ``required`` +
``privileged`` + ``native_only`` + ``NATIVE_ONLY`` + ``native`` transport, one
public ``soul`` tool, bundle-level ``danger=caution``. This module *reuses* that
manifest verbatim (``soul_voice_manifest()`` is a thin re-export) so there is
exactly one ``soul`` manifest in the SDK, and adds two things on top:

1. a **per-action risk table** (:data:`SOUL_ACTION_RISK`) that grades each ``soul``
   action individually (``soul`` is a flat ``action`` discriminator like
   ``system``, not an ``(object, action)`` pair like ``psyche``);
2. a **``soul``-specific native host seam** (:func:`soul_voice_host`) — the
   privileged-native mirror of ``system_lifecycle_host``, wrapping
   :func:`~lingtai_sdk.core_bundles.native_core_host` so a host injecting *one*
   real ``soul`` handler does not have to also supply ``system`` / ``psyche``.

Why a per-action risk table
----------------------------
A single bundle-level ``caution`` posture cannot distinguish the actions that run
an LLM consultation and persist state from a lighter notification dismiss. The
conservative, faithful encoding is: keep the **bundle-level posture at ``caution``**
(what the stage-8 manifest and the stage-17 guard bridge already derive — the
strongest soul action *is* ``caution``), and ship the **graded action table as
metadata**. The grading mirrors the side effects the real kernel intrinsic
performs in code (``intrinsics.soul.__init__.handle`` dispatch); it is a
*declaration of* that posture, never a second gate. Like every stage-3 risk
helper, the lookup fails safe **high** (an unknown action grades ``DESTRUCTIVE``).

What this module is NOT
-----------------------
Exactly as in stages 3A/3B/3C/8, it does **not** migrate, move, rewrite, import,
or call the real ``soul`` implementation. The real handler is a *kernel intrinsic*
(``lingtai_kernel.intrinsics.soul.handle(agent, args)``), wired live by
``BaseAgent._wire_intrinsics``; importing it here would break SDK import-purity
and is unnecessary — this module ships *declarations + an injection seam* only.
The wrapper-side bridge that supplies the handler lives in
``lingtai.core.soul_bundle`` (the wrapper *may* import the SDK and the kernel
intrinsic; the SDK must not import either).

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam.

See ``docs/sdk/architecture-foundation.md`` (stage 3J).
"""
from __future__ import annotations

from typing import Mapping

from .contracts import BundleManifest, SecurityDanger
from .host import NativeBundleHost, ToolHandler
from .core import is_core_manifest, native_core_host, soul_bundle
from ..errors import BundleHostError

#: The one public inner-voice tool this module is about.
SOUL_TOOL_NAME = "soul"

#: Per-action danger grading for the single ``soul`` tool's ``action``
#: discriminator. This is the conservative, faithful encoding of the side effects
#: the real kernel intrinsic performs in code (``intrinsics.soul.__init__.handle``):
#:
#: * **LLM consultation + persisted state** (``CAUTION``) — ``inquiry`` runs a
#:   synchronous mirror-session LLM call and persists the result to
#:   ``logs/soul_inquiry.jsonl`` (and, for ``/btw``-source, ``.notification/btw.json``);
#:   ``flow`` spawns a daemon-thread consultation fire that writes
#:   ``logs/soul_flow.jsonl`` and publishes/clears ``.notification/soul.json``
#:   (in-flight gated by ``agent._soul_fire_lock``). Both do real work with lasting
#:   logs/notifications, but neither tears down or irreversibly destroys state.
#: * **persisted init config** (``CAUTION``) — ``config`` persists soul cadence
#:   knobs (``delay_seconds`` / ``consultation_past_count``) to
#:   ``manifest.soul.*`` in ``init.json`` and restarts the wall-clock timer;
#:   ``voice`` persists the soul voice profile to ``manifest.soul.*``. Lasting
#:   preference writes, recoverable by re-setting.
#: * **notification dismiss** (``CAUTION``) — ``dismiss`` clears the ``soul``
#:   ``.notification/`` surface via the generic dismiss path. A small, self-scoped
#:   side effect — graded ``CAUTION`` to mirror ``system``'s ``dismiss`` (which
#:   also clears one ``.notification/`` surface), not a pure read.
#:
#: Every soul action is ``CAUTION`` — none is a pure read and none is a destructive
#: teardown — so the bundle-level posture (``caution``) equals the strongest
#: action's grade, exactly mirroring ``lifecycle_tools``'s "bundle = strongest"
#: rule.
SOUL_ACTION_RISK: dict[str, SecurityDanger] = {
    # LLM consultation + persisted logs/notifications
    "inquiry": SecurityDanger.CAUTION,
    "flow": SecurityDanger.CAUTION,
    # persisted init config (manifest.soul.*)
    "config": SecurityDanger.CAUTION,
    "voice": SecurityDanger.CAUTION,
    # clears the soul notification surface
    "dismiss": SecurityDanger.CAUTION,
}

#: The soul actions that run an LLM consultation (synchronous ``inquiry`` or the
#: daemon-thread ``flow`` fire) — a declaration of the actions with LLM cost and
#: background work, the attention subset parallel to ``SYSTEM`` /
#: ``BASH_PROCESS_ACTIONS``. ``config`` / ``voice`` / ``dismiss`` only persist
#: config or clear a notification, so they are excluded.
SOUL_CONSULTATION_ACTIONS: frozenset[str] = frozenset({"inquiry", "flow"})

#: The soul actions that persist init config to ``manifest.soul.*`` in
#: ``init.json`` — ``config`` (cadence) and ``voice`` (profile).
SOUL_CONFIG_ACTIONS: frozenset[str] = frozenset({"config", "voice"})


def soul_voice_manifest() -> BundleManifest:
    """The ``soul`` inner-voice bundle manifest — re-exported from ``core_bundles``.

    There is exactly one ``soul`` manifest in the SDK: the privileged,
    native-only core bundle declared by
    :func:`~lingtai_sdk.core_bundles.soul_bundle` (stage 8). This module does
    **not** fork or rebuild it — it reuses it verbatim so the bundle-level
    ``danger=caution`` posture and the ``required``/``privileged``/``native_only``
    role flags stay single-sourced. The per-action grading this module adds lives
    in :data:`SOUL_ACTION_RISK`, *alongside* (never replacing) the manifest's
    bundle-level posture.
    """
    return soul_bundle()


def is_soul_voice_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``soul`` inner-voice bundle by name.

    A thin, intention-revealing wrapper over the core-bundle identity check,
    narrowed to the single ``soul`` surface this module owns.
    """
    return is_core_manifest(manifest) and manifest.name == SOUL_TOOL_NAME


def action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``soul`` ``action``.

    Looks the action up in :data:`SOUL_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than
    silently treated as safe — the same fail-safe-*high* direction the other
    stage-3 risk helpers use. This is a pure declaration helper; it gates nothing
    and never raises.
    """
    return SOUL_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def soul_voice_host(handler: ToolHandler) -> NativeBundleHost:
    """Build a native-authority host for the ``soul`` bundle from an injected handler.

    The privileged-native, single-tool mirror of
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_host` /
    :func:`~lingtai_sdk.psyche_tools.psyche_identity_host`. Given the single
    *supplied* ``soul`` handler callable, returns a
    :class:`~lingtai_sdk.capability_host.NativeBundleHost` (built with
    ``native_authority=True`` by :func:`~lingtai_sdk.core_bundles.native_core_host`)
    hosting the one declared ``soul`` tool.

    Unlike :func:`~lingtai_sdk.core_bundles.native_core_hosts` — which requires
    handlers for *all three* core bundles together — this seam hosts ``soul``
    alone, so the wrapper bridge can adopt the inner-voice surface incrementally
    without supplying ``system`` / ``psyche``. The handler is whatever the wrapper
    bridge injects (the real kernel intrinsic ``soul.handle`` bound to an agent);
    this shim never imports or calls the real implementation, and
    ``native_core_host`` enforces the manifest/handler/native-authority contract.

    The declared ``danger`` posture (bundle-level ``caution`` plus the
    :data:`SOUL_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. Danger is a *declaration* the stage-17
    :mod:`lingtai_sdk.guard_bridge` reads — a separate, not-installed seam. Note
    the real in-flight gate for ``flow`` (``agent._soul_fire_lock``) lives in the
    kernel intrinsic, not in this host.
    """
    if not callable(handler):
        raise BundleHostError(
            f"soul voice bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return native_core_host(soul_voice_manifest(), handler)


def soul_voice_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, NativeBundleHost]:
    """Build ``{"soul": NativeBundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`soul_voice_host`, parallel to
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_hosts`, so the wrapper
    bridge has the same ``{name: host}`` shape across all stages. The mapping must
    contain exactly the ``soul`` handler — a missing ``soul`` handler or any
    handler for a non-``soul`` name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial/typo'd wiring can
    never silently host the wrong surface.
    """
    expected = {SOUL_TOOL_NAME}
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for soul voice bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-soul voice bundle name(s): {sorted(extra)}"
        )
    return {SOUL_TOOL_NAME: soul_voice_host(handlers[SOUL_TOOL_NAME])}


__all__ = [
    "SOUL_TOOL_NAME",
    "SOUL_ACTION_RISK",
    "SOUL_CONSULTATION_ACTIONS",
    "SOUL_CONFIG_ACTIONS",
    "soul_voice_manifest",
    "is_soul_voice_manifest",
    "action_risk",
    "soul_voice_host",
    "soul_voice_hosts",
]
