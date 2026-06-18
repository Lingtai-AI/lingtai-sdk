"""Low-state file/query tool bundle declarations: ``read`` / ``glob`` / ``grep``.

The **migration template** for low-state, non-privileged file/query tools — the
non-privileged, in-process counterpart of :mod:`lingtai_sdk.core_bundles`. Where
``core_bundles`` declares the privileged ``system`` / ``psyche`` / ``soul``
surfaces (hosted only by a native authority), this module declares the three
*low-state* file tools the wrapper already ships in ``lingtai.core.{read,glob,
grep}`` as :class:`BundleManifest` objects and offers a thin host seam that wires
each declared tool to an *injected* handler through the non-native
:class:`~lingtai_sdk.capability_host.BundleHost`.

What this module is NOT
-----------------------
It does **not** migrate, move, rewrite, import, or call the real ``read`` /
``glob`` / ``grep`` implementations. Those bind to wrapper-owned services
(``agent._file_io``, ``agent._working_dir``) at ``setup()`` time and stay in the
wrapper; importing them here would break the SDK import-purity boundary (the
SDK must not eagerly pull a wrapper service) and the dependency direction (the
kernel must never import the SDK). So this module ships *declarations + an
injection seam* only:

    declared manifest (read/glob/grep)
       -> file_tools_host({name: handler})   # wrapper injects its real handler
       -> host.invoke(name, **args)          # runs the wrapper's existing logic

The wrapper-side bridge that supplies those handlers — and therefore proves the
bundle-execution pattern end to end against the *real* file-tool behavior —
lives in ``lingtai.core.file_bundle`` (the wrapper *may* import the SDK; the SDK
must not import the wrapper). The tool **schemas and behavior are unchanged**:
the bridge reuses the existing ``lingtai.core.{read,glob,grep}.get_schema`` /
``setup`` handlers verbatim; this module only embeds a copy of each schema in
the manifest metadata as a declaration.

The three manifests share the same posture — non-privileged, freely
``REPLACEABLE``, ``in_process`` transport — and each declares exactly one public
tool named after the file tool (``read`` / ``glob`` / ``grep``). They differ
only in their declared danger posture, which mirrors the real surfaces. All
three are read-only / query surfaces, so all are ``SecurityDanger.SAFE``.

See ``docs/sdk/architecture-foundation.md`` (stage 3A).
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

# The JSON-schema *declarations* for the three file tools. These are a
# language-neutral copy of the shapes returned by
# ``lingtai.core.{read,glob,grep}.get_schema`` (descriptions are i18n'd at
# registration time in the wrapper, so they are intentionally omitted here — the
# manifest carries the structural contract, not the localized prose). The
# wrapper bridge does NOT read these to register tools; the wrapper's own
# ``get_schema(lang)`` remains the registration path. They live here so a host
# inspecting the manifest can see each tool's argument contract without
# importing the wrapper.
_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "offset": {"type": "integer", "default": 1},
        "limit": {"type": "integer", "default": 2000},
    },
    "required": ["file_path"],
}

_GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string"},
        "path": {"type": "string"},
    },
    "required": ["pattern"],
}

_GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "glob": {"type": "string", "default": "*"},
        "max_matches": {"type": "integer", "default": 200},
    },
    "required": ["pattern"],
}


def _file_tool_manifest(
    name: str,
    *,
    summary: str,
    schema: Mapping[str, Any],
    actions: tuple[str, ...],
    role: str,
) -> BundleManifest:
    """Build one low-state file-tool manifest with the shared posture.

    Every file tool here is **non-privileged**, freely ``REPLACEABLE``, carried
    ``in_process``, and read-only (``SecurityDanger.SAFE``). It declares exactly
    one public tool whose name equals the bundle ``name``. The metadata
    (``role`` statement, ``actions`` list, and a copy of the argument ``schema``)
    is helpful, non-secret description only — it carries no handler and no
    implementation; the real handler is injected by the wrapper bridge.
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
        security=SecurityPolicy(danger=SecurityDanger.SAFE.value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "file_tool": True,
            "role": role,
            "actions": list(actions),
            "schema": dict(schema),
        },
    )


def read_bundle() -> BundleManifest:
    """The ``read`` file-tool bundle manifest — read text file contents.

    Declares the public ``read`` tool: read a text file's contents with optional
    ``offset`` / ``limit`` line windowing. Read-only, hence
    ``danger=safe``. **Manifest only** — the real handler (which reads through
    ``agent._file_io`` with the wrapper's path-sandbox semantics) is injected by
    the wrapper bridge.
    """
    return _file_tool_manifest(
        "read",
        summary="Read text file contents with optional offset/limit windowing.",
        schema=_READ_SCHEMA,
        actions=("read",),
        role="The agent's text-file read surface.",
    )


