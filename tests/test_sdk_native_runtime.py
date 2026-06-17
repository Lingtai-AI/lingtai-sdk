"""Stage 1 — the live ``NativeRuntime`` skeleton wraps the existing wrapper
``Agent`` behind the stage-0 runtime contract.

These tests must run with **no real model, no API key, and no long-running
agent process**: the heavy ``Agent`` is replaced by an injected fake factory
so we exercise translation, lifecycle state transitions, event emission, and
``send`` routing without booting a real agent.

Import purity is preserved: ``import lingtai_sdk.native`` and *constructing* a
``NativeRuntime`` must not pull in the ``lingtai`` wrapper or any heavy
provider SDK — the wrapper loads only when a session actually starts.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import native
from lingtai_sdk import runtime as rt

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --------------------------------------------------------------------------
# Fake Agent — stands in for the heavy wrapper Agent. Records the kwargs it
# was constructed with so translation can be asserted, and tracks start/stop.
# --------------------------------------------------------------------------
class _FakeAgent:
    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.sent: list[tuple] = []
        self.working_dir = Path(kwargs["working_dir"])

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def send(self, content, sender: str = "user") -> None:
        self.sent.append((content, sender))


def _factory(**kwargs):
    return _FakeAgent(**kwargs)


# --------------------------------------------------------------------------
# Translation helper
# --------------------------------------------------------------------------
def test_translate_minimal_working_dir_only():
    opts = rt.RuntimeOptions(working_dir="/tmp/a")
    agent_kwargs, deferred = native._agent_kwargs_from_options(opts)
    assert agent_kwargs["working_dir"] == "/tmp/a"
    # No agent_name / capabilities / addons declared -> not forced in.
    assert "agent_name" not in agent_kwargs
    assert "capabilities" not in agent_kwargs
    assert deferred["llm"] == {}


def test_translate_passes_safe_fields():
    opts = rt.RuntimeOptions(
        working_dir="/tmp/b",
        agent_name="alice",
        capabilities=["file", "web_search"],
        addons=["imap"],
        streaming=True,
    )
    agent_kwargs, _ = native._agent_kwargs_from_options(opts)
    assert agent_kwargs["agent_name"] == "alice"
    assert agent_kwargs["capabilities"] == ["file", "web_search"]
    assert agent_kwargs["addons"] == ["imap"]
    assert agent_kwargs["streaming"] is True


def test_translate_defers_llm_and_manifest_fields():
    opts = rt.RuntimeOptions(
        working_dir="/tmp/c",
        provider="anthropic",
        model="claude-opus-4-8",
        base_url="https://example",
        api_key="sk-secret",
        manifest={"covenant": "be good"},
        system_prompt_overrides={"rules": "no harm"},
    )
    agent_kwargs, deferred = native._agent_kwargs_from_options(opts)
    # LLM/provider fields are NOT forced onto Agent (Agent takes a service).
    assert "provider" not in agent_kwargs
    assert "model" not in agent_kwargs
    assert "api_key" not in agent_kwargs
    assert deferred["llm"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "base_url": "https://example",
    }
    assert "sk-secret" not in repr(deferred)
    assert deferred["manifest"] == {"covenant": "be good"}
    assert deferred["system_prompt_overrides"] == {"rules": "no harm"}


# --------------------------------------------------------------------------
# Session lifecycle (fake Agent injected)
# --------------------------------------------------------------------------
def test_create_session_is_pending_before_start(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    assert session.state is rt.RuntimeState.PENDING
    assert session.working_dir == Path(tmp_path)
    # No agent built yet.
    assert session.agent is None


def test_start_builds_agent_and_goes_active(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(
        rt.RuntimeOptions(working_dir=tmp_path, agent_name="bob")
    )
    session.start()
    assert session.state is rt.RuntimeState.ACTIVE
    assert isinstance(session.agent, _FakeAgent)
    assert session.agent.started is True
    assert session.agent.kwargs["agent_name"] == "bob"
    # A STATE event was emitted on activation.
    kinds = [e.kind for e in session.events()]
    assert rt.EventKind.STATE in kinds


def test_start_is_idempotent(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.start()
    first_agent = session.agent
    session.start()
    assert session.agent is first_agent  # not rebuilt


def test_send_routes_to_agent_when_active(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.start()
    session.send("hello")
    session.send(rt.RuntimeMessage(content="world", sender="ops"))
    assert session.agent.sent == [("hello", "user"), ("world", "ops")]
    # Each enqueue surfaced a NOTIFICATION event.
    notes = [e for e in session.events() if e.kind is rt.EventKind.NOTIFICATION]
    assert len(notes) == 2


def test_send_before_start_emits_non_fatal_error(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.send("too early")
    errs = [e for e in session.events() if e.kind is rt.EventKind.ERROR]
    assert len(errs) == 1
    assert errs[0].data["fatal"] is False
    assert session.agent is None  # nothing was built / no agent touched


def test_stop_transitions_to_stopped(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.start()
    agent = session.agent
    session.stop()
    assert session.state is rt.RuntimeState.STOPPED
    assert agent.stopped is True


def test_stop_before_start_is_safe(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.stop()
    assert session.state is rt.RuntimeState.STOPPED


def test_context_manager_runs_and_stops(tmp_path):
    rtm = native.NativeRuntime(agent_factory=_factory)
    opts = rt.RuntimeOptions(working_dir=tmp_path)
    with rtm.run(opts) as session:
        assert session.state is rt.RuntimeState.ACTIVE
    assert session.state is rt.RuntimeState.STOPPED


def test_runtime_id_is_native():
    assert native.NativeRuntime().id == "native"
    assert native.NativeRuntimeSession.source == "native"


# --------------------------------------------------------------------------
# Lazy export from the package root
# --------------------------------------------------------------------------
def test_native_runtime_exported_lazily_from_package():
    import lingtai_sdk

    assert lingtai_sdk.NativeRuntime is native.NativeRuntime
    assert lingtai_sdk.NativeRuntimeSession is native.NativeRuntimeSession


# --------------------------------------------------------------------------
# Import purity: importing native + constructing the runtime stays wrapper-free
# --------------------------------------------------------------------------
def test_importing_native_and_constructing_runtime_is_pure():
    code = (
        "import sys\n"
        "import lingtai_sdk.native as native\n"
        "import lingtai_sdk\n"
        "rtm = lingtai_sdk.NativeRuntime()\n"
        "_ = native.NativeRuntimeSession\n"
        "providers = ('anthropic','openai','google.genai',"
        "'google.generativeai','mcp','trafilatura','ddgs')\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "bad += [m for m in sys.modules "
        "if any(m == p or m.startswith(p + '.') for p in providers)]\n"
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
