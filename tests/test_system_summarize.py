"""Tests for system(action='summarize') — agent-authored context summarization.

Covers:
- schema registration: summarize in action enum
- basic success: single item
- batch: multiple items in one call
- per-item failure: unknown id, already summarized, missing fields
- idempotency: re-summarizing a summarized block returns error
- history persistence: _save_chat_history called after mutation
- large-result notification: per-result threshold (default 3000) shown in text
- large-result notification: total-length gate — fires only when the combined
  length of pending large-result cases exceeds 50000 chars
- large-result notification: excludes daemon-named tools
- large-result notification: skips spill manifests
"""
from __future__ import annotations

import json
import threading
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


def test_schema_has_rebuild_only_properties():
    from lingtai_kernel.intrinsics.system.schema import get_schema
    schema = get_schema("en")
    assert schema["properties"]["rebuild_only"]["type"] == "boolean"
    assert schema["properties"]["dry_run"]["type"] == "boolean"




def test_rebuild_only_with_no_items_requests_chat_rebuild():
    agent = _make_stub_agent()
    agent._chat.request_history_rebuild = MagicMock(return_value=True)

    result = _summarize(agent, {"action": "summarize", "rebuild_only": True})

    assert result["status"] == "ok"
    assert result["mode"] == "rebuild_only"
    assert result["summarized"] == 0
    assert result["items"] == []
    assert result["rebuild_requested"] is True
    agent._chat.request_history_rebuild.assert_called_once_with(
        reason="summarize_rebuild_only"
    )
    agent._save_chat_history.assert_not_called()


def test_dry_run_alias_requests_rebuild_without_items():
    agent = _make_stub_agent()
    agent._chat.request_history_rebuild = MagicMock(return_value=True)

    result = _summarize(agent, {"action": "summarize", "dry_run": True})

    assert result["status"] == "ok"
    assert result["mode"] == "rebuild_only"
    assert result["rebuild_requested"] is True


def test_rebuild_only_rejects_items():
    agent = _make_stub_agent()
    result = _summarize(
        agent,
        {
            "action": "summarize",
            "rebuild_only": True,
            "items": [{"tool_call_id": "x", "summary": "y"}],
        },
    )

    assert result["status"] == "error"
    assert result["reason"] == "rebuild_only_with_items"

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


def test_visible_len_ignores_meta_notifications():
    content = {
        "payload": "ok",
        "_meta": {
            "notifications": {"system": {"body": "N" * 10_000}},
            "notification_guidance": "G" * 10_000,
            "guidance": {
                "sections": [
                    {
                        "id": "meta_readme",
                        "title": "_meta envelope readme",
                        "body": "notifications: do not summarize",
                    }
                ]
            },
        },
    }
    assert _visible_len(content) == len(json.dumps({"payload": "ok"}, ensure_ascii=False))


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
    # A successful summarize carries a short, generic reassurance that the
    # summary bookkeeping is recorded now and provider reconstruction is delayed.
    assert "reconstruction" in result
    assert "runtime history" in result["reconstruction"]
    assert "active provider context may still contain the old result" in result["reconstruction"]
    assert "delayed" in result["reconstruction"]
    assert "keep working" in result["reconstruction"]
    assert "summarized history" in result["reconstruction"]
    assert "See meta_guidance and substrate for details" in result["reconstruction"]
    # Not a provider-specific policy object — a plain status string.
    assert isinstance(result["reconstruction"], str)


def test_summarize_notifies_chat_after_successful_history_mutation():
    iface = ChatInterface()
    _add_tool_pair(iface, "call_1", "bash", {"ok": True})
    agent = _make_stub_agent(iface)
    called = []
    agent._chat.on_history_summarized = lambda ids: called.append(ids)

    result = _summarize(
        agent,
        {"items": [{"tool_call_id": "call_1", "summary": "kept facts"}]},
    )

    assert result["status"] == "ok"
    assert called == [["call_1"]]


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


