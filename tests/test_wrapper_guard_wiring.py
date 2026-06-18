"""Stage 18 (C3) — advisory-first wrapper wiring of the SDK guard bridge.

The wrapper ``Agent`` construction path installs the SDK ``guard_bridge`` into
the Stage-16 ``BaseAgent._tool_call_guard`` seam so declared SDK bundle
manifests can advise on a proposed tool call before dispatch. This stage is
behaviour-visible but **advisory-first**:

* default live wiring NEVER introduces a blocking denial — a manifest-declared
  ``destructive`` tool becomes a *warning*, not a block, in default live mode;
* default/existing agents stay pure pass-through — nothing is wired unless a
  capability actually declares a bundle manifest, and the default registry is
  empty, so a freshly built agent's guard is the unchanged ``default_allow``
  pass-through;
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


def test_wire_agent_guard_default_registry_keeps_pass_through():
    """With the default (empty) registry, wiring a live agent leaves the seam a
    pure pass-through — destructive-free, advisory-free, behaviour-neutral."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("psyche", {}), ("vision", {})]

    gw.wire_agent_guard(agent)

    decision = agent._tool_call_guard.evaluate(_proposal("psyche"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


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


def test_live_agent_default_construction_is_pass_through(tmp_path):
    """A real wrapper ``Agent`` built with the default registry owns a
    pass-through guard — no behaviour change for existing/default agents."""
    from lingtai.agent import Agent

    agent = Agent(
        service=_make_mock_service(),
        agent_name="t",
        working_dir=tmp_path / "agent",
        capabilities=["psyche"],
    )
    guard = agent._tool_call_guard
    assert isinstance(guard, ToolCallGuard)
    decision = guard.evaluate(_proposal("psyche"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"


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
    pass-through when a subsequent wiring call collects no manifests."""
    agent = MagicMock()
    agent._tool_call_guard = ToolCallGuard()
    agent._capabilities = [("scary", {})]
    registry = {
        "scary": lambda: _manifest(
            "scary", ("wipe",), cap.SecurityDanger.DESTRUCTIVE.value
        )
    }
    # First wiring installs a bundle-derived advisory guard.
    gw.wire_agent_guard(agent, registry=registry)
    assert agent._tool_call_guard.evaluate(_proposal("wipe")).action == "warn"
    assert getattr(agent, "_bundle_guard_installed", False) is True

    # Now capabilities/registry go empty (refresh/reconstruct). Re-wiring must
    # clear the stale bundle-derived guard back to a clean pass-through.
    agent._capabilities = []
    gw.wire_agent_guard(agent)

    decision = agent._tool_call_guard.evaluate(_proposal("wipe"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"
    # Provenance cleared so we don't keep "owning" a now-default guard.
    assert getattr(agent, "_bundle_guard_installed", False) is False


def test_wire_agent_guard_does_not_clobber_manual_guard_when_no_manifests():
    """A host/subclass manually-installed guard (no wrapper provenance) is left
    untouched when wiring collects no manifests — never clobbered."""
    from lingtai_kernel.tool_call_guard import GuardDecision

    def _deny_check(proposal):
        return GuardDecision(allowed=False, check_name="host_manual", reason="nope")

    manual_guard = ToolCallGuard([_deny_check])
    agent = MagicMock()
    agent._tool_call_guard = manual_guard
    agent._capabilities = []  # no manifests will be collected

    gw.wire_agent_guard(agent)  # default empty registry → no manifests

    # The host's manual guard object is preserved untouched.
    assert agent._tool_call_guard is manual_guard
    assert agent._tool_call_guard.evaluate(_proposal("anything")).allowed is False


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
    gw.wire_agent_guard(agent, registry=registry)

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

    # Must not raise.
    gw.wire_agent_guard(boom)


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
    # Install a bundle-derived guard via the shared seam.
    gw.wire_agent_guard(stub, registry=registry)
    assert getattr(stub, "_bundle_guard_installed", False) is True

    # Capabilities go empty; the agent re-wires via its own (default-registry)
    # path — the stale bundle guard must be reset to pass-through.
    stub._capabilities = []
    stub._wire_bundle_guard()

    decision = stub._tool_call_guard.evaluate(_proposal("wipe"))
    assert decision.allowed is True
    assert decision.approval_mode == "pass_through"
    assert getattr(stub, "_bundle_guard_installed", False) is False


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
