"""Wrapper-side bridge: host the real ``skills`` tool through the SDK
catalog bundle declaration (stage 3G).

The skill-catalog counterpart of ``lingtai.core.knowledge_bundle`` (the
``knowledge`` private-memory bridge, this same stage) and ``lingtai.core.mcp_bundle``
(the ``mcp`` registry-view bridge, stage 3F). The SDK module
:mod:`lingtai_sdk.skill_tools` *declares* the ``skills`` catalog surface as a
:class:`~lingtai_sdk.capabilities.BundleManifest` and offers a host injection seam,
but — to respect the import boundary (the SDK must not import the wrapper, and the
kernel must never import the SDK) — it ships **no real handler**. This module is
where the wrapper, which *may* import the SDK and the wrapper capability, injects
the genuine handler and so proves the catalog bundle-execution pattern end to end
against actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
``skills`` is a **wrapper capability** (like ``knowledge``, ``mcp``, ``daemon``, and
the file tools), not a kernel intrinsic. Its real handler is the closure
``lingtai.core.skills.make_handler(agent, paths)`` builds — the *same* closure
``skills.setup()`` registers via ``agent.add_tool`` (single source of truth; see
``lingtai.core.skills.make_handler``). The bridge reuses that *same* factory —
there is no second implementation. The bundle-hosted ``skills`` tool runs
byte-identical logic to the tool ``setup()`` registers, against the same
``agent._working_dir`` / ``.library`` and the same Tier-1 ``paths`` and
``_reconcile`` (scan ``.library/`` + paths → re-render the ``skills`` system-prompt
section → manual body + health snapshot) semantics. The bridge lives in the
wrapper — not the kernel/SDK — because the dependency direction is one-way: the
wrapper *may* import the SDK (``lingtai_sdk.skill_tools``) and the wrapper
capability; the **kernel/SDK must never import the wrapper**. Putting the SDK
import here (lazily, inside the bridge functions) preserves that one-way
``wrapper -> sdk`` edge.

The Tier-1 ``paths`` contract
-----------------------------
Unlike ``mcp`` / ``knowledge`` (whose handlers bind to *agent* alone), the
``skills`` handler also closes over the Tier-1 ``paths`` from ``init.json``
``manifest.capabilities.skills.paths``. The bridge accepts the same optional
``paths`` argument and threads it to ``skills.make_handler`` verbatim — so a host
built with the same ``paths`` the live ``setup()`` saw dispatches byte-identically.
Omitting ``paths`` (the common direct-``Agent`` case) scans only the per-agent
``.library/``, exactly as live ``setup()`` does without kwargs.

The SDK ``BundleHost`` invokes its handler with keyword args; the wrapper
``skills`` handler takes a single ``args: dict``. The tiny ``_kwargs_adapter``
reconciles the two without changing either contract — the identical adapter the
file / lifecycle / communication / mcp / knowledge bridges use.

This module does **not** change how ``Agent`` registers or dispatches the
``skills`` tool — ``skills.setup()`` remains the live path. It is an additive,
observable seam. Constructing the bridge host writes nothing — only an explicit
``host.invoke("skills", action="info")`` re-reconciles, exactly as the live path
does. Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

    from lingtai_sdk.bundles.host import BundleHost

# The wrapper's real skills handler factory — the single source of truth shared
# with the capability ``setup()`` path (``skills.make_handler``). Imported at
# wrapper module load (the wrapper may import its own capability surface); the SDK
# is imported lazily inside the bridge functions to preserve the wrapper -> sdk edge.
from . import skills as _skills


def _kwargs_adapter(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to a host's kwargs invocation.

    A host's ``invoke(tool, **kwargs)`` calls its handler with keyword args, but
    the wrapper ``skills`` handler takes a single positional ``args`` dict. This
    collects the kwargs back into that dict so the real handler runs unchanged —
    the skills mirror of the adapter ``lingtai.core.knowledge_bundle`` uses.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def skills_catalog_handler(
    agent: "BaseAgent",
    paths: list[str] | None = None,
) -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``skills`` host seam expects.

    The handler is the wrapper's real ``skills.make_handler(agent, paths)`` closure
    — the *same* handler ``skills.setup()`` registers — adapted to the host's
    keyword-args invocation contract. Bound to *agent* and the Tier-1 *paths*, so
    it reads through ``agent._working_dir`` / ``.library`` and the configured paths,
    re-rendering the ``skills`` system-prompt section exactly as the registered tool
    does. Building it writes nothing.
    """
    return _kwargs_adapter(_skills.make_handler(agent, paths))


def skills_catalog_bundle_host(
    agent: "BaseAgent",
    paths: list[str] | None = None,
) -> "BundleHost":
    """Host the real ``skills`` tool through the SDK ``skills`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``skills``,
    hosting the bundle's one declared tool with the wrapper's genuine handler
    (``skills.make_handler`` bound to *agent* and *paths*). The SDK is imported
    here, in the wrapper, not at SDK module load — preserving the one-way
    ``wrapper -> sdk`` direction. ``host.invoke("skills", action="info")`` runs the
    real catalog logic through the declared manifest without altering the agent's
    live capability registration — and constructing the host writes nothing.
    """
    from lingtai_sdk.bundles.skill_tools import skills_catalog_host

    return skills_catalog_host(skills_catalog_handler(agent, paths))


def skills_catalog_bundle_hosts(
    agent: "BaseAgent",
    paths: list[str] | None = None,
) -> dict[str, "BundleHost"]:
    """Host the real ``skills`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`skills_catalog_bundle_host`, parallel to
    ``knowledge_bundle.knowledge_catalog_bundle_hosts`` /
    ``mcp_bundle.mcp_config_bundle_hosts``, so the wrapper bridge exposes the same
    ``{name: host}`` shape across all stages. Builds via the SDK
    ``skills_catalog_hosts`` mapping seam, which enforces the single-``skills``-
    handler contract.
    """
    from lingtai_sdk.bundles.skill_tools import SKILLS_TOOL_NAME, skills_catalog_hosts

    return skills_catalog_hosts(
        {SKILLS_TOOL_NAME: skills_catalog_handler(agent, paths)}
    )


__all__ = [
    "skills_catalog_handler",
    "skills_catalog_bundle_host",
    "skills_catalog_bundle_hosts",
]
