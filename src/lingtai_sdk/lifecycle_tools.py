"""High-state lifecycle/``system`` tool bundle declaration + native host seam.

The **high-state counterpart** of :mod:`lingtai_sdk.file_tools` (stage 3A,
read-only) and :mod:`lingtai_sdk.file_mutation_tools` (stage 3B, side-effecting).
Where those declare the *non-privileged, in-process* low-state file tools, this
module is the first declare-and-inject seam for the **privileged, native-only**
``system`` lifecycle surface — the agent's runtime/lifecycle/inter-agent control
tool (refresh / sleep / karma actions / nirvana / notification / dismiss).

Single source of manifest truth
--------------------------------
The ``system`` :class:`~lingtai_sdk.capabilities.BundleManifest` is **not**
redeclared here. It already exists in :mod:`lingtai_sdk.core_bundles`
(:func:`~lingtai_sdk.core_bundles.system_bundle`, stage 8) — ``required`` +
``privileged`` + ``native_only`` + ``NATIVE_ONLY`` + ``native`` transport, one
public ``system`` tool, bundle-level ``danger=destructive``. This module *reuses*
that manifest verbatim (``lifecycle_system_manifest()`` is a thin re-export) so
there is exactly one ``system`` manifest in the SDK, and adds two things on top:

1. a **per-action risk table** (:data:`SYSTEM_ACTION_RISK`) that grades each
   ``system`` action individually, because the single bundle-level
   ``destructive`` posture cannot faithfully express that ``sleep`` (self only,
   no authority) and a privileged ``nirvana`` (irreversible teardown) sit at
   opposite ends of the risk spectrum;
2. a **``system``-specific native host seam**
   (:func:`system_lifecycle_host`) — the privileged-native mirror of
   ``file_tool_host`` / ``file_mutation_tool_host``, wrapping
   :func:`~lingtai_sdk.core_bundles.native_core_host` so a host injecting *one*
   real ``system`` handler does not have to also supply ``psyche`` / ``soul``
   (which the all-three :func:`~lingtai_sdk.core_bundles.native_core_hosts`
   requires).

Why a per-action risk table, not a per-action manifest
------------------------------------------------------
The ``system`` tool is a single public tool with an ``action`` discriminator, not
one tool per action — so its danger cannot vary per-action at the *manifest*
level without inventing one bundle per action (which would fork the live tool
registration, exactly what this stage must not do). The conservative, faithful
encoding is therefore: keep the **bundle-level posture at its strongest action**
(``destructive`` — what the stage-17 guard bridge already derives), and ship the
**graded action table as metadata** so a host that wants finer-than-bundle
grading can read it *without* any live runtime gate. The grading mirrors the
authority the real kernel intrinsic already enforces in code
(``lingtai_kernel.intrinsics.system.karma._KARMA_ACTIONS`` /
``_NIRVANA_ACTIONS``); it is a *declaration of* that posture, never a second
gate.

What this module is NOT
-----------------------
Exactly as in stages 3A/3B/8, it does **not** migrate, move, rewrite, import, or
call the real ``system`` implementation. The real handler is a *kernel intrinsic*
(``lingtai_kernel.intrinsics.system.handle(agent, args)``), wired live by
``BaseAgent._wire_intrinsics``; importing it here would break SDK import-purity
(the SDK must not eagerly pull the kernel intrinsic surface) and is unnecessary —
this module ships *declarations + an injection seam* only:

    system manifest (core_bundles.system_bundle)
       -> system_lifecycle_host(handler)   # wrapper injects the real intrinsic handler
       -> host.invoke("system", **args)     # runs the kernel intrinsic's existing dispatch

The wrapper-side bridge that supplies that handler lives in
``lingtai.core.system_bundle`` (the wrapper *may* import the SDK and the kernel
intrinsic; the SDK must not import either). The tool **schema and behavior are
unchanged**: the bridge reuses the kernel intrinsic's existing ``handle`` /
``get_schema`` verbatim, and the live ``_wire_intrinsics`` registration path is
untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam, the high-state
mirror of the low-state file bundles.

See ``docs/sdk/architecture-foundation.md`` (stage 3C).
"""
from __future__ import annotations

