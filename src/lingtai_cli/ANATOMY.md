# lingtai_cli

Thin product-assembly / host package for the PyPI CLI surface.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. Update this file in the same commit as code changes that move or reshape this package.

## Components

| File | Role |
|---|---|
| `__init__.py` | Import-light public facade. Uses lazy PEP 562 exports so bare `import lingtai_cli` does not import the heavy `lingtai` wrapper, provider SDKs, or MCP servers. |
| `assembly.py` | Dependency-light product assembly contract: `ProjectState` reads project `init.json` into a declarative view; `ProjectState.to_runtime_options()` returns SDK `RuntimeOptions`; `ProjectState.assemble()` returns `CLIAssembly` with RuntimeOptions plus capability/addon/prompt/MCP/custom-tool plan. Native backend only in this first slice. |
| `host.py` | Full host/runtime CLI behaviour moved from `lingtai.cli`: `load_init`, `build_agent`, `run`, `main`, log helpers, signal handling, duplicate-process checks. This module intentionally imports the wrapper/runtime stack and is only loaded by console scripts, legacy shim imports, or explicit callers. |
| `cli.py` | New `lingtai-cli` console-script entrypoint. Delegates to `host.main(prog="lingtai-cli")` so the new thin CLI package has its own binary name while preserving the existing command surface. |

## Composition

`lingtai_cli` is the boundary described by the SDK/CLI migration spec:

- `lingtai_sdk` owns definitions, DTOs, runtime protocols, and capability-bundle building blocks.
- `lingtai_cli` owns product composition/translation: project state (`init.json`, addons, skills paths, prompt sections, CLI flags, backend choice) into `RuntimeOptions` plus a declarative plan.
- `lingtai` remains the wrapper/runtime implementation; `lingtai.cli` is now a compatibility shim re-exporting `lingtai_cli.host`.

The current implementation is native-only and conservative. Non-native backend translation should extend the `ProjectState`/`CLIAssembly` seam rather than importing the TUI/Go frontend or reintroducing composition logic into `lingtai_sdk`.

## Entry points

`pyproject.toml` declares both:

- `lingtai-agent = "lingtai.cli:main"` — legacy script, still routed through the shim.
- `lingtai-cli = "lingtai_cli.cli:main"` — new thin host package script.

## Invariants

- Bare `import lingtai_cli` stays light; do not import `lingtai_cli.host` from `__init__.py` at module import time.
- `assembly.py` must stay data-only and avoid importing the `lingtai` wrapper or provider SDKs.
- Runtime behaviour remains in `host.py`; moving it must preserve `lingtai-agent` compatibility.
- Do not implement non-native backends here until the native composition seam is stable.
