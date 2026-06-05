"""Tests for WorkingDir filesystem Time Machine — list, diff, preview, rollback.

These exercise the git-backed rollback primitives on a real temporary repo.
If git is not installed the whole module is skipped rather than failing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lingtai_kernel.workdir import WorkingDir

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _commit(wd: WorkingDir, name: str, content: str) -> str:
    """Write a file and snapshot it, returning the short commit hash."""
    (wd.path / name).write_text(content)
    h = wd.snapshot()
    assert h is not None
    return h


# --- snapshot_list -------------------------------------------------------


def test_snapshot_list_returns_recent_commits(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    _commit(wd, "a.md", "first")
    _commit(wd, "b.md", "second")

    snaps = wd.snapshot_list()

    # Newest first; at least the two snapshots plus the init commit.
    assert len(snaps) >= 3
    assert all({"hash", "date", "subject"} <= set(s) for s in snaps)
    # Most recent snapshot subject should reference the b.md snapshot commit.
    assert snaps[0]["subject"].startswith("snapshot ")


def test_snapshot_list_respects_limit(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    for i in range(5):
        _commit(wd, f"f{i}.md", f"v{i}")

    snaps = wd.snapshot_list(limit=2)
    assert len(snaps) == 2


def test_snapshot_list_empty_without_git(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")  # never init_git
    assert wd.snapshot_list() == []


# --- snapshot_diff -------------------------------------------------------


def test_snapshot_diff_shows_stat_and_patch(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "original\n")
    _commit(wd, "a.md", "changed\n")  # modify a.md in a later commit

    diff = wd.snapshot_diff(first)

    assert diff["ref"] == first
    assert "a.md" in diff["stat"]
    assert "changed" in diff["patch"] or "original" in diff["patch"]


def test_snapshot_diff_invalid_ref(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    _commit(wd, "a.md", "x")

    diff = wd.snapshot_diff("deadbeef")
    assert diff["error"] is True


# --- rollback_preview ----------------------------------------------------


def test_rollback_preview_reports_changes(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    _commit(wd, "a.md", "v2\n")  # HEAD now has v2

    preview = wd.rollback_preview(first)

    assert preview["error"] is False
    assert preview["ref_resolved"]  # full hash of `first`
    assert preview["current_head"]
    assert preview["dirty"] is False
    # Rolling back from HEAD (v2) to `first` (v1) changes a.md.
    assert any("a.md" in c for c in preview["changes"])
    assert preview["warning"]


def test_rollback_preview_detects_dirty_tree(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    _commit(wd, "a.md", "v2\n")
    (wd.path / "a.md").write_text("uncommitted edit\n")  # dirty tree

    preview = wd.rollback_preview(first)
    assert preview["dirty"] is True


def test_rollback_preview_invalid_ref(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    _commit(wd, "a.md", "x")

    preview = wd.rollback_preview("nope-not-a-ref")
    assert preview["error"] is True


# --- rollback_apply ------------------------------------------------------


def test_rollback_apply_restores_clean_tree(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    head_before = _commit(wd, "a.md", "v2\n")

    result = wd.rollback_apply(first)

    assert result["status"] == "ok"
    assert result["restored_to"].startswith(first[:7])
    assert result["previous_head"].startswith(head_before[:7])
    # Working tree content reverted to v1.
    assert (wd.path / "a.md").read_text() == "v1\n"
    # A safety ref recording the prior HEAD exists.
    assert result["safety_ref"]
    safety = subprocess.run(
        ["git", "rev-parse", "--verify", result["safety_ref"]],
        cwd=wd.path, capture_output=True, text=True,
    )
    assert safety.returncode == 0


def test_rollback_apply_refuses_dirty_without_force(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    _commit(wd, "a.md", "v2\n")
    (wd.path / "a.md").write_text("uncommitted\n")  # dirty

    result = wd.rollback_apply(first, force=False)

    assert result["status"] == "refused"
    assert result["reason"] == "dirty"
    # Nothing was reverted — still the dirty content.
    assert (wd.path / "a.md").read_text() == "uncommitted\n"


def test_rollback_apply_force_overrides_dirty(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    _commit(wd, "a.md", "v2\n")
    (wd.path / "a.md").write_text("uncommitted\n")  # dirty

    result = wd.rollback_apply(first, force=True)

    assert result["status"] == "ok"
    assert (wd.path / "a.md").read_text() == "v1\n"


def test_rollback_apply_refuses_invalid_ref(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    _commit(wd, "a.md", "v1\n")

    result = wd.rollback_apply("not-a-real-ref")
    assert result["status"] == "refused"
    assert result["reason"] == "invalid_ref"


def test_rollback_apply_preserves_git_dir(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "agent")
    wd.init_git()
    first = _commit(wd, "a.md", "v1\n")
    _commit(wd, "a.md", "v2\n")

    wd.rollback_apply(first)
    assert (wd.path / ".git").is_dir()  # never deletes .git
