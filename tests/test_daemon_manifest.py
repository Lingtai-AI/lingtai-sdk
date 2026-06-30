"""Tests for the daemon artifact manifest (artifacts.json) and its surfacing
through daemon(action='check').

The manifest is a compact, durable index of a run dir's important files —
relative path, size, mtime, inferred role — plus run-level state/result/error
paths, written at terminal time and surfaced by `check` so a parent does not
have to manually inspect logs/paths.
"""
import json
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock

from lingtai.core.daemon.run_dir import DaemonRunDir


def _make_run_dir(tmp_path: Path, **overrides) -> DaemonRunDir:
    parent_wd = tmp_path / "parent"
    parent_wd.mkdir(exist_ok=True)
    kwargs = dict(
        parent_working_dir=parent_wd,
        handle="em-3",
        task="find todos",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=300.0,
        parent_addr="parent",
        parent_pid=12345,
        system_prompt="You are a daemon emanation.",
    )
    kwargs.update(overrides)
    return DaemonRunDir(**kwargs)


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


# ----------------------------------------------------------------------
# DaemonRunDir.build_manifest / write_manifest
# ----------------------------------------------------------------------

def test_manifest_path_property(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.manifest_path == rd.path / "artifacts.json"


def test_build_manifest_lists_well_known_artifacts(tmp_path):
    rd = _make_run_dir(tmp_path)
    m = DaemonRunDir.build_manifest(rd.path)

    assert m["manifest_version"] == DaemonRunDir.MANIFEST_VERSION
    assert _ISO.fullmatch(m["generated_at"])
    assert m["run_id"] == rd.path.name
    assert m["state"] == "running"
    assert m["truncated"] is False

    by_path = {a["path"]: a for a in m["artifacts"]}
    # The always-present artifacts from construction.
    assert "daemon.json" in by_path
    assert ".prompt" in by_path
    assert ".heartbeat" in by_path
    assert "logs/events.jsonl" in by_path
    # Roles are inferred for well-known files.
    assert by_path["daemon.json"]["role"] == "status"
    assert by_path[".prompt"]["role"] == "prompt"
    assert by_path["logs/events.jsonl"]["role"] == "events"
    # Each entry carries metadata only — path/size/mtime/role, no content.
    for a in m["artifacts"]:
        assert set(a.keys()) == {"path", "size", "mtime", "role"}
        assert isinstance(a["size"], int)
        assert _ISO.fullmatch(a["mtime"])


def test_build_manifest_includes_extra_work_product_files(tmp_path):
    rd = _make_run_dir(tmp_path)
    (rd.path / "report.md").write_text("# work product\n", encoding="utf-8")
    (rd.path / "subdir").mkdir()
    (rd.path / "subdir" / "data.json").write_text("{}", encoding="utf-8")

    m = DaemonRunDir.build_manifest(rd.path)
    paths = {a["path"] for a in m["artifacts"]}
    assert "report.md" in paths
    assert "subdir/data.json" in paths
    # Extra files have no inferred role.
    by_path = {a["path"]: a for a in m["artifacts"]}
    assert by_path["report.md"]["role"] is None


def test_build_manifest_skips_tmp_files(tmp_path):
    rd = _make_run_dir(tmp_path)
    (rd.path / "daemon.json.tmp").write_text("partial", encoding="utf-8")
    m = DaemonRunDir.build_manifest(rd.path)
    paths = {a["path"] for a in m["artifacts"]}
    assert "daemon.json.tmp" not in paths


def test_build_manifest_caps_entries(tmp_path):
    rd = _make_run_dir(tmp_path)
    # Drop more files than the cap allows.
    extra = DaemonRunDir._MANIFEST_MAX_ENTRIES + 20
    for i in range(extra):
        (rd.path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")

    m = DaemonRunDir.build_manifest(rd.path)
    assert m["artifact_count"] == DaemonRunDir._MANIFEST_MAX_ENTRIES
    assert len(m["artifacts"]) == DaemonRunDir._MANIFEST_MAX_ENTRIES
    assert m["artifacts_total"] > DaemonRunDir._MANIFEST_MAX_ENTRIES
    assert m["truncated"] is True
    # Well-known artifacts survive the cap (listed first).
    paths = {a["path"] for a in m["artifacts"]}
    assert "daemon.json" in paths


def test_mark_done_writes_manifest_with_result(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("final report text")

    assert rd.manifest_path.is_file()
    m = json.loads(rd.manifest_path.read_text())
    assert m["state"] == "done"
    assert m["result_path"] == str(rd.result_path)
    assert m["error_path"] is None
    by_path = {a["path"]: a for a in m["artifacts"]}
    assert by_path["result.txt"]["role"] == "result"
    # artifacts.json itself isn't part of the scan it triggers (built first).
    assert "artifacts.json" not in by_path


def test_rebuild_manifest_skips_existing_manifest_file(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("final report text")

    rd.write_manifest()

    m = json.loads(rd.manifest_path.read_text())
    paths = {a["path"] for a in m["artifacts"]}
    assert "artifacts.json" not in paths


def test_mark_failed_writes_manifest(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_failed(RuntimeError("boom"))

    assert rd.manifest_path.is_file()
    m = json.loads(rd.manifest_path.read_text())
    assert m["state"] == "failed"


def test_mark_timeout_sets_error_path(tmp_path):
    rd = _make_run_dir(tmp_path)
    # A timeout run with a partial result.txt — error_path points at it.
    rd.result_path.write_text("partial output before kill", encoding="utf-8")
    rd.mark_timeout()

    m = json.loads(rd.manifest_path.read_text())
    assert m["state"] == "timeout"
    assert m["error_path"] == str(rd.result_path)


def test_build_manifest_missing_daemon_json_is_graceful(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.daemon_json_path.unlink()
    m = DaemonRunDir.build_manifest(rd.path)
    # state unknown, but file listing still works.
    assert m["state"] is None
    assert m["artifact_count"] >= 1


# ----------------------------------------------------------------------
# daemon(action="check") surfacing
# ----------------------------------------------------------------------

def _make_agent(tmp_path):
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["daemon"],
        config=AgentConfig(),
    )


def _register(mgr, em_id, run_dir):
    mgr._emanations[em_id] = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "test task",
        "start_time": 0.0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }


def test_check_surfaces_manifest_for_done_run(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle="em-1", task="t", tools=["file"], model="mock-model",
        max_turns=30, timeout_s=300.0, parent_addr="parent",
        parent_pid=1, system_prompt="p",
    )
    _register(mgr, "em-1", rd)
    rd.mark_done("the result")

    out = mgr.handle({"action": "check", "id": "em-1"})
    arts = out["artifacts"]
    assert arts["source"] == "manifest"  # persisted at terminal time
    assert arts["state"] == "done"
    assert arts["result_path"] == str(rd.result_path)
    paths = {a["path"] for a in arts["artifacts"]}
    assert "daemon.json" in paths
    assert "result.txt" in paths


def test_check_computes_fallback_manifest_for_running_run(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle="em-2", task="t", tools=["file"], model="mock-model",
        max_turns=30, timeout_s=300.0, parent_addr="parent",
        parent_pid=1, system_prompt="p",
    )
    _register(mgr, "em-2", rd)
    # No terminal marker → no artifacts.json yet.
    assert not rd.manifest_path.is_file()

    out = mgr.handle({"action": "check", "id": "em-2"})
    arts = out["artifacts"]
    assert arts["source"] == "fallback"
    assert arts["state"] == "running"
    assert arts["artifact_count"] >= 1


def test_check_historical_run_without_manifest_falls_back(tmp_path):
    """An old run dir on disk that predates artifacts.json still gets a
    computed listing via the historical-resolution path."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle="em-9", task="t", tools=["file"], model="mock-model",
        max_turns=30, timeout_s=300.0, parent_addr="parent",
        parent_pid=1, system_prompt="p",
    )
    rd.mark_done("done")
    # Simulate an old run: remove the manifest, leave the folder on disk.
    rd.manifest_path.unlink()
    # Not in the in-memory registry → resolves via historical fallback.

    out = mgr.handle({"action": "check", "id": rd.run_id})
    assert out["source"] == "history"
    arts = out["artifacts"]
    assert arts["source"] == "fallback"
    assert arts["state"] == "done"
