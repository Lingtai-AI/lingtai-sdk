# tests/test_daemon_cli_watchdog_scope.py
"""Regression tests for daemon CLI watchdog scoping (GH overlapping-batch kill).

An older daemon batch's timeout watchdog must only kill the CLI subprocesses
that belong to its own batch/group — never the procs of a newer, unrelated
batch. Reclaim-all (agent stop / explicit reclaim) may still kill everything.
"""
import threading
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig


class _FakeProc:
    """A stand-in for subprocess.Popen the kill helper can record against."""

    _next_pid = 9000

    def __init__(self):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.killed = False

    def wait(self, timeout=None):
        return 0


def _make_manager(tmp_path):
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["daemon"],
        config=AgentConfig(),
    )
    return agent.get_capability("daemon")


def test_watchdog_only_kills_its_own_group(tmp_path, monkeypatch):
    """An earlier batch's watchdog must not kill a later batch's CLI procs."""
    mgr = _make_manager(tmp_path)

    killed: list = []
    monkeypatch.setattr(
        "lingtai.core.daemon._kill_process_group",
        lambda proc: killed.append(proc),
    )

    # Two overlapping batches, each with its own group id and CLI proc.
    old_proc = _FakeProc()
    new_proc = _FakeProc()
    mgr._register_cli_proc(old_proc, group_id="group-old")
    mgr._register_cli_proc(new_proc, group_id="group-new")

    # The old batch's watchdog fires. It is scoped to group-old.
    cancel = threading.Event()
    timeout = threading.Event()
    mgr._watchdog(cancel, timeout, 0.0, cli_group_id="group-old")

    assert old_proc in killed, "watchdog must kill its own group's proc"
    assert new_proc not in killed, "watchdog must NOT kill another batch's proc"

    # The newer proc remains tracked for its own watchdog / reclaim.
    assert new_proc in mgr._cli_procs


def test_reclaim_all_kills_every_tracked_proc(tmp_path, monkeypatch):
    """Reclaim-all / agent stop still kills all tracked CLI procs."""
    mgr = _make_manager(tmp_path)

    killed: list = []
    monkeypatch.setattr(
        "lingtai.core.daemon._kill_process_group",
        lambda proc: killed.append(proc),
    )

    p_a = _FakeProc()
    p_b = _FakeProc()
    p_ask = _FakeProc()
    mgr._register_cli_proc(p_a, group_id="group-a")
    mgr._register_cli_proc(p_b, group_id="group-b")
    mgr._register_cli_proc(p_ask, group_id=None)  # ask procs: no batch group

    report = mgr._handle_reclaim()

    assert report["status"] == "reclaimed"
    assert set(killed) == {p_a, p_b, p_ask}
    assert mgr._cli_procs == []
    assert mgr._cli_proc_groups == {}


def test_unregister_removes_from_group_and_global(tmp_path):
    """Normal completion detaches a proc from both global and group tracking."""
    mgr = _make_manager(tmp_path)

    proc = _FakeProc()
    mgr._register_cli_proc(proc, group_id="group-x")
    assert proc in mgr._cli_procs
    assert proc in mgr._cli_proc_groups["group-x"]

    mgr._unregister_cli_proc(proc, group_id="group-x")
    assert proc not in mgr._cli_procs
    # Empty group buckets are pruned so they don't leak across runs.
    assert "group-x" not in mgr._cli_proc_groups

    # Idempotent: a second unregister (e.g. after watchdog already drained it)
    # must not raise.
    mgr._unregister_cli_proc(proc, group_id="group-x")


def test_completed_batch_watchdog_cancels_when_all_futures_done(tmp_path):
    """When all futures in a batch finish, the watchdog stops early.

    A completed batch must not let its watchdog wake later and kill procs.
    We model this with the cancel_event the watchdog observes: once the batch
    is done, the dispatch path sets cancel_event so the watchdog returns
    without killing anything.
    """
    mgr = _make_manager(tmp_path)

    cancel = threading.Event()
    timeout = threading.Event()
    # Simulate "all futures done" by signalling cancel before the deadline.
    cancel.set()

    killed: list = []
    import lingtai.core.daemon as daemon_mod

    orig = daemon_mod._kill_process_group
    daemon_mod._kill_process_group = lambda proc: killed.append(proc)
    try:
        # Long timeout, but cancel is already set, so it returns immediately
        # and kills nothing.
        mgr._watchdog(cancel, timeout, 1000.0, cli_group_id="group-done")
    finally:
        daemon_mod._kill_process_group = orig

    assert killed == []
    assert not timeout.is_set()
