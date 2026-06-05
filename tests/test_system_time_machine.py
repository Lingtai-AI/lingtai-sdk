"""Tests for the filesystem Time Machine actions on the `system` intrinsic.

Actions: snapshot, snapshots, rollback_preview, rollback. These wrap the
WorkingDir git primitives and form the agent-callable rollback surface.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.workdir import WorkingDir

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


@dataclass
class _StubAgent:
    _working_dir: Path
    _workdir: WorkingDir = None  # type: ignore[assignment]
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._workdir is None:
            self._workdir = WorkingDir(self._working_dir)

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _agent(tmp_path: Path) -> _StubAgent:
    wd = WorkingDir(tmp_path / "agent")
    wd.init_git()
    agent = _StubAgent(_working_dir=wd.path, _workdir=wd)
    return agent


def _events(agent: _StubAgent, name: str) -> list[dict]:
    return [fields for event, fields in agent._logs if event == name]


# --- snapshot ------------------------------------------------------------


def test_snapshot_action_creates_commit(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "note.md").write_text("hello")

    res = sys_intrinsic.handle(agent, {"action": "snapshot"})

    assert res["status"] == "ok"
    assert res["hash"]
    assert _events(agent, "system_snapshot")


def test_snapshot_action_noop_when_clean(tmp_path):
    agent = _agent(tmp_path)
    res = sys_intrinsic.handle(agent, {"action": "snapshot"})
    assert res["status"] == "ok"
    assert res["hash"] is None  # nothing changed


# --- snapshots (list) ----------------------------------------------------


def test_snapshots_action_lists_recent(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1")
    sys_intrinsic.handle(agent, {"action": "snapshot"})

    res = sys_intrinsic.handle(agent, {"action": "snapshots"})

    assert res["status"] == "ok"
    assert isinstance(res["snapshots"], list)
    assert len(res["snapshots"]) >= 1
    assert {"hash", "date", "subject"} <= set(res["snapshots"][0])


# --- rollback_preview ----------------------------------------------------


def test_rollback_preview_action_requires_ref(tmp_path):
    agent = _agent(tmp_path)
    res = sys_intrinsic.handle(agent, {"action": "rollback_preview"})
    assert res["status"] == "error"
    assert res["reason"] == "missing_ref"


def test_rollback_preview_action_reports(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1\n")
    first = sys_intrinsic.handle(agent, {"action": "snapshot"})["hash"]
    (agent._working_dir / "a.md").write_text("v2\n")
    sys_intrinsic.handle(agent, {"action": "snapshot"})

    res = sys_intrinsic.handle(agent, {"action": "rollback_preview", "ref": first})

    assert res["status"] == "ok"
    assert res["ref_resolved"]
    assert res["warning"]
    assert any("a.md" in c for c in res["changes"])


def test_rollback_preview_action_invalid_ref(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1")
    sys_intrinsic.handle(agent, {"action": "snapshot"})

    res = sys_intrinsic.handle(agent, {"action": "rollback_preview", "ref": "bogus"})
    assert res["status"] == "error"
    assert res["reason"] == "invalid_ref"


# --- rollback (apply) ----------------------------------------------------


def test_rollback_action_requires_ref(tmp_path):
    agent = _agent(tmp_path)
    res = sys_intrinsic.handle(agent, {"action": "rollback"})
    assert res["status"] == "error"
    assert res["reason"] == "missing_ref"


def test_rollback_action_restores_clean_tree(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1\n")
    first = sys_intrinsic.handle(agent, {"action": "snapshot"})["hash"]
    (agent._working_dir / "a.md").write_text("v2\n")
    sys_intrinsic.handle(agent, {"action": "snapshot"})

    res = sys_intrinsic.handle(agent, {"action": "rollback", "ref": first})

    assert res["status"] == "ok"
    assert res["safety_ref"]
    assert (agent._working_dir / "a.md").read_text() == "v1\n"
    assert _events(agent, "system_rollback")


def test_rollback_action_refuses_dirty_without_force(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1\n")
    first = sys_intrinsic.handle(agent, {"action": "snapshot"})["hash"]
    (agent._working_dir / "a.md").write_text("v2\n")
    sys_intrinsic.handle(agent, {"action": "snapshot"})
    (agent._working_dir / "a.md").write_text("dirty\n")

    res = sys_intrinsic.handle(agent, {"action": "rollback", "ref": first})

    assert res["status"] == "refused"
    assert res["reason"] == "dirty"
    assert (agent._working_dir / "a.md").read_text() == "dirty\n"


def test_rollback_action_force_overrides_dirty(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1\n")
    first = sys_intrinsic.handle(agent, {"action": "snapshot"})["hash"]
    (agent._working_dir / "a.md").write_text("v2\n")
    sys_intrinsic.handle(agent, {"action": "snapshot"})
    (agent._working_dir / "a.md").write_text("dirty\n")

    res = sys_intrinsic.handle(
        agent, {"action": "rollback", "ref": first, "force": True}
    )

    assert res["status"] == "ok"
    assert (agent._working_dir / "a.md").read_text() == "v1\n"


def test_rollback_action_invalid_ref(tmp_path):
    agent = _agent(tmp_path)
    (agent._working_dir / "a.md").write_text("v1")
    sys_intrinsic.handle(agent, {"action": "snapshot"})

    res = sys_intrinsic.handle(agent, {"action": "rollback", "ref": "bogus"})
    assert res["status"] == "refused"
    assert res["reason"] == "invalid_ref"


# --- schema --------------------------------------------------------------


def test_schema_exposes_time_machine_actions():
    schema = sys_intrinsic.get_schema("en")
    enum = schema["properties"]["action"]["enum"]
    assert {"snapshot", "snapshots", "rollback_preview", "rollback"} <= set(enum)
    assert "ref" in schema["properties"]