def glob_bundle() -> BundleManifest:
    """The ``glob`` file-tool bundle manifest — find files by pattern.

    Declares the public ``glob`` tool: list files matching a glob ``pattern``
    under an optional ``path``. Read-only / query, hence ``danger=safe``.
    **Manifest only** — the real handler (which traverses through
    ``agent._file_io`` with the wrapper's traversal-budget semantics) is injected
    by the wrapper bridge.
    """
    return _file_tool_manifest(
        "glob",
        summary="Find files by glob pattern under an optional path.",
        schema=_GLOB_SCHEMA,
        actions=("glob",),
        role="The agent's file-name query surface.",
    )


def grep_bundle() -> BundleManifest:
    """The ``grep`` file-tool bundle manifest — search file contents by regex.

    Declares the public ``grep`` tool: search file contents by regex
    ``pattern``, optionally scoped by ``path`` / ``glob`` and capped by
    ``max_matches``. Read-only / query, hence ``danger=safe``. **Manifest only**
    — the real handler (which scans through ``agent._file_io`` with the wrapper's
    traversal-budget / truncation semantics) is injected by the wrapper bridge.
    """
    return _file_tool_manifest(
        "grep",
        summary="Search file contents by regex, scoped by path/glob and capped "
        "by max_matches.",
        schema=_GREP_SCHEMA,
        actions=("grep",),
        role="The agent's file-content query surface.",
    )


# Stable, canonical order for the three file-tool bundles.
_FILE_TOOL_BUILDERS: tuple[Callable[[], BundleManifest], ...] = (
    read_bundle,
    glob_bundle,
    grep_bundle,
)


def file_tool_manifests() -> tuple[BundleManifest, ...]:
    """The three file-tool bundle manifests in stable order: read, glob, grep."""
    return tuple(builder() for builder in _FILE_TOOL_BUILDERS)


def file_tool_names() -> tuple[str, ...]:
    """The three file-tool names in stable order: ``("read", "glob", "grep")``."""
    return tuple(m.name for m in file_tool_manifests())


def is_file_tool_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is one of the three file-tool bundles by name."""
    return manifest.name in file_tool_names()


def file_tool_host(manifest: BundleManifest, handler: ToolHandler) -> BundleHost:
    """Build an in-process host for a file-tool bundle from an *injected* handler.

    The thin adapter/host shim — the non-privileged, in-process mirror of
    :func:`~lingtai_sdk.core_bundles.native_core_host`. Given a file-tool
    :class:`BundleManifest` and a single *supplied* callable, returns a
    :class:`~lingtai_sdk.capability_host.BundleHost` hosting the bundle's one
    declared tool. The handler is whatever the wrapper bridge injects (the real
    ``read`` / ``glob`` / ``grep`` handler bound to ``agent._file_io``); this
    shim never imports or calls the real implementation.

    It only enforces the contract:

    * ``manifest`` must be one of the file-tool bundles (a non-file-tool manifest
      raises :class:`~lingtai_sdk.errors.BundleHostError`);
    * ``handler`` must be callable (a missing / non-callable handler raises);
    * the manifest/handler parity and the non-privileged / ``in_process`` rules
      are then enforced by :class:`BundleHost` itself (it refuses any
      privileged / native-only bundle and any non-``in_process`` transport).
    """
    if not is_file_tool_manifest(manifest):
        raise BundleHostError(
            f"file_tool_host expects a file-tool bundle "
            f"(one of {list(file_tool_names())}), got {manifest.name!r}"
        )
    if not callable(handler):
        raise BundleHostError(
            f"file-tool bundle {manifest.name!r} requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    # The single declared tool always equals the bundle name (see _file_tool_manifest).
    tool_name = manifest.name
    return BundleHost(manifest, {tool_name: handler})


def file_tool_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{name: BundleHost}`` for all file-tool bundles from injected handlers.

    ``handlers`` maps each file-tool name (``read`` / ``glob`` / ``grep``) to the
    callable the wrapper bridge supplies for that tool. Every file-tool bundle
    must have a handler and there must be no handler for a name that is not a
    file-tool bundle — a missing or undeclared/extra handler raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial wiring can never
    silently host a subset of the file tools.
    """
    expected = set(file_tool_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for file-tool bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-file-tool bundle name(s): {sorted(extra)}"
        )
    return {
        manifest.name: file_tool_host(manifest, handlers[manifest.name])
        for manifest in file_tool_manifests()
    }


__all__ = [
    "read_bundle",
    "glob_bundle",
    "grep_bundle",
    "file_tool_manifests",
    "file_tool_names",
    "is_file_tool_manifest",
    "file_tool_host",
    "file_tool_hosts",
]
