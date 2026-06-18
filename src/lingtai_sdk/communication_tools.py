"""High-state communication/execution tool bundle declarations + host seams.

The **communication/execution** counterpart of the lifecycle ``system`` bundle
(stage 3C). Where :mod:`lingtai_sdk.lifecycle_tools` is the declare-and-inject
seam for the privileged, native-only ``system`` lifecycle surface, this module is
the seam for the next two high-state surfaces — the ones with **external or
process side effects**:

* ``email`` — the agent's communication surface. A **kernel intrinsic**
  (``lingtai_kernel.intrinsics.email.handle``), wired live by
  ``BaseAgent._wire_intrinsics`` exactly like ``system``. It can send mail to
  *external* SMTP/IMAP recipients (a real outside-world side effect) as well as
  deliver internally between agents. Native-carried and privileged (it touches
  the agent's mailbox / notification state and the configured mail service), but
  **not** ``native_only`` — an external backend could re-implement a mail
  surface — so it is hosted by a :class:`~lingtai_sdk.capability_host.NativeBundleHost`,
  mirroring how the live intrinsic path carries it.
* ``daemon`` — the agent's execution surface (神識 / 分神). A **wrapper
  capability** (``lingtai.core.daemon.DaemonManager.handle``), wired live by its
  ``setup()`` through ``agent.add_tool`` — the *same non-native, in-process
  registration path the file tools use*, not an intrinsic. It spawns and kills
  child processes / CLI subagents and runs long child-agent executions, so it is
  the highest-danger in-process surface; hosted by a non-native
  :class:`~lingtai_sdk.capability_host.BundleHost`, mirroring how the live
  capability path carries it.

This split is deliberate and faithful to the live wiring: ``email`` rides the
**native intrinsic** path (like ``system``), so it gets the native host seam;
``daemon`` rides the **in-process capability** path (like ``write`` / ``edit``),
so it gets the in-process host seam. Each surface's declared host therefore
matches the mechanism the kernel/wrapper already uses to carry it.

Per-action risk tables, not per-action manifests
-------------------------------------------------
Both surfaces are a single public tool with an ``action`` discriminator (one tool
per action would fork the live registration — exactly what this stage must not
do). So, exactly as stage 3C did for ``system``, the bundle keeps a single
**bundle-level danger at its strongest action** and ships a finer-grained,
graded **action risk table** as metadata, a *declaration* a host may read without
any live runtime gate:

* :data:`EMAIL_ACTION_RISK` grades the 13 ``email`` actions: read-only inbox
  queries (``check`` / ``read`` / ``search`` / ``contacts``) are ``SAFE``; the
  contact-book / dismiss / organize mutations are ``CAUTION``; ``send`` /
  ``reply`` / ``reply_all`` are ``CAUTION`` because they can dispatch to an
  *external* recipient; ``delete`` is ``DESTRUCTIVE`` (irreversible mailbox
  removal). Bundle-level: ``CAUTION`` is the typical posture; ``delete`` lifts
  the bundle to ``DESTRUCTIVE``.
* :data:`DAEMON_ACTION_RISK` grades the 5 ``daemon`` actions: ``list`` /
  ``check`` are read-only (``SAFE``); ``emanate`` / ``ask`` spawn or drive child
  processes / subagents (``DESTRUCTIVE`` — external process side effect, long
  execution); ``reclaim`` kills running emanations / CLI process groups
  (``DESTRUCTIVE``). Bundle-level: ``DESTRUCTIVE``.

What this module is NOT
-----------------------
Exactly as in stages 3A/3B/3C, it does **not** migrate, move, rewrite, import, or
call the real ``email`` / ``daemon`` implementations. The real ``email`` handler
is a *kernel intrinsic*; the real ``daemon`` handler is a wrapper
``DaemonManager`` method built at ``setup()``. Importing either here would break
SDK import-purity (the SDK must not eagerly pull the kernel intrinsic surface or
a wrapper service) and is unnecessary — this module ships *declarations + an
injection seam* only:

    email manifest    -> email_comm_host(handler)   # wrapper injects the real intrinsic handler
    daemon manifest   -> daemon_exec_host(handler)   # wrapper injects the real DaemonManager.handle
       -> host.invoke(name, **args)                  # runs the wrapper/kernel's existing dispatch

The wrapper-side bridge that supplies those handlers lives in
``lingtai.core.communication_bundle`` (the wrapper *may* import the SDK and the
kernel intrinsic; the SDK must not import either). The tool **schemas and
behavior are unchanged**: the bridge reuses the kernel intrinsic's ``handle`` and
the wrapper capability's ``make_handler`` verbatim, and the live registration
paths are untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + injection seams, the high-state
communication/execution mirror of the lifecycle bundle.

See ``docs/sdk/architecture-foundation.md`` (stage 3D).
"""
from __future__ import annotations

