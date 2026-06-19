"""Tests for WorkingDir — filesystem, locking, git, manifest."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lingtai_kernel.workdir import WorkingDir


def test_workdir_accepts_path(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    assert wd.path == tmp_path / "myagent"
    assert wd.path.is_dir()


def test_workdir_creates_parents(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "deep" / "nested" / "agent")
    assert wd.path == tmp_path / "deep" / "nested" / "agent"
    assert wd.path.is_dir()


def test_lock_prevents_second_instance(tmp_path):
    wd1 = WorkingDir(working_dir=tmp_path / "myagent")
    wd1.acquire_lock()
    try:
        wd2 = WorkingDir(working_dir=tmp_path / "myagent")
        with pytest.raises(RuntimeError, match="already in use"):
            wd2.acquire_lock()
    finally:
        wd1.release_lock()


def test_lock_release_allows_reuse(tmp_path):
    wd1 = WorkingDir(working_dir=tmp_path / "myagent")
    wd1.acquire_lock()
    wd1.release_lock()
    wd2 = WorkingDir(working_dir=tmp_path / "myagent")
    wd2.acquire_lock()  # should not raise
    wd2.release_lock()


def test_git_init_creates_repo(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    wd.init_git()
    assert (wd.path / ".git").is_dir()
    assert (wd.path / ".gitignore").is_file()
    assert (wd.path / "system" / "covenant.md").is_file()
    assert (wd.path / "system" / "pad.md").is_file()


def test_git_init_skips_if_already_initialized(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    wd.init_git()
    result1 = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=wd.path, capture_output=True, text=True,
    )
    wd.init_git()  # second call — should be no-op
    result2 = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=wd.path, capture_output=True, text=True,
    )
    assert result1.stdout.strip() == result2.stdout.strip()


def test_read_manifest_returns_empty_when_missing(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    assert wd.read_manifest() == ""


def test_write_and_read_manifest(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    manifest = {"address": "/agents/a1b2c3d4e5f6", "covenant": "researcher", "started_at": "2026-01-01T00:00:00Z"}
    wd.write_manifest(manifest)
    covenant = wd.read_manifest()
    assert covenant == "researcher"


def test_diff_and_commit(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    wd.init_git()
    # Write to tracked file
    pad_file = wd.path / "system" / "pad.md"
    pad_file.write_text("hello world")
    diff_text, commit_hash = wd.diff_and_commit("system/pad.md", "pad")
    assert commit_hash is not None
    assert diff_text  # should have some diff content


def test_diff_and_commit_no_changes(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    wd.init_git()
    diff_text, commit_hash = wd.diff_and_commit("system/pad.md", "pad")
    assert diff_text is None
    assert commit_hash is None


def test_diff_read_only(tmp_path):
    wd = WorkingDir(working_dir=tmp_path / "myagent")
    wd.init_git()
    pad_file = wd.path / "system" / "pad.md"
    pad_file.write_text("new content")
    result = wd.diff("system/pad.md")
    assert isinstance(result, str)
    # Should not commit — file should still show as changed
    status = subprocess.run(
        ["git", "status", "--porcelain", "system/pad.md"],
        cwd=wd.path, capture_output=True, text=True,
    )
    assert status.stdout.strip()  # still dirty


import time
import threading


def test_acquire_lock_timeout_succeeds_after_release(tmp_path):
    """acquire_lock with timeout should succeed once the lock is released."""
    dir_a = tmp_path / "agent"
    dir_a.mkdir()
    wd1 = WorkingDir(dir_a)
    wd1.acquire_lock()

    acquired = threading.Event()

    def try_lock():
        wd2 = WorkingDir(dir_a)
        wd2.acquire_lock(timeout=5.0)
        acquired.set()
        wd2.release_lock()

    t = threading.Thread(target=try_lock)
    t.start()

    time.sleep(0.5)
    assert not acquired.is_set()  # still waiting

    wd1.release_lock()
    t.join(timeout=5.0)
    assert acquired.is_set()


def test_acquire_lock_timeout_zero_raises_immediately(tmp_path):
    """acquire_lock with timeout=0 (default) raises immediately if locked."""
    dir_a = tmp_path / "agent"
    dir_a.mkdir()
    wd1 = WorkingDir(dir_a)
    wd1.acquire_lock()

    wd2 = WorkingDir(dir_a)
    with pytest.raises(RuntimeError, match="already in use"):
        wd2.acquire_lock(timeout=0)

    wd1.release_lock()


def test_acquire_lock_timeout_expires(tmp_path):
    """acquire_lock should raise after timeout if lock is never released."""
    dir_a = tmp_path / "agent"
    dir_a.mkdir()
    wd1 = WorkingDir(dir_a)
    wd1.acquire_lock()

    wd2 = WorkingDir(dir_a)
    with pytest.raises(RuntimeError, match="already in use"):
        wd2.acquire_lock(timeout=1.0)

    wd1.release_lock()
