"""High-state ``psyche`` identity/context bundle declaration + native host seam.

The privileged, native-only mirror of :mod:`lingtai_sdk.lifecycle_tools` (the
``system`` lifecycle surface, stage 3C) for the **second** core surface — the
agent's identity, working-notes (pad), and conversation-context (molt) tool.
Where ``lifecycle_tools`` is the high-state template for ``system``, this module
is the same template applied to ``psyche``: the agent's "bare essentials of self"
(edit/load identity ``lingtai.md``, edit/load/pin the pad sketchboard, molt the
conversation context, and naming).

Single source of manifest truth
--------------------------------
The ``psyche`` :class:`~lingtai_sdk.capabilities.BundleManifest` is **not**
redeclared here. It already exists in :mod:`lingtai_sdk.core_bundles`
(:func:`~lingtai_sdk.core_bundles.psyche_bundle`, stage 8) — ``required`` +
``privileged`` + ``native_only`` + ``NATIVE_ONLY`` + ``native`` transport, one
public ``psyche`` tool, bundle-level ``danger=destructive``. This module *reuses*
that manifest verbatim (``psyche_identity_manifest()`` is a thin re-export) so
there is exactly one ``psyche`` manifest in the SDK, and adds two things on top:

1. a **per-(object, action) risk table** (:data:`PSYCHE_OBJECT_ACTION_RISK`) that
   grades each ``psyche`` ``(object, action)`` pair individually. Unlike
   ``system`` (a flat ``action`` discriminator), the live ``psyche`` tool
   dispatches on an ``(object, action)`` pair (see
   ``lingtai_kernel.intrinsics.psyche._VALID_ACTIONS`` / ``_DISPATCH``), so the
   table is keyed by that pair. A single bundle-level ``caution`` posture cannot
   faithfully express that ``pad.load`` / ``lingtai.load`` are pure reads while
   ``name.set`` writes the **immutable** true name (set-once, irreversible);
2. a **``psyche``-specific native host seam** (:func:`psyche_identity_host`) — the
   privileged-native mirror of ``system_lifecycle_host``, wrapping
   :func:`~lingtai_sdk.core_bundles.native_core_host` so a host injecting *one*
   real ``psyche`` handler does not have to also supply ``system`` / ``soul``.

Why a per-(object, action) table, not a per-pair manifest
---------------------------------------------------------
The ``psyche`` tool is a single public tool with ``(object, action)``
discriminators, not one tool per pair — so its danger cannot vary per-pair at the
*manifest* level without inventing one bundle per pair (which would fork the live
tool registration, exactly what this stage must not do). The conservative,
faithful encoding is therefore: set the **bundle-level posture to ``destructive``**
(the strongest per-pair grade, mirroring the other stage-3 bundle posture rule),
and ship the **graded pair table as metadata** so a host that wants finer-than-
bundle grading can read it *without* any live runtime gate. The grading mirrors the
side effects the real kernel intrinsic already performs in code; it is a
*declaration of* that posture, never a second gate.

Note the table grades the one **irreversible** pair ``name.set`` ``DESTRUCTIVE``;
that strongest grade is also the bundle posture. The per-pair table still refines
within the single ``psyche`` tool — downward for pure reads (``*.load`` → ``SAFE``)
and to ``CAUTION`` for lasting-but-recoverable writes. Like every stage-3 risk
helper, the lookup fails safe **high** (an unknown object/action grades
``DESTRUCTIVE``).

What this module is NOT
-----------------------
Exactly as in stages 3A/3B/3C/8, it does **not** migrate, move, rewrite, import,
or call the real ``psyche`` implementation. The real handler is a *kernel
intrinsic* (``lingtai_kernel.intrinsics.psyche.handle(agent, args)``), wired live
by ``BaseAgent._wire_intrinsics``; importing it here would break SDK
import-purity and is unnecessary — this module ships *declarations + an injection
seam* only. The wrapper-side bridge that supplies the handler lives in
``lingtai.core.psyche_bundle`` (the wrapper *may* import the SDK and the kernel
intrinsic; the SDK must not import either).

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam.

See ``docs/sdk/architecture-foundation.md`` (stage 3J).
"""
from __future__ import annotations

from typing import Mapping

