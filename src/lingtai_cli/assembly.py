"""Thin assembly bridge: project workdir → ``RuntimeOptions`` + a summary.

This is the smallest piece of the migration contract: it reads a project's
resolved ``init.json`` from a workdir and produces the backend-neutral inputs a
runtime needs, *without constructing an ``Agent``*. It is the CLI's
"translate project state into a runtime's options" seam — the host
(:mod:`lingtai_cli.host`) still owns the full boot path (validation, env
resolution, preset materialization, live refresh); this module only exposes a
light, declarative view for callers that want options/summary but not a process.

Light by design
---------------
``import lingtai_cli.assembly`` and every method here avoid importing the
``lingtai`` wrapper or any provider SDK: the file reads ``init.json`` with the
stdlib ``json`` module and surfaces fields as plain dataclasses. Validation,
preset materialization, and Agent construction stay in the host. Keeping this
module wrapper-free is what lets product tooling inspect a project's resolved
capability / addon / prompt / MCP shape without paying the heavy import cost.

Scope of this PR
----------------
Only the ``native`` backend is supported by :meth:`ProjectState.to_runtime_options`.
The Anthropic / future backends translate the same ``ProjectState`` into their
own client config in later PRs; the assembly contract is the stable shape they
share.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lingtai_sdk.runtime import RuntimeOptions

# Backends this assembly bridge can currently translate a project into. The
# Anthropic / future backends land in later PRs (they consume the same
# ``ProjectState``); rejecting them here keeps the contract honest.
_SUPPORTED_BACKENDS = ("native",)


@dataclass(frozen=True)
class CLIAssembly:
    """The CLI/product assembly output for a project and backend.

    This is the spec seam made explicit: CLI owns product composition and
    translation, so callers can inspect the runtime options together with the
    capability/addon/prompt/MCP plan the CLI derived from project state. The
    first PR keeps the plan declarative and native-only; later backend PRs can
    consume the same shape for Anthropic/Codex/etc.
    """

    backend: str
    runtime_options: RuntimeOptions
    capability_bundles: dict[str, dict]
    addons: list[str]
    prompt_assets: dict[str, str]
    mcp: dict[str, Any]
    custom_tools: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectState:
    """A resolved view of a project workdir's ``init.json``.

    Loaded by :meth:`load`. Exposes the resolved capability / addon / prompt /
    MCP information as plain data and can produce :class:`RuntimeOptions` for a
    backend — the thin "project state → runtime options" bridge. Does not start
    or construct an ``Agent``.
    """

    working_dir: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # --- load -------------------------------------------------------------

    @classmethod
    def from_init(cls, working_dir: str | Path, data: dict[str, Any]) -> "ProjectState":
        """Build a project-state view from an already-loaded init mapping.

        Host code can call this with the output of ``lingtai_cli.host.load_init``
        when it needs the heavier, validated/materialized view. The lightweight
        :meth:`load` path below deliberately reads only the current on-disk
        ``init.json``.
        """
        wd = Path(working_dir)
        manifest = data.get("manifest") or {}
        return cls(working_dir=wd, manifest=dict(manifest), raw=dict(data))

    @classmethod
    def load(cls, working_dir: str | Path) -> "ProjectState":
        """Read ``init.json`` from *working_dir* into a ``ProjectState``.

        This is a light read — it does NOT run migrations, materialize presets,
        validate the schema, or resolve env/secrets. The host's
        ``load_init`` owns that fuller boot path; this method is the
        declarative project-state view used to derive runtime options.
        """
        wd = Path(working_dir)
        init_path = wd / "init.json"
        if not init_path.is_file():
            raise FileNotFoundError(f"{init_path} not found")
        data = json.loads(init_path.read_text(encoding="utf-8"))
        return cls.from_init(wd, data)

    # --- resolved summary -------------------------------------------------

    @property
    def agent_name(self) -> str | None:
        return self.manifest.get("agent_name")

    @property
    def llm(self) -> dict[str, Any]:
        return self.manifest.get("llm") or {}

    @property
    def capabilities(self) -> dict[str, dict]:
        """The capability map (name → kwargs) declared in the manifest."""
        caps = self.manifest.get("capabilities")
        return dict(caps) if isinstance(caps, dict) else {}

    @property
    def addons(self) -> list[str]:
        """Curated MCP addon names from the top-level ``addons`` list."""
        addons = self.raw.get("addons")
        return list(addons) if isinstance(addons, list) else []

    @property
    def mcp(self) -> dict[str, Any]:
        """The per-MCP activation map from the top-level ``mcp`` object."""
        mcp = self.raw.get("mcp")
        return dict(mcp) if isinstance(mcp, dict) else {}

    @property
    def prompt_sections(self) -> dict[str, str]:
        """Inline prompt-asset sections present in ``init.json``.

        Surfaces only the inline string sections (``principle`` / ``covenant``
        / ``pad`` / ``prompt`` / ``brief`` / ``substrate`` / ``comment``). The
        ``*_file`` indirection and full asset-plan resolution belong to a later
        ``AssetPlan`` step; this is the minimal prompt view.
        """
        keys = (
            "principle",
            "covenant",
            "pad",
            "prompt",
            "brief",
            "substrate",
            "comment",
        )
        return {k: self.raw[k] for k in keys if isinstance(self.raw.get(k), str)}

    @property
    def custom_tools(self) -> dict[str, Any]:
        """Top-level custom tool declarations, when present.

        Current native init.json rarely uses this top-level form, but the CLI
        assembly contract includes a custom-tool-config slot so non-native
        backends can later consume the same plan shape.
        """
        tools = self.raw.get("tools")
        return dict(tools) if isinstance(tools, dict) else {}

    # --- runtime options / assembly ---------------------------------------

    def to_runtime_options(self, *, backend: str = "native") -> RuntimeOptions:
        """Translate this project state into :class:`RuntimeOptions`.

        Only the ``native`` backend is supported in this PR. Secrets are NOT
        resolved here — ``api_key`` is passed through verbatim from the manifest
        (env/secret injection happens at the runtime boundary in the host).
        """
        if backend not in _SUPPORTED_BACKENDS:
            raise ValueError(
                f"unsupported backend {backend!r}; "
                f"supported: {', '.join(_SUPPORTED_BACKENDS)}"
            )
        llm = self.llm
        return RuntimeOptions(
            working_dir=self.working_dir,
            agent_name=self.agent_name,
            provider=llm.get("provider"),
            model=llm.get("model"),
            base_url=llm.get("base_url"),
            api_key=llm.get("api_key"),
            capabilities=self.capabilities or None,
            addons=self.addons or None,
            system_prompt_overrides=self.prompt_sections,
            manifest=self.manifest,
            streaming=bool(self.manifest.get("streaming", False)),
            extra={"mcp": self.mcp, "custom_tools": self.custom_tools},
        )

    def assemble(self, *, backend: str = "native") -> CLIAssembly:
        """Return the full CLI assembly plan for *backend*.

        The plan pairs ``RuntimeOptions`` with the resolved (for this first PR,
        declarative/on-disk) capability bundle set, prompt asset overrides, MCP
        configs, and custom tool configs. It is intentionally data-only and does
        not construct an Agent or runtime session.
        """
        options = self.to_runtime_options(backend=backend)
        return CLIAssembly(
            backend=backend,
            runtime_options=options,
            capability_bundles=self.capabilities,
            addons=self.addons,
            prompt_assets=self.prompt_sections,
            mcp=self.mcp,
            custom_tools=self.custom_tools,
        )


__all__ = ["CLIAssembly", "ProjectState"]
