"""Regression tests for daemon resources during agent teardown.

Production incident 2026-06-04: refresh stopped heartbeat/released lock while
CLI daemon executor workers still kept the old Python process alive. The next
watcher relaunch then hit the duplicate-process guard. These tests pin the
contract that daemon-owned pools/process groups are reclaimed before parent
liveness is withdrawn.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from types import SimpleNamespace


def test_daemon_shutdown_for_agent_stop_reclaims_pools_and_cli_processes(tmp_path, monkeypatch):
    from lingtai.core import daemon as daemon_module

    agent = SimpleNamespace(
        service=SimpleNamespace(model="mock-model"),
        _working_dir=tmp_path / "agent",
        _log=lambda *args, **kwargs: None,
    )
    mgr = daemon_module.DaemonManager(agent)

    pending = Future()
    ask_pending = Future()
    mgr._emanations["em-1"] = {
        "future": pending,
        "ask_future": ask_pending,
    }

    class FakePool:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    pool = FakePool()
    cancel = threading.Event()
    mgr._pools.append((pool, cancel))

    killed = []
    monkeypatch.setattr(
        daemon_module,
        "_kill_process_group",
        lambda proc: killed.append(proc.pid),
    )
    proc = SimpleNamespace(pid=4242)
    with mgr._cli_lock:
        mgr._cli_procs.append(proc)

    logs = []
    monkeypatch.setattr(mgr, "_log", lambda event, **fields: logs.append((event, fields)))

    report = mgr.shutdown_for_agent_stop(reason="agent_stop", wait_timeout=0.0)

    assert report["status"] == "shutdown"
    assert report["reason"] == "agent_stop"
    assert report["cancelled"] == 2
    assert report["cli_processes_killed"] == 1
    assert report["pools_shutdown"] == 1
    assert report["ask_futures_shutdown"] == 1
    assert killed == [4242]
    assert cancel.is_set()
    assert pool.shutdown_calls == [{"wait": False, "cancel_futures": True}]
    assert mgr._pools == []
    assert mgr._cli_procs == []
    assert mgr._emanations == {}
    assert any(event == "daemon_lifecycle_shutdown" for event, _ in logs)


def test_agent_stop_shuts_down_daemon_before_heartbeat_and_lock(monkeypatch):
    from lingtai_kernel.base_agent import lifecycle
    import lingtai_kernel.intrinsics.soul.flow as soul_flow

    order = []

    class FakeDaemon:
        def shutdown_for_agent_stop(self, *, reason):
            order.append(("daemon", reason))

    class FakeWorkdir:
        def write_manifest(self, manifest):
            order.append(("manifest", manifest))

        def release_lock(self):
            order.append(("lock", None))

    agent = SimpleNamespace(
        _log=lambda event, **fields: order.append(("log", event)),
        _shutdown=threading.Event(),
        _thread=None,
        _session=SimpleNamespace(close=lambda: order.append(("session", None))),
        _mail_service=None,
        _log_service=None,
        _workdir=FakeWorkdir(),
        _build_manifest=lambda: {"agent": "test"},
        get_capability=lambda name: FakeDaemon() if name == "daemon" else None,
    )

    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda a: order.append(("soul", None)))
    monkeypatch.setattr(lifecycle, "_stop_heartbeat", lambda a: order.append(("heartbeat", None)))

    lifecycle._stop(agent, timeout=0.01)

    assert ("daemon", "agent_stop") in order
    assert order.index(("daemon", "agent_stop")) < order.index(("heartbeat", None))
    assert order.index(("daemon", "agent_stop")) < order.index(("lock", None))
    assert order.index(("manifest", {"agent": "test"})) < order.index(("heartbeat", None))


def test_daemon_shutdown_waits_for_cli_ask_future_before_releasing_liveness(tmp_path, monkeypatch):
    from lingtai.core import daemon as daemon_module

    agent = SimpleNamespace(
        service=SimpleNamespace(model="mock-model"),
        _working_dir=tmp_path / "agent",
        _log=lambda *args, **kwargs: None,
    )
    mgr = daemon_module.DaemonManager(agent)

    primary_done = Future()
    primary_done.set_result("done")
    ask_done = Future()
    mgr._emanations["em-1"] = {
        "future": primary_done,
        "ask_future": ask_done,
    }

    waits = []

    def fake_wait(futures, timeout):
        waits.append((set(futures), timeout))
        ask_done.set_result("ask done")

    monkeypatch.setattr(daemon_module, "wait", fake_wait)
    report = mgr.shutdown_for_agent_stop(reason="agent_stop", wait_timeout=2.5)

    assert waits == [({primary_done, ask_done}, 2.5)]
    assert report["ask_futures_shutdown"] == 1
    assert report["futures_remaining"] == 0
