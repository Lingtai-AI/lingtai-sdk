"""Turn-loop tests for terminal async daemon dispatch."""
from __future__ import annotations

import threading

from lingtai.kernel.base_agent.turn import _process_response
from lingtai.kernel.llm.base import LLMResponse, ToolCall
from lingtai.kernel.loop_guard import LoopGuard
from lingtai.kernel.tool_executor import ToolExecutor


class _NoopSentTracker:
    def was_recently_sent(self, content, recipient):
        return False

    def record_sent(self, content, recipient, tool):
        pass

    def reset_poll(self, tool):
        pass

    def record_poll(self, tool, *, found_new=False):
        pass

    def should_stop_polling(self, tool):
        return False


class _StubChat:
    def __init__(self):
        self.committed = []

    def commit_tool_results(self, results):
        self.committed.append(list(results))


class _StubSession:
    def __init__(self):
        self.sends = []

    def send(self, payload):
        self.sends.append(payload)
        return LLMResponse(text="continued after tool")


class _StubAgent:
    def __init__(self, *, dispatch_fn, allow_terminal_async_dispatch=True):
        self._cancel_event = threading.Event()
        self._session = _StubSession()
        self._chat = _StubChat()
        self._notification_live_holder = None
        self._working_dir = None
        self._logs = []
        self._saves = []
        self._sent_tracker = _NoopSentTracker()
        self._intrinsics = {}
        self._on_tool_result_hook = None
        self._executor = ToolExecutor(
            dispatch_fn=dispatch_fn,
            make_tool_result_fn=lambda name, result, **kw: {
                "name": name,
                "result": result,
                "tool_call_id": kw.get("tool_call_id"),
            },
            guard=LoopGuard(max_total_calls=50),
            known_tools={"daemon", "read"},
            logger_fn=self._log,
            meta_fn=lambda: {},
            allow_terminal_async_dispatch=allow_terminal_async_dispatch,
        )

    def _log(self, event_type, **fields):
        self._logs.append((event_type, fields))

    def _save_chat_history(self, *, ledger_source=None):
        self._saves.append(ledger_source)

    def _sync_notifications(self):
        pass


def test_terminal_async_daemon_dispatch_skips_post_tool_continuation_send():
    def dispatch(call):
        assert call.name == "daemon"
        return {
            "status": "dispatched",
            "ids": ["em-1"],
            "group_id": "dg-1",
            "terminal_async_dispatch": True,
        }

    agent = _StubAgent(dispatch_fn=dispatch)
    result = _process_response(agent, LLMResponse(tool_calls=[
        ToolCall(
            name="daemon",
            args={"action": "emanate", "tasks": [{"task": "scan", "tools": ["file"]}]},
            id="daemon-call",
        )
    ]))

    assert result == {"text": "", "failed": False, "errors": []}
    assert agent._session.sends == []
    assert len(agent._chat.committed) == 1
    committed = agent._chat.committed[0]
    assert len(committed) == 1
    assert committed[0]["tool_call_id"] == "daemon-call"
    assert committed[0]["result"]["status"] == "dispatched"
    assert committed[0]["result"]["terminal_async_dispatch"] is True
    assert any(event == "tool_call_terminal_async_dispatch" for event, _ in agent._logs)


def test_normal_tool_still_continues_after_tool_result():
    agent = _StubAgent(dispatch_fn=lambda call: {"status": "ok"})

    result = _process_response(agent, LLMResponse(tool_calls=[
        ToolCall(name="read", args={"file_path": "x"}, id="read-call")
    ]))

    assert result == {"text": "continued after tool", "failed": False, "errors": []}
    assert len(agent._session.sends) == 1
    sent = agent._session.sends[0]
    assert len(sent) == 1
    assert sent[0]["tool_call_id"] == "read-call"
    assert agent._chat.committed == []


def test_daemon_dispatch_without_terminal_signal_still_continues():
    agent = _StubAgent(dispatch_fn=lambda call: {
        "status": "dispatched",
        "ids": ["em-1"],
        "group_id": "dg-1",
    })

    result = _process_response(agent, LLMResponse(tool_calls=[
        ToolCall(name="daemon", args={"action": "emanate", "tasks": []}, id="daemon-call")
    ]))

    assert result == {"text": "continued after tool", "failed": False, "errors": []}
    assert len(agent._session.sends) == 1
    assert agent._chat.committed == []
