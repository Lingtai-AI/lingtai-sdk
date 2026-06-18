"""The ``bash`` shell-execution bundle declaration + an in-process host seam (stage 3H).

The **arbitrary-shell-execution** counterpart of the ``daemon`` execution bundle
(stage 3D). Where :mod:`lingtai_sdk.communication_tools` declares ``daemon`` (which
spawns and kills *child agents*), this module declares the single highest-danger
host-process surface — ``bash`` — which runs **arbitrary shell commands** in the
agent's working-directory sandbox and manages background (async) jobs.

Carrier and host class — consistent with the live wiring
--------------------------------------------------------
``bash`` is a **wrapper capability** registered live by its ``setup()`` through
``agent.add_tool`` — the *same* non-native, in-process registration path the file
tools, ``daemon``, ``mcp``, and ``knowledge`` use, **not** a kernel intrinsic. So,
like ``daemon``, it declares ``in_process`` transport + ``privileged=False`` and is
hosted by the non-native :class:`~lingtai_sdk.capability_host.BundleHost`,
mirroring how the live ``setup()`` path carries it. Unlike the read-only catalog
surfaces (``mcp`` / ``knowledge``), it carries the **strongest** danger posture
because running a shell command is, by construction, arbitrary host-side execution.

Single tool, three actions — a per-action risk table
----------------------------------------------------
``bash`` is a single public tool with an ``action`` discriminator whose live enum
is **``run`` / ``poll`` / ``cancel``** (see ``lingtai.core.bash.get_schema``). As
with the other stage-3 bundles, the action grades ship as a small
:data:`BASH_ACTION_RISK` table plus a :func:`bash_action_risk` helper that fails
safe *high* (an unknown action grades ``DESTRUCTIVE``, never silently ``SAFE``) — a
declaration the stage-17 guard bridge may read, never a second gate. The
conservative, faithful encoding of what each action does:

* ``run`` (``DESTRUCTIVE``) — executes an **arbitrary shell command** (sync or, with
  ``async=true``, in a background process group). The defining capability and the
  highest-danger action: it can run anything the host shell can, bounded only by
  the agent's :class:`~lingtai.core.bash.BashPolicy` (which this declaration does
  **not** model — policy is the live handler's concern).
* ``poll`` (``CAUTION``) — checks an async job's status. Mostly read-only, but on
  job completion it reads the captured output **and removes the job directory**
  (``shutil.rmtree``) and closes the held file handles — a bounded local side
  effect, so ``CAUTION`` rather than ``SAFE``.
* ``cancel`` (``DESTRUCTIVE``) — sends ``SIGTERM`` to the job's process group
  (escalating to ``SIGKILL``) and removes the job directory. A process-kill side
  effect, graded ``DESTRUCTIVE`` like ``daemon``'s ``reclaim``.

The bundle-level posture equals the strongest action's grade: ``DESTRUCTIVE`` (the
same posture as ``daemon``), faithful to ``bash`` being arbitrary host execution.

What this module is NOT
-----------------------
Exactly as in the prior stages, it does **not** migrate, move, rewrite, import, or
call the real ``bash`` implementation. The real handler is the ``handle`` method of
a :class:`~lingtai.core.bash.BashManager` built by
``lingtai.core.bash.make_handler(agent)`` (bound to a live ``agent`` and a resolved
policy); importing it here would break SDK import-purity (the SDK must not eagerly
pull the wrapper) and is unnecessary — this module ships *declarations + an
injection seam* only:

    bash manifest -> bash_exec_host(handler)   # wrapper injects make_handler(agent)
       -> host.invoke("bash", **args)          # runs the wrapper capability's dispatch

The wrapper-side bridge that supplies the handler lives in
``lingtai.core.bash_bundle`` (the wrapper *may* import the SDK and the wrapper
capability; the SDK must not import either). The tool **schema and behavior are
unchanged**: the bridge reuses ``bash.make_handler`` verbatim, and the live
``setup()`` registration path is untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam, the
arbitrary-shell-execution mirror of the prior bundles.
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
from .capability_host import BundleHost, ToolHandler
from .errors import BundleHostError

#: The one public tool this module is about.
BASH_TOOL_NAME = "bash"

# --- declared argument schema (structural copy, descriptions i18n'd live) -----

# Language-neutral copy of the action enum returned by
# ``lingtai.core.bash.get_schema``. The wrapper's own ``get_schema(lang)`` remains
# the registration path; this copy lives in the manifest metadata so a host
# inspecting the manifest can see the argument contract without importing the
# wrapper capability. Descriptions are i18n'd at registration time and
# intentionally omitted here.
_BASH_ACTIONS: tuple[str, ...] = ("run", "poll", "cancel")

_BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_BASH_ACTIONS)},
        "command": {"type": "string"},
        "timeout": {"type": "number"},
        "working_dir": {"type": "string"},
        "async": {"type": "boolean"},
        "job_id": {"type": "string"},
    },
    # The live schema requires nothing at the top level (command is required only
    # for action=run, job_id only for poll/cancel — enforced by the handler).
    "required": [],
}


# --- per-action risk table ----------------------------------------------------

#: Per-action danger grading for the single ``bash`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what each action does:
#:
#: * **arbitrary shell execution** (``DESTRUCTIVE``) — ``run`` executes an arbitrary
#:   shell command (sync or, with ``async=true``, in a background process group).
#:   The defining, highest-danger capability — it can do anything the host shell
#:   can, bounded only by the live ``BashPolicy`` (not modelled here).
#: * **job status check with cleanup side effect** (``CAUTION``) — ``poll`` reads an
#:   async job's status; on completion it reads the output and removes the job
#:   directory (and closes file handles). A bounded local side effect, so
#:   ``CAUTION`` rather than ``SAFE``.
#: * **process kill** (``DESTRUCTIVE``) — ``cancel`` sends ``SIGTERM`` (escalating to
#:   ``SIGKILL``) to the job's process group and removes the job directory.
#:
#: The bundle-level posture equals the strongest action's grade: ``DESTRUCTIVE``
#: (the same posture as ``daemon``) — arbitrary host execution is its defining
#: capability.
BASH_ACTION_RISK: dict[str, SecurityDanger] = {
    # arbitrary shell execution — sync or background process group
    "run": SecurityDanger.DESTRUCTIVE,
    # job status check — read-only except the on-completion cleanup
    "poll": SecurityDanger.CAUTION,
    # process kill — SIGTERM/SIGKILL to the job's process group
    "cancel": SecurityDanger.DESTRUCTIVE,
}

#: The ``bash`` actions that execute commands or kill processes — a declaration of
#: the host-process side effects of the bundle (the live execution / signalling is
#: the ``BashManager``'s, not here).
BASH_PROCESS_ACTIONS: frozenset[str] = frozenset({"run", "cancel"})


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


def bash_exec_manifest() -> BundleManifest:
    """The ``bash`` execution bundle manifest — the agent's shell surface.

    Declares the single public ``bash`` tool (actions ``run`` / ``poll`` /
    ``cancel``): run an arbitrary shell command (sync or async in a background
    process group), poll a background job's status, and cancel (kill) a running
    job's process group. Carried ``in_process`` via the wrapper capability
    ``setup()`` path (the same mechanism the file tools and ``daemon`` use), so it
    is **non-privileged** and freely ``REPLACEABLE``. The bundle-level posture is
    ``destructive`` (the strongest action — ``run`` executes arbitrary host
    commands); the finer per-action grading lives in :data:`BASH_ACTION_RISK`. The
    metadata is non-secret description only. **Manifest only** — the real handler
    (``bash.make_handler`` bound to an agent + a resolved policy) is injected by the
    wrapper bridge.
    """
    return BundleManifest(
        name=BASH_TOOL_NAME,
        version="0.0.1",
        summary="Run an arbitrary shell command (sync or async), poll a background "
        "job's status, and cancel (kill) a running job's process group.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(BASH_TOOL_NAME,)),
        security=SecurityPolicy(danger=_strongest(BASH_ACTION_RISK).value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "execution": True,
            "side_effect": True,
            "shell": True,
            "arbitrary_command": True,
            "process_spawning": True,
            "manages_async_jobs": True,
            "role": "The agent's arbitrary shell-command execution surface.",
            "actions": list(_BASH_ACTIONS),
            "schema": dict(_BASH_SCHEMA),
        },
    )


def bash_exec_manifests() -> tuple[BundleManifest, ...]:
    """The bash manifests in stable order: just ``("bash",)`` for now."""
    return (bash_exec_manifest(),)


def bash_exec_names() -> tuple[str, ...]:
    """The bash tool names in stable order: ``("bash",)``."""
    return tuple(m.name for m in bash_exec_manifests())


def is_bash_exec_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``bash`` execution bundle by name."""
    return manifest.name == BASH_TOOL_NAME


