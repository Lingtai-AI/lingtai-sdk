from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lingtai_kernel.maintenance import RetentionOptions, TargetError, report_to_dict, scan_retention


NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _agent(
    root: Path,
    name: str = "agent",
    *,
    human: bool = False,
    admin_missing: bool = False,
    status_state: str | None = None,
    heartbeat_age: float | None = None,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    manifest = {"agent_name": name}
    if not admin_missing:
        manifest["admin"] = None if human else {"karma": True}
    (path / ".agent.json").write_text(json.dumps(manifest), encoding="utf-8")
    for folder in ("inbox", "outbox", "sent", "archive", "schedules"):
        (path / "mailbox" / folder).mkdir(parents=True)
    (path / "daemons").mkdir()
    (path / "logs").mkdir()
    if status_state:
        (path / ".status.json").write_text(
            json.dumps({"runtime": {"state": status_state}}),
            encoding="utf-8",
        )
    if heartbeat_age is not None:
        (path / ".agent.heartbeat").write_text(
            str(NOW.timestamp() - heartbeat_age),
            encoding="utf-8",
        )
    return path


def _daemon(
    agent: Path,
    run_id: str = "em-1-20260401-010101-abcdef",
    *,
    state: str | None = "done",
    finished_at: datetime | None = NOW - timedelta(days=60),
    heartbeat_age: float | None = None,
    corrupt: bool = False,
) -> Path:
    path = agent / "daemons" / run_id
    path.mkdir(parents=True)
    (path / "result.txt").write_text("result", encoding="utf-8")
    if corrupt:
        (path / "daemon.json").write_text("{bad", encoding="utf-8")
    else:
        data = {"state": state}
        if finished_at is not None:
            data["finished_at"] = finished_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        (path / "daemon.json").write_text(json.dumps(data), encoding="utf-8")
    if heartbeat_age is not None:
        hb = path / ".heartbeat"
        hb.touch()
        ts = NOW.timestamp() - heartbeat_age
        os.utime(hb, (ts, ts))
    return path


def _mail(agent: Path, folder: str, days_old: int, suffix: str = "abcd") -> Path:
    timestamp = (NOW - timedelta(days=days_old)).strftime("%Y%m%dT%H%M%S")
    path = agent / "mailbox" / folder / f"{timestamp}-{suffix}"
    path.mkdir(parents=True)
    (path / "message.json").write_text(json.dumps({"message": "body"}), encoding="utf-8")
    return path


def _sqlite(agent: Path, days_old: int) -> Path:
    path = agent / "logs" / "log.sqlite"
    path.write_bytes(b"sqlite sidecar")
    ts = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))
    return path


def _report(agent: Path, **kwargs):
    options = RetentionOptions(**{"older_than_days": 30, **kwargs})
    return report_to_dict(scan_retention(agent, options, _now=NOW))


def test_dry_run_reports_candidates_without_mutating(tmp_path):
    agent = _agent(tmp_path)
    run = _daemon(agent)
    sent = _mail(agent, "sent", 60)
    sqlite = _sqlite(agent, 60)

    data = _report(agent)

    assert data["mode"] == "dry_run"
    assert data["totals"]["candidates"] == 3
    assert data["classes"]["terminal_daemon_run"]["candidates"] == 1
    assert data["classes"]["sent_mail"]["candidates"] == 1
    assert data["classes"]["rebuildable_log_index"]["candidates"] == 1
    assert run.exists()
    assert sent.exists()
    assert sqlite.exists()
    for candidate in data["candidates"]:
        assert candidate["path"]
        assert candidate["reason"]
        assert candidate["bytes"] > 0
        assert candidate["age_days"] >= 30


@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "timeout"])
def test_terminal_daemon_states_are_candidates(tmp_path, state):
    agent = _agent(tmp_path)
    _daemon(agent, state=state)

    data = _report(agent)

    assert data["classes"]["terminal_daemon_run"]["candidates"] == 1
    assert data["candidates"][0]["detail"]["daemon_state"] == state


def test_running_daemon_and_fresh_daemon_heartbeat_are_protected(tmp_path):
    agent = _agent(tmp_path)
    _daemon(agent, "em-1-20260401-010101-running", state="running")
    _daemon(agent, "em-2-20260401-010101-freshh", heartbeat_age=1.0)

    data = _report(agent)

    assert data["classes"]["terminal_daemon_run"]["candidates"] == 0
    reasons = {item["reason"] for item in data["protected"]}
    assert "daemon_state_not_terminal:running" in reasons
    assert "daemon_heartbeat_fresh" in reasons