from typing import Any, Mapping

from .capabilities import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityDanger,
    SecurityPolicy,
    TransportKind,
    TransportSpec,
)
from .capability_host import BundleHost, NativeBundleHost, ToolHandler
from .errors import BundleHostError

#: The two public tool names this module is about.
EMAIL_TOOL_NAME = "email"
DAEMON_TOOL_NAME = "daemon"

# --- declared argument schemas (structural copies, descriptions i18n'd live) --

# Language-neutral copy of the shape returned by
# ``lingtai_kernel.intrinsics.email.schema.get_schema``. The wrapper's own
# ``get_schema(lang)`` remains the registration path; this copy lives in the
# manifest metadata so a host inspecting the manifest can see the argument
# contract without importing the kernel intrinsic. Descriptions are i18n'd at
# registration time and intentionally omitted here.
_EMAIL_ACTIONS: tuple[str, ...] = (
    "send",
    "check",
    "read",
    "dismiss",
    "reply",
    "reply_all",
    "search",
    "archive",
    "delete",
    "contacts",
    "add_contact",
    "remove_contact",
    "edit_contact",
)

_EMAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_EMAIL_ACTIONS)},
        "address": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "email_id": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action"],
}

# Language-neutral copy of the shape returned by
# ``lingtai.core.daemon.get_schema``. Same rationale as ``_EMAIL_SCHEMA``.
_DAEMON_ACTIONS: tuple[str, ...] = (
    "emanate",
    "list",
    "ask",
    "check",
    "reclaim",
)

_DAEMON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_DAEMON_ACTIONS)},
        "tasks": {"type": "array", "items": {"type": "object"}},
        "id": {"type": "string"},
        "message": {"type": "string"},
        "backend": {"type": "string"},
    },
    "required": ["action"],
}


# --- per-action risk tables (the heart of stage 3D) --------------------------

#: Per-action danger grading for the single ``email`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what each action does:
#:
#: * **read-only inbox queries** (``SAFE``) — ``check`` (list inbox/sent/archive
#:   summaries), ``read`` (fetch bodies by id), ``search`` (server-side regex),
#:   ``contacts`` (list the contact book). No mutation, no outbound send.
#: * **self-scoped mailbox / contact mutations** (``CAUTION``) — ``dismiss``
#:   (mark read), ``archive`` (move inbox→archive), ``add_contact`` /
#:   ``remove_contact`` / ``edit_contact`` (mutate the contact book). Real but
#:   bounded, reversible-ish, local side effects.
#: * **outbound sends** (``CAUTION``) — ``send`` / ``reply`` / ``reply_all`` can
#:   dispatch to an **external** SMTP/IMAP recipient (or deliver internally). An
#:   outside-world side effect worth a second look, but not irreversible local
#:   data loss.
#: * **irreversible removal** (``DESTRUCTIVE``) — ``delete`` permanently removes
#:   an email from the mailbox; the strongest action, which lifts the bundle-level
#:   posture to ``destructive``.
EMAIL_ACTION_RISK: dict[str, SecurityDanger] = {
    # read-only inbox queries
    "check": SecurityDanger.SAFE,
    "read": SecurityDanger.SAFE,
    "search": SecurityDanger.SAFE,
    "contacts": SecurityDanger.SAFE,
    # self-scoped mailbox / contact mutations
    "dismiss": SecurityDanger.CAUTION,
    "archive": SecurityDanger.CAUTION,
    "add_contact": SecurityDanger.CAUTION,
    "remove_contact": SecurityDanger.CAUTION,
    "edit_contact": SecurityDanger.CAUTION,
    # outbound sends — can reach an EXTERNAL recipient
    "send": SecurityDanger.CAUTION,
    "reply": SecurityDanger.CAUTION,
    "reply_all": SecurityDanger.CAUTION,
    # irreversible mailbox removal
    "delete": SecurityDanger.DESTRUCTIVE,
}

