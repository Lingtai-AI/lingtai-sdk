"""Tests for preset-driven capability surface on daemon emanations.

When a per-task preset is given, the emanation's tool surface comes from
the preset's manifest.capabilities (instantiated in a sandbox with
expand_inherit against the preset's LLM), unioned with the parent's MCP
tools, minus the EMANATION_BLACKLIST. When omitted, the parent's
currently registered surface is used (existing behavior).
"""
import json
import queue
from unittest.mock import MagicMock, patch

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.llm.base import FunctionSchema


def _make_agent(tmp_path, capabilities=None, presets_dir=None):
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )
    if presets_dir is not None:
        # Build `allowed` from the directory contents so the daemon can
        # resolve specs by path. The daemon test previously relied on
        # path-based directory scanning; under the allowed-paths schema
        # we list each preset file explicitly.
        from pathlib import Path
        allowed_paths = [
            str(p) for p in sorted(Path(presets_dir).glob("*.json"))
            if p.name != "_kernel_meta.json"
        ]
        active = allowed_paths[0] if allowed_paths else "mock"
        agent._read_init = lambda: {
            "manifest": {
                "preset": {
                    "active": active,
                    "default": active,
                    "allowed": allowed_paths or [active],
                },
                "llm": {"provider": "mock", "model": "mock-model"},
            }
        }
    return agent


def _write_preset(presets_dir, name, capabilities, provider="deepseek",
                  model="deepseek-v3", api_key_env="DEEPSEEK_API_KEY"):
    preset = {
        "name": name,
        "description": {"summary": f"{name} preset"},
        "manifest": {
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": None,
                "api_key_env": api_key_env,
            },
            "capabilities": capabilities,
        },
    }
    (presets_dir / f"{name}.json").write_text(json.dumps(preset))


# ---------------------------------------------------------------------------
# _ToolCollector unit tests
# ---------------------------------------------------------------------------

def test_collector_captures_add_tool_calls():
    from lingtai.core.daemon import _ToolCollector

    parent = MagicMock()
    collector = _ToolCollector(parent)

    collector.add_tool("foo", schema={"type": "object"},
                       handler=lambda args: {"ok": True}, description="foo desc")
    collector.add_tool("bar", schema={"type": "object"},
                       handler=lambda args: {"ok": True})

    assert "foo" in collector.schemas
    assert "bar" in collector.schemas
    assert isinstance(collector.schemas["foo"], FunctionSchema)
    assert collector.schemas["foo"].description == "foo desc"
    assert callable(collector.handlers["foo"])


def test_collector_forwards_unknown_attrs_to_parent():
    from lingtai.core.daemon import _ToolCollector

    parent = MagicMock()
    parent._working_dir = "/tmp/x"
    parent._log = MagicMock()

    collector = _ToolCollector(parent)
    # Read-through to parent
    assert collector._working_dir == "/tmp/x"
    collector._log("event", x=1)
    parent._log.assert_called_once_with("event", x=1)


def test_collector_does_not_pollute_parent_tool_registry():
    """Most important property: the parent's _tool_handlers / _tool_schemas
    must remain unchanged after collector add_tool calls."""
    from lingtai.core.daemon import _ToolCollector

    parent = MagicMock()
    parent._tool_handlers = {}
    parent._tool_schemas = []
    collector = _ToolCollector(parent)

    collector.add_tool("foo", schema={}, handler=lambda a: {})
    assert parent._tool_handlers == {}
    assert parent._tool_schemas == []


# ---------------------------------------------------------------------------
# _instantiate_preset_capabilities tests
# ---------------------------------------------------------------------------

def test_instantiate_preset_capabilities_returns_schemas_and_handlers(tmp_path):
    """Preset's capabilities (e.g. 'file' group) instantiate into the sandbox."""
    agent = _make_agent(tmp_path, ["daemon"])  # NOTE: parent has only daemon
    mgr = agent.get_capability("daemon")
    # File group expands to read/write/edit/glob/grep
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},
        {"provider": "mock", "model": "mock"},
    )
    # Each file sub-capability should register its tool
    for name in ("read", "write", "edit", "glob", "grep"):
        assert name in schemas, f"{name} not registered"
        assert name in handlers, f"{name} handler missing"


