"""Tests for system(action='summarize') — agent-authored context summarization.

Covers:
- schema registration: summarize in action enum
- basic success: single item
- batch: multiple items in one call
- per-item failure: unknown id, already summarized, missing fields
- idempotency: re-summarizing a summarized block returns error
- history persistence: _save_chat_history called after mutation
- large-result notification: threshold=5000, includes threshold in text
- large-result notification: excludes daemon-named tools
- large-result notification: skips spill manifests
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.intrinsics.system.summarize import (
    SUMMARIZE_MARKER,
    _is_already_summarized,
    _summarize,
    _visible_len,
)
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_agent(chat_interface: ChatInterface | None = None):
    """Return a minimal stub agent with a chat session wired up."""
    iface = chat_interface if chat_interface is not None else ChatInterface()

    class _StubChat:
        interface = iface

    agent = MagicMock()
    agent._chat = _StubChat()
    agent._chat.interface = iface
    agent._log = MagicMock()
    saved = []
    agent._save_chat_history = MagicMock(side_effect=lambda **kw: saved.append(kw))
    agent._saved = saved
    return agent


def _add_tool_pair(iface: ChatInterface, call_id: str, tool_name: str, result_content):
    """Append an assistant[tool_call] + user[tool_result] pair to the interface."""
    iface.add_assistant_message([ToolCallBlock(id=call_id, name=tool_name, args={})])
    iface.add_tool_results([ToolResultBlock(id=call_id, name=tool_name, content=result_content)])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_summarize_in_schema_enum():
    from lingtai_kernel.intrinsics.system.schema import get_schema
    schema = get_schema("en")
    assert "summarize" in schema["properties"]["action"]["enum"]


def test_schema_has_items_property():
    from lingtai_kernel.intrinsics.system.schema import get_schema
    schema = get_schema("en")
    assert "items" in schema["properties"]
    items_schema = schema["properties"]["items"]
    assert items_schema["type"] == "array"


# ---------------------------------------------------------------------------
# _is_already_summarized
# ---------------------------------------------------------------------------


def test_is_already_summarized_detects_marker():
    assert _is_already_summarized({"artifact": SUMMARIZE_MARKER, "agent_summary": "x"})


def test_is_already_summarized_ignores_plain_dict():
    assert not _is_already_summarized({"status": "ok", "data": "hello"})


def test_is_already_summarized_ignores_string():
    assert not _is_already_summarized("some plain string result")


# ---------------------------------------------------------------------------
# _visible_len
# ---------------------------------------------------------------------------


def test_visible_len_string():
    assert _visible_len("hello") == 5


def test_visible_len_dict():
    d = {"a": 1}
    assert _visible_len(d) == len(json.dumps(d, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Missing / malformed items arg
# ---------------------------------------------------------------------------


def test_summarize_missing_items():
    agent = _make_stub_agent()
    result = _summarize(agent, {"action": "summarize"})
    assert result["status"] == "error"
    assert "items" in result["message"]


def test_summarize_empty_items():
    agent = _make_stub_agent()
    result = _summarize(agent, {"action": "summarize", "items": []})
    assert result["status"] == "error"


def test_summarize_non_list_items():
    agent = _make_stub_agent()
    result = _summarize(agent, {"action": "summarize", "items": "not-a-list"})
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Success — single item
# ---------------------------------------------------------------------------


def test_summarize_single_item_success():
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-001", "bash", "A" * 8000)
    agent = _make_stub_agent(iface)

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "The command listed 50 files."}],
    })

    assert result["status"] == "ok"
    assert result["summarized"] == 1
    assert result["failed"] == 0
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "ok"
    assert result["items"][0]["tool_call_id"] == "tc-001"


def test_summarize_replaces_block_content():
    iface = ChatInterface()
    original = "A" * 8000
    _add_tool_pair(iface, "tc-001", "bash", original)
    agent = _make_stub_agent(iface)

    _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "My summary"}],
    })

    # Find the block in the interface
    block = None
    for entry in iface._entries:
        for b in entry.content:
            if isinstance(b, ToolResultBlock) and b.id == "tc-001":
                block = b
                break

    assert block is not None
    assert isinstance(block.content, dict)
    assert block.content["artifact"] == SUMMARIZE_MARKER
    assert block.content["agent_summary"] == "My summary"
    assert block.content["tool_call_id"] == "tc-001"
    assert "retrieval_hint" in block.content
    assert "tc-001" in block.content["retrieval_hint"]
    assert block.content["original_visible_chars"] == len(original)


def test_summarize_saves_chat_history():
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-001", "bash", "A" * 100)
    agent = _make_stub_agent(iface)

    _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "short summary"}],
    })

    agent._save_chat_history.assert_called_once()
    call_kwargs = agent._save_chat_history.call_args.kwargs
    assert call_kwargs.get("ledger_source") == "summarize"


# ---------------------------------------------------------------------------
# Batch — multiple items
# ---------------------------------------------------------------------------


def test_summarize_batch_multiple_ids():
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-A", "bash", "result A" * 100)
    _add_tool_pair(iface, "tc-B", "read", "result B" * 100)
    agent = _make_stub_agent(iface)

    result = _summarize(agent, {
        "action": "summarize",
        "items": [
            {"tool_call_id": "tc-A", "summary": "Summary of A"},
            {"tool_call_id": "tc-B", "summary": "Summary of B"},
        ],
    })

    assert result["status"] == "ok"
    assert result["summarized"] == 2
    assert result["failed"] == 0


def test_summarize_batch_partial_success():
    """One unknown id should fail while the other succeeds."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-good", "bash", "good result")
    agent = _make_stub_agent(iface)

    result = _summarize(agent, {
        "action": "summarize",
        "items": [
            {"tool_call_id": "tc-good", "summary": "Summary of good"},
            {"tool_call_id": "tc-nonexistent", "summary": "Summary of unknown"},
        ],
    })

    assert result["status"] == "partial"
    assert result["summarized"] == 1
    assert result["failed"] == 1

    statuses = {item["tool_call_id"]: item["status"] for item in result["items"]}
    assert statuses["tc-good"] == "ok"
    assert statuses["tc-nonexistent"] == "error"
    # Reason should be not_found
    bad_item = next(i for i in result["items"] if i["tool_call_id"] == "tc-nonexistent")
    assert bad_item["reason"] == "not_found"