def test_summarize_original_visible_chars_ignores_meta_notifications():
    iface = ChatInterface()
    formal_payload = {"payload": "short"}
    original = {
        **formal_payload,
        "_meta": {
            "notifications": {"system": {"body": "N" * 10_000}},
            "notification_guidance": "G" * 10_000,
        },
    }
    _add_tool_pair(iface, "tc-meta", "bash", original)
    agent = _make_stub_agent(iface)

    _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "tc-meta", "summary": "formal summary"}],
    })

    block = next(
        b
        for entry in iface._entries
        for b in entry.content
        if isinstance(b, ToolResultBlock) and b.id == "tc-meta"
    )
    assert block.content["original_visible_chars"] == len(
        json.dumps(formal_payload, ensure_ascii=False)
    )


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
    # Nothing was summarized, so no reconstruction reassurance is emitted.
    assert "reconstruction" not in result


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
# Large-result hint threshold + tool-result hook wiring
#
# Large tool results no longer raise a `large_tool_result` system notification
# (that producer was removed; see tests/test_large_result_no_notification.py).
# The threshold survives as the ToolExecutor overflow-hint / char-ranking knob,
# and the on_result_hook seam the executor invokes is still exercised here.
# ---------------------------------------------------------------------------


def _make_base_agent_for_notification(tmp_path):
    from lingtai_kernel.base_agent import BaseAgent
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    agent = BaseAgent(service=svc, agent_name="test", working_dir=tmp_path / "ag")
    return agent


def test_large_result_threshold_default(tmp_path):
    """Default large-result hint threshold must be 3000."""
    agent = _make_base_agent_for_notification(tmp_path)
    assert agent._summarize_notification_threshold == 3000


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
# Runtime threshold mutation is rejected (config-only via init.json + refresh)
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
        "notification_threshold_chars": 50000,
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

    # Simulate what _setup_from_init does after reading manifest.  An explicit
    # manifest value must override the default (config override preserved).
    manifest = {
        "llm": {"provider": "gemini", "model": "gemini-test"},
        "summarize_notification_threshold": 1500,
    }
    raw_threshold = manifest.get("summarize_notification_threshold")
    if isinstance(raw_threshold, int) and not isinstance(raw_threshold, bool) and raw_threshold >= 0:
        agent._summarize_notification_threshold = raw_threshold
    else:
        agent._summarize_notification_threshold = 5000

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
        agent._summarize_notification_threshold = 5000

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


# ---------------------------------------------------------------------------
# Requirement #2: successful summarize clears the matching large-result reminder
# ---------------------------------------------------------------------------


def _make_stub_agent_with_workdir(tmp_path, iface):
    """Stub agent with a real working dir, lock, and chat session for reminder clears."""
    workdir = tmp_path / "ag"
    workdir.mkdir(parents=True, exist_ok=True)

    class _StubChat:
        interface = iface

    agent = MagicMock()
    agent._working_dir = workdir
    agent._system_notification_lock = threading.Lock()
    agent._chat = _StubChat()
    agent._chat.interface = iface
    agent._log = MagicMock()
    agent._save_chat_history = MagicMock()
    return agent


def _publish_large_result_event(workdir, tool_call_id, *, extra=None):
    from lingtai_kernel.notifications import publish

    events = []
    if extra:
        events.extend(extra)
    events.append({
        "event_id": f"evt_{tool_call_id}",
        "source": "large_tool_result",
        "ref_id": f"large_tool_result:{tool_call_id}",
        "body": "summarize me",
    })
    publish(
        workdir,
        "system",
        {
            "header": f"{len(events)} system notifications",
            "data": {"events": events},
        },
    )


def test_summarize_clears_matching_large_result_reminder(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    iface = ChatInterface()
    _add_tool_pair(iface, "toolu_big", "bash", "A" * 9000)
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    _publish_large_result_event(agent._working_dir, "toolu_big")

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "toolu_big", "summary": "digested"}],
    })

    assert result["status"] == "ok"
    assert result["cleared_reminders"] == ["large_tool_result:toolu_big"]
    # The reminder event is gone (file removed since it was the only event).
    assert "system" not in collect_notifications(agent._working_dir)


