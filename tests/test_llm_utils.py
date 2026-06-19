"""Tests for lingtai.llm_utils."""

from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from lingtai_kernel.llm_utils import (
    WorkerStillRunningError,
    track_llm_usage,
    execute_tools_batch,
    send_with_timeout,
    _SubmitFn,
    _wait_for_worker_settle,
)


class FakeLLMResponse:
    """Minimal mock for LLMResponse."""
    class Usage:
        input_tokens = 100
        output_tokens = 50
        thinking_tokens = 10
        cached_tokens = 20
    usage = Usage()
    thoughts = ["I think therefore I am"]


def test_track_llm_usage_accumulates():
    """track_llm_usage should update token_state in place."""
    state = {"input": 0, "output": 0, "thinking": 0, "cached": 0, "api_calls": 0}

    track_llm_usage(
        FakeLLMResponse(),
        state,
        "test_agent",
        "some_tool",
    )

    assert state["input"] == 100
    assert state["output"] == 50
    assert state["thinking"] == 10
    assert state["cached"] == 20
    assert state["api_calls"] == 1


class FakeToolCall:
    def __init__(self, name, args, id=None):
        self.name = name
        self.args = args
        self.id = id


def test_execute_tools_batch_sequential():
    """execute_tools_batch runs sequentially when parallel is disabled."""
    calls = [FakeToolCall("tool_a", {"x": 1}), FakeToolCall("tool_b", {"y": 2})]
    execution_order = []

    def executor(name, args, tc_id):
        execution_order.append(name)
        return {"status": "ok", "tool": name}

    results = execute_tools_batch(
        calls, executor, set(), False, 4, "test", None,
    )
    assert len(results) == 2
    assert results[0][1] == "tool_a"
    assert results[1][1] == "tool_b"
    assert execution_order == ["tool_a", "tool_b"]


def test_execute_tools_batch_parallel():
    """execute_tools_batch runs in parallel when all tools are safe."""
    calls = [FakeToolCall("safe_a", {}), FakeToolCall("safe_b", {})]

    def executor(name, args, tc_id):
        return {"status": "ok"}

    results = execute_tools_batch(
        calls, executor, {"safe_a", "safe_b"}, True, 4, "test", None,
    )
    assert len(results) == 2


# --- Timeout plumbing tests ----------------------------------------------

class _FakeChat:
    """Fake chat session that lets a test control when send() returns.

    Exposes _request_timeout like the real adapter sessions so SubmitFn
    can plumb the per-call timeout.
    """

    def __init__(self, blocker, result=None, raises=None):
        self._request_timeout = None
        self._blocker = blocker  # threading.Event — set to unblock send()
        self._result = result
        self._raises = raises
        self.send_called_with_timeout = None

    def send(self, message):
        # Record what timeout the adapter would use on its HTTP call.
        self.send_called_with_timeout = self._request_timeout
        self._blocker.wait(timeout=10.0)
        if self._raises is not None:
            raise self._raises
        return self._result


def test_submit_fn_plumbs_retry_timeout_to_chat():
    """_SubmitFn sets chat._request_timeout = retry_timeout before dispatch
    so the adapter can pass a matching per-call HTTP timeout."""
    import threading
    pool = ThreadPoolExecutor(max_workers=1)
    blocker = threading.Event()
    chat = _FakeChat(blocker, result=FakeLLMResponse())

    submit_fn = _SubmitFn(pool, chat, "hi", "send", retry_timeout=45.0)
    future = submit_fn()
    # Let the worker proceed — it'll record the timeout value it saw.
    blocker.set()
    future.result(timeout=5.0)
    pool.shutdown(wait=True)

    assert chat._request_timeout == 45.0
    assert chat.send_called_with_timeout == 45.0


