"""Side-effecting file-mutation tool bundle declarations: ``write`` / ``edit``.

The **side-effect counterpart** of :mod:`lingtai_sdk.file_tools` (stage 3A). Where
``file_tools`` declares the three *read-only / query* file tools (``read`` /
``glob`` / ``grep``) — all uniformly ``SecurityDanger.SAFE`` — this module
declares the two low-state file tools that *mutate the filesystem*, ``write`` and
``edit``, as :class:`BundleManifest` objects and offers the same thin
declare-and-inject host seam through the non-native
:class:`~lingtai_sdk.capability_host.BundleHost`.

It lives in its own module (rather than extending ``file_tools``) precisely
because ``file_tools`` carries an invariant this module must break: *every*
``file_tools`` manifest is read-only / ``SAFE``. Write and edit are
side-effecting, and — unlike the read tools — they do **not** share a single
danger posture, so they cannot flow through ``file_tools``'s shared
all-``SAFE`` ``_file_tool_manifest`` helper. Keeping them apart preserves
``file_tools``'s "all SAFE" guarantee and gives the side-effect posture a clean,
explicit home.

What this module is NOT
-----------------------
Exactly as in ``file_tools``, it does **not** migrate, move, rewrite, import, or
call the real ``write`` / ``edit`` implementations. Those bind to wrapper-owned
services (``agent._file_io``, ``agent._working_dir``) at ``setup()`` time and
stay in the wrapper; importing them here would break SDK import-purity (the SDK
must not eagerly pull a wrapper service) and the dependency direction (the kernel
must never import the SDK). So this module ships *declarations + an injection
seam* only:

    declared manifest (write/edit)
       -> file_mutation_tool_hosts({name: handler})  # wrapper injects its real handler
       -> host.invoke(name, **args)                  # runs the wrapper's existing logic

The wrapper-side bridge that supplies those handlers lives in
``lingtai.core.file_bundle`` (the wrapper *may* import the SDK; the SDK must not
import the wrapper). The tool **schemas and behavior are unchanged**: the bridge
reuses the existing ``lingtai.core.{write,edit}.make_handler`` /
``get_schema`` handlers verbatim; this module only embeds a copy of each schema
in the manifest metadata as a declaration.

The two manifests share most of their posture — non-privileged, freely
``REPLACEABLE``, ``in_process`` transport — and each declares exactly one public
tool named after the file tool (``write`` / ``edit``). They differ in their
declared **danger posture**, which is the heart of this module:

* ``write`` creates a file **or silently overwrites an existing one wholesale**
  — an irreversible clobber of prior content — so it declares
  ``SecurityDanger.DESTRUCTIVE`` (the same posture the privileged ``system``
  bundle uses for its irreversible-teardown actions). Under the stage-17
  :mod:`lingtai_sdk.guard_bridge`, a ``destructive`` tool is **denied** in the
  default ``BLOCKING`` policy mode and **allowed-with-warning** in ``ADVISORY``
  mode.
* ``edit`` performs a **bounded, in-place string replacement** in an *existing*
  file and refuses ambiguous edits (``old_string`` not found, or found more than
  once without ``replace_all``). It is a real side effect with lasting effects,
  but not a wholesale clobber, so it declares ``SecurityDanger.CAUTION`` (the
  same posture ``psyche`` / ``soul`` use for lasting-but-not-destructive
  actions). Under the guard bridge, a ``caution`` tool is **allowed-with-warning**
  in both policy modes.

This posture is a *declaration only*. This stage wires **nothing** into live tool
dispatch: ``setup()`` remains the live registration path and is unchanged, and no
guard is installed on any ``Agent``. The guard semantics above are what the
existing stage-17 :mod:`lingtai_sdk.guard_bridge` would derive from these
manifests *if* a later stage chose to install it — which this stage asserts as an
invariant (see ``tests/test_sdk_file_mutation_tools.py``) without changing any
runtime behavior.

See ``docs/sdk/architecture-foundation.md`` (stage 3B).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .contracts import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityDanger,
    SecurityPolicy,
    TransportKind,
    TransportSpec,
)
from .host import BundleHost, ToolHandler
from ..errors import BundleHostError

# The JSON-schema *declarations* for the two file-mutation tools — a
# language-neutral copy of the shapes returned by
# ``lingtai.core.{write,edit}.get_schema`` (descriptions are i18n'd at
# registration time in the wrapper, so they are intentionally omitted here — the
# manifest carries the structural contract, not the localized prose). The
# wrapper bridge does NOT read these to register tools; the wrapper's own
# ``get_schema(lang)`` remains the registration path. They live here so a host
# inspecting the manifest can see each tool's argument contract without importing
# the wrapper.
_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["file_path", "content"],
}

_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean", "default": False},
    },
    "required": ["file_path", "old_string", "new_string"],
}


def _file_mutation_manifest(
    name: str,
    *,
    summary: str,
    danger: SecurityDanger,
    schema: Mapping[str, Any],
    actions: tuple[str, ...],
    role: str,
) -> BundleManifest:
    """Build one side-effecting file-mutation manifest with the shared posture.

    Every file-mutation tool here is **non-privileged**, freely ``REPLACEABLE``,
    and carried ``in_process`` — the same low-state posture as
    :func:`lingtai_sdk.file_tools._file_tool_manifest`. Unlike that helper,
    ``danger`` is a **required, per-tool** argument: these tools mutate the
    filesystem and do not share one danger posture (``write`` is
    ``DESTRUCTIVE``; ``edit`` is ``CAUTION``). It declares exactly one public
    tool whose name equals the bundle ``name``. The metadata (``role``
    statement, ``actions`` list, ``side_effect`` marker, and a copy of the
    argument ``schema``) is helpful, non-secret description only — it carries no
    handler and no implementation; the real handler is injected by the wrapper
    bridge.
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
            "file_tool": True,
            "side_effect": True,
            "role": role,
            "actions": list(actions),
            "schema": dict(schema),
        },
    )