#: The ``email`` actions that can dispatch to an **external** recipient — a
#: declaration of the outside-world reach of the bundle (the live external/
#: internal routing is decided by ``intrinsics.email`` at dispatch via the
#: configured mail service / self-send detection, not here).
EMAIL_SEND_ACTIONS: frozenset[str] = frozenset({"send", "reply", "reply_all"})

#: Per-action danger grading for the single ``daemon`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what each action does:
#:
#: * **read-only status queries** (``SAFE``) — ``list`` (query running/completed
#:   emanations), ``check`` (inspect one emanation's recent run-dir events). No
#:   process spawn, no kill.
#: * **process spawn / drive** (``DESTRUCTIVE``) — ``emanate`` dispatches one or
#:   more child subagents (in-process LLM loops, or external CLI subprocesses for
#:   the CLI backends) and ``ask`` sends a follow-up to a running CLI-backed
#:   emanation. Both can spawn child processes and run long child-agent
#:   executions — a real external/process side effect.
#: * **process kill** (``DESTRUCTIVE``) — ``reclaim`` cancels all running
#:   emanations and force-kills their CLI process groups.
#:
#: Unlike ``email`` (where the common posture is ``CAUTION``), the bundle-level
#: ``daemon`` posture is ``DESTRUCTIVE``: spawning and killing child processes is
#: its defining capability.
DAEMON_ACTION_RISK: dict[str, SecurityDanger] = {
    # read-only status queries
    "list": SecurityDanger.SAFE,
    "check": SecurityDanger.SAFE,
    # process spawn / drive — child processes, long child-agent execution
    "emanate": SecurityDanger.DESTRUCTIVE,
    "ask": SecurityDanger.DESTRUCTIVE,
    # process kill
    "reclaim": SecurityDanger.DESTRUCTIVE,
}

#: The ``daemon`` actions that spawn / drive / kill child processes or subagents
#: — a declaration of the process side effects of the bundle (the live spawn/kill
#: is the ``DaemonManager``'s, not here).
DAEMON_PROCESS_ACTIONS: frozenset[str] = frozenset(
    {"emanate", "ask", "reclaim"}
)


def _strongest(grades: Mapping[str, SecurityDanger]) -> SecurityDanger:
    """Return the strongest (highest-risk) danger grade in *grades*.

    The bundle-level posture is the strongest of its per-action grades, so a
    single bundle danger can never under-state any one action. Ordering is
    SAFE < CAUTION < DESTRUCTIVE.
    """
    order = {
        SecurityDanger.SAFE: 0,
        SecurityDanger.CAUTION: 1,
        SecurityDanger.DESTRUCTIVE: 2,
    }
    return max(grades.values(), key=lambda d: order[d])


def _comm_native_manifest(
    name: str,
    *,
    summary: str,
    danger: SecurityDanger,
    schema: Mapping[str, Any],
    actions: tuple[str, ...],
    role: str,
) -> BundleManifest:
    """Build a high-state **native** (intrinsic-carried) communication manifest.

    The native, privileged-but-not-``native_only`` posture for a kernel-intrinsic
    surface that has external reach (``email``): ``privileged=True``,
    ``native_only=False`` (a backend could re-implement a mail surface, so it is
    ``AUGMENTABLE`` rather than ``NATIVE_ONLY``), carried over the ``native``
    transport — so it is hosted by a :class:`NativeBundleHost`, mirroring how the
    live ``_wire_intrinsics`` path carries it. Declares exactly one public tool
    whose name equals the bundle ``name``. The metadata is non-secret description
    only — no handler, no implementation.
    """
    return BundleManifest(
        name=name,
        version="0.0.1",
        summary=summary,
        roles=RoleFlags(
            required=True,
            privileged=True,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.AUGMENTABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(name,)),
        security=SecurityPolicy(danger=danger.value),
        transport=TransportSpec(kind=TransportKind.NATIVE.value),
        metadata={
            "communication": True,
            "intrinsic": True,
            "role": role,
            "actions": list(actions),
            "schema": dict(schema),
        },
    )


