"""Agent — BaseAgent + composable capabilities.

Anatomy leaf: docs/plans/drafts/2026-04-30-anatomy-tree/leaves/core/preset-materialization/

Layer 2 of the three-layer hierarchy:
    BaseAgent (kernel) → Agent (capabilities) → CustomAgent (domain)

Capabilities are declared at construction and sealed before start().
"""
from __future__ import annotations

from typing import Any

from pathlib import Path

from lingtai_kernel.base_agent import BaseAgent
from lingtai.llm.service import LLMService
from lingtai_kernel.prompt import build_system_prompt


class Agent(BaseAgent):
    """BaseAgent with composable capabilities.

    Args:
        capabilities: Capability names to enable. Either a list of strings
            (no kwargs) or a dict mapping names to kwargs dicts.
            Each capability dict may include ``"provider"`` to route that
            capability to a specific LLM provider (e.g. ``"gemini"``, ``"minimax"``).
            Group names (e.g. ``"file"``) expand to individual capabilities.
        *args, **kwargs: Passed through to BaseAgent.
    """

    def __init__(
        self,
        *args: Any,
        capabilities: list[str] | dict[str, dict] | None = None,
        addons: list[str] | None = None,
        combo_name: str | None = None,
        disable: list[str] | None = None,
        **kwargs: Any,
    ):
        # Default karma authority for the primary agent (本我)
        kwargs.setdefault("admin", {"karma": True})

        # Store combo name before super().__init__ (not forwarded to BaseAgent)
        self._combo_name = combo_name

        super().__init__(*args, **kwargs)

        # Persist LLM config for revive (self-sufficient agents contract)
        self._persist_llm_config()

        # Auto-create FileIOService if not provided by host
        if self._file_io is None:
            from .services.file_io import LocalFileIOService
            self._file_io = LocalFileIOService(root=self._working_dir)

        # Expand groups and normalize to dict
        if isinstance(capabilities, list):
            from .capabilities import expand_groups, normalize_capabilities
            expanded = expand_groups(capabilities)
            capabilities = normalize_capabilities({name: {} for name in expanded})
        elif isinstance(capabilities, dict):
            from .capabilities import _GROUPS, normalize_capabilities
            expanded_dict: dict[str, dict] = {}
            for name, cap_kwargs in capabilities.items():
                if name in _GROUPS:
                    for sub in _GROUPS[name]:
                        expanded_dict[sub] = {}
                elif cap_kwargs is None:
                    expanded_dict[name] = None  # propagate disable-sentinel
                else:
                    expanded_dict[name] = cap_kwargs
            capabilities = normalize_capabilities(
                {n: (v if v is not None else {}) for n, v in expanded_dict.items()}
            )
            # Preserve null sentinels for apply_core_defaults to interpret as opt-out.
            for n, v in expanded_dict.items():
                if v is None:
                    capabilities[n] = None  # type: ignore[assignment]

        # Apply core defaults — the `lingtai.core.*` floor boots on every agent
        # unless explicitly disabled via `disable=[...]` or `"name": null` in
        # the capabilities dict. init.json kwargs override default kwargs.
        from .capabilities import apply_core_defaults
        capabilities = apply_core_defaults(capabilities, disable=disable)

        # Track for avatar replay
        self._capabilities: list[tuple[str, dict]] = []
        self._capability_managers: dict[str, Any] = {}

        # Decompress addons BEFORE capability setup so the `mcp` capability
        # sees the populated registry on its first reconcile.
        if addons:
            try:
                from .core.mcp import decompress_addons
                report = decompress_addons(self._working_dir, addons)
                self._log("mcp_decompress", **report)
            except Exception as e:
                self._log("mcp_decompress_failed", reason=str(e))

        # Register capabilities — provider kwarg flows through to setup() naturally
        if capabilities:
            for name, cap_kwargs in capabilities.items():
                try:
                    self._setup_capability(name, **cap_kwargs)
                except (ValueError, ImportError, TypeError) as e:
                    self._log("capability_skipped", capability=name, reason=str(e))

        # Install intrinsic manuals (wipe-and-rewrite .library/intrinsic/)
        # from the bundles shipped with each enabled capability.
        self._install_intrinsic_manuals()

        # Auto-load MCP servers from working directory.
        # Runs AFTER addon decompression so init.json mcp entries can reference
        # newly-decompressed registry records.
        self._load_mcp_from_workdir()

        # Re-write manifest now that capabilities are registered
        if self._capabilities:
            self._workdir.write_manifest(self._build_manifest())

    def _persist_llm_config(self) -> None:
        """Persist LLM config to llm.json for agent revive.

        Extracted from __init__ to avoid duplication.
        """
        _service = getattr(self, "service", None)
        if _service is None:
            return
        try:
            import json as _json
            llm_config: dict[str, Any] = {
                "provider": _service.provider,
                "model": _service.model,
            }
            _base_url = getattr(_service, "_base_url", None)
            if isinstance(_base_url, str) and _base_url:
                llm_config["base_url"] = _base_url
            llm_dir = self._working_dir / "system"
            llm_dir.mkdir(exist_ok=True)
            (llm_dir / "llm.json").write_text(
                _json.dumps(llm_config, ensure_ascii=False)
            )
        except (TypeError, AttributeError, OSError):
            pass  # LLM config not available (e.g., mock service in tests)

    def _setup_capability(self, name: str, **kwargs: Any) -> Any:
        """Load a named capability.

        Not directly sealed — but setup() calls add_tool() which checks the seal.
        Must only be called from __init__ (before start()).
        """
        from .capabilities import setup_capability

        serializable_kw = {
            k: v for k, v in kwargs.items()
            if isinstance(v, (str, int, float, bool, type(None), list, dict))
        }
        self._capabilities.append((name, serializable_kw))
        try:
            mgr = setup_capability(self, name, **kwargs)
        except Exception:
            # Roll back the entry so _capabilities only lists registered caps.
            self._capabilities.pop()
            raise
        self._capability_managers[name] = mgr
        return mgr

    def _install_intrinsic_manuals(self) -> None:
        """Wipe and rewrite ``.library/intrinsic/`` from kernel-shipped manuals.

        Runs near the end of ``__init__`` and ``_setup_from_init``. Installs
        every capability's ``manual/`` bundle into
        ``.library/intrinsic/capabilities/<name>/``, **regardless of whether
        this agent enabled the capability**. The library is kernel-shipped
        documentation — agents should be able to read about a capability
        before they configure it.

        Never touches ``.library/custom/``. That is the agent's territory.
        """
        import shutil
        import lingtai.capabilities as caps_pkg
        import lingtai.core as core_pkg
        import lingtai.intrinsic_skills as skills_pkg

        library_dir = self._working_dir / ".library"
        intrinsic_dir = library_dir / "intrinsic"

        (library_dir / "custom").mkdir(parents=True, exist_ok=True)

        if intrinsic_dir.exists():
            shutil.rmtree(intrinsic_dir)
        (intrinsic_dir / "capabilities").mkdir(parents=True, exist_ok=True)

        def install_from(pkg, subdir: str) -> None:
            pkg_file = getattr(pkg, "__file__", None)
            if not pkg_file:
                return
            pkg_root = Path(pkg_file).parent
            for entry in sorted(pkg_root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("_"):
                    continue
                src = entry / "manual"
                if src.is_dir():
                    shutil.copytree(src, intrinsic_dir / subdir / entry.name)

        def install_skills_from(pkg, subdir: str) -> None:
            """Install standalone skill bundles (no companion code, no manual/ wrapper).

            Each ``<pkg>/<entry>/`` directory IS the skill — copied verbatim into
            ``intrinsic/<subdir>/<entry>/``. Used for documentation-only skills
            like ``lingtai-kernel-anatomy`` that don't belong to any single tool.
            """
            pkg_file = getattr(pkg, "__file__", None)
            if not pkg_file:
                return
            pkg_root = Path(pkg_file).parent
            for entry in sorted(pkg_root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("_"):
                    continue
                shutil.copytree(entry, intrinsic_dir / subdir / entry.name)

        # core/ and capabilities/ both install into intrinsic/capabilities/ —
        # agents see one flat capability namespace.
        install_from(core_pkg, "capabilities")
        install_from(caps_pkg, "capabilities")
        install_skills_from(skills_pkg, "capabilities")

        # If the skills capability is loaded, re-run its reconcile now that
        # the manuals are on disk — so the injected catalog reflects them on
        # the very first turn (skills.setup()'s initial _reconcile ran BEFORE
        # install, when the manual dir was empty).
        for cap_name, cap_kwargs in self._capabilities:
            if cap_name == "skills":
                try:
                    from .core import skills as skillsmod
                    skillsmod._reconcile(self, list(cap_kwargs.get("paths", []) or []))
                except Exception as e:
                    self._log("skills_reconcile_failed", reason=str(e))
                break

    _SENSITIVE_KEYS = {"api_key", "api_key_env", "api_secret", "token", "password"}

    #: Safelist for the public ``llm`` block surfaced in .agent.json. Mirrors
    #: ``base_agent.identity._LLM_PUBLIC_KEYS`` and exists at the wrapper layer
    #: as defense-in-depth — init.json's ``manifest.llm`` may carry api_key /
    #: api_key_env values that must never reach the on-disk manifest or the
    #: system prompt's identity section.
    _LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")

    #: Safelist for the public ``preset`` block. ``active`` and ``default`` are
    #: path strings, ``allowed`` is a list of path strings — none of these
    #: carry secrets, but pinning the safelist guards against future preset
    #: schema growth that might introduce sensitive fields.
    _PRESET_PUBLIC_KEYS = ("active", "default", "allowed")

    def _build_manifest(self) -> dict:
        """Extend kernel manifest with capabilities, preset, and combo.

        Strips sensitive fields (api_key, etc.) from capability kwargs
        so they don't leak into the system prompt or outgoing mail identity.
        Adds a sanitized ``preset`` block (active/default/allowed) and
        re-applies the ``llm`` safelist for defense-in-depth — even if a
        future LLMService grew a sensitive attribute, the manifest never
        carries anything outside ``_LLM_PUBLIC_KEYS``.
        """
        data = super()._build_manifest()
        caps = getattr(self, "_capabilities", None)
        if caps:
            data["capabilities"] = [
                (name, {k: v for k, v in kw.items() if k not in self._SENSITIVE_KEYS})
                for name, kw in caps
            ]
        if self._combo_name:
            data["combo"] = self._combo_name

        # Enforce the llm safelist a second time — the kernel layer already
        # filters, but a subclass override or future service shape might add
        # a non-safelisted attribute. Doing it here means anything written
        # to disk is guaranteed safelist-only.
        if isinstance(data.get("llm"), dict):
            data["llm"] = {
                k: v for k, v in data["llm"].items()
                if k in self._LLM_PUBLIC_KEYS
            }
            if not data["llm"]:
                del data["llm"]

        preset = self._read_preset_from_init()
        if preset:
            data["preset"] = preset

        return data

    def _read_preset_from_init(self) -> dict:
        """Read ``manifest.preset`` from init.json and sanitize.

        Returns ``{}`` if init.json is missing, unreadable, or has no preset
        block — bare init.json (e.g. tests) and pre-preset deployments both
        silently fall through. Never raises: a corrupt init.json must not
        break manifest writes.

        Filters to ``_PRESET_PUBLIC_KEYS`` and string/list-of-string values so
        the disk manifest never carries anything the safelist doesn't explicitly
        allow.
        """
        import json

        init_path = self._working_dir / "init.json"
        if not init_path.is_file():
            return {}
        try:
            raw = json.loads(init_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        manifest = raw.get("manifest") if isinstance(raw, dict) else None
        if not isinstance(manifest, dict):
            return {}
        preset = manifest.get("preset")
        if not isinstance(preset, dict):
            return {}
        clean: dict = {}
        for key in self._PRESET_PUBLIC_KEYS:
            if key not in preset:
                continue
            val = preset[key]
            if key in {"active", "default"}:
                if isinstance(val, str) and val:
                    clean[key] = val
                continue
            if key == "allowed" and isinstance(val, list):
                allowed = [item for item in val if isinstance(item, str) and item]
                if allowed:
                    clean[key] = allowed
        return clean

    def _refresh_tool_inventory_section(self) -> None:
        """Refresh the 'tools' section — wrapper override includes MCP schemas."""
        lang = self._config.language
        lines = []
        from lingtai_kernel.intrinsics import ALL_INTRINSICS
        for name in self._intrinsics:
            info = ALL_INTRINSICS.get(name)
            if info:
                lines.append(f"### {name}\n{info['module'].get_description(lang)}")
        for s in self._tool_schemas:
            if s.description:
                lines.append(f"### {s.name}\n{s.description}")
        if lines:
            self._prompt_manager.write_section(
                "tools", "\n\n".join(lines), protected=True
            )

    def _build_system_prompt(self) -> str:
        """Override kernel's prompt builder to inject tool descriptions."""
        self._refresh_tool_inventory_section()
        return build_system_prompt(
            prompt_manager=self._prompt_manager,
            language=self._config.language,
        )

    def _build_system_prompt_batches(self) -> list[str]:
        """Override kernel's batched builder to inject tool descriptions."""
        from lingtai_kernel.prompt import build_system_prompt_batches
        self._refresh_tool_inventory_section()
        return build_system_prompt_batches(
            prompt_manager=self._prompt_manager,
            language=self._config.language,
        )

    def _load_mcp_from_workdir(self) -> None:
        """Auto-load MCP servers from two sources, in order:

        1. ``working_dir/mcp/servers.json`` — legacy, ungated. Loaded as-is.
        2. ``init.json`` top-level ``mcp`` field — gated by the per-agent
           registry at ``working_dir/mcp_registry.jsonl``. An init.json mcp
           entry whose name is not in the registry is skipped with a warning.

        Both sources accept stdio and HTTP entries:

            {
              "vision-server": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@z_ai/mcp-server"],
                "env": {"Z_AI_API_KEY": "...", "Z_AI_MODE": "ZAI"}
              },
              "web-search": {
                "type": "http",
                "url": "https://api.z.ai/api/mcp/web_search_prime/mcp",
                "headers": {"Authorization": "Bearer ..."}
              }
            }

        The ``type`` field defaults to ``"stdio"`` if omitted.

        Side effect: every init.json mcp entry whose name was registered
        is recorded in ``self._mcp_init_specs``. The ``_retry_failed_mcps``
        helper consults this dict on ``system(action="refresh")`` to detect
        and re-spawn MCPs whose subprocess died (issue #34).
        """
        import json

        from lingtai_kernel.logging import get_logger
        logger = get_logger()

        # Per-name tracking of init.json MCP launches. Populated below and
        # consulted by `_retry_failed_mcps`. Reset on every load so that
        # entries removed from init.json drop out of the retry pool.
        self._mcp_init_specs: dict[str, dict] = {}

        # LICC env injection — every spawned MCP gets these so it can
        # locate the agent's working dir + know its own registry name and
        # write events into the LICC inbox. User-supplied env in cfg wins.
        licc_env = {
            "LINGTAI_AGENT_DIR": str(self._working_dir),
        }

        def _spawn(name: str, cfg: dict, source: str) -> object | None:
            """Return the MCPClient/HTTPMCPClient on success, None on failure."""
            # Snapshot the client list pre-spawn so we can identify the new
            # client connect_mcp* appended (avoids changing connect_mcp's
            # public return contract — it returns tool names, not the client).
            pre_clients = list(getattr(self, "_mcp_clients", []) or [])
            try:
                server_type = cfg.get("type", "stdio")
                if server_type == "http":
                    if "url" not in cfg:
                        return None
                    tools = self.connect_mcp_http(
                        url=cfg["url"],
                        headers=cfg.get("headers"),
                    )
                else:
                    if "command" not in cfg:
                        return None
                    # Merge: LICC defaults < per-MCP env (user-supplied).
                    # Add LINGTAI_MCP_NAME per-spawn so each MCP knows its
                    # own registry name without needing to be told elsewhere.
                    merged_env = {
                        **licc_env,
                        "LINGTAI_MCP_NAME": name,
                        **(cfg.get("env") or {}),
                    }
                    tools = self.connect_mcp(
                        command=cfg["command"],
                        args=cfg.get("args"),
                        env=merged_env,
                    )
                logger.info("[%s] MCP %s (%s): loaded %d tools (%s)",
                            self.agent_name, name, source, len(tools),
                            ", ".join(tools))
                # Identify the client connect_mcp* just appended.
                post_clients = list(getattr(self, "_mcp_clients", []) or [])
                new_clients = post_clients[len(pre_clients):]
                # connect_mcp / connect_mcp_http always append exactly one.
                return new_clients[-1] if new_clients else None
            except Exception as e:
                logger.warning("[%s] MCP %s (%s): failed to load: %s",
                               self.agent_name, name, source, e)
                return None

        # Source 1: legacy mcp/servers.json
        legacy_config = self._working_dir / "mcp" / "servers.json"
        if legacy_config.is_file():
            try:
                servers = json.loads(legacy_config.read_text())
                if isinstance(servers, dict):
                    for name, cfg in servers.items():
                        if isinstance(cfg, dict):
                            _spawn(name, cfg, source="mcp/servers.json")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("[%s] mcp/servers.json: failed to read: %s",
                               self.agent_name, e)

        # Source 2: init.json top-level mcp section, gated by registry.
        init_path = self._working_dir / "init.json"
        if not init_path.is_file():
            return
        try:
            init_data = json.loads(init_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        init_mcp = init_data.get("mcp")
        if not isinstance(init_mcp, dict) or not init_mcp:
            return

        # Cross-reference against the registry.
        try:
            from .core.mcp import read_registry
            registered, _problems = read_registry(self._working_dir)
            registered_names = {r["name"] for r in registered}
        except Exception as e:
            logger.warning("[%s] mcp registry read failed: %s",
                           self.agent_name, e)
            registered_names = set()

        for name, cfg in init_mcp.items():
            if not isinstance(cfg, dict):
                continue
            if name not in registered_names:
                logger.warning(
                    "[%s] init.json mcp %r: skipped — not in mcp_registry.jsonl. "
                    "Register it first (see mcp-manual skill).",
                    self.agent_name, name,
                )
                continue
            client = _spawn(name, cfg, source="init.json:mcp")
            # Record every registered init.json mcp entry — failures (client
            # is None) and successes alike — so `_retry_failed_mcps` can
            # tell which ones to re-attempt vs leave alone.
            self._mcp_init_specs[name] = {
                "cfg": cfg,
                "source": "init.json:mcp",
                "client": client,
            }

    def _retry_failed_mcps(self) -> dict:
        """Re-spawn any init.json MCP whose subprocess is dead or never started.

        Walks ``self._mcp_init_specs`` (populated by ``_load_mcp_from_workdir``)
        and, for each entry whose tracked client is missing or visibly
        unhealthy, tears it down and re-attempts the spawn with the original
        config. Returns a report dict ``{retried: [...], recovered: [...],
        still_failed: [...], healthy: [...]}``.

        Why this exists: ``system(action="refresh")`` is the documented
        "fix config → refresh" recovery path for curated addons (imap,
        telegram, feishu, wechat). Without this retry, an MCP that exited
        during initial boot stays dead until full process restart — see
        Lingtai-AI/lingtai#34.

        Health check: missing client (boot-time spawn raised) is the
        clearest signal. For clients that registered but whose subprocess
        later died, ``MCPClient.is_connected()`` is the cheapest probe — it
        returns False when the background loop has exited (which happens
        when the stdio transport closes due to subprocess death).
        """
        from lingtai_kernel.logging import get_logger
        logger = get_logger()

        specs = getattr(self, "_mcp_init_specs", None)
        if not specs:
            return {"retried": [], "recovered": [], "still_failed": [],
                    "healthy": []}

        retried: list[str] = []
        recovered: list[str] = []
        still_failed: list[str] = []
        healthy: list[str] = []

        # LICC env (must mirror _load_mcp_from_workdir).
        licc_env = {
            "LINGTAI_AGENT_DIR": str(self._working_dir),
        }

        for name, spec in list(specs.items()):
            client = spec.get("client")
            cfg = spec.get("cfg") or {}
            source = spec.get("source", "init.json:mcp")

            # Health: client present AND its session is connected.
            if client is not None and getattr(client, "is_connected", lambda: False)():
                healthy.append(name)
                continue

            retried.append(name)
            self._log("mcp_retry_attempt", name=name, source=source)

            # Tear down the dead client (if any). connect_mcp* will append a
            # fresh one. We also remove the dead client from
            # self._mcp_clients so stop()/refresh teardown does not double-
            # close it.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
                clients = getattr(self, "_mcp_clients", None)
                if isinstance(clients, list):
                    try:
                        clients.remove(client)
                    except ValueError:
                        pass

            # Re-attempt the spawn. Mirrors the dispatch in
            # `_load_mcp_from_workdir._spawn` — kept inline (not factored)
            # to avoid leaking the closure-captured `licc_env` / logger.
            pre_clients = list(getattr(self, "_mcp_clients", []) or [])
            new_client: object | None = None
            try:
                server_type = cfg.get("type", "stdio")
                if server_type == "http":
                    if "url" not in cfg:
                        raise ValueError("http transport requires 'url'")
                    self.connect_mcp_http(
                        url=cfg["url"],
                        headers=cfg.get("headers"),
                    )
                else:
                    if "command" not in cfg:
                        raise ValueError("stdio transport requires 'command'")
                    merged_env = {
                        **licc_env,
                        "LINGTAI_MCP_NAME": name,
                        **(cfg.get("env") or {}),
                    }
                    self.connect_mcp(
                        command=cfg["command"],
                        args=cfg.get("args"),
                        env=merged_env,
                    )
                post_clients = list(getattr(self, "_mcp_clients", []) or [])
                new = post_clients[len(pre_clients):]
                new_client = new[-1] if new else None
            except Exception as e:
                logger.warning("[%s] MCP %s (%s): retry failed: %s",
                               self.agent_name, name, source, e)
                self._log("mcp_retry_failed", name=name, error=str(e))
                spec["client"] = None
                still_failed.append(name)
                continue

            spec["client"] = new_client
            if new_client is not None and getattr(
                new_client, "is_connected", lambda: False)():
                logger.info("[%s] MCP %s (%s): retry recovered",
                            self.agent_name, name, source)
                self._log("mcp_retry_recovered", name=name)
                recovered.append(name)
            else:
                # Spawn returned without raising but the client is not
                # connected — treat as still failed.
                self._log("mcp_retry_failed", name=name,
                          error="client not connected after retry")
                still_failed.append(name)

        return {
            "retried": retried,
            "recovered": recovered,
            "still_failed": still_failed,
            "healthy": healthy,
        }

    def _cpr_agent(self, address: str) -> "Agent | None":
        """Resuscitate a suspended agent by launching it as a detached process.

        Uses the resolved venv Python to run `lingtai-agent run <dir>`.
        The target must have init.json to boot from.
        """
        import subprocess
        from lingtai_kernel.handshake import is_agent, resolve_address
        from lingtai.venv_resolve import resolve_venv, venv_python

        base_dir = self._working_dir.parent
        target = resolve_address(address, base_dir)
        if not is_agent(target):
            return None

        init_path = target / "init.json"
        if not init_path.is_file():
            self._log("cpr_no_init", path=str(target))
            return None

        # Clean stale signal files so a CPR'd agent boots cleanly.
        for sig in (".suspend", ".sleep", ".interrupt"):
            sig_file = target / sig
            if sig_file.is_file():
                sig_file.unlink(missing_ok=True)

        # Resolve Python: target's init.json venv_path → global runtime
        try:
            import json as _json
            target_data = _json.loads(init_path.read_text())
        except (ValueError, OSError):
            target_data = None
        venv_dir = resolve_venv(target_data)
        python = venv_python(venv_dir)
        cmd = [python, "-m", "lingtai", "run", str(target)]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._log("cpr_launched", target=str(target), pid=proc.pid)
        return True  # non-None signals success to the kernel

    def start(self) -> None:
        super().start()
        # LICC poller: watch .mcp_inbox/ for events from out-of-process MCPs.
        from .core.mcp.inbox import MCPInboxPoller
        self._mcp_inbox_poller = MCPInboxPoller(self)
        self._mcp_inbox_poller.start()

    def connect_mcp(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Connect to an MCP server and auto-register all its tools.

        Args:
            command: Executable to run (e.g., "uvx", "xhelio-spice-mcp").
            args: Arguments to the command.
            env: Environment variables for the subprocess.

        Returns:
            List of registered tool names.
        """
        from .services.mcp import MCPClient

        client = MCPClient(command=command, args=args, env=env)
        client.start()

        # Track for cleanup
        if not hasattr(self, "_mcp_clients"):
            self._mcp_clients: list = []
        self._mcp_clients.append(client)

        # List tools and register each one
        tools = client.list_tools()
        registered = []
        for tool in tools:
            name = tool["name"]

            def _make_handler(c: MCPClient, tool_name: str):
                def handler(tool_args: dict) -> dict:
                    return c.call_tool(tool_name, tool_args)
                return handler

            # Extract schema properties (MCP uses inputSchema with JSON Schema)
            schema = tool.get("schema", {})
            # Remove top-level keys that aren't valid for our FunctionSchema
            schema.pop("additionalProperties", None)

            self.add_tool(
                name,
                schema=schema,
                handler=_make_handler(client, name),
                description=tool.get("description", ""),
            )
            registered.append(name)

        return registered

    def connect_mcp_http(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[str]:
        """Connect to a remote HTTP MCP server and auto-register all its tools.

        Args:
            url: HTTP endpoint of the MCP server.
            headers: HTTP headers (e.g., {"Authorization": "Bearer ..."}).

        Returns:
            List of registered tool names.
        """
        from .services.mcp import HTTPMCPClient

        client = HTTPMCPClient(url=url, headers=headers)
        client.start()

        if not hasattr(self, "_mcp_clients"):
            self._mcp_clients: list = []
        self._mcp_clients.append(client)

        tools = client.list_tools()
        registered = []
        for tool in tools:
            name = tool["name"]

            def _make_handler(c: HTTPMCPClient, tool_name: str):
                def handler(tool_args: dict) -> dict:
                    return c.call_tool(tool_name, tool_args)
                return handler

            schema = tool.get("schema", {})
            schema.pop("additionalProperties", None)

            self.add_tool(
                name,
                schema=schema,
                handler=_make_handler(client, name),
                description=tool.get("description", ""),
            )
            registered.append(name)

        return registered

    def stop(self, timeout: float = 5.0) -> None:
        # Stop LICC poller before closing MCP clients so any in-flight events
        # finish dispatching before subprocess teardown.
        poller = getattr(self, "_mcp_inbox_poller", None)
        if poller is not None:
            try:
                poller.stop()
            except Exception:
                pass

        # Close MCP clients
        for client in getattr(self, "_mcp_clients", []):
            try:
                client.close()
            except Exception:
                pass

        super().stop(timeout=timeout)

    def has_capability(self, name: str) -> bool:
        """Check if a capability is registered."""
        return name in self._capability_managers

    def get_capability(self, name: str) -> Any:
        """Return the manager instance for a registered capability, or None."""
        return self._capability_managers.get(name)

    # ------------------------------------------------------------------
    # Deep refresh — full reconstruct from init.json
    # ------------------------------------------------------------------

    def _read_init(self) -> dict | None:
        """Read and validate init.json from working directory.

        If ``manifest.preset.active`` is set, materialize the named preset's
        ``llm`` and ``capabilities`` into the manifest before validation. The
        running agent thus always sees a fully resolved manifest.
        """
        import json
        from .init_schema import strip_deprecated, validate_init
        from .config_resolve import resolve_paths
        from .presets import expand_inherit, materialize_active_preset

        init_path = self._working_dir / "init.json"
        if not init_path.is_file():
            return None

        try:
            data = json.loads(init_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            self._log("refresh_init_error", error="failed to read init.json")
            return None

        # Materialize active preset, if any, BEFORE validation so the manifest
        # the schema validates is the fully-resolved one the agent will run on.
        try:
            materialize_active_preset(data, self._working_dir)
        except (KeyError, ValueError) as e:
            self._log("refresh_init_error",
                      error=f"preset materialization failed: {e}")
            return None

        # Strip deprecated fields before validation so they don't trigger
        # warnings or interfere with the refresh path.
        stripped = strip_deprecated(data)
        if stripped:
            try:
                init_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass  # best-effort disk cleanup

        # Resolve "provider": "inherit" in capabilities against the main LLM.
        manifest = data.get("manifest")
        if isinstance(manifest, dict):
            llm = manifest.get("llm") or {}
            caps = manifest.get("capabilities") or {}
            if isinstance(caps, dict):
                expand_inherit(caps, llm)

        try:
            warnings = validate_init(data)
        except ValueError as e:
            self._log("refresh_init_error", error=str(e))
            return None
        for w in warnings:
            self._log("refresh_init_warning", warning=w)

        resolve_paths(data, self._working_dir)
        return data

    def _activate_preset(self, name: str) -> None:
        """Substitute a preset's llm + capabilities into init.json on disk.

        `name` is the preset's path (absolute, ~-prefixed, or relative to
        working_dir). Substitutes the file's `manifest.llm` and
        `manifest.capabilities` into the agent's init.json, sets
        `manifest.preset.active = name` (storing the path string verbatim —
        no canonicalization), and writes atomically.

        Other manifest fields are preserved.

        Raises:
            KeyError: the preset file does not exist
            ValueError: the preset file is malformed or the name is invalid
            OSError: the on-disk write failed (init.json untouched)
        """
        import json
        import os
        from .presets import load_preset

        init_path = self._working_dir / "init.json"
        data = json.loads(init_path.read_text(encoding="utf-8"))
        manifest = data.setdefault("manifest", {})

        preset = load_preset(name, working_dir=self._working_dir)
        preset_manifest = preset.get("manifest", {})

        preset_llm = dict(preset_manifest.get("llm") or manifest.get("llm") or {})
        # context_limit lives inside manifest.llm in the preset, but stays
        # at manifest root in init.json — strip it from the llm dict before
        # substitution and write it to the root.
        preset_ctx = preset_llm.pop("context_limit", None)
        manifest["llm"] = preset_llm
        manifest["capabilities"] = preset_manifest.get(
            "capabilities", manifest.get("capabilities", {}))
        if preset_ctx is not None:
            manifest["context_limit"] = preset_ctx

        # Set active in the umbrella. Preserve default if already set; otherwise
        # initialize default to the same value as active (first activation).
        # Also ensure `name` appears in `allowed` — _activate_preset is the
        # final gate and the manifest must remain self-consistent. The caller
        # (system._refresh) also validates against `allowed` before invoking
        # us; this is belt-and-braces for direct callers and AED auto-fallback.
        preset_block = manifest.setdefault("preset", {})
        preset_block["active"] = name
        if not preset_block.get("default"):
            preset_block["default"] = name
        allowed = preset_block.get("allowed")
        if not isinstance(allowed, list):
            preset_block["allowed"] = [name]
            self._log("preset_allowed_widened", name=name,
                      reason="allowed_field_initialized")
        elif name not in allowed:
            preset_block["allowed"] = [*allowed, name]
            self._log("preset_allowed_widened", name=name,
                      reason="direct_activate_bypassed_gate")

        # Atomic write
        tmp = init_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(str(tmp), str(init_path))

    def _activate_default_preset(self) -> None:
        """Read manifest.preset.default and activate it. Used by AED auto-fallback."""
        import json
        data = json.loads((self._working_dir / "init.json").read_text(encoding="utf-8"))
        preset = data.get("manifest", {}).get("preset") or {}
        default_name = preset.get("default")
        if not default_name:
            raise RuntimeError("no default preset configured")
        self._activate_preset(default_name)

    def _setup_from_init(self) -> None:
        """Full construct/reconstruct from init.json."""
        self._log("refresh_start")

        data = self._read_init()
        if data is None:
            self._log("refresh_skipped", reason="no valid init.json")
            return

        from .config_resolve import (
            load_env_file,
            resolve_env,
            resolve_file,
            _resolve_capabilities,
        )
        from lingtai_kernel.config import AgentConfig

        env_file = data.get("env_file")
        if env_file:
            load_env_file(env_file)

        # Resolve *_file fields for top-level text content.
        # Note: "soul" / "soul_file" were retired in v0.7.6 and are now
        # stripped by strip_deprecated() before we get here.
        for key in ("covenant", "principle", "substrate", "procedures",
                    "brief", "pad", "prompt", "comment"):
            file_key = f"{key}_file"
            if file_key in data:
                data[key] = resolve_file(data.get(key), data.pop(file_key))

        m = data["manifest"]

        # Save conversation history
        saved_interface = None
        if self._session.chat is not None:
            saved_interface = self._session.chat.interface

        # Tear down
        # Cancel soul timer to prevent racing on config/service during rebuild
        self._cancel_soul_timer()

        for client in getattr(self, "_mcp_clients", []):
            try:
                client.close()
            except Exception:
                pass
        self._mcp_clients = []

        self._sealed = False
        self._tool_handlers.clear()
        self._tool_schemas.clear()
        self._capabilities.clear()
        self._capability_managers.clear()

        self._intrinsics.clear()
        self._wire_intrinsics()

        # Reset capability-owned flags (email.boot below resets to "email box"/"email")
        self._mailbox_name = "email box"
        self._mailbox_tool = "email"
        if hasattr(self, "_post_molt_hooks"):
            self._post_molt_hooks.clear()

        # Reset prompt manager
        self._prompt_manager._sections.clear()

        # Reconstruct LLM service if changed
        llm = m["llm"]
        api_key = resolve_env(llm.get("api_key"), llm.get("api_key_env"))
        new_provider = llm["provider"]
        new_model = llm["model"]
        new_base_url = llm.get("base_url")

        # Default 60 matches AgentConfig.max_rpm — existing agents whose
        # init.json predates this field cooperatively share the network-wide
        # 60 RPM cap by default. Set to 0 in init.json to disable gating.
        new_max_rpm = m.get("max_rpm", 60)
        new_provider_key = new_provider.lower()
        new_per_provider: dict = {}
        if new_max_rpm > 0:
            new_per_provider["max_rpm"] = new_max_rpm
        new_user_headers = llm.get("default_headers")
        if isinstance(new_user_headers, dict) and new_user_headers:
            new_per_provider["default_headers"] = dict(new_user_headers)
        new_provider_defaults: dict | None = (
            {new_provider_key: new_per_provider} if new_per_provider else None
        )

        if (
            new_provider != self.service.provider
            or new_model != self.service.model
            or new_base_url != getattr(self.service, "_base_url", None)
            or new_max_rpm != getattr(self.service, "_provider_defaults", {}).get(
                new_provider.lower(), {}
            ).get("max_rpm", 0)
        ):
            self.service = LLMService(
                provider=new_provider, model=new_model,
                api_key=api_key, base_url=new_base_url,
                provider_defaults=new_provider_defaults,
            )
            self._session._llm_service = self.service

        # Reload admin from init.json (avatars have admin: {}, not inherited from parent)
        self._admin = m.get("admin", {})

        # Reload config (all fields optional — fall back to AgentConfig defaults)
        soul = m.get("soul", {})
        # NOTE: defaults here MUST mirror src/lingtai_kernel/config.py
        # AgentConfig defaults — _read_init reload re-constructs the
        # whole config and would otherwise silently override any kernel-
        # side default change with the stale literal here. Kept as
        # explicit literals for readability rather than introspecting
        # AgentConfig fields.
        self._config = AgentConfig(
            stamina=m.get("stamina", 86400.0),
            soul_delay=soul.get("delay", 99999.0),
            consultation_past_count=soul.get("consultation_past_count", 2),
            soul_voice=soul.get("voice", "inner"),
            soul_voice_prompt=soul.get("voice_prompt", ""),
            max_turns=m.get("max_turns", 50),
            language=m.get("language", "en"),
            context_limit=m.get("context_limit"),
            molt_pressure=m.get("molt_pressure", 0.8),
            molt_prompt=m.get("molt_prompt", ""),
            snapshot_interval=m.get("snapshot_interval"),
            time_awareness=m.get("time_awareness", True),
            timezone_awareness=m.get("timezone_awareness", True),
            aed_timeout=m.get("aed_timeout", 360.0),
            max_aed_attempts=m.get("max_aed_attempts", 3),
            max_rpm=new_max_rpm,
        )
        self._soul_delay = max(1.0, self._config.soul_delay)
        self._session._config = self._config

        # Reload all prompt sections (covenant, principle, procedures, brief,
        # rules, pad, comment) from init.json and disk.
        self._reload_prompt_sections(data)

        # Re-boot psyche so lingtai.md gets layered onto covenant section and
        # the post-molt hook is re-registered on the cleared hook list.
        from lingtai_kernel.intrinsics import psyche as _psyche
        _psyche.boot(self)

        # Re-boot email so a fresh EmailManager + scheduler thread are wired.
        # The previous manager's scheduler thread is daemon and was abandoned
        # by the _intrinsics.clear() above; this starts a clean one.
        from lingtai_kernel.intrinsics import email as _email
        _email.boot(self)

        # Decompress addons BEFORE capability setup so the `mcp` capability
        # sees the populated registry on its first reconcile.
        addons = data.get("addons") or []
        if addons:
            try:
                from .core.mcp import decompress_addons
                report = decompress_addons(self._working_dir, addons)
                self._log("mcp_decompress", **report)
            except Exception as e:
                self._log("mcp_decompress_failed", reason=str(e))

        # Re-run capability setup. init.json declares overrides/opt-ins;
        # `apply_core_defaults` ensures the `lingtai.core.*` floor boots even
        # when the manifest omits it. `manifest.disable` and `"name": null`
        # entries are the opt-out channels.
        raw_caps = m.get("capabilities", {}) or {}
        resolved = _resolve_capabilities(raw_caps)
        # Preserve null sentinels through env-resolution (it converts None to {}).
        null_outs = {n for n, v in raw_caps.items() if v is None}

        from .capabilities import (
            _GROUPS,
            apply_core_defaults,
            normalize_capabilities,
        )
        expanded: dict[str, Any] = {}
        for name, cap_kwargs in resolved.items():
            if name in _GROUPS:
                for sub in _GROUPS[name]:
                    expanded[sub] = {}
            elif name in null_outs:
                expanded[name] = None
            elif cap_kwargs is None:
                expanded[name] = None
            else:
                expanded[name] = cap_kwargs
        normalized = normalize_capabilities(
            {n: (v if v is not None else {}) for n, v in expanded.items()}
        )
        for n, v in expanded.items():
            if v is None:
                normalized[n] = None  # type: ignore[assignment]

        disable_list = m.get("disable") or []
        capabilities = apply_core_defaults(normalized, disable=disable_list)

        if capabilities:
            for name, cap_kwargs in capabilities.items():
                try:
                    self._setup_capability(name, **cap_kwargs)
                except (ValueError, ImportError, TypeError) as e:
                    self._log("capability_skipped", capability=name, reason=str(e))

        # Install intrinsic manuals (wipe-and-rewrite .library/intrinsic/)
        # from the bundles shipped with each enabled capability.
        self._install_intrinsic_manuals()

        # Register system prompt reload as post-molt hook — molt should
        # reconstruct the system prompt the same way refresh does.
        if not hasattr(self, "_post_molt_hooks"):
            self._post_molt_hooks = []
        self._post_molt_hooks.append(self._reload_prompt_sections)

        # Reload MCP
        self._load_mcp_from_workdir()

        # Persist LLM config
        self._persist_llm_config()

        # Re-write manifest and identity
        self._update_identity()

        # Re-seal
        self._sealed = True

        # Rebuild session with preserved history
        if saved_interface is not None:
            self._session._rebuild_session(saved_interface)

        self._log(
            "refresh_complete",
            capabilities=[name for name, _ in self._capabilities],
            tools=list(self._tool_handlers.keys()),
        )

    def _reload_prompt_sections(self, data: dict | None = None) -> None:
        """Re-read all prompt sections from init.json and disk.

        Called by _setup_from_init() on refresh (with pre-resolved data) and
        as a post-molt hook (no args — re-reads init.json from scratch).
        Ensures the system prompt after molt is identical to after refresh.
        """
        if data is None:
            data = self._read_init()
            if data is None:
                return
            # Resolve *_file fields (brief_file, covenant_file, etc.)
            from .config_resolve import resolve_file
            for key in ("covenant", "principle", "substrate", "procedures",
                        "brief", "pad", "comment"):
                file_key = f"{key}_file"
                if file_key in data:
                    data[key] = resolve_file(data.get(key), data.pop(file_key))

        system_dir = self._working_dir / "system"
        system_dir.mkdir(exist_ok=True)

        # --- Covenant ---
        covenant = data.get("covenant", "")
        covenant_file = system_dir / "covenant.md"
        if covenant:
            covenant_file.write_text(covenant)
        elif covenant_file.is_file():
            covenant = covenant_file.read_text()
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)

        # --- Substrate (kernel-owned, cross-app stable; #39) ---
        # The substrate section sits right after `## tools` and describes
        # the agent's architecture to itself (tool tiers, data-flow
        # topology, life states, channel discipline, attention model).
        #
        # Resolution precedence:
        #   1. data["substrate"]          — inline init.json string
        #   2. system/substrate.md        — agent's own copy (editable)
        #   3. packaged prompts/substrate.md — kernel default (v1)
        #
        # Every agent gets the section populated by default. To opt out,
        # set `"substrate": " "` (a single space, written to system/
        # substrate.md and rendered as effectively blank).
        substrate = data.get("substrate", "")
        substrate_file = system_dir / "substrate.md"
        if substrate:
            substrate_file.write_text(substrate)
        elif substrate_file.is_file():
            substrate = substrate_file.read_text()
        else:
            # Packaged default — write to system_dir so the agent has its
            # own editable copy on disk going forward.
            try:
                from importlib.resources import files
                packaged = files("lingtai.prompts").joinpath("substrate.md").read_text()
                substrate_file.write_text(packaged)
                substrate = packaged
            except (FileNotFoundError, ModuleNotFoundError, OSError):
                substrate = ""
        if substrate:
            self._prompt_manager.write_section("substrate", substrate, protected=True)
        else:
            self._prompt_manager.delete_section("substrate")

        # --- Rules (from system/rules.md, not init.json) ---
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text().strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
                else:
                    self._prompt_manager.delete_section("rules")
            except OSError:
                pass
        else:
            self._prompt_manager.delete_section("rules")

        # --- Pad ---
        pad_file = system_dir / "pad.md"
        loaded_pad = ""
        if pad_file.is_file():
            loaded_pad = pad_file.read_text()
        if loaded_pad.strip():
            self._prompt_manager.write_section("pad", loaded_pad)

        # --- Principle ---
        principle = data.get("principle", "")
        principle_file = system_dir / "principle.md"
        if principle:
            principle_file.write_text(principle)
        elif principle_file.is_file():
            principle = principle_file.read_text()
        if principle:
            self._prompt_manager.write_section("principle", principle, protected=True)

        # --- Procedures ---
        # Resolution precedence (mirrors substrate above):
        #   1. data["procedures"]          — inline init.json string
        #   2. system/procedures.md        — agent's own copy (editable)
        #   3. packaged prompts/procedures.md — kernel default
        #
        # Every agent gets the section populated by default. To opt out,
        # set `"procedures": " "` (a single space, written to system/
        # procedures.md and rendered as effectively blank).
        procedures = data.get("procedures", "")
        procedures_file = system_dir / "procedures.md"
        if procedures:
            procedures_file.write_text(procedures)
        elif procedures_file.is_file():
            procedures = procedures_file.read_text()
        else:
            try:
                from importlib.resources import files
                packaged = files("lingtai.prompts").joinpath("procedures.md").read_text()
                procedures_file.write_text(packaged)
                procedures = packaged
            except (FileNotFoundError, ModuleNotFoundError, OSError):
                procedures = ""
        if procedures:
            self._prompt_manager.write_section("procedures", procedures, protected=True)
        else:
            self._prompt_manager.delete_section("procedures")

        # --- Brief (externally-maintained, written by secretary) ---
        brief = data.get("brief", "")
        brief_file = system_dir / "brief.md"
        if brief:
            brief_file.write_text(brief)
        elif brief_file.is_file():
            brief = brief_file.read_text()
        if brief:
            self._prompt_manager.write_section("brief", brief, protected=True)
        else:
            self._prompt_manager.delete_section("brief")

        # --- Comment ---
        comment = data.get("comment", "")
        if comment:
            self._prompt_manager.write_section("comment", comment)
        else:
            self._prompt_manager.delete_section("comment")

    def _build_launch_cmd(self) -> list[str] | None:
        """Return the command to relaunch this agent via lingtai-agent run."""
        from .venv_resolve import resolve_venv, venv_python
        data = self._read_init()
        venv_dir = resolve_venv(data)
        python = venv_python(venv_dir)
        return [python, "-m", "lingtai", "run", str(self._working_dir)]