def write_bundle() -> BundleManifest:
    """The ``write`` file-mutation bundle manifest — create or overwrite a file.

    Declares the public ``write`` tool: create a file (parent directories made as
    needed) **or overwrite an existing one wholesale** with new content. Because
    overwriting irreversibly clobbers prior content, the declared posture is
    ``danger=destructive`` — the side-effecting mirror of the privileged
    ``system`` bundle's destructive actions. **Manifest only** — the real handler
    (which writes through ``agent._file_io`` with the wrapper's path-sandbox
    semantics) is injected by the wrapper bridge.
    """
    return _file_mutation_manifest(
        "write",
        summary="Create a file or overwrite an existing one wholesale.",
        danger=SecurityDanger.DESTRUCTIVE,
        schema=_WRITE_SCHEMA,
        actions=("write",),
        role="The agent's file create/overwrite surface.",
    )


def edit_bundle() -> BundleManifest:
    """The ``edit`` file-mutation bundle manifest — exact in-place replacement.

    Declares the public ``edit`` tool: replace an exact ``old_string`` with
    ``new_string`` in an *existing* file, refusing the edit when ``old_string``
    is not found or is found more than once without ``replace_all``. It is a
    bounded in-place mutation — a real side effect with lasting effects, but not a
    wholesale clobber — so the declared posture is ``danger=caution`` (the same
    posture ``psyche`` / ``soul`` use for lasting-but-not-destructive actions).
    **Manifest only** — the real handler (which reads, replaces, and writes
    through ``agent._file_io``) is injected by the wrapper bridge.
    """
    return _file_mutation_manifest(
        "edit",
        summary="Exact in-place string replacement in an existing file.",
        danger=SecurityDanger.CAUTION,
        schema=_EDIT_SCHEMA,
        actions=("edit",),
        role="The agent's in-place file-edit surface.",
    )