def test_summarize_clears_only_matching_reminder_preserves_others(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    iface = ChatInterface()
    _add_tool_pair(iface, "toolu_big", "bash", "A" * 9000)
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    _publish_large_result_event(
        agent._working_dir,
        "toolu_big",
        extra=[
            {"event_id": "evt_other", "source": "daemon", "ref_id": "d", "body": "D"},
            {
                "event_id": "evt_keep",
                "source": "large_tool_result",
                "ref_id": "large_tool_result:toolu_other",
                "body": "still pending",
            },
        ],
    )

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "toolu_big", "summary": "digested"}],
    })

    assert result["status"] == "ok"
    assert result["cleared_reminders"] == ["large_tool_result:toolu_big"]
    events = collect_notifications(agent._working_dir)["system"]["data"]["events"]
    ref_ids = {ev["ref_id"] for ev in events}
    assert "large_tool_result:toolu_big" not in ref_ids
    # Other daemon event and the OTHER pending large-result reminder are kept.
    assert "d" in ref_ids
    assert "large_tool_result:toolu_other" in ref_ids


def test_summarize_batch_clears_each_matching_reminder(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    iface = ChatInterface()
    _add_tool_pair(iface, "tc-A", "bash", "A" * 9000)
    _add_tool_pair(iface, "tc-B", "read", "B" * 9000)
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    _publish_large_result_event(
        agent._working_dir,
        "tc-A",
        extra=[{
            "event_id": "evt_tc-B",
            "source": "large_tool_result",
            "ref_id": "large_tool_result:tc-B",
            "body": "summarize me too",
        }],
    )

    result = _summarize(agent, {
        "action": "summarize",
        "items": [
            {"tool_call_id": "tc-A", "summary": "A digest"},
            {"tool_call_id": "tc-B", "summary": "B digest"},
        ],
    })

    assert result["status"] == "ok"
    assert set(result["cleared_reminders"]) == {
        "large_tool_result:tc-A",
        "large_tool_result:tc-B",
    }
    assert "system" not in collect_notifications(agent._working_dir)


def test_summarize_failure_does_not_clear_reminder(tmp_path):
    """A failed (not_found) summarize must NOT clear any reminder."""
    from lingtai_kernel.notifications import collect_notifications

    iface = ChatInterface()
    # No tool pair for toolu_big — summarize will fail with not_found.
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    _publish_large_result_event(agent._working_dir, "toolu_big")

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "toolu_big", "summary": "digest"}],
    })

    assert result["status"] == "error"
    assert result["cleared_reminders"] == []
    events = collect_notifications(agent._working_dir)["system"]["data"]["events"]
    assert any(ev["ref_id"] == "large_tool_result:toolu_big" for ev in events)


def test_summarize_clear_is_noop_when_no_reminder_present(tmp_path):
    """Summarize with no pending reminder still succeeds; cleared list is empty."""
    iface = ChatInterface()
    _add_tool_pair(iface, "toolu_big", "bash", "A" * 9000)
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    # No system.json published.

    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "toolu_big", "summary": "digest"}],
    })

    assert result["status"] == "ok"
    assert result["cleared_reminders"] == []


def test_summarize_then_dismiss_is_unnecessary_end_to_end(tmp_path):
    """End-to-end: notification dismiss now succeeds as an escape hatch (issue #425),
    and system summarize also clears the reminder. Dismissal is an alternative
    to summarize, not blocked. Summarize stays on the system tool."""
    from lingtai_kernel.intrinsics import notification as notif_intrinsic
    from lingtai_kernel.notifications import collect_notifications, notification_fingerprint

    iface = ChatInterface()
    _add_tool_pair(iface, "toolu_big", "bash", "A" * 9000)
    agent = _make_stub_agent_with_workdir(tmp_path, iface)
    _publish_large_result_event(agent._working_dir, "toolu_big")
    agent._notification_fp = notification_fingerprint(agent._working_dir)

    # Dismiss now succeeds — large_tool_result reminders are dismissable as escape hatch.
    dismissed = notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": "system", "force": True}
    )
    assert dismissed["status"] == "ok"
    assert "acked_large_result_refs" in dismissed
    assert "system" not in collect_notifications(agent._working_dir)

    # Re-publish the reminder and show summarize also clears it.
    _publish_large_result_event(agent._working_dir, "toolu_big")
    agent._notification_fp = notification_fingerprint(agent._working_dir)
    result = _summarize(agent, {
        "action": "summarize",
        "items": [{"tool_call_id": "toolu_big", "summary": "digest"}],
    })
    assert result["status"] == "ok"
    assert result["cleared_reminders"] == ["large_tool_result:toolu_big"]
    assert "system" not in collect_notifications(agent._working_dir)