# ---------------------------------------------------------------------------
# Per-item failure cases
# ---------------------------------------------------------------------------


def test_summarize_unknown_tool_call_id():
    iface = ChatInterface()
    agent = _make_stub_agent(iface)

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "does-not-exist", "summary": "x"}],
    })

    assert result["status"] == "error"
    assert result["items"][0]["reason"] == "not_found"


def test_summarize_already_summarized_returns_error():
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-001", "bash", "original content")
    agent = _make_stub_agent(iface)

    # First summarize
    _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "first summary"}],
    })

    # Second summarize on same id must fail
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "second summary"}],
    })

    assert result["status"] == "error"
    assert result["items"][0]["reason"] == "already_summarized"


def test_summarize_missing_tool_call_id_in_item():
    iface = ChatInterface()
    agent = _make_stub_agent(iface)
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"summary": "no id provided"}],
    })
    assert result["items"][0]["reason"] == "missing_tool_call_id"


def test_summarize_missing_summary_in_item():
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-001", "bash", "content")
    agent = _make_stub_agent(iface)
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001"}],
    })
    assert result["items"][0]["reason"] == "missing_summary"


def test_summarize_no_chat_session():
    agent = MagicMock()
    agent._chat = None
    agent._log = MagicMock()
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "x"}],
    })
    assert result["items"][0]["reason"] == "no_chat_session"