def _exec_in_process_manifest(
    name: str,
    *,
    summary: str,
    danger: SecurityDanger,
    schema: Mapping[str, Any],
    actions: tuple[str, ...],
    role: str,
) -> BundleManifest:
    """Build a high-state **in-process** (capability-carried) execution manifest.

    The non-privileged, ``in_process`` posture for a wrapper capability registered
    via ``agent.add_tool`` (``daemon``) — the *same* host mechanism the file tools
    use, so it is hosted by a non-native :class:`BundleHost`, mirroring how the
    live ``setup()`` path carries it. It is non-privileged (it is not a
    kernel-protected intrinsic) and freely ``REPLACEABLE`` in principle, but
    declares the strongest danger posture (``DESTRUCTIVE``) because it spawns and
    kills child processes. Declares exactly one public tool whose name equals the
    bundle ``name``. The metadata is non-secret description only.
    """
    return BundleManifest(
        name=name,
        version="0.0.1",
        summary=summary,
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(name,)),
        security=SecurityPolicy(danger=danger.value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "execution": True,
            "side_effect": True,
            "process_spawning": True,
            "role": role,
            "actions": list(actions),
            "schema": dict(schema),
        },
    )


def email_comm_manifest() -> BundleManifest:
    """The ``email`` communication bundle manifest — the agent's mail surface.

    Declares the public ``email`` tool: send / reply (incl. to **external**
    SMTP/IMAP recipients), inbox check / read / search, archive / delete /
    dismiss, and contact-book management. Native-carried (it is the kernel
    intrinsic the live ``_wire_intrinsics`` path dispatches) and privileged (it
    touches the agent's mailbox / notification state and the configured mail
    service), but not ``native_only``. The bundle-level posture is
    ``destructive`` — the strongest action's grade (``delete``); the finer
    per-action grading lives in :data:`EMAIL_ACTION_RISK`. **Manifest only** —
    the real handler (the kernel intrinsic ``email.handle`` bound to an agent) is
    injected by the wrapper bridge.
    """
    return _comm_native_manifest(
        EMAIL_TOOL_NAME,
        summary="Send/reply mail (internal or external), check/read/search the "
        "inbox, archive/delete, and manage contacts.",
        danger=_strongest(EMAIL_ACTION_RISK),
        schema=_EMAIL_SCHEMA,
        actions=_EMAIL_ACTIONS,
        role="The agent's communication surface.",
    )


def daemon_exec_manifest() -> BundleManifest:
    """The ``daemon`` execution bundle manifest — the agent's subagent surface.

    Declares the public ``daemon`` tool (神識): dispatch ephemeral child subagents
    (分神) in parallel — in-process LLM loops or external CLI subprocesses — query
    their status, drive a running CLI emanation, and reclaim (kill) running
    emanations / CLI process groups. Carried ``in_process`` via the wrapper
    capability ``setup()`` path (the same mechanism the file tools use), so it is
    non-privileged. The bundle-level posture is ``destructive`` (it spawns and
    kills child processes and runs long child-agent executions); the finer
    per-action grading lives in :data:`DAEMON_ACTION_RISK`. **Manifest only** —
    the real handler (``DaemonManager.handle`` bound to an agent) is injected by
    the wrapper bridge.
    """
    return _exec_in_process_manifest(
        DAEMON_TOOL_NAME,
        summary="Dispatch ephemeral child subagents (emanations) in parallel, "
        "query their status, drive them, and reclaim (kill) them.",
        danger=_strongest(DAEMON_ACTION_RISK),
        schema=_DAEMON_SCHEMA,
        actions=_DAEMON_ACTIONS,
        role="The agent's parallel-subagent execution surface.",
    )


# Stable, canonical order for the two communication/execution bundles.
_COMM_BUILDERS = (email_comm_manifest, daemon_exec_manifest)


def communication_tool_manifests() -> tuple[BundleManifest, ...]:
    """The two communication/execution manifests in stable order: email, daemon."""
    return tuple(builder() for builder in _COMM_BUILDERS)


def communication_tool_names() -> tuple[str, ...]:
    """The two tool names in stable order: ``("email", "daemon")``."""
    return tuple(m.name for m in communication_tool_manifests())


def is_email_comm_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``email`` communication bundle by name."""
    return manifest.name == EMAIL_TOOL_NAME


def is_daemon_exec_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``daemon`` execution bundle by name."""
    return manifest.name == DAEMON_TOOL_NAME


