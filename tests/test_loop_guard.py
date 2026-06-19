from lingtai_kernel.loop_guard import LoopGuard


def test_loop_guard_no_loop():
    """Distinct tool calls with different args should never be blocked."""
    guard = LoopGuard(max_total_calls=10)
    for i in range(5):
        verdict = guard.record_tool_call(f"tool_{i}", {"arg": i})
        assert not verdict.blocked


def test_loop_guard_repeated_identical_calls_are_advisory_only():
    """Identical calls receive guidance but are not blocked by the kernel."""
    guard = LoopGuard(max_total_calls=20)
    for _ in range(3):
        verdict = guard.record_tool_call("read", {"path": "/foo"})
        assert not verdict.blocked
        assert verdict.warning is None

    verdict = guard.record_tool_call("read", {"path": "/foo"})
    assert not verdict.blocked
    assert verdict.warning is not None
    assert "NOT blocked" in verdict.warning
    assert verdict.count == 4
    advisory = guard.advisory_metadata(verdict)
    assert advisory is not None
    assert advisory["type"] == "duplicate_tool_call"
    assert advisory["allowed"] is True
    assert advisory["blocked"] is False
    assert advisory["advisory_only"] is True
    assert advisory["repeat_count"] == 4
    assert "system-manual" in advisory["skill_refs"]


def test_loop_guard_custom_free_passes_start_advisory_without_blocking():
    """Custom free-pass count controls when advisory metadata starts."""
    guard = LoopGuard(max_total_calls=20, dup_free_passes=1, dup_hard_block=5)
    guard.record_tool_call("read", {"path": "/foo"})  # count=1 (free pass)
    verdict = guard.record_tool_call("read", {"path": "/foo"})  # count=2 (warn)
    assert not verdict.blocked
    assert verdict.warning is not None
    assert guard.advisory_metadata(verdict)["severity"] == "caution"


def test_loop_guard_ignores_reasoning_metadata_for_duplicate_detection():
    """Different reasoning text must not let polling/list loops bypass dedup."""
    guard = LoopGuard(max_total_calls=20, dup_free_passes=1, dup_hard_block=3)
    guard.record_tool_call("bash", {"action": "poll", "job_id": "job-1", "_reasoning": "first"})
    second = guard.record_tool_call("bash", {"action": "poll", "job_id": "job-1", "_reasoning": "again"})
    third = guard.record_tool_call("bash", {"action": "poll", "job_id": "job-1", "_reasoning": "still waiting"})
    assert second.count == 2
    assert second.warning is not None
    assert not third.blocked
    assert third.warning is not None
    assert third.count == 3
    advisory = guard.advisory_metadata(third)
    assert advisory is not None
    assert "_reasoning" in advisory["ignored_fields"]


def test_loop_guard_check_limit():
    """check_limit returns a reason when total would be exceeded."""
    guard = LoopGuard(max_total_calls=3)
    guard.record_calls(3)
    reason = guard.check_limit(1)
    assert reason is not None
    assert "3" in reason


def test_loop_guard_invalid_tool():
    """Repeated invalid tool names should trigger a stop reason."""
    guard = LoopGuard(max_total_calls=20, invalid_tool_limit=2)
    guard.record_invalid_tool("ghost_tool")
    guard.record_invalid_tool("ghost_tool")
    guard.record_invalid_tool("ghost_tool")  # count=3 > limit=2
    reason = guard.check_invalid_tool_limit()
    assert reason is not None
    assert "ghost_tool" in reason


def test_loop_guard_progress_metadata_and_interval_notice():
    """ACTIVE-turn progress metadata is always available and adds soft notices at intervals."""
    guard = LoopGuard(max_total_calls=10_000, notice_interval=500)

    assert guard.record_calls(499) is None
    meta = guard.progress_metadata()
    assert meta == {"active_turn_tool_calls": 499}

    notice = guard.record_calls(1)
    assert notice is not None
    assert "Soft self-check" in notice
    assert "500 tool calls" in notice
    assert "may be repeating a loop" in notice
    assert "10000" not in notice
    assert "limit" not in notice.lower()
    meta = guard.progress_metadata()
    assert meta["active_turn_tool_calls"] == 500
    assert meta["active_turn_tool_call_notice"] == notice
    assert "active_turn_tool_call_limit" not in meta
    assert "active_turn_tool_call_notice_interval" not in meta

    guard.clear_progress_notice()
    assert "active_turn_tool_call_notice" not in guard.progress_metadata()


def test_loop_guard_default_emergency_limit_is_large():
    """The default total limit is an emergency fuse, not a 100-call guard."""
    guard = LoopGuard()
    assert guard.max_total_calls == 10_000