def test_submit_fn_skips_timeout_for_chat_without_attribute():
    """_SubmitFn only sets _request_timeout when the chat supports it —
    legacy chat classes without the attribute are not modified."""
    import threading

    class _LegacyChat:
        # no _request_timeout attribute
        def __init__(self):
            self._event = threading.Event()

        def send(self, message):
            self._event.wait(timeout=10.0)
            return FakeLLMResponse()

    pool = ThreadPoolExecutor(max_workers=1)
    chat = _LegacyChat()
    submit_fn = _SubmitFn(pool, chat, "hi", "send", retry_timeout=45.0)
    future = submit_fn()
    chat._event.set()
    future.result(timeout=5.0)
    pool.shutdown(wait=True)
    # No attribute was added.
    assert not hasattr(chat, "_request_timeout")


def test_send_with_timeout_waits_for_worker_to_settle_after_timeout():
    """When the main-thread watchdog expires, _send should wait briefly for
    the worker future to settle so its except-block (e.g. drop_trailing)
    completes before AED sees the interface. Verified by timing: the
    TimeoutError should not be raised until after the worker finishes."""
    import threading, time as _time
    pool = ThreadPoolExecutor(max_workers=1)
    blocker = threading.Event()
    # Worker will block until the blocker is set, then raise (simulating
    # the HTTP client giving up after its per-call timeout).
    chat = _FakeChat(blocker, raises=RuntimeError("simulated HTTP timeout"))
    worker_settled_at = []

    # In a separate thread, release the worker shortly after the watchdog
    # fires, so we can observe that _send waited for it.
    def release_worker_after(delay):
        _time.sleep(delay)
        worker_settled_at.append(_time.monotonic())
        blocker.set()

    releaser = threading.Thread(target=release_worker_after, args=(0.3,))
    t_start = _time.monotonic()
    releaser.start()

    try:
        send_with_timeout(
            chat=chat, message="hi",
            timeout_pool=pool, retry_timeout=0.1,
            agent_name="test", logger=None,
        )
        assert False, "expected TimeoutError"
    except TimeoutError:
        pass
    t_end = _time.monotonic()
    releaser.join()
    pool.shutdown(wait=True)

    # The TimeoutError should be raised AFTER the worker settled, not before.
    # (Worker settles at ~0.3s; watchdog fires at ~0.1s; _send waits up to
    # _WORKER_SETTLE_GRACE=5s for worker, so we expect t_end >= worker_settled_at.)
    assert len(worker_settled_at) == 1
    assert t_end >= worker_settled_at[0], (
        f"send_with_timeout raised before worker settled: "
        f"t_end={t_end - t_start:.3f}s, settled={worker_settled_at[0] - t_start:.3f}s"
    )


def test_wait_for_worker_settle_raises_when_future_still_running(monkeypatch):
    """A worker that survives settle grace is unsafe for AED retry."""
    monkeypatch.setattr("lingtai_kernel.llm_utils._WORKER_SETTLE_GRACE", 0.01)
    future = Future()

    with pytest.raises(WorkerStillRunningError) as exc:
        _wait_for_worker_settle(future, elapsed=300.0, agent_name="test")

    assert exc.value.elapsed == 300.0
    assert exc.value.grace == 0.01
    assert exc.value.agent_name == "test"


def test_send_with_timeout_raises_worker_still_running_when_worker_never_settles(monkeypatch):
    """send_with_timeout propagates WorkerStillRunningError when the worker
    remains alive past retry_timeout + grace.
    """
    import threading

    monkeypatch.setattr("lingtai_kernel.llm_utils._WORKER_SETTLE_GRACE", 0.01)
    pool = ThreadPoolExecutor(max_workers=1)
    blocker = threading.Event()
    chat = _FakeChat(blocker, result=FakeLLMResponse())

    try:
        with pytest.raises(WorkerStillRunningError):
            send_with_timeout(
                chat=chat, message="hi",
                timeout_pool=pool, retry_timeout=0.01,
                agent_name="test", logger=None,
            )
    finally:
        blocker.set()
        pool.shutdown(wait=True)