# Stable, canonical order for the two file-mutation bundles.
_FILE_MUTATION_BUILDERS: tuple[Callable[[], BundleManifest], ...] = (
    write_bundle,
    edit_bundle,
)


def file_mutation_tool_manifests() -> tuple[BundleManifest, ...]:
    """The two file-mutation bundle manifests in stable order: write, edit."""
    return tuple(builder() for builder in _FILE_MUTATION_BUILDERS)


def file_mutation_tool_names() -> tuple[str, ...]:
    """The two file-mutation names in stable order: ``("write", "edit")``."""
    return tuple(m.name for m in file_mutation_tool_manifests())


def is_file_mutation_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is one of the two file-mutation bundles by name."""
    return manifest.name in file_mutation_tool_names()


def file_mutation_tool_host(
    manifest: BundleManifest, handler: ToolHandler
) -> BundleHost:
    """Build an in-process host for a file-mutation bundle from an injected handler.

    The side-effecting mirror of
    :func:`~lingtai_sdk.file_tools.file_tool_host`. Given a file-mutation
    :class:`BundleManifest` and a single *supplied* callable, returns a
    :class:`~lingtai_sdk.capability_host.BundleHost` hosting the bundle's one
    declared tool. The handler is whatever the wrapper bridge injects (the real
    ``write`` / ``edit`` handler bound to ``agent._file_io``); this shim never
    imports or calls the real implementation.

    It only enforces the contract:

    * ``manifest`` must be one of the file-mutation bundles (a non-file-mutation
      manifest raises :class:`~lingtai_sdk.errors.BundleHostError`);
    * ``handler`` must be callable (a missing / non-callable handler raises);
    * the manifest/handler parity and the non-privileged / ``in_process`` rules
      are then enforced by :class:`BundleHost` itself.

    Note that the declared ``danger`` posture is **not** enforced here: a host
    runs whatever handler it is given. Danger is a *declaration* the stage-17
    :mod:`lingtai_sdk.guard_bridge` reads to gate dispatch — a separate,
    not-yet-installed seam — so hosting a ``destructive`` ``write`` bundle is
    exactly as permitted as hosting a ``safe`` ``read`` bundle. This keeps the
    host a thin executor and the danger posture a pure declaration.
    """
    if not is_file_mutation_manifest(manifest):
        raise BundleHostError(
            f"file_mutation_tool_host expects a file-mutation bundle "
            f"(one of {list(file_mutation_tool_names())}), got {manifest.name!r}"
        )
    if not callable(handler):
        raise BundleHostError(
            f"file-mutation bundle {manifest.name!r} requires a callable handler, "
            f"got {type(handler).__name__}"
        )
    # The single declared tool always equals the bundle name (see _file_mutation_manifest).
    tool_name = manifest.name
    return BundleHost(manifest, {tool_name: handler})


def file_mutation_tool_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{name: BundleHost}`` for all file-mutation bundles from handlers.

    ``handlers`` maps each file-mutation name (``write`` / ``edit``) to the
    callable the wrapper bridge supplies for that tool. Every file-mutation
    bundle must have a handler and there must be no handler for a name that is
    not a file-mutation bundle — a missing or undeclared/extra handler raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial wiring can never
    silently host a subset of the file-mutation tools.
    """
    expected = set(file_mutation_tool_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for file-mutation bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-file-mutation bundle name(s): {sorted(extra)}"
        )
    return {
        manifest.name: file_mutation_tool_host(manifest, handlers[manifest.name])
        for manifest in file_mutation_tool_manifests()
    }


__all__ = [
    "write_bundle",
    "edit_bundle",
    "file_mutation_tool_manifests",
    "file_mutation_tool_names",
    "is_file_mutation_manifest",
    "file_mutation_tool_host",
    "file_mutation_tool_hosts",
]
