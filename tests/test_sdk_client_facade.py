"""Stage 10: thin public client facade over the runtime contract."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import runtime as rt
from lingtai_sdk.client import LingTaiClient, QueryResult, open_session, query

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


class FakeSession(rt.RuntimeSession):
    source = "fake"

    def __init__(self, options: rt.RuntimeOptions):
        self.options = options
        self.sent: list[rt.RuntimeMessage] = []
        self._state = rt.RuntimeState.PENDING
        self._events: list[rt.RuntimeEvent] = []
        self.stopped = False

    @property
    def state(self) -> rt.RuntimeState:
        return self._state

    @property
    def working_dir(self) -> Path:
        return Path(self.options.working_dir)

    def start(self) -> None:
        self._state = rt.RuntimeState.ACTIVE
        self._events.append(rt.RuntimeEvent.state(self._state, source=self.source))

    def send(self, message: rt.RuntimeMessage | str) -> None:
        msg = message if isinstance(message, rt.RuntimeMessage) else rt.RuntimeMessage(message)
        self.sent.append(msg)
        self._events.append(rt.RuntimeEvent.text(f"echo:{msg.content}", source=self.source))

    def events(self):
        events = list(self._events)
        self._events.clear()
        return iter(events)

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True
        self._state = rt.RuntimeState.STOPPED
        self._events.append(rt.RuntimeEvent.state(self._state, source=self.source))


class FakeRuntime(rt.Runtime):
    id = "fake"

    def __init__(self):
        self.sessions: list[FakeSession] = []

    def create_session(self, options: rt.RuntimeOptions) -> FakeSession:
        session = FakeSession(options)
        self.sessions.append(session)
        return session


def test_client_query_sends_message_collects_text_and_stops(tmp_path):
    runtime = FakeRuntime()
    client = LingTaiClient(runtime=runtime, options=rt.RuntimeOptions(working_dir=tmp_path))

    result = client.query("hello", sender="ops", subject="greeting", metadata={"k": "v"})

    assert isinstance(result, QueryResult)
    assert result.text == "echo:hello"
    assert [event.kind for event in result.events] == [
        rt.EventKind.STATE,
        rt.EventKind.TEXT,
        rt.EventKind.STATE,
    ]
    session = runtime.sessions[0]
    assert session.sent[0].sender == "ops"
    assert session.sent[0].subject == "greeting"
    assert session.sent[0].metadata == {"k": "v"}
    assert session.stopped is True


def test_client_query_accepts_runtime_message_and_per_call_options(tmp_path):
    runtime = FakeRuntime()
    client = LingTaiClient(runtime=runtime)
    msg = rt.RuntimeMessage("world", sender="system")

    result = client.query(msg, options=rt.RuntimeOptions(working_dir=tmp_path))

    assert result.text == "echo:world"
    assert runtime.sessions[0].sent[0] is msg


def test_client_query_can_leave_session_running(tmp_path):
    runtime = FakeRuntime()
    client = LingTaiClient(runtime=runtime, options=rt.RuntimeOptions(working_dir=tmp_path))

    result = client.query("stay", stop=False)

    assert result.text == "echo:stay"
    assert runtime.sessions[0].state is rt.RuntimeState.ACTIVE
    assert runtime.sessions[0].stopped is False


def test_client_query_requires_options():
    client = LingTaiClient(runtime=FakeRuntime())
    with pytest.raises(ValueError, match="requires RuntimeOptions"):
        client.query("missing")


def test_module_query_helper(tmp_path):
    runtime = FakeRuntime()

    result = query("helper", runtime=runtime, options=rt.RuntimeOptions(working_dir=tmp_path))

    assert result.text == "echo:helper"
    assert runtime.sessions[0].stopped is True


def test_root_exports_client_facade_lazily_and_wrapper_free(tmp_path):
    code = f"""
import sys
sys.path.insert(0, {str(SRC)!r})
import lingtai_sdk
Client = lingtai_sdk.LingTaiClient
Result = lingtai_sdk.QueryResult
helper = lingtai_sdk.query
assert Client.__name__ == 'LingTaiClient'
assert Result.__name__ == 'QueryResult'
assert callable(helper)
bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]
assert not bad, bad
print('OK')
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_open_session_keeps_session_live_for_multiple_messages(tmp_path):
    runtime = FakeRuntime()
    client = LingTaiClient(runtime=runtime, options=rt.RuntimeOptions(working_dir=tmp_path))

    session = client.open_session()
    assert session.state is rt.RuntimeState.ACTIVE
    assert session.working_dir == tmp_path

    session.send("one").send("two", sender="ops")
    assert session.text() == "echo:oneecho:two"
    assert session.events() == ()
    assert runtime.sessions[0].sent[0].content == "one"
    assert runtime.sessions[0].sent[1].sender == "ops"
    final_events = session.close()
    assert runtime.sessions[0].stopped is True
    assert [event.kind for event in final_events] == [rt.EventKind.STATE]


def test_open_session_helper_and_context_manager_close(tmp_path):
    runtime = FakeRuntime()

    with open_session(runtime=runtime, options=rt.RuntimeOptions(working_dir=tmp_path)) as session:
        assert session.raw_session is runtime.sessions[0]
        session.send("ctx")
        assert session.text() == "echo:ctx"
        assert runtime.sessions[0].stopped is False

    assert runtime.sessions[0].stopped is True


def test_open_session_requires_options():
    client = LingTaiClient(runtime=FakeRuntime())
    with pytest.raises(ValueError, match="open_session.*requires RuntimeOptions"):
        client.open_session()


def test_root_exports_session_facade_lazily_and_wrapper_free(tmp_path):
    code = f"""
import sys
sys.path.insert(0, {str(SRC)!r})
import lingtai_sdk
Session = lingtai_sdk.LingTaiSession
open_session = lingtai_sdk.open_session
assert Session.__name__ == 'LingTaiSession'
assert callable(open_session)
bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]
assert not bad, bad
assert 'LingTaiSession' in dir(lingtai_sdk)
assert 'open_session' in dir(lingtai_sdk)
print('OK')
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_root_exports_runtime_contract_lazily_and_wrapper_free(tmp_path):
    code = f"""
import sys
sys.path.insert(0, {str(SRC)!r})
import lingtai_sdk
Options = lingtai_sdk.RuntimeOptions
Message = lingtai_sdk.RuntimeMessage
Event = lingtai_sdk.RuntimeEvent
State = lingtai_sdk.RuntimeState
Kind = lingtai_sdk.EventKind
Runtime = lingtai_sdk.Runtime
Session = lingtai_sdk.RuntimeSession
assert Options.__name__ == 'RuntimeOptions'
assert Message.__name__ == 'RuntimeMessage'
assert Event.text('hi').kind is Kind.TEXT
assert State.PENDING.value == 'pending'
assert Runtime.__name__ == 'Runtime'
assert Session.__name__ == 'RuntimeSession'
opts = Options(working_dir={str(tmp_path)!r})
assert str(opts.working_dir).endswith({tmp_path.name!r})
bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]
assert not bad, bad
for name in ('RuntimeOptions', 'RuntimeMessage', 'RuntimeEvent', 'RuntimeState', 'EventKind', 'Runtime', 'RuntimeSession'):
    assert name in dir(lingtai_sdk)
print('OK')
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
