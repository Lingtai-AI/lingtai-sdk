"""Tests for the post-molt child/delegation awareness reminder."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock


def _make_agent_with_psyche(tmp_path, name: str = "test"):
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return Agent(
        service=svc,
        agent_name=name,
        working_dir=tmp_path / name,
        capabilities=["psyche"],
    )


def _setup_mock_chat(agent):
    mock_interface = MagicMock()
    mock_interface.entries = []
    mock_interface.estimate_context_tokens.return_value = 50000

    mock_chat = MagicMock()
    mock_chat.interface = mock_interface

    def patched_ensure():
        if agent._session._chat is None:
            new_interface = MagicMock()
            new_interface.entries = []
            new_interface.estimate_context_tokens.return_value = 5000
            new_chat = MagicMock()
            new_chat.interface = new_interface
            agent._session._chat = new_chat
        return agent._session._chat

    agent._session.ensure_session = patched_ensure
    agent._session._chat = mock_chat
    agent._chat = mock_chat

    manifest_path = agent._working_dir / ".agent.json"
    if not manifest_path.exists():
        manifest_path.write_text("{}")

    return mock_interface


def _build_molt_call_entry(mock_interface, tc_id: str, summary: str):
    from lingtai_kernel.llm.interface import ToolCallBlock

    tc_block = ToolCallBlock(
        id=tc_id,
        name="psyche",
        args={"object": "context", "action": "molt", "summary": summary},
    )
    mock_entry = MagicMock()
    mock_entry.role = "assistant"
    mock_entry.content = [tc_block]
    mock_interface.entries = [mock_entry]


def _run_agent_molt(agent, summary: str = "continue after molt"):
    from lingtai_kernel.intrinsics.psyche._molt import _context_molt

    tc_id = f"toolu_delegate_{time.time_ns()}"
    mock_interface = _setup_mock_chat(agent)
    _build_molt_call_entry(mock_interface, tc_id, summary)
    return _context_molt(agent, {"summary": summary, "_tc_id": tc_id})


def _run_context_forget(agent, source: str = "warning_ladder"):
    from lingtai_kernel.intrinsics.psyche._molt import context_forget

    _setup_mock_chat(agent)
    return context_forget(agent, source=source)


def _read_notification(agent, channel: str) -> dict:
    path = agent._working_dir / ".notification" / f"{channel}.json"
    assert path.is_file(), f"{channel}.json should exist"
    return json.loads(path.read_text(encoding="utf-8"))


def _notification_path(agent, channel: str):
    return agent._working_dir / ".notification" / f"{channel}.json"


def _append_avatar_ledger(agent, *records: dict, **record_fields) -> None:
    ledger = agent._working_dir / "delegates" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if record_fields:
        records = (*records, record_fields)
    with open(ledger, "a", encoding="utf-8") as f:
        for record in records:
            payload = {"event": "avatar", "ts": time.time(), **record}
            f.write(json.dumps(payload) + "\n")


def _make_child(agent, name: str, *, state: str = "idle", heartbeat_age_s=0.0):
    child = agent._working_dir.parent / name
    child.mkdir(parents=True, exist_ok=True)
    (child / ".agent.json").write_text(
        json.dumps({"agent_name": name, "state": state}),
        encoding="utf-8",
    )
    if heartbeat_age_s is not None:
        (child / ".agent.heartbeat").write_text(
            str(time.time() - heartbeat_age_s),
            encoding="utf-8",
        )
    return child


def _make_daemon(agent, run_id: str, *, state: str = "running", heartbeat=True):
    run_dir = agent._working_dir / "daemons" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    daemon = {
        "handle": "em-2",
        "run_id": run_id,
        "state": state,
        "backend": "codex",
        "task": "research issue #256 and report a concise implementation plan",
        "elapsed_s": 91.2,
        "turn": 3,
        "current_tool": "bash",
        "last_output": "found notification sync and molt publication points",
    }
    (run_dir / "daemon.json").write_text(json.dumps(daemon), encoding="utf-8")
    if heartbeat:
        (run_dir / ".heartbeat").touch()
    return run_dir


def test_no_delegation_clears_stale_channel_and_preserves_post_molt(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        stale = _notification_path(agent, "post-child-delegation")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"header": "stale", "data": {}}), encoding="utf-8")

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        assert _notification_path(agent, "post-molt").is_file()
        assert not stale.exists()
    finally:
        agent.stop()


def test_live_avatar_publishes_reminder_without_signal_side_effects(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        child = _make_child(agent, "helper", state="idle", heartbeat_age_s=0.0)
        _append_avatar_ledger(
            agent,
            name="helper",
            working_dir="helper",
            type="shallow",
            boot_status="ok",
            mission="Investigate the post-molt reminder without touching runtime.",
        )

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        payload = _read_notification(agent, "post-child-delegation")
        data = payload["data"]
        assert data["awareness_only"] is True
        assert data["automatic_lifecycle_actions"] is False
        assert data["counts"]["avatars"]["alive"] == 1
        assert data["avatars"][0]["state"] == "alive"
        assert data["avatars"][0]["manifest_state"] == "idle"
        assert "Do not CPR" in payload["instructions"]

        for signal in (".interrupt", ".sleep", ".suspend", ".refresh", ".clear"):
            assert not (child / signal).exists()
    finally:
        agent.stop()


def test_avatar_stale_missing_and_boot_failed_classification(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        _make_child(agent, "stale-child", state="asleep", heartbeat_age_s=999.0)
        _append_avatar_ledger(
            agent,
            name="stale-child",
            working_dir="stale-child",
            boot_status="ok",
            mission="stale heartbeat",
        )
        _append_avatar_ledger(
            agent,
            name="missing-child",
            working_dir="missing-child",
            boot_status="ok",
            mission="missing directory",
        )
        _append_avatar_ledger(
            agent,
            name="failed-child",
            working_dir="failed-child",
            boot_status="failed",
            mission="failed during boot",
        )

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        data = _read_notification(agent, "post-child-delegation")["data"]
        states = {entry["name"]: entry["state"] for entry in data["avatars"]}
        assert states["stale-child"] == "stale"
        assert states["missing-child"] == "missing"
        assert states["failed-child"] == "boot_failed"
        assert data["counts"]["avatars"]["stale"] == 1
        assert data["counts"]["avatars"]["missing"] == 1
        assert data["counts"]["avatars"]["boot_failed"] == 1
    finally:
        agent.stop()


def test_running_daemon_publishes_reminder(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        _make_daemon(agent, "em-2-20260612-120000-a1b2c3")

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        data = _read_notification(agent, "post-child-delegation")["data"]
        assert data["counts"]["daemons"]["running"] == 1
        daemon = data["daemons"][0]
        assert daemon["id"] == "em-2"
        assert daemon["run_id"] == "em-2-20260612-120000-a1b2c3"
        assert daemon["state"] == "running"
        assert daemon["backend"] == "codex"
        assert daemon["current_tool"] == "bash"
        assert "daemon(action='check', id='em-2')" == daemon["suggested_action"]
    finally:
        agent.stop()


def test_malformed_ledger_and_daemon_json_do_not_fail_molt(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        ledger = agent._working_dir / "delegates" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("{not-json}\n", encoding="utf-8")
        run_dir = agent._working_dir / "daemons" / "em-bad"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "daemon.json").write_text("{not-json}", encoding="utf-8")

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        assert _notification_path(agent, "post-molt").is_file()
        assert not _notification_path(agent, "post-child-delegation").exists()
    finally:
        agent.stop()


def test_context_forget_publishes_delegation_reminder(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        _make_daemon(agent, "em-2-20260612-120000-a1b2c3")

        result = _run_context_forget(agent, source="warning_ladder")

        assert result.get("status") == "ok"
        data = _read_notification(agent, "post-child-delegation")["data"]
        assert data["initiator"] == "system"
        assert data["source"] == "warning_ladder"
        assert data["molt_count"] == result["molt_count"]
        assert data["counts"]["daemons"]["running"] == 1
    finally:
        agent.stop()


def test_payload_is_bounded_and_truncated(tmp_path):
    agent = _make_agent_with_psyche(tmp_path)
    agent.start()
    try:
        for i in range(25):
            name = f"helper-{i}"
            _make_child(agent, name, heartbeat_age_s=0.0)
            _append_avatar_ledger(
                agent,
                name=name,
                working_dir=name,
                boot_status="ok",
                mission=f"mission {i}",
            )

        result = _run_agent_molt(agent)

        assert result.get("status") == "ok"
        data = _read_notification(agent, "post-child-delegation")["data"]
        assert len(data["avatars"]) == 20
        assert data["counts"]["avatars"]["alive"] == 25
        assert data["limits"]["truncated"] is True
        assert data["limits"]["avatars_truncated"] is True
    finally:
        agent.stop()
