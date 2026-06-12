"""Tests for per-batch max_turns and timeout overrides on daemon.emanate."""
import threading
from unittest.mock import MagicMock, patch

from lingtai_kernel.config import AgentConfig


def _make_agent(tmp_path, capabilities=None):
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )


def _stub_emanate_internals(mgr):
    """Patch the heavy parts of _handle_emanate so we can inspect ceilings
    without actually running an LLM loop. Returns the captured args.
    """
    captured = {}

    real_submit = mgr._pools  # placeholder to avoid unused warning

    class _StubFuture:
        def done(self): return True
        def exception(self): return None
        def result(self): return "ok"
        def add_done_callback(self, fn): pass

    def fake_submit(*args, **kwargs):
        # _handle_emanate calls pool.submit(self._run_emanation, em_id,
        # run_dir, schemas, dispatch, task, cancel_event, timeout_event,
        # preset_llm, max_turns)
        captured["run_emanation_args"] = args
        captured["run_emanation_kwargs"] = kwargs
        return _StubFuture()

    return captured, fake_submit


def test_emanate_default_uses_ceiling(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    captured, fake_submit = _stub_emanate_internals(mgr)

    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool:
        pool = MockPool.return_value
        pool.submit.side_effect = fake_submit
        out = mgr.handle({"action": "emanate",
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    # max_turns is the 9th positional arg (index 8) to _run_emanation
    assert mgr._max_turns == 1000
    assert captured["run_emanation_args"][9] == 1000



def test_daemon_schema_advertises_1000_turn_ceiling():
    from lingtai.core.daemon import get_schema

    max_turns_schema = get_schema("en")["properties"]["max_turns"]
    assert max_turns_schema["minimum"] == 1
    assert max_turns_schema["maximum"] == 1000
    assert "1000" in max_turns_schema["description"]

def test_emanate_respects_per_batch_max_turns(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    captured, fake_submit = _stub_emanate_internals(mgr)

    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool:
        pool = MockPool.return_value
        pool.submit.side_effect = fake_submit
        out = mgr.handle({"action": "emanate", "max_turns": 50,
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    assert captured["run_emanation_args"][9] == 50


def test_emanate_caps_max_turns_at_ceiling(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    captured, fake_submit = _stub_emanate_internals(mgr)

    # ceiling is 1000; ask for 9999
    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool:
        pool = MockPool.return_value
        pool.submit.side_effect = fake_submit
        out = mgr.handle({"action": "emanate", "max_turns": 9999,
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    assert mgr._max_turns == 1000
    assert captured["run_emanation_args"][9] == 1000


def test_emanate_allows_new_1000_turn_ceiling(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    captured, fake_submit = _stub_emanate_internals(mgr)

    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool:
        pool = MockPool.return_value
        pool.submit.side_effect = fake_submit
        out = mgr.handle({"action": "emanate", "max_turns": 1000,
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    assert captured["run_emanation_args"][9] == 1000


def test_emanate_rejects_zero_max_turns(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "emanate", "max_turns": 0,
                      "tasks": [{"task": "x", "tools": ["read"]}]})
    assert out["status"] == "error"
    assert "max_turns" in out["message"]


def test_emanate_rejects_negative_max_turns(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "emanate", "max_turns": -5,
                      "tasks": [{"task": "x", "tools": ["read"]}]})
    assert out["status"] == "error"
    assert "max_turns" in out["message"]


def test_emanate_respects_per_batch_timeout(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    captured = {}
    def fake_thread_init(target, args, daemon):
        captured["watchdog_args"] = args
        # Return an object with a no-op start()
        m = MagicMock()
        m.start = MagicMock()
        return m

    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool, \
         patch("lingtai.core.daemon.threading.Thread", side_effect=fake_thread_init):
        pool = MockPool.return_value
        pool.submit.return_value = MagicMock(
            done=MagicMock(return_value=True),
            exception=MagicMock(return_value=None),
            add_done_callback=MagicMock(),
        )
        out = mgr.handle({"action": "emanate", "timeout": 600,
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    # Watchdog third arg is the timeout
    assert captured["watchdog_args"][2] == 600.0


def test_emanate_caps_timeout_at_ceiling(tmp_path):
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    captured = {}
    def fake_thread_init(target, args, daemon):
        captured["watchdog_args"] = args
        m = MagicMock()
        m.start = MagicMock()
        return m

    with patch("lingtai.core.daemon.ThreadPoolExecutor") as MockPool, \
         patch("lingtai.core.daemon.threading.Thread", side_effect=fake_thread_init):
        pool = MockPool.return_value
        pool.submit.return_value = MagicMock(
            done=MagicMock(return_value=True),
            exception=MagicMock(return_value=None),
            add_done_callback=MagicMock(),
        )
        out = mgr.handle({"action": "emanate", "timeout": 99999,
                          "tasks": [{"task": "x", "tools": ["file"]}]})

    assert out["status"] == "dispatched"
    assert captured["watchdog_args"][2] == mgr._timeout


def test_emanate_rejects_zero_timeout(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "emanate", "timeout": 0,
                      "tasks": [{"task": "x", "tools": ["read"]}]})
    assert out["status"] == "error"
    assert "timeout" in out["message"]


def test_emanate_rejects_negative_timeout(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "emanate", "timeout": -1,
                      "tasks": [{"task": "x", "tools": ["read"]}]})
    assert out["status"] == "error"
    assert "timeout" in out["message"]


def test_emanate_rejects_sub_5s_timeout(tmp_path):
    """Sub-5s timeouts can fire before the emanation thread starts (the
    watchdog ticks at 1s and OS scheduling can delay its first run).
    Refuse rather than silently mark emanations as 'timeout' before they ran."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "emanate", "timeout": 2,
                      "tasks": [{"task": "x", "tools": ["read"]}]})
    assert out["status"] == "error"
    assert "timeout" in out["message"]
    assert "5" in out["message"]