from .contracts import BundleManifest, SecurityDanger
from .host import NativeBundleHost, ToolHandler
from .core import is_core_manifest, native_core_host, psyche_bundle
from ..errors import BundleHostError

#: The one public identity/context tool this module is about.
PSYCHE_TOOL_NAME = "psyche"

#: The live ``(object -> {actions})`` validity map, a language-neutral copy of
#: ``lingtai_kernel.intrinsics.psyche._VALID_ACTIONS``. ``psyche`` dispatches on a
#: pair, not a flat action enum, so this records which actions each object
#: accepts. Pinned against the kernel by ``tests/test_sdk_psyche_tools.py`` so the
#: declaration cannot drift from the live dispatch table.
PSYCHE_VALID_ACTIONS: dict[str, frozenset[str]] = {
    "lingtai": frozenset({"update", "load"}),
    "pad": frozenset({"edit", "load", "append"}),
    "context": frozenset({"molt"}),
    "name": frozenset({"set", "nickname"}),
}

#: Per-``(object, action)`` danger grading for the single ``psyche`` tool. This is
#: the conservative, faithful encoding of the side effects the real kernel
#: intrinsic performs in code (``intrinsics.psyche._DISPATCH`` handlers):
#:
#: * **pure reads** (``SAFE``) — ``lingtai.load`` recomposes the ``character``
#:   prompt section from ``system/lingtai.md`` (no write); ``pad.load`` loads
#:   ``system/pad.md`` + pinned append-files into the prompt (no write).
#: * **lasting-but-recoverable self side effects** (``CAUTION``) —
#:   ``lingtai.update`` writes ``system/lingtai.md`` then reloads ``character``;
#:   ``pad.edit`` writes ``system/pad.md``; ``pad.append`` sets/clears the pinned
#:   append-file list (persists ``pad_append.json``); ``name.nickname`` sets the
#:   *mutable* nickname; ``context.molt`` sheds conversation context but
#:   **archives** history to ``chat_history_archive.jsonl``, snapshots the
#:   pre-molt interface, and keeps a briefing — lasting, but recoverable, so it
#:   remains ``CAUTION`` rather than an irreversible teardown.
#: * **irreversible** (``DESTRUCTIVE``) — ``name.set`` writes the agent's
#:   **immutable true name**; the live handler raises if a name is already set
#:   (``agent.set_name`` is set-once). A one-way, unrecoverable identity write, so
#:   it sets the bundle-level posture to ``destructive`` as the strongest psyche
#:   action grade.
PSYCHE_OBJECT_ACTION_RISK: dict[tuple[str, str], SecurityDanger] = {
    # pure reads — recompose a prompt section, no write
    ("lingtai", "load"): SecurityDanger.SAFE,
    ("pad", "load"): SecurityDanger.SAFE,
    # lasting-but-recoverable self side effects
    ("lingtai", "update"): SecurityDanger.CAUTION,
    ("pad", "edit"): SecurityDanger.CAUTION,
    ("pad", "append"): SecurityDanger.CAUTION,
    ("name", "nickname"): SecurityDanger.CAUTION,
    ("context", "molt"): SecurityDanger.CAUTION,
    # irreversible — sets the immutable true name (set-once)
    ("name", "set"): SecurityDanger.DESTRUCTIVE,
}

#: The ``(object, action)`` pairs that write persistent state or shed context — a
#: declaration of the lasting side effects of the bundle (the live writes are the
#: kernel intrinsic's, not here). Everything that is not a pure ``*.load`` read.
PSYCHE_MUTATING_PAIRS: frozenset[tuple[str, str]] = frozenset(
    pair
    for pair, grade in PSYCHE_OBJECT_ACTION_RISK.items()
    if grade is not SecurityDanger.SAFE
)

#: The pure-read ``(object, action)`` pairs — ``*.load``. Self only, no write.
PSYCHE_READ_PAIRS: frozenset[tuple[str, str]] = frozenset(
    pair
    for pair, grade in PSYCHE_OBJECT_ACTION_RISK.items()
    if grade is SecurityDanger.SAFE
)


