"""Regression tests for LingTai-initiated daemon CLI SIGTERM/143 attribution.

Issue #455: a ``claude-p`` daemon (``em-5``) was SIGTERM'd in the same second
its parent ran ``system.refresh`` -> ``shutdown_for_agent_stop(reason="agent_stop")``
-> ``_drain_all_cli_procs`` -> ``_kill_process_group``. The local reason was
known at the kill site but discarded, so the run failed with the opaque
``RuntimeError: claude CLI exited with code 143``.

These tests pin the small fix: stamp the reason at the out-of-loop kill sites
(``_drain_all_cli_procs`` / ``_kill_cli_group``) and recover it when the read
loop classifies the resulting -15/143 returncode, producing an attributed
message and a structured forensic record — while leaving an external/unknown
SIGTERM (no recorded reason) as the existing opaque failure.
"""
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig


class _FakeProc:
    """Minimal stand-in for subprocess.Popen the kill/classify helpers use."""

    _next_pid = 7000

    def __init__(self, returncode=None):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode


def _make_manager(tmp_path):
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["daemon"],
        config=AgentConfig(),
    )
    return agent.get_capability("daemon")


def _make_run_dir(tmp_path, handle="em-5"):
    from lingtai.core.daemon.run_dir import DaemonRunDir

    parent_wd = tmp_path / "rd-parent"
    parent_wd.mkdir(exist_ok=True)
    return DaemonRunDir(
        parent_working_dir=parent_wd,
        handle=handle,
        task="patch the kernel",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=1800.0,
        parent_addr="parent",
        parent_pid=12345,
        system_prompt="You are a daemon emanation.",
        backend="claude-p",
    )


# --- signal-name mapping -------------------------------------------------


def test_signal_exit_name_covers_both_conventions():
    from lingtai.core.daemon import DaemonManager

    # subprocess negative convention and shell 128+signum convention
    assert DaemonManager._signal_exit_name(-15) == "SIGTERM"
    assert DaemonManager._signal_exit_name(143) == "SIGTERM"
    assert DaemonManager._signal_exit_name(-9) == "SIGKILL"
    assert DaemonManager._signal_exit_name(137) == "SIGKILL"
    # a genuine non-zero CLI error is not a signal
    assert DaemonManager._signal_exit_name(1) is None
    assert DaemonManager._signal_exit_name(0) is None
    assert DaemonManager._signal_exit_name(None) is None


# --- reason stamping at the out-of-loop kill sites -----------------------


def test_drain_all_stamps_reason(tmp_path, monkeypatch):
    """The agent_stop / parent-refresh / reclaim path records the reason.

    This is the exact em-5 path: the kill is issued from the shutdown thread,
    so the read loop only observes the returncode and must recover the reason.
    """
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("lingtai.core.daemon._kill_process_group", lambda p: None)

    proc = _FakeProc()
    mgr._register_cli_proc(proc, group_id="g")

    drained = mgr._drain_all_cli_procs(reason="agent_stop")
    assert drained == [proc]
    assert mgr._take_cli_term_reason(proc) == "agent_stop"
    # consumed exactly once
    assert mgr._take_cli_term_reason(proc) is None


def test_drain_all_without_reason_stamps_nothing(tmp_path):
    """Reclaim callers that pass no reason must not invent attribution."""
    mgr = _make_manager(tmp_path)
    proc = _FakeProc()
    mgr._register_cli_proc(proc, group_id=None)

    mgr._drain_all_cli_procs()
    assert mgr._take_cli_term_reason(proc) is None


def test_kill_cli_group_stamps_timeout(tmp_path, monkeypatch):
    """A batch timeout watchdog kill attributes its procs as ``timeout``."""
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("lingtai.core.daemon._kill_process_group", lambda p: None)

    proc = _FakeProc()
    mgr._register_cli_proc(proc, group_id="grp")

    mgr._kill_cli_group("grp")
    assert mgr._take_cli_term_reason(proc) == "timeout"


def test_first_reason_wins(tmp_path):
    """A later teardown kill must not overwrite the original causal reason."""
    mgr = _make_manager(tmp_path)
    proc = _FakeProc()

    mgr._note_cli_term_reason(proc, "timeout")
    mgr._note_cli_term_reason(proc, "agent_stop")  # later sweep — must not win
    assert mgr._take_cli_term_reason(proc) == "timeout"


def test_register_clears_stale_reason(tmp_path):
    """A recycled id() must not carry a previous proc's reason onto a new one."""
    mgr = _make_manager(tmp_path)
    proc = _FakeProc()
    mgr._note_cli_term_reason(proc, "reclaim")

    # Re-registering the same object id (models id() recycling) drops the stale
    # reason so a fresh subprocess is never mis-attributed.
    mgr._register_cli_proc(proc, group_id=None)
    assert mgr._take_cli_term_reason(proc) is None


# --- classification at the returncode site -------------------------------


def test_attributed_exit_names_reason_and_records(tmp_path, monkeypatch):
    """A -15 exit with a recorded reason -> attributed message + forensic record."""
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("lingtai.core.daemon._kill_process_group", lambda p: None)
    run_dir = _make_run_dir(tmp_path)

    proc = _FakeProc(returncode=-15)
    mgr._register_cli_proc(proc, group_id="g")
    mgr._drain_all_cli_procs(reason="agent_stop")

    msg = mgr._attributed_cli_exit(proc, "claude", "stderr tail", run_dir)
    assert msg is not None
    assert "terminated by LingTai" in msg
    assert "agent_stop" in msg
    assert "SIGTERM" in msg
    assert "-15" in msg  # raw exit code preserved for forensics
    assert "stderr tail" in msg

    state = run_dir.state_snapshot()
    assert state["cli_termination"]["reason"] == "agent_stop"
    assert state["cli_termination"]["signal"] == "SIGTERM"
    assert state["cli_termination"]["returncode"] == -15
    # The reason is consumed by classification.
    assert mgr._take_cli_term_reason(proc) is None


def test_attributed_exit_handles_143_shell_convention(tmp_path):
    """143 (128 + SIGTERM through a shell) is attributed identically to -15."""
    mgr = _make_manager(tmp_path)
    run_dir = _make_run_dir(tmp_path)

    proc = _FakeProc(returncode=143)
    mgr._note_cli_term_reason(proc, "reclaim")

    msg = mgr._attributed_cli_exit(proc, "codex", "", run_dir)
    assert msg is not None
    assert "reclaim" in msg
    assert "143" in msg


def test_external_sigterm_stays_opaque(tmp_path):
    """No recorded reason -> None, so the caller keeps its existing message.

    This is the external/unknown-kill case: we must not relabel it as a
    deliberate LingTai cancellation.
    """
    mgr = _make_manager(tmp_path)
    run_dir = _make_run_dir(tmp_path)

    proc = _FakeProc(returncode=143)  # killed, but not via a tracked path
    assert mgr._attributed_cli_exit(proc, "claude", "tail", run_dir) is None
    assert "cli_termination" not in run_dir.state_snapshot()


def test_nonsignal_error_not_attributed(tmp_path):
    """A genuine non-zero CLI error (exit 1) is never reason-attributed."""
    mgr = _make_manager(tmp_path)
    run_dir = _make_run_dir(tmp_path)

    proc = _FakeProc(returncode=1)
    mgr._note_cli_term_reason(proc, "agent_stop")  # even if a reason leaked in
    assert mgr._attributed_cli_exit(proc, "claude", "boom", run_dir) is None