def test_instantiate_skips_unknown_capability_names(tmp_path):
    """Unknown names (not in _BUILTIN, not a group, not blacklisted) are
    skipped with a log entry. Behavior changed in the lingtai #29 fix —
    previously these raised ValueError and aborted the whole batch.
    Rationale: the TUI preset wizard sometimes writes intrinsic names
    (email, psyche, ...) into manifest.capabilities. Aborting on unknown
    names made full user presets unusable as daemon presets. Real config
    bugs in *known* capabilities still raise; see
    test_instantiate_still_raises_on_broken_known_capability."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"nonsense_capability": {}, "read": {}},
        {"provider": "mock", "model": "mock"},
    )
    assert "nonsense_capability" not in schemas
    assert "read" in schemas


def test_instantiate_skips_intrinsic_names_in_capabilities(tmp_path):
    """email/psyche/system/soul in manifest.capabilities are intrinsics, not
    capabilities — the daemon must skip them silently (not abort the batch).
    Mirrors the main Agent.__init__ tolerance for legacy/wizard-written
    presets that mix intrinsic names into the capabilities map.
    See lingtai #29."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {
            "read": {},
            "write": {},
            "email": {},      # intrinsic — should skip
            "psyche": {},     # intrinsic (also blacklisted) — should skip
            "system": {},     # intrinsic — should skip
            "soul": {},       # intrinsic — should skip
        },
        {"provider": "mock", "model": "mock"},
    )
    assert "read" in schemas
    assert "write" in schemas
    assert "email" not in schemas
    assert "psyche" not in schemas
    assert "system" not in schemas
    assert "soul" not in schemas


def test_instantiate_still_raises_on_broken_known_capability(tmp_path, monkeypatch):
    """A KNOWN capability that fails inside its setup() must still abort the
    batch — we only tolerate UNKNOWN names. This guards against the lingtai
    #29 fix swallowing real config bugs (e.g. malformed provider for
    web_search)."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    def boom(target, name, **kwargs):
        raise ValueError("simulated broken setup")

    monkeypatch.setattr("lingtai.capabilities.setup_capability", boom)
    try:
        mgr._instantiate_preset_capabilities(
            {"read": {}},  # known capability — should propagate the failure
            {"provider": "mock", "model": "mock"},
        )
    except ValueError as e:
        assert "read" in str(e)
        assert "simulated broken setup" in str(e)
    else:
        raise AssertionError("expected ValueError for broken known capability")


def test_instantiate_skips_broken_unused_known_capability(tmp_path, monkeypatch):
    """A known capability that fails setup is skipped when this task did not
    request any tool it provides."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    original_setup = __import__(
        "lingtai.capabilities", fromlist=["setup_capability"]
    ).setup_capability

    def boom_for_vision(target, name, **kwargs):
        if name == "vision":
            raise ValueError("simulated broken vision")
        return original_setup(target, name, **kwargs)

    monkeypatch.setattr("lingtai.capabilities.setup_capability", boom_for_vision)

    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"file": {}, "vision": {"provider": "codex", "api_key_env": "IGNORED"}},
        {"provider": "mock", "model": "mock"},
        required_tools={"read", "write", "edit", "glob", "grep"},
    )

    assert "read" in schemas
    assert "vision" not in schemas


def test_instantiate_raises_for_broken_required_known_capability(tmp_path, monkeypatch):
    """A known capability that fails setup is still hard when requested."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    def boom(target, name, **kwargs):
        raise ValueError("simulated broken vision")

    monkeypatch.setattr("lingtai.capabilities.setup_capability", boom)

    try:
        mgr._instantiate_preset_capabilities(
            {"vision": {"provider": "codex", "api_key_env": "IGNORED"}},
            {"provider": "mock", "model": "mock"},
            required_tools={"vision"},
        )
    except ValueError as e:
        assert "vision" in str(e)
        assert "simulated broken vision" in str(e)
    else:
        raise AssertionError("expected ValueError for broken required capability")


def test_instantiate_skips_blacklisted_capabilities(tmp_path):
    """daemon/avatar/psyche/skills/knowledge/library/codex in preset capabilities are skipped
    (not instantiated, no error)."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"daemon": {}, "avatar": {}, "knowledge": {}, "read": {}},
        {"provider": "mock", "model": "mock"},
    )
    assert "daemon" not in schemas
    assert "avatar" not in schemas
    assert "knowledge" not in schemas
    assert "read" in schemas


