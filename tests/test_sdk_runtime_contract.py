"""The runtime contract is a seed: pure DTOs + abstract protocols describing how
a future live runtime is driven (options in, messages in, events out). This PR
ships the shapes, not a live runtime — so the tests exercise construction,
convenience constructors, and that the abstract base cannot be instantiated.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import runtime as rt

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def test_runtime_options_construct_and_defaults():
    opts = rt.RuntimeOptions(working_dir="/tmp/agent")
    assert str(opts.working_dir) == "/tmp/agent"
    assert opts.agent_name is None
    assert opts.capabilities is None
    assert opts.extra == {}
    assert opts.streaming is False


def test_runtime_options_for_adapter():
    opts = rt.RuntimeOptions(
        working_dir="/tmp/a",
        extra={"adapters": {"anthropic": {"allowed_tools": ["read"]}}},
    )
    assert opts.for_adapter("anthropic") == {"allowed_tools": ["read"]}
    assert opts.for_adapter("missing") == {}


def test_runtime_message_defaults_and_id():
    m = rt.RuntimeMessage(content="hello")
    assert m.content == "hello"
    assert m.sender == "user"
    assert m.id.startswith("rtmsg_")


def test_runtime_event_constructors():
    e = rt.RuntimeEvent.state(rt.RuntimeState.ACTIVE, source="native")
    assert e.kind is rt.EventKind.STATE
    assert e.data["state"] == rt.RuntimeState.ACTIVE.value
    assert e.source == "native"
    assert e.id.startswith("rtevt_")

    t = rt.RuntimeEvent.text("hi")
    assert t.kind is rt.EventKind.TEXT and t.data["text"] == "hi"

    err = rt.RuntimeEvent.error("boom", fatal=True)
    assert err.kind is rt.EventKind.ERROR
    assert err.data["error"] == "boom" and err.data["fatal"] is True


def test_runtime_abc_not_instantiable():
    with pytest.raises(TypeError):
        rt.Runtime()
    with pytest.raises(TypeError):
        rt.RuntimeSession()


def test_runtime_concrete_subclass_drives_contract():
    """A trivial in-memory implementation proves the contract is usable."""

    class _Echo(rt.RuntimeSession):
        source = "echo"

        def __init__(self, options: rt.RuntimeOptions):
            self._opts = options
            self._state = rt.RuntimeState.PENDING
            self._inbox: list[str] = []

        @property
        def state(self) -> rt.RuntimeState:
            return self._state

        @property
        def working_dir(self) -> Path:
            return Path(self._opts.working_dir)

        def start(self) -> None:
            self._state = rt.RuntimeState.ACTIVE

        def send(self, message):
            self._inbox.append(
                message if isinstance(message, str) else str(message.content)
            )

        def events(self):
            for text in self._inbox:
                yield rt.RuntimeEvent.text(text, source=self.source)

        def stop(self, timeout: float = 5.0) -> None:
            self._state = rt.RuntimeState.STOPPED

    class _EchoRuntime(rt.Runtime):
        id = "echo"

        def create_session(self, options):
            return _Echo(options)

    runtime = _EchoRuntime()
    with runtime.run(rt.RuntimeOptions(working_dir="/tmp/echo")) as session:
        assert session.state is rt.RuntimeState.ACTIVE
        session.send("ping")
        events = list(session.events())
        assert [e.data["text"] for e in events] == ["ping"]
    assert session.state is rt.RuntimeState.STOPPED


def test_runtime_module_import_is_pure():
    code = (
        "import sys, lingtai_sdk.runtime\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
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