def bash_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``bash`` ``action``.

    Looks the action up in :data:`BASH_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than silently
    treated as safe — the same fail-safe-*high* direction the other stage-3 risk
    tables use (``daemon``, ``mcp``, ``system``, ``email``, and ``knowledge``
    unknown actions also grade as ``DESTRUCTIVE``). Pure declaration helper; gates
    nothing, never raises.
    """
    return BASH_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def bash_exec_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``bash`` bundle from an injected handler.

    The execution mirror of :func:`~lingtai_sdk.communication_tools.daemon_exec_host`:
    ``bash`` is an in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``bash`` handler callable (the real ``BashManager.handle`` from
    ``bash.make_handler(agent)``, which the wrapper bridge injects), returns a host
    of the one declared ``bash`` tool. This shim never imports or calls the real
    implementation, and constructing the host runs no command and starts no job —
    only an explicit ``host.invoke("bash", action="run", command=...)`` would,
    which the wrapper bridge gates exactly as the live path does.

    The declared ``danger`` posture (bundle-level ``destructive`` plus the
    :data:`BASH_ACTION_RISK` grading) is **not** enforced here: a host runs whatever
    handler it is given. ``BundleHost`` enforces the non-privileged / ``in_process``
    contract; danger gating is the stage-17 guard bridge's job, and command
    allow/deny is the live ``BashPolicy``'s, not this host's.
    """
    if not callable(handler):
        raise BundleHostError(
            f"bash execution bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(bash_exec_manifest(), {BASH_TOOL_NAME: handler})


def bash_exec_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{"bash": BundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`bash_exec_host`, parallel to
    :func:`~lingtai_sdk.communication_tools.communication_tool_hosts` /
    :func:`~lingtai_sdk.knowledge_tools.knowledge_catalog_hosts`, so the wrapper
    bridge has the same ``{name: host}`` shape across all stages. The mapping must
    contain exactly the ``bash`` handler — a missing ``bash`` handler or any handler
    for a non-``bash`` name raises :class:`~lingtai_sdk.errors.BundleHostError`, so a
    partial/typo'd wiring can never silently host the wrong surface.
    """
    expected = set(bash_exec_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for bash execution bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-bash execution bundle name(s): {sorted(extra)}"
        )
    return {BASH_TOOL_NAME: bash_exec_host(handlers[BASH_TOOL_NAME])}


__all__ = [
    "BASH_TOOL_NAME",
    "BASH_ACTION_RISK",
    "BASH_PROCESS_ACTIONS",
    "bash_exec_manifest",
    "bash_exec_manifests",
    "bash_exec_names",
    "is_bash_exec_manifest",
    "bash_action_risk",
    "bash_exec_host",
    "bash_exec_hosts",
]