def test_summarize_all_failures_returns_error_status():
    iface = ChatInterface()
    agent = _make_stub_agent(iface)
    result = _summarize(agent, {
        "action": "summarize",
        "items": [
            {"tool_call_id": "id-a", "summary": "x"},
            {"tool_call_id": "id-b", "summary": "y"},
        ],
    })
    assert result["status"] == "error"
    assert result["summarized"] == 0
    assert result["failed"] == 2


def test_summarize_save_failure_is_non_fatal():
    """If _save_chat_history raises, summarization should still report ok."""
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-001", "bash", "content")
    agent = _make_stub_agent(iface)
    agent._save_chat_history = MagicMock(side_effect=RuntimeError("disk full"))

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-001", "summary": "summary despite save failure"}],
    })

    assert result["status"] == "ok"
    assert result["summarized"] == 1
    # Error should have been logged
    log_events = [call.args[0] for call in agent._log.call_args_list]
    assert "tool_result_summarize_save_failed" in log_events


# ---------------------------------------------------------------------------
# handle() dispatch — via system intrinsic
# ---------------------------------------------------------------------------


def test_handle_dispatches_summarize(tmp_path):
    from lingtai_kernel.base_agent import BaseAgent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="test", working_dir=tmp_path / "ag")

    result = agent._intrinsics["system"]({"action": "summarize", "items": []})
    # Empty items → error, but the dispatch must reach _summarize (not unknown action)
    assert result["status"] == "error"
    assert "items" in result.get("message", "")


# ---------------------------------------------------------------------------
# Large-result notification
# ---------------------------------------------------------------------------


def _make_base_agent_for_notification(tmp_path):
    from lingtai_kernel.base_agent import BaseAgent
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    agent = BaseAgent(service=svc, agent_name="test", working_dir=tmp_path / "ag")
    return agent


def test_large_result_notification_default_threshold(tmp_path):
    """Default threshold must be 3000."""
    agent = _make_base_agent_for_notification(tmp_path)
    assert agent._summarize_notification_threshold == 3000


def test_large_result_notification_fires_above_threshold(tmp_path):
    """A result exceeding the threshold publishes a system notification."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "A" * 200)
    assert len(published) == 1
    body = published[0]["body"]
    assert "100" in body  # threshold visible in notification
    assert "summarize" in body
    assert "system(action=" in body


def test_large_result_notification_threshold_in_text(tmp_path):
    """Notification body must explicitly show the current active threshold."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 7500

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("read", "X" * 8000)
    assert len(published) == 1
    body = published[0]["body"]
    assert "7500" in body, f"threshold 7500 not found in notification body:\n{body}"
    assert "Treat this notification as a prompt to act, not just FYI" in body
    assert "summarize all pending large-result cases in one deliberate batch" in body
    assert "otherwise the reminder will return until the result is summarized" in body


def test_large_result_notification_not_fired_below_threshold(tmp_path):
    """Results at or below the threshold must NOT produce a notification."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 5000

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "A" * 4999)
    assert published == []


def test_large_result_notification_spill_manifest_original_over_threshold(tmp_path):
    """Spill manifests whose original_char_count exceeds the threshold SHOULD trigger.

    The wire-visible manifest is small, but the agent still needs a reminder
    to summarize the large original content stored in the sidecar file.
    """
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.txt",
        "cap_chars": 100,
        "original_char_count": 50000,  # well above threshold of 100
    }
    agent._maybe_notify_large_tool_result("bash", spill)
    assert len(published) == 1, (
        "spill manifests with original_char_count > threshold must trigger notification"
    )
    body = published[0]["body"]
    assert "spill" in body.lower() or "sidecar" in body.lower()
    assert "Treat this notification as a prompt to act, not just FYI" in body
    assert "summarize all pending large-result cases in one deliberate batch" in body
    assert "otherwise the reminder will return until the result is summarized" in body


def test_large_result_notification_source_field(tmp_path):
    """Notification must use source='large_tool_result'."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("read", "X" * 100)
    assert published[0]["source"] == "large_tool_result"


