"""Pure unit tests for daemon backend runtime primitives.

These exercise ``lingtai.core.daemon.runtime`` helpers in isolation with small
fakes — no real subprocesses, no LLM mocks, no DaemonManager. The integrated
behavior (closure -> helper swap inside the backend runners) is covered by the
existing ``tests/test_daemon.py`` cancellation/timeout tests.
"""
import threading

from lingtai.core.daemon import runtime


class _FakeRunDir:
    """Records terminal-marker and cli_output calls for assertions."""

    def __init__(self, *, raise_on_record: bool = False):
        self.marked: list[str] = []
        self.cli_output: list[tuple[str, str]] = []
        self._raise_on_record = raise_on_record

    def mark_timeout(self) -> None:
        self.marked.append("timeout")

    def mark_cancelled(self) -> None:
        self.marked.append("cancelled")

    def record_cli_output(self, text: str, *, stream: str) -> None:
        if self._raise_on_record:
            raise RuntimeError("boom")
        self.cli_output.append((text, stream))


class _FakeProc:
    """Minimal Popen stand-in exposing a pre-baked stderr iterable."""

    def __init__(self, stderr_lines):
        self.stderr = iter(stderr_lines)


# --------------------------------------------------------------------------
# mark_cancelled_or_timeout
# --------------------------------------------------------------------------

def test_mark_cancelled_or_timeout_marks_timeout_when_event_set():
    rd = _FakeRunDir()
    ev = threading.Event()
    ev.set()
    result = runtime.mark_cancelled_or_timeout(rd, ev)
    assert result == "[cancelled]"
    assert rd.marked == ["timeout"]


def test_mark_cancelled_or_timeout_marks_cancelled_when_event_unset():
    rd = _FakeRunDir()
    ev = threading.Event()  # not set
    result = runtime.mark_cancelled_or_timeout(rd, ev)
    assert result == "[cancelled]"
    assert rd.marked == ["cancelled"]


def test_mark_cancelled_or_timeout_marks_cancelled_when_event_none():
    rd = _FakeRunDir()
    result = runtime.mark_cancelled_or_timeout(rd, None)
    assert result == "[cancelled]"
    assert rd.marked == ["cancelled"]


# --------------------------------------------------------------------------
# spawn_stderr_drainer / StderrDrain
# --------------------------------------------------------------------------

def test_stderr_drainer_captures_lines_and_records_cli_output():
    rd = _FakeRunDir()
    proc = _FakeProc(["alpha\n", "beta\n"])
    drain = runtime.spawn_stderr_drainer(proc, rd, thread_name="t-stderr")
    drain.join(timeout=2.0)
    assert drain.lines == ["alpha", "beta"]
    # Mirrored to record_cli_output verbatim with stream="stderr".
    assert rd.cli_output == [("alpha", "stderr"), ("beta", "stderr")]


def test_stderr_drainer_ignores_blank_lines():
    rd = _FakeRunDir()
    # Behavior preserved exactly: lines are only ``rstrip("\n")``-ed, so a
    # whitespace-only line ("   ") survives as truthy and IS kept; only lines
    # that become empty after stripping the newline are dropped.
    proc = _FakeProc(["x\n", "\n", "   \n", "y\n", ""])
    drain = runtime.spawn_stderr_drainer(proc, rd, thread_name="t-stderr")
    drain.join(timeout=2.0)
    assert drain.lines == ["x", "   ", "y"]
    assert rd.cli_output == [("x", "stderr"), ("   ", "stderr"), ("y", "stderr")]


def test_stderr_drainer_swallows_record_exceptions():
    rd = _FakeRunDir(raise_on_record=True)
    proc = _FakeProc(["one\n", "two\n"])
    drain = runtime.spawn_stderr_drainer(proc, rd, thread_name="t-stderr")
    drain.join(timeout=2.0)
    # record_cli_output raised every time, but lines are still captured.
    assert drain.lines == ["one", "two"]
    assert rd.cli_output == []


def test_stderr_drain_tail_returns_last_n_lines():
    drain = runtime.StderrDrain(threading.Thread(target=lambda: None), [])
    assert drain.tail() == ""
    drain.lines = [f"line-{i}" for i in range(30)]
    tail = drain.tail()
    assert tail.splitlines() == [f"line-{i}" for i in range(10, 30)]  # last 20
    assert drain.tail(n=3).splitlines() == ["line-27", "line-28", "line-29"]


# --------------------------------------------------------------------------
# iter_stdout_with_deadline
# --------------------------------------------------------------------------

def test_iter_stdout_yields_until_eof():
    proc = _FakeProc([])  # placeholder; override stdout below
    proc.stdout = iter(["a\n", "b\n", "c\n"])
    deadline = time_far_future()
    lines = list(runtime.iter_stdout_with_deadline(proc, deadline, "t-stdout"))
    assert lines == ["a\n", "b\n", "c\n"]


def test_iter_stdout_stops_at_past_deadline():
    proc = _FakeProc([])
    proc.stdout = iter(["never\n"])
    # Deadline already passed -> generator returns immediately, yields nothing.
    lines = list(runtime.iter_stdout_with_deadline(proc, 0.0, "t-stdout"))
    assert lines == []


def time_far_future() -> float:
    import time
    return time.monotonic() + 30.0