from typing import Mapping

from .capabilities import BundleManifest, SecurityDanger
from .capability_host import NativeBundleHost, ToolHandler
from .core_bundles import is_core_manifest, native_core_host, system_bundle
from .errors import BundleHostError

#: The one public lifecycle tool this module is about.
SYSTEM_TOOL_NAME = "system"

#: Per-action danger grading for the single ``system`` tool's ``action``
#: discriminator. This is the conservative, faithful encoding of the risk the
#: real kernel intrinsic already enforces in code
#: (``intrinsics.system.karma._KARMA_ACTIONS`` / ``_NIRVANA_ACTIONS``):
#:
#: * **self / normal lifecycle** (``SAFE`` / ``CAUTION``) — no inter-agent
#:   authority. ``refresh`` reloads *self* config/MCP and restarts the loop;
#:   ``sleep`` puts *self* to sleep (explicitly "no karma needed"); ``presets``
#:   lists library presets (read-only); ``notification`` reads the live
#:   notification surface (read-only placeholder); ``dismiss`` clears one
#:   ``.notification/<channel>`` surface (a small, self-scoped side effect).
#: * **karma — privileged inter-agent control** (``DESTRUCTIVE``) —
#:   ``lull`` / ``suspend`` / ``cpr`` / ``interrupt`` / ``clear`` each act on
#:   *another* agent's runtime and require ``admin.karma=True``.
#: * **nirvana — irreversible teardown** (``DESTRUCTIVE``) — permanently
#:   ``shutil.rmtree``s another agent's working directory; requires
#:   ``admin.karma=True`` AND ``admin.nirvana=True``.
#:
#: ``refresh`` is graded ``CAUTION`` rather than ``SAFE``: it is self-only and
#: needs no authority, but it tears down and rebuilds the live MCP/config state
#: and restarts the loop — a real, lasting self side effect worth a second look,
#: not a pure read. The bundle-level posture (``destructive``) remains the
#: strongest action's grade; this table is the finer-grained declaration.
SYSTEM_ACTION_RISK: dict[str, SecurityDanger] = {
    # self / normal lifecycle — no inter-agent authority
    "refresh": SecurityDanger.CAUTION,
    "sleep": SecurityDanger.CAUTION,
    "presets": SecurityDanger.SAFE,
    "notification": SecurityDanger.SAFE,
    "dismiss": SecurityDanger.CAUTION,
    # karma — privileged inter-agent control (requires admin.karma)
    "lull": SecurityDanger.DESTRUCTIVE,
    "suspend": SecurityDanger.DESTRUCTIVE,
    "cpr": SecurityDanger.DESTRUCTIVE,
    "interrupt": SecurityDanger.DESTRUCTIVE,
    "clear": SecurityDanger.DESTRUCTIVE,
    # nirvana — irreversible teardown (requires admin.karma AND admin.nirvana)
    "nirvana": SecurityDanger.DESTRUCTIVE,
}

#: The karma-gated (privileged inter-agent) actions — a declaration mirroring
#: ``intrinsics.system.karma._KARMA_ACTIONS``. Acting on another agent's runtime
#: requires ``admin.karma=True`` at live dispatch (enforced by the kernel, not
#: here).
KARMA_ACTIONS: frozenset[str] = frozenset(
    {"lull", "suspend", "cpr", "interrupt", "clear"}
)

#: The nirvana-gated (irreversible teardown) actions — a declaration mirroring
#: ``intrinsics.system.karma._NIRVANA_ACTIONS``. Requires both
#: ``admin.karma=True`` and ``admin.nirvana=True`` at live dispatch.
NIRVANA_ACTIONS: frozenset[str] = frozenset({"nirvana"})

#: Self-scoped / normal-lifecycle actions — everything that is neither karma- nor
#: nirvana-gated. Self only, no inter-agent authority required.
SELF_ACTIONS: frozenset[str] = frozenset(SYSTEM_ACTION_RISK) - KARMA_ACTIONS - NIRVANA_ACTIONS