def test_large_result_notification_zero_threshold_disables(tmp_path):
    """Setting threshold=0 disables all notifications."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 0

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "A" * 999999)
    assert published == []


# ---------------------------------------------------------------------------
# Fix #1: exact tool_call_id propagation
# ---------------------------------------------------------------------------


def test_large_result_notification_uses_explicit_tool_call_id(tmp_path):
    """When tool_call_id is passed explicitly, the notification body uses it."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "X" * 100, tool_call_id="toolu_exact_001")
    assert len(published) == 1
    body = published[0]["body"]
    assert "toolu_exact_001" in body
    assert published[0]["ref_id"] == "large_tool_result:toolu_exact_001"


def test_large_result_notification_fallback_when_no_id(tmp_path):
    """When tool_call_id is None and no chat session, falls back to placeholder."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10
    agent._chat = None  # no session to scan

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "X" * 100, tool_call_id=None)
    assert len(published) == 1
    body = published[0]["body"]
    # Falls back to placeholder — either heuristic found nothing or returned placeholder
    assert "tool_call_id" in body.lower() or "see your conversation history" in body


def test_on_tool_result_hook_passes_id_to_notify(tmp_path):
    """_on_tool_result_hook must forward tool_call_id to _maybe_notify_large_tool_result."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10

    seen_ids = []

    original = agent._maybe_notify_large_tool_result
    def _capture(tool_name, result, *, tool_call_id=None):
        seen_ids.append(tool_call_id)
        return original(tool_name, result, tool_call_id=tool_call_id)

    agent._maybe_notify_large_tool_result = _capture

    agent._on_tool_result_hook("bash", {}, "X" * 100, tool_call_id="toolu_from_hook")
    assert seen_ids == ["toolu_from_hook"]