def test_instantiate_resolves_inherit_against_preset_llm(tmp_path):
    """provider:'inherit' in a capability kwarg gets the preset's LLM, not
    the parent's. We check this by giving the parent a 'mock' provider but
    the preset a 'gemini' provider — the resolved capability must see gemini.
    """
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    # Capture what setup_capability is called with
    captured = {}

    def fake_setup(target, name, **kwargs):
        captured[name] = kwargs

    with patch("lingtai.capabilities.setup_capability", side_effect=fake_setup):
        mgr._instantiate_preset_capabilities(
            {"web_search": {"provider": "inherit"}},
            {"provider": "gemini", "model": "gemini-pro",
             "api_key_env": "GEMINI_API_KEY"},
        )

    assert captured.get("web_search", {}).get("provider") == "gemini"
    # api credentials inherited too
    assert captured["web_search"].get("api_key_env") == "GEMINI_API_KEY"


# ---------------------------------------------------------------------------
# _build_tool_surface integration with preset_surface
# ---------------------------------------------------------------------------

def test_build_tool_surface_with_preset_uses_preset_capabilities(tmp_path):
    """Parent has only daemon; preset has 'file' — the emanation can request
    'file' tools and they resolve from the preset's surface."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    preset_schemas, preset_handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},
        {"provider": "mock", "model": "mock"},
    )
    schemas, dispatch = mgr._build_tool_surface(
        ["file"],
        preset_surface=(preset_schemas, preset_handlers),
    )
    names = {s.name for s in schemas}
    # Parent didn't have these — they came from the preset
    assert "read" in names
    assert "write" in names
    assert "grep" in names
    # Handlers wired up
    assert "read" in dispatch


def test_build_tool_surface_with_preset_unknown_tool_raises(tmp_path):
    """Even with a preset, a tool name not in (preset ∪ parent MCP) raises."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    preset_schemas, preset_handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},  # provides read/write/edit/glob/grep
        {"provider": "mock", "model": "mock"},
    )
    try:
        mgr._build_tool_surface(
            ["bogus_tool"],
            preset_surface=(preset_schemas, preset_handlers),
        )
    except ValueError as e:
        assert "bogus_tool" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_build_tool_surface_omitted_preset_uses_parent(tmp_path):
    """Regression: when preset_surface is None, parent's surface is used
    exactly like before."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface(["file"])
    names = {s.name for s in schemas}
    assert "read" in names
    assert "grep" in names
    # And the dispatch is the parent's actual handler
    assert dispatch["read"] is agent._tool_handlers["read"]


# ---------------------------------------------------------------------------
# End-to-end through _handle_emanate
# ---------------------------------------------------------------------------

def test_emanate_with_preset_instantiates_caps_for_emanation(tmp_path,
                                                              monkeypatch):
    """Parent has only ['daemon']; preset declares 'file'. Emanation can
    request 'file' tools and the daemon spawns successfully."""
    import lingtai_kernel.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "thinker", capabilities={"file": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    thinker_path = str(presets_dir / "thinker.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "scan files", "tools": ["file"], "preset": thinker_path},
        ]})

    assert result["status"] == "dispatched", result.get("message")
    # Folder was created — preset surface was satisfied
    daemons_dir = agent._working_dir / "daemons"
    assert daemons_dir.is_dir()
    assert len(list(daemons_dir.iterdir())) == 1


def test_emanate_preset_with_intrinsics_dispatches(tmp_path, monkeypatch):
    """End-to-end: preset whose manifest.capabilities mixes intrinsic names
    (email/psyche) with real capabilities should dispatch successfully —
    the intrinsic names are skipped, the real capabilities form the
    sandbox, and tools the agent requested resolve from that sandbox.

    This is the lingtai #29 repro: the TUI wizard writes 'email' into
    user presets, and the daemon used to refuse the whole batch on it."""
    import lingtai_kernel.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "wizard_style",
                  capabilities={
                      "read": {},
                      "write": {},
                      "email": {},      # intrinsic in capabilities map
                      "psyche": {},     # intrinsic + blacklisted
                  })

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "wizard_style.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "x", "tools": ["read"], "preset": preset_path},
        ]})

    assert result["status"] == "dispatched", \
        f"expected dispatched, got {result!r}"
    assert result["count"] == 1


def test_emanate_preset_request_for_email_intrinsic_dispatches(tmp_path, monkeypatch):
    """Email is now the explicit daemon-eligible intrinsic exception.

    Preset instantiation still skips intrinsic entries in manifest.capabilities,
    but a parent may request tools:["email"] and the daemon receives the parent
    email intrinsic through the normal tool-surface builder.
    """
    import lingtai_kernel.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "wizard_style",
                  capabilities={"read": {}, "email": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "wizard_style.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "x", "tools": ["email"], "preset": preset_path},
        ]})

    assert result["status"] == "dispatched", result
    assert result["count"] == 1


def test_emanate_preset_does_not_pollute_parent_tool_registry(tmp_path,
                                                                monkeypatch):
    """After a preset-driven emanation is scheduled, the parent's tool
    registry is unchanged — no preset tools leaked into the parent."""
    import lingtai_kernel.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "thinker", capabilities={"file": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    pre_handlers = set(agent._tool_handlers.keys())
    pre_schemas = {s.name for s in agent._tool_schemas}

    thinker_path = str(presets_dir / "thinker.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "x", "tools": ["file"], "preset": thinker_path},
        ]})
    assert result["status"] == "dispatched"

    # Parent unchanged
    assert set(agent._tool_handlers.keys()) == pre_handlers
    assert {s.name for s in agent._tool_schemas} == pre_schemas


def test_emanate_preset_broken_unused_vision_dispatches(tmp_path, monkeypatch):
    """File-only daemon dispatch is not blocked by broken unused vision."""
    import lingtai_kernel.preset_connectivity as preset_connectivity
    import lingtai.capabilities as capabilities
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    original_setup = capabilities.setup_capability

    def boom_for_vision(target, name, **kwargs):
        if name == "vision":
            raise ValueError("simulated broken vision")
        return original_setup(target, name, **kwargs)

    monkeypatch.setattr(capabilities, "setup_capability", boom_for_vision)

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(
        presets_dir,
        "file_plus_broken_vision",
        capabilities={
            "file": {},
            "vision": {"provider": "codex", "api_key_env": "IGNORED"},
        },
    )

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "file_plus_broken_vision.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "scan files", "tools": ["file"], "preset": preset_path},
        ]})

    assert result["status"] == "dispatched", result.get("message")
    assert result["count"] == 1


def test_emanate_preset_broken_requested_vision_fails(tmp_path, monkeypatch):
    """Requested capability setup failures remain hard errors."""
    import lingtai_kernel.preset_connectivity as preset_connectivity
    import lingtai.capabilities as capabilities
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    original_setup = capabilities.setup_capability

    def boom_for_vision(target, name, **kwargs):
        if name == "vision":
            raise ValueError("simulated broken vision")
        return original_setup(target, name, **kwargs)

    monkeypatch.setattr(capabilities, "setup_capability", boom_for_vision)

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(
        presets_dir,
        "broken_vision",
        capabilities={"vision": {"provider": "codex", "api_key_env": "IGNORED"}},
    )

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "broken_vision.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "inspect image", "tools": ["vision"], "preset": preset_path},
        ]})

    assert result["status"] == "error"
    assert "preset capability 'vision' failed to set up" in result["message"]
    assert "simulated broken vision" in result["message"]
