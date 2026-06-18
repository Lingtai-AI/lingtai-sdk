"""Wrapper-side bridge: host the real file tools through the SDK bundle
declarations — read/glob/grep (stage 3A) and write/edit (stage 3B).

This is the wrapper half of the SDK file-tool bundle template. The SDK modules
*declare* the low-state file tools as
:class:`~lingtai_sdk.capabilities.BundleManifest` objects and offer host
injection seams, but — to respect the import boundary (the SDK must not import
the wrapper, and the kernel must not import the SDK) — they ship **no real
handler**. This module is where the wrapper, which *may* import the SDK, injects
the genuine file-tool handlers and so proves the bundle-execution pattern end to
end against the real behavior. Two SDK modules are bridged here:

* :mod:`lingtai_sdk.file_tools` — the **read-only / query** tools ``read`` /
  ``glob`` / ``grep`` (stage 3A); all ``SecurityDanger.SAFE``.
* :mod:`lingtai_sdk.file_mutation_tools` — the **side-effecting** tools
  ``write`` / ``edit`` (stage 3B); ``write`` is ``DESTRUCTIVE``, ``edit`` is
  ``CAUTION``.

The handlers are the *same* closures the normal capability path registers:
``lingtai.core.{read,glob,grep,write,edit}.make_handler(agent)``. There is no
second implementation — the bundle-hosted tool runs byte-identical logic to the
tool ``setup()`` registers on the agent, against the same ``agent._file_io`` /
``agent._working_dir`` and the same path-sandbox / traversal-budget / overwrite /
error semantics.

The wrapper handlers take a single ``args: dict``; the SDK
:class:`~lingtai_sdk.capability_host.BundleHost.invoke` passes keyword args. The
tiny ``_kwargs_adapter`` reconciles the two without changing either contract.

This module does **not** change how ``Agent`` registers or dispatches the file
tools — ``setup()`` remains the live path. It is an additive, observable seam:
a host (or a test) can drive the real file tools through the declared manifest,
which is the migration template later stages build on. In particular, the
declared danger posture on the write/edit manifests is **not** enforced here:
this bridge only hosts and runs the real handler. Danger is a declaration the
stage-17 :mod:`lingtai_sdk.guard_bridge` reads — a separate, not-yet-installed
gate — so the side-effecting tools run here exactly as the read tools do.
Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_sdk.bundles.host import BundleHost

# The wrapper's real handler factories — the single source of truth for each
# file tool's behavior, shared with the capability ``setup()`` path.
from . import edit as _edit
from . import glob as _glob
from . import grep as _grep
from . import read as _read
from . import write as _write

# name -> the module exposing ``make_handler(agent)`` for that read-only/query
# file tool (stage 3A).
_HANDLER_FACTORIES: dict[str, Callable[["BaseAgent"], Callable[[dict], Any]]] = {
    "read": _read.make_handler,
    "glob": _glob.make_handler,
    "grep": _grep.make_handler,
}

# name -> the module exposing ``make_handler(agent)`` for that side-effecting
# file-mutation tool (stage 3B).
_MUTATION_HANDLER_FACTORIES: dict[
    str, Callable[["BaseAgent"], Callable[[dict], Any]]
] = {
    "write": _write.make_handler,
    "edit": _edit.make_handler,
}


def _kwargs_adapter(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to ``BundleHost.invoke``'s kwargs.

    ``BundleHost.invoke(tool, **kwargs)`` calls its handler with keyword args,
    but the wrapper file-tool handlers take a single positional ``args`` dict.
    This collects the kwargs back into that dict so the real handler runs
    unchanged.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def file_tool_handlers(agent: "BaseAgent") -> dict[str, Callable[..., Any]]:
    """Build the ``{name: kwargs-handler}`` mapping the SDK host seam expects.

    Each handler is the wrapper's real ``make_handler(agent)`` closure, adapted
    to the host's keyword-args invocation contract. Bound to *agent*, so it reads
    through ``agent._file_io`` / ``agent._working_dir`` exactly as the registered
    tool does.
    """
    return {
        name: _kwargs_adapter(factory(agent))
        for name, factory in _HANDLER_FACTORIES.items()
    }


def file_tool_bundle_hosts(agent: "BaseAgent") -> dict[str, "BundleHost"]:
    """Host the real file tools through the SDK file-tool bundle declarations.

    Returns ``{name: BundleHost}`` for ``read`` / ``glob`` / ``grep``, each
    hosting the bundle's one declared tool with the wrapper's genuine handler
    (bound to *agent*). The SDK is imported here, in the wrapper, not at SDK
    module load — preserving the one-way ``wrapper -> sdk`` direction.

    ``host.invoke(name, **args)`` then runs the real file-tool logic through the
    declared manifest, proving the bundle-execution pattern against actual
    behavior. This does not alter the agent's live tool registration.
    """
    from lingtai_sdk.bundles.file_tools import file_tool_hosts

    return file_tool_hosts(file_tool_handlers(agent))


def file_mutation_tool_handlers(agent: "BaseAgent") -> dict[str, Callable[..., Any]]:
    """Build the ``{name: kwargs-handler}`` mapping the SDK mutation host seam expects.

    The side-effecting counterpart of :func:`file_tool_handlers`. Each handler is
    the wrapper's real ``make_handler(agent)`` closure for ``write`` / ``edit``,
    adapted to the host's keyword-args invocation contract. Bound to *agent*, so
    it writes through ``agent._file_io`` / ``agent._working_dir`` exactly as the
    registered tool does — including the same overwrite / read-modify-write side
    effect.
    """
    return {
        name: _kwargs_adapter(factory(agent))
        for name, factory in _MUTATION_HANDLER_FACTORIES.items()
    }


def file_mutation_tool_bundle_hosts(agent: "BaseAgent") -> dict[str, "BundleHost"]:
    """Host the real ``write`` / ``edit`` tools through the SDK mutation bundle.

    Returns ``{name: BundleHost}`` for ``write`` / ``edit``, each hosting the
    bundle's one declared tool with the wrapper's genuine handler (bound to
    *agent*). The SDK is imported here, in the wrapper, not at SDK module load —
    preserving the one-way ``wrapper -> sdk`` direction.

    ``host.invoke(name, **args)`` then runs the real file-mutation logic through
    the declared manifest, proving the side-effect bundle-execution pattern
    against actual behavior. This does not alter the agent's live tool
    registration, and the declared danger posture (``write`` destructive,
    ``edit`` caution) is a declaration only — the host runs the handler
    unconditionally; gating is the stage-17 guard bridge's job, not installed
    here.
    """
    from lingtai_sdk.bundles.file_mutation_tools import file_mutation_tool_hosts

    return file_mutation_tool_hosts(file_mutation_tool_handlers(agent))


__all__ = [
    "file_tool_handlers",
    "file_tool_bundle_hosts",
    "file_mutation_tool_handlers",
    "file_mutation_tool_bundle_hosts",
]