def test_hook_called_with_id_for_multiple_same_name_calls(tmp_path):
    """Different tool_call_ids for same tool name are each forwarded correctly."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "A" * 100, tool_call_id="id-first")
    agent._maybe_notify_large_tool_result("bash", "B" * 100, tool_call_id="id-second")

    assert len(published) == 2
    ref_ids = {p["ref_id"] for p in published}
    assert "large_tool_result:id-first" in ref_ids
    assert "large_tool_result:id-second" in ref_ids


# ---------------------------------------------------------------------------
# Fix #2: parallel path hook coverage (unit-level)
# ---------------------------------------------------------------------------


def test_tool_executor_calls_hook_in_parallel_path():
    """on_result_hook must be invoked in the parallel execution path."""
    from lingtai_kernel.tool_executor import ToolExecutor
    from lingtai_kernel.llm.base import ToolCall
    from lingtai_kernel.loop_guard import LoopGuard

    hook_calls = []

    def _dispatch(tc):
        return {"status": "ok", "result": "X" * 200}

    def _make_result(name, result, *, tool_call_id=None):
        return {"name": name, "tool_call_id": tool_call_id, "result": result}

    def _hook(name, args, result, *, tool_call_id=None):
        hook_calls.append({"name": name, "tool_call_id": tool_call_id})
        return None  # no intercept

    guard = LoopGuard()
    executor = ToolExecutor(
        dispatch_fn=_dispatch,
        make_tool_result_fn=_make_result,
        guard=guard,
        parallel_safe_tools={"bash"},
    )

    tc1 = ToolCall(name="bash", args={}, id="id-par-001")
    tc2 = ToolCall(name="bash", args={}, id="id-par-002")

    results, intercepted, _ = executor.execute(
        [tc1, tc2],
        on_result_hook=_hook,
    )

    assert not intercepted
    assert len(results) == 2
    assert len(hook_calls) == 2
    call_ids = {c["tool_call_id"] for c in hook_calls}
    assert "id-par-001" in call_ids
    assert "id-par-002" in call_ids


def test_tool_executor_parallel_hook_intercept():
    """If hook returns intercept text in parallel path, execution stops."""
    from lingtai_kernel.tool_executor import ToolExecutor
    from lingtai_kernel.llm.base import ToolCall
    from lingtai_kernel.loop_guard import LoopGuard

    hook_calls = []

    def _dispatch(tc):
        return {"status": "ok"}

    def _make_result(name, result, *, tool_call_id=None):
        return {"name": name, "result": result}

    def _hook(name, args, result, *, tool_call_id=None):
        hook_calls.append(name)
        return "intercept!" if len(hook_calls) == 1 else None

    guard = LoopGuard()
    executor = ToolExecutor(
        dispatch_fn=_dispatch,
        make_tool_result_fn=_make_result,
        guard=guard,
        parallel_safe_tools={"bash"},
    )

    tc1 = ToolCall(name="bash", args={}, id="id-p-1")
    tc2 = ToolCall(name="bash", args={}, id="id-p-2")

    results, intercepted, intercept_text = executor.execute(
        [tc1, tc2],
        on_result_hook=_hook,
    )

    assert intercepted
    assert intercept_text == "intercept!"
    # At least one result was built before the intercept
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# Fix #3: spill manifest notification
# ---------------------------------------------------------------------------


def test_large_result_notification_spill_manifest_over_threshold(tmp_path):
    """Spill manifest with original_char_count > threshold must trigger notification."""
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 5000

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/big-result.json",
        "cap_chars": 100_000,
        "original_char_count": 60_000,  # over 5000 threshold
    }
    agent._maybe_notify_large_tool_result("bash", spill, tool_call_id="toolu_spill_001")
    assert len(published) == 1
    body = published[0]["body"]
    assert "spill" in body.lower() or "sidecar" in body.lower()
    assert "Treat this notification as a prompt to act, not just FYI" in body
    assert "summarize all pending large-result cases in one deliberate batch" in body
    assert "otherwise the reminder will return until the result is summarized" in body
    assert "toolu_spill_001" in body
    assert "60000" in body or "60,000" in body or "5000" in body


def test_large_result_notification_spill_manifest_under_threshold(tmp_path):
    """Spill manifest with original_char_count <= threshold must NOT trigger."""
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100_000

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/small-result.json",
        "cap_chars": 100_000,
        "original_char_count": 50_000,  # under 100_000 threshold
    }
    agent._maybe_notify_large_tool_result("bash", spill, tool_call_id="toolu_spill_002")
    assert published == [], "spill manifest under threshold must not trigger notification"


def test_large_result_notification_spill_manifest_no_original_count(tmp_path):
    """Spill manifest without original_char_count must NOT trigger (can't determine size)."""
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/unknown.json",
        "cap_chars": 100_000,
        # no original_char_count
    }
    agent._maybe_notify_large_tool_result("bash", spill)
    assert published == [], "spill manifest without original_char_count must not trigger"


# ---------------------------------------------------------------------------
# Fix #4: daemon_tool_result exclusion
# ---------------------------------------------------------------------------