def email_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for an ``email`` ``action``.

    Looks the action up in :data:`EMAIL_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than
    silently treated as safe — the same fail-safe direction the guard bridge and
    the ``system`` lifecycle table use. Pure declaration helper; gates nothing,
    never raises.
    """
    return EMAIL_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def daemon_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``daemon`` ``action``.

    Looks the action up in :data:`DAEMON_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE`. Pure declaration
    helper; gates nothing, never raises.
    """
    return DAEMON_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def email_comm_host(handler: ToolHandler) -> NativeBundleHost:
    """Build a native-authority host for the ``email`` bundle from an injected handler.

    The communication mirror of
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_host`: ``email`` is a
    native-carried kernel intrinsic, so its host is a
    :class:`~lingtai_sdk.capability_host.NativeBundleHost` built with
    ``native_authority=True``. Given the single *supplied* ``email`` handler
    callable (the real kernel intrinsic ``email.handle`` bound to an agent, which
    the wrapper bridge injects), returns a host of the one declared ``email``
    tool. This shim never imports or calls the real implementation.

    The declared ``danger`` posture (bundle-level ``destructive`` plus the
    :data:`EMAIL_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. Danger is a *declaration* the stage-17
    :mod:`lingtai_sdk.guard_bridge` reads to gate dispatch — a separate,
    not-installed seam — and the live internal/external mail routing is the kernel
    intrinsic's, not this host's.
    """
    if not callable(handler):
        raise BundleHostError(
            f"email communication bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return NativeBundleHost(
        email_comm_manifest(), {EMAIL_TOOL_NAME: handler}, native_authority=True
    )


def daemon_exec_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``daemon`` bundle from an injected handler.

    The execution mirror of
    :func:`~lingtai_sdk.file_mutation_tools.file_mutation_tool_host`: ``daemon``
    is an in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``daemon`` handler callable (the real ``DaemonManager.handle`` bound to an
    agent, which the wrapper bridge injects), returns a host of the one declared
    ``daemon`` tool. This shim never imports or calls the real implementation, and
    constructing the host neither spawns nor kills any process — only an explicit
    ``host.invoke("daemon", action="emanate", ...)`` would, which the wrapper
    bridge gates exactly as the live path does.

    The declared ``danger`` posture (bundle-level ``destructive`` plus the
    :data:`DAEMON_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. ``BundleHost`` enforces the non-privileged /
    ``in_process`` contract; danger gating is the stage-17 guard bridge's job.
    """
    if not callable(handler):
        raise BundleHostError(
            f"daemon execution bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(daemon_exec_manifest(), {DAEMON_TOOL_NAME: handler})


def communication_tool_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost | NativeBundleHost]:
    """Build ``{name: host}`` for the communication/execution bundles.

    The mapping mirror of the per-bundle host seams, parallel to
    :func:`~lingtai_sdk.file_tools.file_tool_hosts` /
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_hosts`, so the wrapper
    bridge has the same ``{name: host}`` shape across all stages. ``handlers``
    must contain exactly the ``email`` and ``daemon`` handlers — a missing handler
    or any handler for a non-communication name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial / typo'd wiring can
    never silently host the wrong surface. ``email`` is hosted natively and
    ``daemon`` in-process, each matching its live carrier.
    """
    expected = set(communication_tool_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for communication bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-communication bundle name(s): {sorted(extra)}"
        )
    return {
        EMAIL_TOOL_NAME: email_comm_host(handlers[EMAIL_TOOL_NAME]),
        DAEMON_TOOL_NAME: daemon_exec_host(handlers[DAEMON_TOOL_NAME]),
    }


__all__ = [
    "EMAIL_TOOL_NAME",
    "DAEMON_TOOL_NAME",
    "EMAIL_ACTION_RISK",
    "EMAIL_SEND_ACTIONS",
    "DAEMON_ACTION_RISK",
    "DAEMON_PROCESS_ACTIONS",
    "email_comm_manifest",
    "daemon_exec_manifest",
    "communication_tool_manifests",
    "communication_tool_names",
    "is_email_comm_manifest",
    "is_daemon_exec_manifest",
    "email_action_risk",
    "daemon_action_risk",
    "email_comm_host",
    "daemon_exec_host",
    "communication_tool_hosts",
]