def lifecycle_system_manifest() -> BundleManifest:
    """The ``system`` lifecycle bundle manifest — re-exported from ``core_bundles``.

    There is exactly one ``system`` manifest in the SDK: the privileged,
    native-only core bundle declared by
    :func:`~lingtai_sdk.core_bundles.system_bundle` (stage 8). This module does
    **not** fork or rebuild it — it reuses it verbatim so the bundle-level
    ``danger=destructive`` posture and the ``required``/``privileged``/
    ``native_only`` role flags stay single-sourced. The per-action grading this
    module adds lives in :data:`SYSTEM_ACTION_RISK`, *alongside* (never replacing)
    the manifest's bundle-level posture.
    """
    return system_bundle()


def is_lifecycle_system_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``system`` lifecycle bundle by name.

    A thin, intention-revealing wrapper over the core-bundle identity check,
    narrowed to the single ``system`` surface this module owns.
    """
    return is_core_manifest(manifest) and manifest.name == SYSTEM_TOOL_NAME


def action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``system`` ``action``.

    Looks the action up in :data:`SYSTEM_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than
    silently treated as safe — the same fail-safe direction the guard bridge uses
    for an unrecognized danger string. This is a pure declaration helper; it gates
    nothing and never raises.
    """
    return SYSTEM_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def system_lifecycle_host(handler: ToolHandler) -> NativeBundleHost:
    """Build a native-authority host for the ``system`` bundle from an injected handler.

    The privileged-native, single-tool mirror of
    :func:`~lingtai_sdk.file_tools.file_tool_host` /
    :func:`~lingtai_sdk.file_mutation_tools.file_mutation_tool_host`. Given the
    single *supplied* ``system`` handler callable, returns a
    :class:`~lingtai_sdk.capability_host.NativeBundleHost` (built with
    ``native_authority=True`` by :func:`~lingtai_sdk.core_bundles.native_core_host`)
    hosting the one declared ``system`` tool.

    Unlike :func:`~lingtai_sdk.core_bundles.native_core_hosts` — which requires
    handlers for *all three* core bundles (``system`` / ``psyche`` / ``soul``)
    together — this seam hosts ``system`` alone, so the wrapper bridge can adopt
    the lifecycle surface incrementally without supplying the other two. The
    handler is whatever the wrapper bridge injects (the real kernel intrinsic
    ``system.handle`` bound to an agent); this shim never imports or calls the
    real implementation, and ``native_core_host`` enforces the
    manifest/handler/native-authority contract.

    Note the declared ``danger`` posture (bundle-level ``destructive`` plus the
    :data:`SYSTEM_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. Danger is a *declaration* the stage-17
    :mod:`lingtai_sdk.guard_bridge` reads to gate dispatch — a separate,
    not-installed seam — and the real per-action authority gate
    (karma / nirvana) lives in the kernel intrinsic, not in this host.
    """
    if not callable(handler):
        raise BundleHostError(
            f"system lifecycle bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return native_core_host(lifecycle_system_manifest(), handler)


def system_lifecycle_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, NativeBundleHost]:
    """Build ``{"system": NativeBundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`system_lifecycle_host`, parallel to
    :func:`~lingtai_sdk.file_tools.file_tool_hosts` /
    :func:`~lingtai_sdk.file_mutation_tools.file_mutation_tool_hosts`, so the
    wrapper bridge has the same ``{name: host}`` shape across all stages. The
    mapping must contain exactly the ``system`` handler — a missing ``system``
    handler or any handler for a non-``system`` name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial/typo'd wiring can
    never silently host the wrong surface.
    """
    expected = {SYSTEM_TOOL_NAME}
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for system lifecycle bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-system lifecycle bundle name(s): {sorted(extra)}"
        )
    return {SYSTEM_TOOL_NAME: system_lifecycle_host(handlers[SYSTEM_TOOL_NAME])}


__all__ = [
    "SYSTEM_TOOL_NAME",
    "SYSTEM_ACTION_RISK",
    "KARMA_ACTIONS",
    "NIRVANA_ACTIONS",
    "SELF_ACTIONS",
    "lifecycle_system_manifest",
    "is_lifecycle_system_manifest",
    "action_risk",
    "system_lifecycle_host",
    "system_lifecycle_hosts",
]
