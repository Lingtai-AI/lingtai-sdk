"""Stage 18 (C3) — advisory-first wrapper wiring of the SDK guard bridge.

The wrapper ``Agent`` construction path installs the SDK ``guard_bridge`` into
the Stage-16 ``BaseAgent._tool_call_guard`` seam so declared SDK bundle
manifests can advise on a proposed tool call before dispatch. This stage is
behaviour-visible but **advisory-first**:

* default live wiring NEVER introduces a blocking denial — a manifest-declared
  ``destructive`` tool becomes a *warning*, not a block, in default live mode;
* default agents get advisory-only core warnings for ``system``/``psyche``/
  ``soul`` (Stage 20), while undeclared/non-core tools stay pass-through;
* unknown / unmanifested tools (MCP, add_tool, capability tools without a
  manifest) fail open — they are never blocked by this slice;
* the installed guard is actually threaded through the Stage-16 seam to the
  ``ToolExecutor`` the turn loop builds;
* no lifecycle/system tool is blocked by default wiring.

Import direction is one-way: the wrapper may import the SDK ``guard_bridge`` /
``capabilities``; the kernel never imports the SDK.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.tool_call_guard import ToolCallGuard, ToolProposal
from lingtai_sdk import capabilities as cap
from lingtai_sdk import core_bundles as core
from lingtai_sdk.guard_bridge import GuardPolicyMode

from lingtai import guard_wiring as gw

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def _proposal(tool_name: str) -> ToolProposal:
    return ToolProposal(tool_name=tool_name, tool_args={})


def _manifest(name: str, tools: tuple[str, ...], danger: str) -> cap.BundleManifest:
    return cap.BundleManifest(
        name=name,
        version="0.0.1",
        surfaces=cap.CapabilitySurfaces(tools=tools),
        security=cap.SecurityPolicy(danger=danger),
        transport=cap.TransportSpec(kind=cap.TransportKind.IN_PROCESS.value),
    )


def _make_mock_service():
    svc = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


# --- module-level defaults --------------------------------------------------


def test_default_live_mode_is_advisory():
    """The wrapper's default live policy mode is advisory (non-blocking)."""
    assert gw.DEFAULT_LIVE_GUARD_MODE is GuardPolicyMode.ADVISORY


def test_default_manifest_registry_is_empty():
    """No capability declares a bundle manifest by default, so collecting from a
    default registry yields nothing — keeping existing agents pass-through."""
    assert gw.default_manifest_registry() == {}


# --- install_bundle_guard: the seam writer ----------------------------------


