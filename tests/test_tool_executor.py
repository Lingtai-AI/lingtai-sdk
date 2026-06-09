"""Tests for ToolExecutor — sequential and parallel tool execution."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_executor import ToolExecutor
from lingtai_kernel.types import UnknownToolError


def make_executor(dispatch_fn=None, parallel_safe=None, known_tools=None):
    if dispatch_fn is None:
        dispatch_fn = lambda tc: {"status": "ok", "result": f"ran {tc.name}"}
    make_result = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    guard = LoopGuard(max_total_calls=50)
    return ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=make_result,
        guard=guard,
        known_tools=known_tools,
        parallel_safe_tools=parallel_safe or set(),
    )


def test_execute_single_tool():
    executor = make_executor()
    calls = [ToolCall(name="read", args={"path": "/tmp"}, id="tc1")]
    results, intercepted, text = executor.execute(calls)
    assert len(results) == 1
    assert not intercepted


def test_execute_sequential_multiple():
    order = []
    def dispatch(tc):
        order.append(tc.name)
        return {"status": "ok"}
    executor = make_executor(dispatch_fn=dispatch)
    calls = [
        ToolCall(name="a", args={}, id="1"),
        ToolCall(name="b", args={}, id="2"),
    ]
    results, intercepted, text = executor.execute(calls)
    assert len(results) == 2
    assert order == ["a", "b"]


def test_execute_parallel():
    def dispatch(tc):
        time.sleep(0.05)
        return {"status": "ok", "tool": tc.name}
    executor = make_executor(
        dispatch_fn=dispatch,
        parallel_safe={"a", "b"},
    )
    calls = [
        ToolCall(name="a", args={}, id="1"),
        ToolCall(name="b", args={}, id="2"),
    ]
    t0 = time.monotonic()
    results, intercepted, text = executor.execute(calls)
    elapsed = time.monotonic() - t0
    assert len(results) == 2
    assert elapsed < 0.15


def test_intercept_hook():
    executor = make_executor()
    hook = MagicMock(return_value="intercepted!")
    calls = [ToolCall(name="read", args={}, id="1")]
    results, intercepted, text = executor.execute(calls, on_result_hook=hook)
    assert intercepted
    assert text == "intercepted!"


def test_error_collected():
    def dispatch(tc):
        raise ValueError("something broke")
    executor = make_executor(dispatch_fn=dispatch)
    calls = [ToolCall(name="bad", args={}, id="1")]
    errors = []
    results, intercepted, text = executor.execute(calls, collected_errors=errors)
    assert len(results) == 1
    assert "bad" in errors[0]
    assert "something broke" in errors[0]


def test_cancel_event_stops_sequential():
    cancel = threading.Event()
    cancel.set()
    executor = make_executor()
    calls = [ToolCall(name="a", args={}, id="1")]
    results, intercepted, text = executor.execute(calls, cancel_event=cancel)
    assert results == []


def test_unknown_tool_with_known_tools():
    executor = make_executor(known_tools={"read", "write"})
    calls = [ToolCall(name="bogus", args={}, id="1")]
    errors = []
    results, intercepted, text = executor.execute(calls, collected_errors=errors)
    assert len(results) == 1
    assert any("bogus" in e for e in errors)


def test_guard_property():
    executor = make_executor()
    old_guard = executor.guard
    new_guard = LoopGuard(max_total_calls=10)
    executor.guard = new_guard
    assert executor.guard is new_guard


def test_reasoning_stripped_from_args():
    dispatched_args = []
    def dispatch(tc):
        dispatched_args.append(tc.args)
        return {"status": "ok"}
    executor = make_executor(dispatch_fn=dispatch)
    calls = [ToolCall(name="read", args={"path": "/tmp", "reasoning": "because"}, id="1")]
    executor.execute(calls)
    assert "reasoning" not in dispatched_args[0]
    assert dispatched_args[0].get("_reasoning") == "because"


def test_tool_executor_uses_meta_fn_for_stamping():
    """ToolExecutor calls meta_fn once per tool call and merges the returned
    dict onto the result alongside _elapsed_ms."""
    meta_calls = {"n": 0}

    def meta_fn():
        meta_calls["n"] += 1
        return {"current_time": "FAKE-TS", "future_field": meta_calls["n"]}

    def dispatch(tc):
        return {"status": "ok", "echo": tc.args}

    def make_result(name, result, **kw):
        return {"name": name, "result": result, **kw}

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"noop"},
        parallel_safe_tools=set(),
        logger_fn=None,
        meta_fn=meta_fn,
    )
    results, intercepted, _ = exe.execute([ToolCall(id="c1", name="noop", args={})])
    assert not intercepted
    assert meta_calls["n"] == 1
    payload = results[0]["result"]
    assert payload["current_time"] == "FAKE-TS"
    assert payload["future_field"] == 1
    assert "_elapsed_ms" in payload


def test_tool_executor_meta_fn_covers_parallel_path():
    """meta_fn is called per-tool in the parallel execution path too,
    and each stamped result carries its meta fields and _elapsed_ms."""
    meta_calls = {"n": 0}

    def meta_fn():
        meta_calls["n"] += 1
        return {"current_time": "FAKE-TS"}

    def dispatch(tc):
        return {"status": "ok"}

    def make_result(name, result, **kw):
        return {"name": name, "result": result, **kw}

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"noop"},
        parallel_safe_tools={"noop"},  # force parallel path
        logger_fn=None,
        meta_fn=meta_fn,
    )
    results, intercepted, _ = exe.execute([
        ToolCall(id="c1", name="noop", args={}),
        ToolCall(id="c2", name="noop", args={}),
    ])
    assert not intercepted
    assert meta_calls["n"] == 2
    for r in results:
        payload = r["result"]
        assert payload["current_time"] == "FAKE-TS"
        assert "_elapsed_ms" in payload


def test_secondary_executes_before_primary_and_is_stripped():
    seen = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        if tc.name == "telegram":
            return {"status": "ok", "messages": [{"id": "m1", "text": "hi"}]}
        assert "secondary" not in tc.args
        return {"status": "ok", "echo": tc.args}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "path": "/tmp",
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123, "limit": 5},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert [name for name, _ in seen] == ["telegram", "read"]
    assert "secondary" not in seen[1][1]
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    assert payload["_secondary"]["status"] == "success"
    assert payload["_secondary"]["tool"] == "telegram"
    assert payload["_secondary"]["action"] == "read"


def test_secondary_send_action_is_rejected():
    """The secondary channel is read-only — send must be rejected at runtime
    without blocking the primary."""
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "send", "chat_id": 123, "text": "starting"},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    # secondary send must never reach the communication handler
    assert seen == ["read"]
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    assert payload["_secondary"]["status"] == "error"
    assert payload["_secondary"]["tool"] == "telegram"
    assert payload["_secondary"]["action"] == "send"
    assert "action" in payload["_secondary"]["message"]


def test_secondary_reply_action_is_rejected():
    """The secondary channel is read-only — reply must be rejected at runtime
    without blocking the primary."""
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "email"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "email",
                "args": {"action": "reply", "email_id": ["e1"], "message": "ack"},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    payload = results[0]["result"]
    assert payload["_secondary"]["status"] == "error"
    assert payload["_secondary"]["tool"] == "email"
    assert payload["_secondary"]["action"] == "reply"
    assert "action" in payload["_secondary"]["message"]


def test_secondary_unknown_tool_does_not_block_primary():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={"secondary": {"tool": "bash", "args": {"action": "run"}}},
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    assert payload["_secondary"]["status"] == "error"
    assert payload["_secondary"]["tool"] == "bash"
    assert "not allowed" in payload["_secondary"]["message"]


def test_secondary_disallowed_action_does_not_block_primary():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "delete", "message_id": "abc"},
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    payload = results[0]["result"]
    assert payload["_secondary"]["status"] == "error"
    assert payload["_secondary"]["tool"] == "telegram"
    assert payload["_secondary"]["action"] == "delete"
    assert "action" in payload["_secondary"]["message"]


def test_secondary_recursive_call_rejected():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {
                    "action": "read",
                    "chat_id": 123,
                    "secondary": {"tool": "telegram", "args": {"action": "read"}},
                },
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    payload = results[0]["result"]
    assert payload["_secondary"]["status"] == "error"
    assert "recursive" in payload["_secondary"]["message"]


def test_secondary_exception_does_not_block_primary():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        if tc.name == "telegram":
            raise RuntimeError("network down")
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123},
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["telegram", "read"]
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    assert payload["_secondary"]["status"] == "error"
    assert "network down" in payload["_secondary"]["message"]


def test_secondary_parallel_path():
    seen = []
    lock = threading.Lock()

    def dispatch(tc):
        with lock:
            seen.append((tc.name, dict(tc.args)))
        return {"status": "ok", "tool": tc.name}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"read", "telegram"},
        parallel_safe={"read"},
    )
    calls = [
        ToolCall(
            name="read",
            args={
                "path": "/tmp/a",
                "secondary": {
                    "tool": "telegram",
                    "args": {"action": "read", "chat_id": 123},
                },
            },
            id="tc1",
        ),
        ToolCall(name="read", args={"path": "/tmp/b"}, id="tc2"),
    ]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert len(results) == 2
    assert results[0]["result"]["_secondary"]["status"] == "success"
    assert "_secondary" not in results[1]["result"]
    assert all("secondary" not in args for _, args in seen if args.get("path"))



def test_secondary_rejected_when_primary_is_communication_tool():
    seen = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        return {"status": "ok", "tool": tc.name}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"telegram"})
    calls = [ToolCall(
        name="telegram",
        args={
            "action": "send",
            "chat_id": 123,
            "text": "primary message",
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == [("telegram", {"action": "send", "chat_id": 123, "text": "primary message"})]
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    assert payload["_secondary"] == {
        "status": "error",
        "message": "primary tool 'telegram' may not carry a secondary",
    }


def test_secondary_reasoning_fields_are_stripped_from_secondary_args():
    seen = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {
                    "action": "read",
                    "chat_id": 123,
                    "reasoning": "nested reason should not reach handler",
                    "commentary": "nested commentary should not reach handler",
                    "_sync": True,
                },
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    telegram_args = seen[0][1]
    assert seen[0][0] == "telegram"
    assert "reasoning" not in telegram_args
    assert "commentary" not in telegram_args
    assert "_sync" not in telegram_args
    assert results[0]["result"]["_secondary"]["status"] == "success"


def test_secondary_missing_action_is_rejected_without_blocking_primary():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={"secondary": {"tool": "telegram", "args": {"chat_id": 123, "text": "starting"}}},
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    secondary = results[0]["result"]["_secondary"]
    assert secondary["status"] == "error"
    assert secondary["tool"] == "telegram"
    assert "action" in secondary["message"]


def test_secondary_deep_recursive_key_rejected():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {
                    "action": "read",
                    "chat_id": 123,
                    "reply_markup": {"secondary": {"tool": "telegram", "args": {"action": "read"}}},
                },
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    assert results[0]["result"]["_secondary"]["status"] == "error"
    assert "recursive" in results[0]["result"]["_secondary"]["message"]


def test_secondary_still_reports_when_primary_unknown_sequential():
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"telegram"})
    calls = [ToolCall(
        name="bogus",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123},
            }
        },
        id="tc1",
    )]

    errors = []
    results, intercepted, _ = executor.execute(calls, collected_errors=errors)

    assert not intercepted
    assert seen == ["telegram"]
    payload = results[0]["result"]
    assert payload["status"] == "error"
    assert payload["_secondary"]["status"] == "success"
    assert any("bogus" in err for err in errors)


def test_secondary_still_reports_when_primary_unknown_parallel():
    seen = []
    lock = threading.Lock()

    def dispatch(tc):
        with lock:
            seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"read", "telegram"},
        parallel_safe={"read", "bogus"},
    )
    calls = [
        ToolCall(
            name="bogus",
            args={
                "secondary": {
                    "tool": "telegram",
                    "args": {"action": "read", "chat_id": 123},
                }
            },
            id="tc1",
        ),
        ToolCall(name="read", args={"path": "/tmp/b"}, id="tc2"),
    ]

    errors = []
    results, intercepted, _ = executor.execute(calls, collected_errors=errors)

    assert not intercepted
    assert "telegram" in seen
    assert "read" in seen
    assert results[0]["result"]["status"] == "error"
    assert results[0]["result"]["_secondary"]["status"] == "success"
    assert any("bogus" in err for err in errors)


def test_secondary_wraps_non_dict_primary_result_under_reserved_key():
    def dispatch(tc):
        if tc.name == "telegram":
            return {"status": "ok", "messages": [{"id": "m1"}]}
        return "plain primary result"

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123},
            }
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    payload = results[0]["result"]
    assert payload["result"] == "plain primary result"
    assert payload["_secondary"]["status"] == "success"
    assert payload["_secondary"]["tool"] == "telegram"
    assert payload["_secondary"]["action"] == "read"


def test_secondary_survives_canonical_tool_result_block_wire_shape():
    def dispatch(tc):
        if tc.name == "telegram":
            return {"status": "ok", "messages": [{"id": "m1"}]}
        return {"status": "ok"}

    def make_result(name, result, **kw):
        return ToolResultBlock(
            id=kw.get("tool_call_id") or name,
            name=name,
            content=result,
        )

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"read", "telegram"},
        parallel_safe_tools=set(),
    )
    results, intercepted, _ = exe.execute([ToolCall(
        id="tc1",
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123},
            }
        },
    )])

    assert not intercepted
    block = results[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content["_secondary"]["status"] == "success"
    assert block.content["_secondary"]["action"] == "read"
    assert block.to_dict()["content"]["_secondary"]["status"] == "success"


def test_secondary_read_action_forwards_result_under_secondary():
    """A valid secondary read should run before the primary and the read
    payload should be forwarded under _secondary.result so the primary turn
    can act on the content without an extra round-trip."""
    seen = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        if tc.name == "telegram":
            return {
                "status": "ok",
                "messages": [
                    {"id": "m1", "text": "hi"},
                    {"id": "m2", "text": "what's up"},
                ],
            }
        return {"status": "ok", "echo": tc.args}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "path": "/tmp",
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 123, "limit": 5},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    # secondary runs first, then primary
    assert [name for name, _ in seen] == ["telegram", "read"]
    # secondary args reach the comm handler intact (limit included)
    assert seen[0][1] == {"action": "read", "chat_id": 123, "limit": 5}
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    sec = payload["_secondary"]
    assert sec["status"] == "success"
    assert sec["tool"] == "telegram"
    assert sec["action"] == "read"
    # the read body is forwarded under _secondary.result
    assert sec["result"]["status"] == "ok"
    assert sec["result"]["messages"][0]["id"] == "m1"


def test_secondary_read_for_email_with_email_id_list():
    """email.read takes email_id as a list — secondary policy must allow it
    and the read payload must be forwarded under _secondary.result."""
    seen = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        if tc.name == "email":
            return {
                "status": "ok",
                "emails": [{"id": "e1", "subject": "hello", "body": "full body"}],
            }
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "email"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "email",
                "args": {"action": "read", "email_id": ["e1"]},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert [name for name, _ in seen] == ["email", "read"]
    sec = results[0]["result"]["_secondary"]
    assert sec["status"] == "success"
    assert sec["action"] == "read"
    assert sec["result"]["emails"][0]["id"] == "e1"


def test_secondary_read_result_is_truncated_under_payload_cap():
    """Read results that exceed SECONDARY_READ_RESULT_MAX_BYTES must be
    truncated before being attached under _secondary.result, so a chatty
    secondary read can never balloon the primary tool message."""
    from lingtai_kernel.secondary_tools import SECONDARY_READ_RESULT_MAX_BYTES

    big_text = "x" * (SECONDARY_READ_RESULT_MAX_BYTES * 3)

    def dispatch(tc):
        if tc.name == "telegram":
            return {"status": "ok", "body": big_text}
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "read", "chat_id": 1},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    sec = results[0]["result"]["_secondary"]
    assert sec["status"] == "success"
    forwarded_body = sec["result"]["body"]
    # The truncator slices oversize string values to about half of max_bytes
    # and appends a marker, so the forwarded body must shrink and be marked.
    assert len(forwarded_body) < len(big_text)
    assert "truncated" in forwarded_body


def test_secondary_send_never_reaches_handler_so_nothing_leaks():
    """send is forbidden on the read-only secondary channel, so the comm
    handler never runs and its internals (message_ids etc.) can never leak
    under _secondary."""
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        if tc.name == "telegram":
            return {"status": "sent", "message_id": "secret-should-not-leak"}
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {"action": "send", "chat_id": 1, "text": "hi"},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]  # telegram send handler never invoked
    sec = results[0]["result"]["_secondary"]
    assert sec["status"] == "error"
    assert sec["action"] == "send"
    assert "result" not in sec
    assert "secret-should-not-leak" not in str(sec)


def test_secondary_read_disallowed_for_imap_excluded_target():
    """``imap`` is not a secondary-allowed target even with read in the action
    whitelist — the tool-level allowlist still applies."""
    def dispatch(tc):
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "imap"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "imap",
                "args": {"action": "read", "email_id": ["e1"]},
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    sec = results[0]["result"]["_secondary"]
    assert sec["status"] == "error"
    assert sec["tool"] == "imap"
    assert "not allowed" in sec["message"]


def test_secondary_read_recursive_payload_still_rejected():
    """Recursive secondary fields nested inside a read-action payload must
    still be rejected — the read carve-out doesn't relax the no-recursion rule."""
    seen = []

    def dispatch(tc):
        seen.append(tc.name)
        return {"status": "ok"}

    executor = make_executor(dispatch_fn=dispatch, known_tools={"read", "telegram"})
    calls = [ToolCall(
        name="read",
        args={
            "secondary": {
                "tool": "telegram",
                "args": {
                    "action": "read",
                    "chat_id": 1,
                    "secondary": {"tool": "telegram", "args": {"action": "read", "chat_id": 2}},
                },
            },
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == ["read"]
    sec = results[0]["result"]["_secondary"]
    assert sec["status"] == "error"
    assert "recursive" in sec["message"]