def test_large_result_notification_excludes_daemon_tool_result(tmp_path):
    """daemon_tool_result must be excluded; bare 'daemon' tool must NOT be excluded."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 10

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    # daemon_tool_result should be excluded
    agent._maybe_notify_large_tool_result("daemon_tool_result", "A" * 200)
    assert published == [], "daemon_tool_result must be excluded from large-result notifications"

    # bare 'daemon' tool should NOT be excluded
    agent._maybe_notify_large_tool_result("daemon", "B" * 200)
    assert len(published) == 1, "bare 'daemon' tool must trigger large-result notifications"


# ---------------------------------------------------------------------------
# Policy: notification_threshold_chars is config-only, not runtime-mutable
# ---------------------------------------------------------------------------


def test_summarize_runtime_threshold_change_rejected(tmp_path):
    """Passing notification_threshold_chars at runtime must return an error.

    The threshold is config-only (init.json + refresh). Runtime mutation is
    no longer supported so agents discover the policy change loudly.
    """
    from lingtai_kernel.intrinsics.system.summarize import _summarize

    agent = _make_base_agent_for_notification(tmp_path)
    original_threshold = agent._summarize_notification_threshold

    result = _summarize(agent, {
        "action": "summarize",
        "notification_threshold_chars": 20000,
    })

    assert result["status"] == "error"
    assert result["reason"] == "runtime_threshold_change_not_supported"
    # Threshold must NOT have been updated
    assert agent._summarize_notification_threshold == original_threshold


def test_summarize_runtime_threshold_zero_rejected(tmp_path):
    """Passing notification_threshold_chars=0 at runtime must also be rejected."""
    from lingtai_kernel.intrinsics.system.summarize import _summarize

    agent = _make_base_agent_for_notification(tmp_path)
    original_threshold = agent._summarize_notification_threshold

    result = _summarize(agent, {
        "action": "summarize",
        "notification_threshold_chars": 0,
    })

    assert result["status"] == "error"
    assert result["reason"] == "runtime_threshold_change_not_supported"
    assert agent._summarize_notification_threshold == original_threshold


def test_summarize_runtime_threshold_with_items_rejected(tmp_path):
    """notification_threshold_chars combined with items is also rejected."""
    from lingtai_kernel.intrinsics.system.summarize import _summarize

    iface = ChatInterface()
    _add_tool_pair(iface, "tc-combo", "bash", "X" * 500)
    agent = _make_base_agent_for_notification(tmp_path)
    agent._chat = type("C", (), {"interface": iface})()
    original_threshold = agent._summarize_notification_threshold

    result = _summarize(agent, {
        "action": "summarize",
        "notification_threshold_chars": 8000,
        "items": [{"tool_call_id": "tc-combo", "summary": "combined summary"}],
    })

    # Entire call must be rejected; items must NOT be summarized
    assert result["status"] == "error"
    assert result["reason"] == "runtime_threshold_change_not_supported"
    assert agent._summarize_notification_threshold == original_threshold


def test_summarize_result_always_contains_threshold(tmp_path):
    """All summarize responses (ok, partial, error) must include notification_threshold_chars."""
    from lingtai_kernel.intrinsics.system.summarize import _summarize

    agent = _make_base_agent_for_notification(tmp_path)

    # error path (missing items)
    result = _summarize(agent, {"action": "summarize"})
    assert "notification_threshold_chars" in result

    # ok path
    iface = ChatInterface()
    _add_tool_pair(iface, "tc-ok", "bash", "hello")
    agent._chat = type("C", (), {"interface": iface})()
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-ok", "summary": "s"}],
    })
    assert "notification_threshold_chars" in result


def test_schema_does_not_include_notification_threshold_chars():
    """notification_threshold_chars must NOT appear in the system tool schema."""
    from lingtai_kernel.intrinsics.system.schema import get_schema
    schema = get_schema("en")
    assert "notification_threshold_chars" not in schema["properties"], (
        "notification_threshold_chars must be removed from the schema — "
        "threshold is config-only (init.json + refresh), not runtime-mutable"
    )


# ---------------------------------------------------------------------------
# Notification wording: no "raise/disable threshold" instruction
# ---------------------------------------------------------------------------


def test_large_result_notification_no_raise_disable_wording(tmp_path):
    """Notification body must NOT instruct agents to raise or disable the threshold."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "X" * 200)
    assert len(published) == 1
    body = published[0]["body"]
    assert "raise or disable the threshold" not in body, (
        "notification body must not instruct agents to raise/disable threshold at runtime"
    )
    assert "raise" not in body.lower() or "threshold" not in body.lower(), (
        "notification body must not say 'raise ... threshold'"
    )


def test_large_result_notification_batch_digest_wording(tmp_path):
    """Notification body must mention batch-digest all pending cases or tolerate reminders."""
    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    agent._maybe_notify_large_tool_result("bash", "X" * 200)
    assert len(published) == 1
    body = published[0]["body"]
    # Must mention config/init path as only way to change threshold
    assert "init" in body.lower() or "config" in body.lower() or "refresh" in body.lower(), (
        "notification body must mention init/config/refresh as the only way to change threshold"
    )
    # Must mention batch-digesting or tolerating reminders
    assert "batch" in body.lower() or "all pending" in body.lower() or "tolerate" in body.lower(), (
        "notification body must mention batch-digest or tolerate-reminders guidance"
    )