def test_install_bundle_guard_writes_advisory_guard_to_seam():
    """A manifest-declared destructive tool becomes a *warning* (not blocked)
    under the default advisory live mode, and the guard lands on the seam."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()  # default empty pass-through

    destructive = _manifest("danger_bundle", ("nuke",), cap.SecurityDanger.DESTRUCTIVE.value)
    gw.install_bundle_guard(agent, manifests=[destructive])

    guard = agent._tool_call_guard
    assert isinstance(guard, ToolCallGuard)
    decision = guard.evaluate(_proposal("nuke"))
    # advisory-first: allowed, surfaced as a warning, never denied.
    assert decision.allowed is True
    assert decision.action == "warn"
    assert decision.severity == "warning"
    assert decision.metadata["danger"] == "destructive"
    assert decision.metadata["policy_mode"] == "advisory"


def test_install_bundle_guard_unknown_tool_fails_open():
    """An unmanifested / unknown tool is never blocked — clean pass-through."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    destructive = _manifest("danger_bundle", ("nuke",), cap.SecurityDanger.DESTRUCTIVE.value)
    gw.install_bundle_guard(agent, manifests=[destructive])

    decision = agent._tool_call_guard.evaluate(_proposal("some_mcp_tool"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


def test_install_bundle_guard_empty_manifests_is_pass_through():
    """No manifests → the installed guard is the unchanged default_allow
    pass-through (existing default agents are unaffected)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    gw.install_bundle_guard(agent, manifests=[])

    decision = agent._tool_call_guard.evaluate(_proposal("anything"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


def test_install_bundle_guard_blocking_mode_is_opt_in_only():
    """Blocking is reachable only by explicit opt-in, never the live default."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    destructive = _manifest("danger_bundle", ("nuke",), cap.SecurityDanger.DESTRUCTIVE.value)
    gw.install_bundle_guard(
        agent, manifests=[destructive], mode=GuardPolicyMode.BLOCKING
    )
    decision = agent._tool_call_guard.evaluate(_proposal("nuke"))
    assert decision.allowed is False


# --- wire_agent_guard: the live construction entry point ---------------------


def test_wire_agent_guard_no_capability_manifests_and_no_core_is_pass_through():
    """With the default (empty) *capability* registry and core wiring opted out
    (``include_core=False``), wiring a live agent leaves the seam a pure
    pass-through — this isolates the capability-only path (pre-Stage-20)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("psyche", {}), ("vision", {})]

    gw.wire_agent_guard(agent, include_core=False)

    # No capability declares a manifest, and core wiring is opted out → the
    # capability tool name (here a non-core 'vision') is unknown → pass-through.
    decision = agent._tool_call_guard.evaluate(_proposal("vision"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


def test_wire_agent_guard_default_advises_core_tools_never_blocks():
    """Stage 20 behaviour-active default: a default-registry wiring still installs
    advisory guards for the always-present core surfaces. ``system`` (destructive)
    and ``psyche``/``soul`` (caution) WARN, never deny — no lifecycle block."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("vision", {})]  # core surfaces are NOT capabilities

    gw.wire_agent_guard(agent)  # default registry, include_core defaults True

    for tool, danger in (("system", "destructive"), ("psyche", "caution"), ("soul", "caution")):
        decision = agent._tool_call_guard.evaluate(_proposal(tool))
        assert decision.allowed is True, f"{tool} must never be blocked by default"
        assert decision.action == "warn"
        assert decision.metadata["danger"] == danger
        assert decision.metadata["policy_mode"] == "advisory"
    # A non-core capability tool with no manifest still fails open.
    assert agent._tool_call_guard.evaluate(_proposal("vision")).approval_mode == "pass_through"


def test_wire_agent_guard_advises_declared_destructive_capability_tool():
    """A capability that declares a destructive bundle manifest gets its tool
    advised (warn), never blocked, under default live wiring."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {})]

    registry = {
        "scary": lambda: _manifest(
            "scary", ("delete_everything",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    gw.wire_agent_guard(agent, registry=registry)

    decision = agent._tool_call_guard.evaluate(_proposal("delete_everything"))
    assert decision.allowed is True
    assert decision.action == "warn"
    assert decision.severity == "warning"


def test_wire_agent_guard_never_blocks_lifecycle_system_tool():
    """Even if the core ``system`` (destructive) manifest is somehow in the
    registry, default live wiring is advisory — ``system`` warns, never blocks.
    No lifecycle/system tool is denied by this slice."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("system", {})]

    registry = {"system": core.system_bundle}
    gw.wire_agent_guard(agent, registry=registry)

    decision = agent._tool_call_guard.evaluate(_proposal("system"))
    assert decision.allowed is True  # advisory, NOT blocked
    assert decision.action == "warn"


def test_wire_agent_guard_ignores_capabilities_without_manifest():
    """A capability with no registry entry contributes nothing — its tools fail
    open (unknown → pass-through)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {}), ("plain", {})]

    registry = {
        "scary": lambda: _manifest(
            "scary", ("delete_everything",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    gw.wire_agent_guard(agent, registry=registry)

    # 'plain' declared no manifest → its tool is unknown → pass-through.
    decision = agent._tool_call_guard.evaluate(_proposal("plain_tool"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


def test_wire_agent_guard_fails_open_on_registry_error():
    """A manifest provider that raises must not break agent construction — the
    seam is left at a safe pass-through (fail open, never fail closed)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("boom", {})]

    def _explode():
        raise RuntimeError("manifest build failed")

    registry = {"boom": _explode}
    # Must not raise.
    gw.wire_agent_guard(agent, registry=registry)

    decision = agent._tool_call_guard.evaluate(_proposal("anything"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


# --- live Agent construction: end-to-end seam wiring ------------------------


def test_live_agent_default_construction_advises_core_but_never_blocks(tmp_path):
    """Stage 20: a real wrapper ``Agent`` built with the default registry owns an
    *advisory* guard for the always-present core surfaces — ``psyche``/``soul``
    warn (caution), ``system`` warns (destructive), and NONE is ever blocked.
    Unknown / non-core tools still fail open (pass-through)."""
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="t",
        working_dir=tmp_path / "agent",
        capabilities=["psyche"],
    )
    guard = agent._tool_call_guard
    assert isinstance(guard, ToolCallGuard)
    for tool in ("system", "psyche", "soul"):
        decision = guard.evaluate(_proposal(tool))
        assert decision.allowed is True, f"{tool} must never be blocked"
        assert decision.action == "warn"
    # An unknown / non-core tool (e.g. an MCP or add_tool tool) fails open.
    unknown = guard.evaluate(_proposal("some_unmanifested_tool"))
    assert unknown.allowed is True
    assert unknown.approval_mode == "pass_through"


def test_installed_guard_threads_through_stage16_seam_to_executor(tmp_path):
    """The guard installed on the wrapper seam is the very object the Stage-16
    turn loop hands to the ``ToolExecutor`` — proving the wiring is live."""
    import lingtai_kernel.base_agent.turn as turn_module
    from lingtai_kernel.base_agent.turn import _handle_request
    from lingtai_kernel.message import _make_message, MSG_REQUEST
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="t2",
        working_dir=tmp_path / "agent2",
        capabilities=["psyche"],
    )

    # Install an advisory guard for a declared destructive tool on the seam.
    destructive = _manifest("scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value)
    gw.install_bundle_guard(agent, manifests=[destructive])
    installed = agent._tool_call_guard

    # Drive _handle_request far enough to build the executor, stubbing the LLM
    # round-trip collaborators (mirrors the Stage-16 injection test).
    def _no_tool_response():
        resp = MagicMock()
        resp.text = "done"
        resp.tool_calls = []
        resp.usage = MagicMock(
            input_tokens=0, output_tokens=0, thinking_tokens=0, cached_tokens=0
        )
        return resp

    monkey = pytest.MonkeyPatch()
    monkey.setattr(turn_module, "_check_molt_pressure", lambda agent: None)
    monkey.setattr(turn_module, "_process_response", lambda agent, response, **kw: None)
    agent._pre_request = MagicMock(return_value="hi")
    agent._sync_notifications = MagicMock()
    agent._session = MagicMock()
    agent._session.send.return_value = _no_tool_response()
    agent._save_chat_history = MagicMock()
    agent._post_request = MagicMock()
    try:
        _handle_request(agent, _make_message(MSG_REQUEST, "user", "go"))
    finally:
        monkey.undo()

    assert agent._executor._tool_call_guard is installed
    # And that executor's guard actually advises the declared destructive tool.
    decision = agent._executor._tool_call_guard.evaluate(_proposal("wipe"))
    assert decision.allowed is True
    assert decision.action == "warn"


# --- Stage 19: provenance / reset safety before registry population ---------
#
# Stage 18 wired an advisory bundle guard but its ``wire_agent_guard`` returns
# early when no manifests are collected, so it could not *clear* a stale guard
# this wrapper had previously installed (e.g. a later refresh/reconstruct with
# an emptied registry/capabilities). Stage 19 adds provenance tracking so the
# wrapper can reset its own bundle-derived guard back to a pass-through while
# never clobbering a host/subclass manually-installed guard.


def test_install_bundle_guard_sets_provenance_marker():
    """Installing a bundle-derived guard tags the agent so later wiring can
    recognise the guard as wrapper-installed (and safely reset it)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    destructive = _manifest("scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value)

    gw.install_bundle_guard(agent, manifests=[destructive])

    assert getattr(agent, "_bundle_guard_installed", False) is True
    # The source records which bundles derived the installed guard.
    assert "scary" in (getattr(agent, "_bundle_guard_source", None) or ())


def test_install_bundle_guard_empty_manifests_does_not_mark_provenance():
    """Installing with no manifests is a pass-through and must NOT claim
    provenance — there is no wrapper-derived posture to later reset."""
    agent = MagicMock(spec=[])  # bare object; no attrs unless we set them
    agent._tool_call_guard = ToolCallGuard()

    gw.install_bundle_guard(agent, manifests=[])

    assert getattr(agent, "_bundle_guard_installed", False) is False


def test_wire_agent_guard_resets_stale_bundle_guard_when_no_manifests():
    """A previously wrapper-installed (bundle-derived) guard is reset to a
    pass-through when a subsequent wiring call collects no manifests.

    Uses ``include_core=False`` to isolate the capability-only reset path — with
    Stage-20 core wiring on (the default), the always-present core manifests
    would otherwise keep the collection non-empty (covered separately)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {})]
    registry = {
        "scary": lambda: _manifest(
            "scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    # First wiring installs a bundle-derived advisory guard.
    gw.wire_agent_guard(agent, registry=registry, include_core=False)
    assert agent._tool_call_guard.evaluate(_proposal("wipe")).action == "warn"
    assert getattr(agent, "_bundle_guard_installed", False) is True

    # Now capabilities/registry go empty (refresh/reconstruct). Re-wiring must
    # clear the stale bundle-derived guard back to a clean pass-through.
    agent._capabilities = []
    gw.wire_agent_guard(agent, include_core=False)

    decision = agent._tool_call_guard.evaluate(_proposal("wipe"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"
    # Provenance cleared so we don't keep "owning" a now-default guard.
    assert getattr(agent, "_bundle_guard_installed", False) is False


def test_wire_agent_guard_does_not_clobber_manual_guard_even_with_core_default():
    """A host/subclass manually-installed guard (non-empty chain, no wrapper
    provenance) is left untouched — even under Stage-20 default core wiring, which
    would otherwise install advisory core guards. The Stage-19 guarantee holds:
    a manual guard is never clobbered."""
    from lingtai_kernel.tool_call_guard import GuardDecision

    def _deny_check(proposal):
        return GuardDecision(allowed=False, check_name="host_manual", reason="nope")

    manual_guard = ToolCallGuard([_deny_check])
    agent = MagicMock()
    agent._tool_call_guard = manual_guard
    agent._capabilities = []

    # Default wiring (include_core=True): core manifests WOULD be collected, but
    # the manual guard must still be preserved.
    gw.wire_agent_guard(agent)

    # The host's manual guard object is preserved untouched — core advisories did
    # NOT overwrite it, and 'system' is still denied by the host's own guard.
    assert agent._tool_call_guard is manual_guard
    assert agent._tool_call_guard.evaluate(_proposal("anything")).allowed is False
    assert agent._tool_call_guard.evaluate(_proposal("system")).allowed is False


def test_wire_agent_guard_rederives_bundle_guard_when_manifests_change():
    """A bundle-derived guard can be replaced/re-derived when manifests change
    on a later wiring call (e.g. a different capability set)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {})]
    registry = {
        "scary": lambda: _manifest(
            "scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value
        ),
        "spooky": lambda: _manifest(
            "spooky", ("erase",), cap.SecurityDanger.DESTRUCTIVE.value
        ),
    }
    gw.wire_agent_guard(agent, registry=registry)
    assert agent._tool_call_guard.evaluate(_proposal("wipe")).action == "warn"
    assert agent._tool_call_guard.evaluate(_proposal("erase")).approval_mode == "pass_through"

    # Capability set changes to a different declared bundle.
    agent._capabilities = [("spooky", {})]
    gw.wire_agent_guard(agent, registry=registry)

    # New bundle advises its tool; the old bundle's tool is no longer known.
    assert agent._tool_call_guard.evaluate(_proposal("erase")).action == "warn"
    assert agent._tool_call_guard.evaluate(_proposal("wipe")).approval_mode == "pass_through"
    assert "spooky" in (getattr(agent, "_bundle_guard_source", None) or ())


def test_wire_agent_guard_reset_failure_is_fail_open():
    """If resetting the stale guard raises, wiring must not break the agent —
    fail open, leaving the prior (safe, advisory) seam rather than failing."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {})]
    registry = {
        "scary": lambda: _manifest(
            "scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    gw.wire_agent_guard(agent, registry=registry, include_core=False)

    # Make assignment to the seam blow up to simulate a pathological reset.
    class _Boom:
        def __setattr__(self, name, value):
            if name == "_tool_call_guard":
                raise RuntimeError("cannot set guard")
            object.__setattr__(self, name, value)

    boom = _Boom()
    object.__setattr__(boom, "_capabilities", [])
    object.__setattr__(boom, "_bundle_guard_installed", True)
    object.__setattr__(boom, "_tool_call_guard", agent._tool_call_guard)

    # Emptied capabilities + core opted out → reset path; the reset raises but
    # wiring must fail open (not propagate).
    gw.wire_agent_guard(boom, include_core=False)


def test_agent_rewire_clears_stale_bundle_guard_on_emptied_capabilities():
    """Refresh-like path: the agent's own ``_wire_bundle_guard`` shares the
    wiring seam, so re-running it after capabilities/registry are emptied must
    not leave a stale bundle-derived guard. Mock-level: we exercise the real
    ``BaseAgent._wire_bundle_guard`` against a minimal stand-in agent rather
    than building a full live Agent."""
    from lingtai.agent import Agent

    # A minimal object exposing just what wiring touches; bind the real method.
    class _Stub:
        _wire_bundle_guard = Agent._wire_bundle_guard

        def __init__(self):
            self._tool_call_guard = ToolCallGuard()
            self._capabilities = [("scary", {})]
            self._logs = []

        def _log(self, event, **fields):
            self._logs.append((event, fields))

    stub = _Stub()
    registry = {
        "scary": lambda: _manifest(
            "scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    # Install a bundle-derived guard via the shared seam (scary capability +
    # the always-present core surfaces under the Stage-20 default).
    gw.wire_agent_guard(stub, registry=registry)
    assert getattr(stub, "_bundle_guard_installed", False) is True
    assert stub._tool_call_guard.evaluate(_proposal("wipe")).action == "warn"

    # Capabilities go empty; the agent re-wires via its own (default-registry)
    # path. The stale *capability* posture (``wipe``) must be dropped — but the
    # always-present core surfaces are re-derived, so the guard is core-only now.
    stub._capabilities = []
    stub._wire_bundle_guard()

    # The vanished capability's tool is no longer advised (re-derived away).
    wipe = stub._tool_call_guard.evaluate(_proposal("wipe"))
    assert wipe.allowed is True
    assert wipe.approval_mode == "pass_through"
    # Core surfaces remain advised (Stage 20 default), never blocked.
    assert stub._tool_call_guard.evaluate(_proposal("system")).action == "warn"
    # Provenance is still claimed (now by the core manifests).
    assert getattr(stub, "_bundle_guard_installed", False) is True
    assert "system" in (getattr(stub, "_bundle_guard_source", None) or ())


# --- Stage 20: core manifest registry population ----------------------------
#
# Stages 17-19 built the bridge, the advisory wiring, and provenance/reset, but
# ``default_manifest_registry`` stayed empty so *no* manifest was ever
# discovered. Stage 20 populates the actual core capability manifest providers
# (``system`` / ``psyche`` / ``soul``) into a named ``core_manifest_registry``
# and adds ``collect_core_bundle_manifests`` for the intrinsic core surfaces.
#
# The core surfaces are kernel *intrinsics* (registered in
# ``lingtai_kernel.intrinsics.__init__``), always present and NOT listed in an
# agent's ``_capabilities``. The capability-walk collector therefore never
# reaches them; default live wiring explicitly includes the core collector so
# default agents warn for declared core tools while remaining advisory-only and
# fail-open. ``include_core=False`` is the explicit opt-out/pass-through path.


def test_core_manifest_registry_has_three_core_providers():
    """The core registry maps each core bundle name to a provider that returns
    that bundle's manifest — the actual Stage 20 population."""
    registry = gw.core_manifest_registry()
    assert set(registry) == set(core.core_bundle_names())
    for name, provider in registry.items():
        manifest = provider()
        assert isinstance(manifest, cap.BundleManifest)
        assert manifest.name == name
        assert core.is_core_manifest(manifest)


def test_default_registry_stays_empty_and_distinct_from_core():
    """Stage 20 must NOT auto-populate the *capability* registry — core
    manifests live in a separate registry and are collected by the core seam."""
    assert gw.default_manifest_registry() == {}
    assert gw.core_manifest_registry() != {}


def test_collect_core_bundle_manifests_returns_all_three():
    """The core collector returns the three core manifests directly,
    independent of the agent's ``_capabilities`` (core surfaces are intrinsics,
    always present, never listed as capabilities)."""
    agent = MagicMock()
    agent._capabilities = []  # core surfaces are NOT capabilities
    manifests = gw.collect_core_bundle_manifests(agent)
    names = {m.name for m in manifests}
    assert names == set(core.core_bundle_names())


def test_collect_core_bundle_manifests_fails_open_on_provider_error():
    """A core provider that raises is skipped (fail open), never aborting
    collection — one broken core manifest can't break construction."""
    agent = MagicMock()
    agent._capabilities = []
    bad_registry = {
        "system": core.system_bundle,
        "psyche": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        "soul": core.soul_bundle,
    }
    manifests = gw.collect_core_bundle_manifests(agent, registry=bad_registry)
    names = {m.name for m in manifests}
    # psyche skipped; the others still collected.
    assert "psyche" not in names
    assert {"system", "soul"}.issubset(names)


def test_wire_agent_guard_with_core_registry_advises_core_tools_never_blocks():
    """The core registry surfaces declared core tools as *advisories* (warn),
    never blocks them — even ``system`` (destructive)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    # Exercise the capability-walk path too; default agents collect the same
    # core manifests directly via include_core=True.
    agent._capabilities = [("system", {}), ("psyche", {}), ("soul", {})]

    gw.wire_agent_guard(agent, registry=gw.core_manifest_registry())

    for tool, expect_danger in (
        ("system", "destructive"),
        ("psyche", "caution"),
        ("soul", "caution"),
    ):
        decision = agent._tool_call_guard.evaluate(_proposal(tool))
        assert decision.allowed is True, f"{tool} must never be blocked"
        assert decision.action == "warn"
        assert decision.severity == "warning"
        assert decision.metadata["danger"] == expect_danger
        assert decision.metadata["policy_mode"] == "advisory"


def test_core_registry_only_advises_declared_core_actions_not_unknown():
    """With the core registry wired, only the three declared core *tool* names
    are advised; an unknown / unmanifested tool still fails open."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("system", {})]

    gw.wire_agent_guard(agent, registry=gw.core_manifest_registry())

    # Declared core tool → advised.
    assert agent._tool_call_guard.evaluate(_proposal("system")).action == "warn"
    # Unknown MCP / add_tool style tool → clean pass-through (fail open).
    unknown = agent._tool_call_guard.evaluate(_proposal("some_mcp_tool"))
    assert unknown.allowed is True
    assert unknown.approval_mode == "pass_through"
    # A core *action* string (not a tool name) is not a declared tool surface —
    # the core manifests declare one tool each (``system``/``psyche``/``soul``),
    # so action verbs like ``nirvana`` are unknown and fail open.
    nirvana = agent._tool_call_guard.evaluate(_proposal("nirvana"))
    assert nirvana.allowed is True
    assert nirvana.approval_mode == "pass_through"


def test_core_registry_wiring_preserves_stage19_provenance_and_reset():
    """A core-registry-derived guard carries provenance and is reset like any
    wrapper-derived guard when a later wiring collects no manifests (Stage 19)."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("system", {})]

    gw.wire_agent_guard(agent, registry=gw.core_manifest_registry())
    assert getattr(agent, gw.PROVENANCE_FLAG, False) is True
    assert "system" in (getattr(agent, gw.PROVENANCE_SOURCE, None) or ())
    assert agent._tool_call_guard.evaluate(_proposal("system")).action == "warn"

    # A later wiring that collects NO manifests (capabilities empty AND core
    # opted out) resets the wrapper-derived guard back to pass-through and clears
    # provenance — the Stage-19 reset path, unchanged by Stage 20.
    agent._capabilities = []
    gw.wire_agent_guard(agent, include_core=False)
    decision = agent._tool_call_guard.evaluate(_proposal("system"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"
    assert getattr(agent, gw.PROVENANCE_FLAG, False) is False


def test_core_registry_blocking_is_opt_in_only_never_default():
    """Even with the core registry, blocking is reachable only by explicit
    ``mode=BLOCKING`` opt-in; the default live mode stays advisory."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("system", {})]

    # Default mode: advisory — system warns, never blocks (no lifecycle block).
    gw.wire_agent_guard(agent, registry=gw.core_manifest_registry())
    assert agent._tool_call_guard.evaluate(_proposal("system")).allowed is True

    # Explicit opt-in blocking mode: now system is denied (host's deliberate choice).
    gw.wire_agent_guard(
        agent, registry=gw.core_manifest_registry(), mode=GuardPolicyMode.BLOCKING
    )
    assert agent._tool_call_guard.evaluate(_proposal("system")).allowed is False


def test_wire_agent_guard_include_core_false_recovers_pass_through():
    """``include_core=False`` with the default empty capability registry recovers
    the pre-Stage-20 pure pass-through — an explicit opt-out for a host that wants
    no advisories at all. Core surfaces then fail open like any unknown tool."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("psyche", {}), ("vision", {})]

    gw.wire_agent_guard(agent, include_core=False)

    for tool in ("system", "psyche", "soul", "anything"):
        decision = agent._tool_call_guard.evaluate(_proposal(tool))
        assert decision.allowed is True
        assert decision.approval_mode == "pass_through"


def test_core_advisory_does_not_block_lifecycle_actions_end_to_end(tmp_path):
    """Lifecycle safety: a real default-constructed Agent advises the core
    ``system`` tool (warn) but never denies it — and unknown lifecycle-ish action
    names that are not declared tool surfaces fail open. No lifecycle path is
    blocked by Stage-20 default wiring."""
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="t-core",
        working_dir=tmp_path / "agent-core",
        capabilities=["psyche"],
    )
    # The declared core 'system' tool warns, never blocks.
    sys_decision = agent._tool_call_guard.evaluate(_proposal("system"))
    assert sys_decision.allowed is True
    assert sys_decision.action == "warn"
    # Action verbs (refresh/sleep/nirvana) are NOT tool surfaces — they are
    # arguments to the 'system' tool — so they are unknown and fail open.
    for action in ("refresh", "sleep", "nirvana", "cpr"):
        decision = agent._tool_call_guard.evaluate(_proposal(action))
        assert decision.allowed is True
        assert decision.approval_mode == "pass_through"


# --- import direction: kernel stays SDK-free --------------------------------


def test_wrapper_guard_wiring_import_does_not_invert_into_kernel():
    """Importing ``lingtai.guard_wiring`` must NOT make the *kernel* import the
    SDK — the kernel package stays SDK-free even though the wrapper bridges it."""
    # The wrapper may import lingtai_sdk, but importing the wrapper guard-wiring
    # module must not make any *kernel* module depend on the SDK. We import the
    # wrapper module, then assert that re-importing the kernel guard module in a
    # fresh interpreter (below) stays SDK-free; here we just prove the wrapper
    # module imports cleanly and the kernel is present.
    code = (
        "import sys\n"
        "import lingtai.guard_wiring\n"
        "import lingtai_kernel.tool_call_guard\n"
        "kernel_loaded = [m for m in sys.modules if m == 'lingtai_kernel' "
        "or m.startswith('lingtai_kernel.')]\n"
        "assert kernel_loaded, 'kernel not loaded?'\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- Stage 22: end-to-end native Agent / SDK integration --------------------
#
# Stages 17-21 each verified one seam in isolation: the SDK bridge builds a guard
# from a manifest (17), the wrapper installs it onto the Stage-16 seam (18), the
# guard threads to the ToolExecutor (18) and is reset/provenance-safe (19), the
# default core manifests are collected (20), and the kernel ToolExecutor inlines
# a source-labeled ``guard_advisory`` on ``tool_call_approved`` (21). But the
# Stage-21 source-labeled-log proof (``test_tool_executor.py``) drives a
# *hand-built* guard + fake dispatch/logger, and the live-Agent proofs above call
# ``guard.evaluate`` directly or never dispatch a tool. No single test proves the
# whole chain through the real seams: a real wrapper ``Agent`` whose default core
# manifests were wired by ``wire_agent_guard`` → a real ToolExecutor built the way
# the turn loop builds it → a real intrinsic dispatch → a ``tool_call_approved``
# lifecycle event whose ``guard_advisory`` is attributed to the declaring bundle.
#
# This test closes that E2E confidence gap. Only the LLM service is a mock (the
# standard kernel test seam — it removes the network, nothing in the guard path);
# the agent, its installed guard, the executor, the logger, the dispatch, and the
# ``system`` intrinsic are all real.


def _log_index(logs, event_type, *, trace_id=None):
    """Index of the first captured ``event_type`` log (optionally by trace id)."""
    for index, (event, fields) in enumerate(logs):
        if event != event_type:
            continue
        if trace_id is not None and fields.get("tool_trace_id") != trace_id:
            continue
        return index
    raise AssertionError(f"missing log event {event_type!r} for trace {trace_id!r}")


def _real_turn_loop_executor(agent, logs):
    """Build a ToolExecutor exactly as ``base_agent.turn._handle_request`` does.

    Mirrors the construction at ``turn.py`` (``dispatch_fn=agent._dispatch_tool``,
    ``known_tools`` = intrinsics + tool handlers, real ``_tool_call_guard`` seam),
    but wraps ``agent._log`` so emitted lifecycle events are captured for
    assertion while still flowing through the agent's real logging path. Returns
    the live executor.
    """
    from lingtai_kernel.loop_guard import LoopGuard
    from lingtai_kernel.tool_executor import ToolExecutor

    real_log = agent._log

    def _capturing_log(event_type, **fields):
        logs.append((event_type, fields))
        return real_log(event_type, **fields)

    return ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=LoopGuard(max_total_calls=50),
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        parallel_safe_tools=agent._PARALLEL_SAFE_TOOLS,
        logger_fn=_capturing_log,
        working_dir=agent._working_dir,
        tool_call_guard=getattr(agent, "_tool_call_guard", None),
    )


def test_e2e_default_core_manifest_becomes_source_labeled_lifecycle_advisory(tmp_path):
    """End-to-end: a real default ``Agent`` turns its declared ``system`` core
    bundle into a source-labeled ``tool_call_approved.guard_advisory`` when a real
    ``system`` tool call flows through a real ToolExecutor — and the call is still
    allowed (advisory-first: warn, never block).

    Proves the full Stage 17→21 chain in one path through real seams:
    ``wire_agent_guard`` (real installed core guard) → ``ToolExecutor.evaluate``
    (real Stage-16 seam) → ``_log_guard_approval`` → ``advisory_summary`` (Stage 21).
    """
    from lingtai_kernel.llm.base import ToolCall
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="e2e",
        working_dir=tmp_path / "agent",
        capabilities=["psyche"],
    )

    # The default-wired guard is real and advisory for the core ``system`` bundle.
    installed = agent._tool_call_guard
    assert isinstance(installed, ToolCallGuard)
    assert installed.evaluate(_proposal("system")).action == "warn"

    logs: list[tuple[str, dict]] = []
    executor = _real_turn_loop_executor(agent, logs)
    # The executor consumes the very guard the wrapper installed — not a copy.
    assert executor._tool_call_guard is installed

    # Dispatch a REAL ``system`` intrinsic call. ``action=notification`` is a
    # pure, read-only, no-side-effect, no-network handler that returns a
    # placeholder dict, so the executor exercises the real dispatch + intrinsic
    # without lifecycle teardown or external state.
    calls = [ToolCall(name="system", args={"action": "notification"}, id="e2e-sys")]
    results, intercepted, _text = executor.execute(calls)

    # Advisory-first: the destructive-declared core tool ran (was never blocked).
    assert not intercepted
    assert len(results) == 1
    approved_idx = _log_index(logs, "tool_call_approved", trace_id="e2e-sys")
    denied = [e for e, _ in logs if e == "tool_call_denied"]
    assert not denied, "advisory core bundle must never deny a tool call"

    # The approval log carries the Stage-21 flat, source-labeled summary,
    # attributing the advisory to the bundle that declared the danger posture.
    approved = logs[approved_idx][1]
    summary = approved["guard_advisory"]
    assert summary["allowed"] is True
    assert summary["action"] == "warn"
    assert summary["bundle"] == "system"
    assert summary["danger"] == "destructive"
    assert summary["source"] == "bundle:system:destructive"


def test_e2e_unmanifested_tool_emits_no_guard_advisory(tmp_path):
    """End-to-end fail-open: a real ``add_tool`` tool (unknown to every bundle)
    flows through the same real Agent + executor cleanly — its
    ``tool_call_approved`` event carries NO ``guard_advisory`` because the default
    core guard never matches an undeclared surface. This is the advisory-first
    safety invariant: the slice only ever *adds* advisories for declared core
    tools and never gates an unmanifested one."""
    from lingtai_kernel.llm.base import ToolCall
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="e2e2",
        working_dir=tmp_path / "agent",
        capabilities=["psyche"],
    )

    # Register a real, benign tool the bridge has never heard of.
    agent.add_tool(
        "echo_probe",
        schema={"type": "object", "properties": {}},
        handler=lambda args: {"status": "ok", "echo": args},
        description="test-only probe tool",
    )

    logs: list[tuple[str, dict]] = []
    executor = _real_turn_loop_executor(agent, logs)

    calls = [ToolCall(name="echo_probe", args={}, id="e2e-echo")]
    results, intercepted, _text = executor.execute(calls)

    assert not intercepted
    assert len(results) == 1
    approved_idx = _log_index(logs, "tool_call_approved", trace_id="e2e-echo")
    approved = logs[approved_idx][1]
    assert "guard_advisory" not in approved
    assert approved["approval_mode"] == "pass_through"


def test_kernel_guard_import_is_sdk_free_in_isolation():
    """Kernel ``tool_call_guard`` imported alone must not load the SDK."""
    code = (
        "import sys\n"
        "import lingtai_kernel.tool_call_guard  # noqa: F401\n"
        "bad = [m for m in sys.modules if m == 'lingtai_sdk' "
        "or m.startswith('lingtai_sdk.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