def psyche_identity_manifest() -> BundleManifest:
    """The ``psyche`` identity/context bundle manifest — re-exported from ``core_bundles``.

    There is exactly one ``psyche`` manifest in the SDK: the privileged,
    native-only core bundle declared by
    :func:`~lingtai_sdk.core_bundles.psyche_bundle` (stage 8). This module does
    **not** fork or rebuild it — it reuses it verbatim so the bundle-level
    ``danger=destructive`` posture and the ``required``/``privileged``/``native_only``
    role flags stay single-sourced. The per-(object, action) grading this module
    adds lives in :data:`PSYCHE_OBJECT_ACTION_RISK`, *alongside* (never replacing)
    the manifest's bundle-level posture.
    """
    return psyche_bundle()


def is_psyche_identity_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``psyche`` identity bundle by name.

    A thin, intention-revealing wrapper over the core-bundle identity check,
    narrowed to the single ``psyche`` surface this module owns.
    """
    return is_core_manifest(manifest) and manifest.name == PSYCHE_TOOL_NAME


def object_action_risk(obj: str, action: str) -> SecurityDanger:
    """Return the declared danger grade for a ``psyche`` ``(object, action)`` pair.

    Looks the pair up in :data:`PSYCHE_OBJECT_ACTION_RISK`. An **unknown** pair
    (unknown object, unknown action, or a valid object with an action it does not
    accept) is graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather
    than silently treated as safe — the same fail-safe-*high* direction the other
    stage-3 risk helpers use. This is a pure declaration helper; it gates nothing
    and never raises.
    """
    return PSYCHE_OBJECT_ACTION_RISK.get((obj, action), SecurityDanger.DESTRUCTIVE)


def psyche_identity_host(handler: ToolHandler) -> NativeBundleHost:
    """Build a native-authority host for the ``psyche`` bundle from an injected handler.

    The privileged-native, single-tool mirror of
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_host`. Given the single
    *supplied* ``psyche`` handler callable, returns a
    :class:`~lingtai_sdk.capability_host.NativeBundleHost` (built with
    ``native_authority=True`` by :func:`~lingtai_sdk.core_bundles.native_core_host`)
    hosting the one declared ``psyche`` tool.

    Unlike :func:`~lingtai_sdk.core_bundles.native_core_hosts` — which requires
    handlers for *all three* core bundles together — this seam hosts ``psyche``
    alone, so the wrapper bridge can adopt the identity/context surface
    incrementally without supplying ``system`` / ``soul``. The handler is whatever
    the wrapper bridge injects (the real kernel intrinsic ``psyche.handle`` bound
    to an agent); this shim never imports or calls the real implementation, and
    ``native_core_host`` enforces the manifest/handler/native-authority contract.

    The declared ``danger`` posture (bundle-level ``destructive`` plus the
    :data:`PSYCHE_OBJECT_ACTION_RISK` grading) is **not** enforced here: a host
    runs whatever handler it is given. Danger is a *declaration* the stage-17
    :mod:`lingtai_sdk.guard_bridge` reads — a separate, not-installed seam.
    """
    if not callable(handler):
        raise BundleHostError(
            f"psyche identity bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return native_core_host(psyche_identity_manifest(), handler)


def psyche_identity_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, NativeBundleHost]:
    """Build ``{"psyche": NativeBundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`psyche_identity_host`, parallel to
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_hosts`, so the wrapper
    bridge has the same ``{name: host}`` shape across all stages. The mapping must
    contain exactly the ``psyche`` handler — a missing ``psyche`` handler or any
    handler for a non-``psyche`` name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial/typo'd wiring can
    never silently host the wrong surface.
    """
    expected = {PSYCHE_TOOL_NAME}
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for psyche identity bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-psyche identity bundle name(s): {sorted(extra)}"
        )
    return {PSYCHE_TOOL_NAME: psyche_identity_host(handlers[PSYCHE_TOOL_NAME])}


__all__ = [
    "PSYCHE_TOOL_NAME",
    "PSYCHE_VALID_ACTIONS",
    "PSYCHE_OBJECT_ACTION_RISK",
    "PSYCHE_MUTATING_PAIRS",
    "PSYCHE_READ_PAIRS",
    "psyche_identity_manifest",
    "is_psyche_identity_manifest",
    "object_action_risk",
    "psyche_identity_host",
    "psyche_identity_hosts",
]