def test_protected_mail_and_authoritative_logs_are_never_candidates(tmp_path):
    agent = _agent(tmp_path)
    _mail(agent, "inbox", 60)
    _mail(agent, "outbox", 60)
    _mail(agent, "schedules", 60)
    events = agent / "logs" / "events.jsonl"
    token_ledger = agent / "logs" / "token_ledger.jsonl"
    recovery = agent / "logs" / "refresh_failed_permanent.json"
    for path in (events, token_ledger, recovery):
        path.write_text("{}", encoding="utf-8")

    data = _report(agent)

    assert data["totals"]["candidates"] == 0
    protected = {(item["category"], item["reason"]) for item in data["protected"]}
    assert ("inbox_mail", "inbox_mail_protected") in protected
    assert ("outbox_mail", "outbox_mail_protected") in protected
    assert ("scheduled_mail", "scheduled_mail_protected") in protected
    assert ("authoritative_log", "authoritative_or_recovery_log_protected") in protected


@pytest.mark.parametrize("state", ["active", "asleep", "suspended"])
def test_lifecycle_protected_agents_do_not_report_interior_candidates(tmp_path, state):
    agent = _agent(tmp_path, status_state=state)
    _daemon(agent)
    _mail(agent, "sent", 60)
    _sqlite(agent, 60)

    data = _report(agent)

    assert data["totals"]["candidates"] == 0
    assert data["agents"][0]["protected_agent"] is True
    assert f"status_state:{state}" in data["agents"][0]["protected_reasons"]
    reasons = {item["reason"] for item in data["protected"]}
    assert "agent_lifecycle_protected" in reasons


def test_fresh_agent_heartbeat_protects_agent_interior(tmp_path):
    agent = _agent(tmp_path, heartbeat_age=1.0)
    _daemon(agent)
    _mail(agent, "sent", 60)

    data = _report(agent)

    assert data["totals"]["candidates"] == 0
    assert "fresh_agent_heartbeat" in data["agents"][0]["protected_reasons"]


def test_human_agent_with_missing_admin_can_report_sent_mail(tmp_path):
    agent = _agent(tmp_path, admin_missing=True, status_state="active")
    _mail(agent, "sent", 60)

    data = _report(agent)

    assert data["agents"][0]["is_human"] is True
    assert data["agents"][0]["protected_agent"] is False
    assert data["classes"]["sent_mail"]["candidates"] == 1


def test_archive_is_protected_by_default_and_candidate_when_included(tmp_path):
    agent = _agent(tmp_path)
    _mail(agent, "archive", 60)

    default = _report(agent)
    included = _report(agent, include_archive=True)

    assert default["classes"]["archive_mail"]["candidates"] == 0
    assert any(item["reason"] == "archive_mail_protected_by_default" for item in default["protected"])
    assert included["classes"]["archive_mail"]["candidates"] == 1


def test_lingtai_root_scans_direct_agent_children_only(tmp_path):
    root = tmp_path / ".lingtai"
    root.mkdir()
    (root / "meta.json").write_text("{}", encoding="utf-8")
    alice = _agent(root, "alice")
    _mail(alice, "sent", 60)
    nested_parent = root / "not-agent"
    nested_parent.mkdir()
    nested = _agent(nested_parent, "nested")
    _mail(nested, "sent", 60)

    data = _report(root)

    assert data["target_kind"] == "lingtai_root"
    assert data["totals"]["agents"] == 1
    assert data["classes"]["sent_mail"]["candidates"] == 1
    assert data["agents"][0]["agent"] == "alice"


def test_symlink_target_is_rejected(tmp_path):
    agent = _agent(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(agent, target_is_directory=True)

    with pytest.raises(TargetError):
        scan_retention(link, RetentionOptions(), _now=NOW)


def test_symlink_candidate_is_skipped(tmp_path):
    agent = _agent(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = agent / "mailbox" / "sent" / "20260401T010101-link"
    link.symlink_to(outside, target_is_directory=True)

    data = _report(agent)

    assert data["classes"]["sent_mail"]["candidates"] == 0
    assert data["classes"]["sent_mail"]["skipped"] == 1
    assert data["skipped"][0]["reason"] == "symlink"


def test_json_report_is_deterministic(tmp_path):
    agent = _agent(tmp_path)
    _daemon(agent)
    _mail(agent, "sent", 60)

    first = report_to_dict(scan_retention(agent, RetentionOptions(), _now=NOW))
    second = report_to_dict(scan_retention(agent, RetentionOptions(), _now=NOW))

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