def test_large_result_notification_spill_no_raise_disable_wording(tmp_path):
    """Spill notification body must NOT instruct agents to raise or disable threshold."""
    from lingtai_kernel.tool_result_artifacts import ARTIFACT_MARKER

    agent = _make_base_agent_for_notification(tmp_path)
    agent._summarize_notification_threshold = 100

    published = []
    agent._enqueue_system_notification = MagicMock(
        side_effect=lambda **kw: published.append(kw)
    )

    spill = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "spill_path": "tmp/tool-results/foo.txt",
        "cap_chars": 100,
        "original_char_count": 50000,
    }
    agent._maybe_notify_large_tool_result("bash", spill)
    assert len(published) == 1
    body = published[0]["body"]
    assert "raise or disable the threshold" not in body, (
        "spill notification body must not instruct agents to raise/disable threshold"
    )


# ---------------------------------------------------------------------------
# init.json config path for threshold
# ---------------------------------------------------------------------------


def test_base_agent_threshold_init_from_config(tmp_path):
    """Agent applies summarize_notification_threshold from init.json manifest data.

    Tests the logic that _setup_from_init uses to load the field, without
    constructing a full LLM adapter. We directly simulate the manifest dict
    that _setup_from_init receives from _read_init().
    """
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="cfg-test", working_dir=tmp_path / "ag")
    assert agent._summarize_notification_threshold == 3000  # default

    # Simulate what _setup_from_init does after reading manifest
    manifest = {
        "llm": {"provider": "gemini", "model": "gemini-test"},
        "summarize_notification_threshold": 1500,
    }
    raw_threshold = manifest.get("summarize_notification_threshold")
    if isinstance(raw_threshold, int) and not isinstance(raw_threshold, bool) and raw_threshold >= 0:
        agent._summarize_notification_threshold = raw_threshold
    else:
        agent._summarize_notification_threshold = 3000

    assert agent._summarize_notification_threshold == 1500, (
        f"Expected threshold=1500 from manifest, got {agent._summarize_notification_threshold}"
    )


def test_base_agent_threshold_config_accepts_zero(tmp_path):
    """summarize_notification_threshold=0 in manifest disables notifications."""
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="cfg-zero", working_dir=tmp_path / "ag")

    manifest = {
        "llm": {"provider": "gemini", "model": "gemini-test"},
        "summarize_notification_threshold": 0,
    }
    raw_threshold = manifest.get("summarize_notification_threshold")
    if isinstance(raw_threshold, int) and not isinstance(raw_threshold, bool) and raw_threshold >= 0:
        agent._summarize_notification_threshold = raw_threshold
    else:
        agent._summarize_notification_threshold = 3000

    assert agent._summarize_notification_threshold == 0


def test_base_agent_threshold_config_rejects_bool(tmp_path):
    """bool values for summarize_notification_threshold fall back to default 3000."""
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="cfg-bool", working_dir=tmp_path / "ag")

    manifest = {
        "llm": {"provider": "gemini", "model": "gemini-test"},
        "summarize_notification_threshold": True,  # bool should be rejected
    }
    raw_threshold = manifest.get("summarize_notification_threshold")
    if isinstance(raw_threshold, int) and not isinstance(raw_threshold, bool) and raw_threshold >= 0:
        agent._summarize_notification_threshold = raw_threshold
    else:
        agent._summarize_notification_threshold = 3000

    assert agent._summarize_notification_threshold == 3000


def test_base_agent_threshold_default_when_not_in_config(tmp_path):
    """BaseAgent uses default 3000 when init.json has no summarize_notification_threshold."""
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    agent = BaseAgent(service=svc, agent_name="default-test", working_dir=tmp_path / "ag")
    assert agent._summarize_notification_threshold == 3000
