"""Focused daemon test helpers.

These keep repeated daemon test setup in one place without becoming a general
test framework.  The helpers intentionally model the concrete shapes the daemon
tests already use: a mock daemon-capable agent, a run directory, finite fake CLI
processes, and in-memory daemon entries.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock

from lingtai.agent import Agent
from lingtai.core.daemon.run_dir import DaemonRunDir
from lingtai_kernel.config import AgentConfig


def make_daemon_agent(
    tmp_path: Path,
    *,
    capabilities: list[str] | None = None,
    working_dir_name: str = "daemon-agent",
) -> Agent:
    """Create the minimal mock-service Agent used by daemon tests."""
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / working_dir_name,
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )


def make_daemon_run_dir(
    agent: Agent | None = None,
    *,
    parent_working_dir: Path | None = None,
    handle: str = "em-test",
    task: str = "test task",
    tools: Iterable[str] | None = ("file",),
    model: str = "mock-model",
    max_turns: int = 30,
    timeout_s: float = 300.0,
    parent_addr: str | None = None,
    parent_pid: int = 12345,
    system_prompt: str = "You are a daemon.",
    backend: str = "lingtai",
) -> DaemonRunDir:
    """Create a DaemonRunDir with explicit, daemon-test-oriented defaults."""
    if parent_working_dir is None:
        if agent is None:
            raise ValueError("make_daemon_run_dir requires agent or parent_working_dir")
        parent_working_dir = agent._working_dir
    parent_working_dir.mkdir(parents=True, exist_ok=True)

    return DaemonRunDir(
        parent_working_dir=parent_working_dir,
        handle=handle,
        task=task,
        tools=list(tools or []),
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        parent_addr=parent_addr or parent_working_dir.name,
        parent_pid=parent_pid,
        system_prompt=system_prompt,
        backend=backend,
    )


class FiniteFakeProc:
    """Minimal ``subprocess.Popen`` stand-in with finite stdout/stderr streams."""

    def __init__(
        self,
        *,
        stdout_lines: Iterable[str] = (),
        stderr_lines: Iterable[str] = (),
        returncode: int = 0,
        pid: int = 0,
    ) -> None:
        self.stdout = iter(list(stdout_lines))
        self.stderr = iter(list(stderr_lines))
        self.returncode = returncode
        self.pid = pid

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def completed_future(result: Any = None) -> Future[Any]:
    future: Future[Any] = Future()
    future.set_result(result)
    return future


def register_daemon_entry(
    mgr: Any,
    em_id: str,
    run_dir: DaemonRunDir,
    *,
    future: Any | None = None,
    task: str = "test task",
    start_time: float = 0.0,
    backend: str | None = None,
    ask_in_flight: bool | None = None,
    ask_future: Any | None = None,
) -> dict[str, Any]:
    """Register an in-memory daemon entry and return it for assertions."""
    entry: dict[str, Any] = {
        "future": future if future is not None else Future(),
        "task": task,
        "start_time": start_time,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    if backend is not None:
        entry["backend"] = backend
    if ask_in_flight is not None:
        entry["ask_in_flight"] = ask_in_flight
        entry["ask_future"] = ask_future
    mgr._emanations[em_id] = entry
    return entry
