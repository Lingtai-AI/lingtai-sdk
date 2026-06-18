"""lingtai_cli — the thin product-assembly / host layer.

This package owns *composition and translation*: turning a project workdir
(``init.json`` / meta, recipe / preset / addons / secrets, skills / rules /
flags, backend choice) into the building blocks a runtime consumes
(``RuntimeOptions``, resolved capability/addon/prompt/MCP information). The SDK
(:mod:`lingtai_sdk`) owns the *definitions and runtime building blocks*; this
package owns *product assembly*. The console scripts ``lingtai-agent`` and
``lingtai-cli`` both enter through :func:`lingtai_cli.host.main`.

Import-light boundary
---------------------
A bare ``import lingtai_cli`` stays cheap: this ``__init__`` imports nothing
heavy. The two surfaces resolve lazily via :pep:`562`:

- :class:`~lingtai_cli.assembly.ProjectState` — the dependency-light project →
  ``RuntimeOptions`` bridge (reads ``init.json`` with the stdlib; never imports
  the ``lingtai`` wrapper).
- :func:`~lingtai_cli.host.main` (and the other host entry points) — the full
  boot path. Importing it pulls the ``lingtai`` wrapper, so it is only loaded on
  first attribute access (or when the console script runs it directly).

``lingtai.cli`` remains a compatibility shim re-exporting the host's public
names, so existing imports and ``lingtai-agent`` usage keep working.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Lazy export map: attribute name -> (submodule, attribute). Touching any of
# these imports the submodule on first access only, keeping bare
# ``import lingtai_cli`` free of the wrapper and provider SDKs.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "CLIAssembly": (".assembly", "CLIAssembly"),
    "ProjectState": (".assembly", "ProjectState"),
    "main": (".host", "main"),
    "run": (".host", "run"),
    "load_init": (".host", "load_init"),
    "build_agent": (".host", "build_agent"),
}

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .assembly import CLIAssembly, ProjectState  # noqa: F401
    from .host import build_agent, load_init, main, run  # noqa: F401


def __getattr__(name: str):  # PEP 562 module-level lazy attributes
    import importlib

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__))


__all__ = ["CLIAssembly", "ProjectState", "main", "run", "load_init", "build_agent"]
