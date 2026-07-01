"""Tests for daemon CLI backend free-form options (`backend_options`).

Covers:
- The pure argv conversion helper (`_backend_options_to_argv`).
- Per-task backend_options validation in `_handle_emanate_cli`.
- CLI runners (`_run_claude_code_emanation`, `_run_codex_emanation`,
  `_run_mimocode_emanation`, `_run_qwen_code_emanation`) appending
  backend_argv between required flags and the task prompt.
- Persistence: resolved options land in daemon.json.
- The lingtai backend ignoring the field (no schema breakage).
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from lingtai.core.daemon import (
    _BACKEND_ALIASES,
    _backend_options_to_argv,
    _BACKEND_SCHEMA_ENUM,
    _BACKEND_SPECS,
    _cli_backend_loads_common_mcp as _source_cli_backend_loads_common_mcp,
    _normalize_backend,
)
from tests._daemon_helpers import (
    FiniteFakeProc,
    completed_future,
    make_daemon_agent,
    make_daemon_run_dir,
    register_daemon_entry,
)


# ---------------------------------------------------------------------------
# Pure helper: _backend_options_to_argv
# ---------------------------------------------------------------------------


def test_argv_none_and_empty_return_empty():
    assert _backend_options_to_argv(None) == []
    assert _backend_options_to_argv({}) == []


def test_argv_bool_true_emits_flag_only():
    assert _backend_options_to_argv({"search": True}) == ["--search"]


def test_argv_bool_false_and_null_are_omitted():
    assert _backend_options_to_argv({"search": False, "verbose": None}) == []


def test_argv_string_int_float():
    out = _backend_options_to_argv({"model": "gpt-5"})
    assert out == ["--model", "gpt-5"]

    out = _backend_options_to_argv({"retries": 3})
    assert out == ["--retries", "3"]

    out = _backend_options_to_argv({"temperature": 0.5})
    assert out == ["--temperature", "0.5"]


def test_argv_list_repeats_flag():
    out = _backend_options_to_argv({"include": ["src", "tests"]})
    assert out == ["--include", "src", "--include", "tests"]


def test_argv_underscore_key_becomes_dash():
    out = _backend_options_to_argv({"output_format": "json"})
    assert out == ["--output-format", "json"]


def test_argv_mixed_options_preserve_key_order():
    out = _backend_options_to_argv({
        "model": "claude-opus-4-7",
        "effort": "high",
        "search": True,
    })
    # dict iteration is insertion-ordered in Python 3.7+
    assert out == [
        "--model", "claude-opus-4-7",
        "--effort", "high",
        "--search",
    ]


def test_argv_rejects_leading_dash_key():
    with pytest.raises(ValueError, match="safe CLI flag name"):
        _backend_options_to_argv({"-model": "x"})


def test_argv_rejects_empty_key():
    with pytest.raises(ValueError, match="safe CLI flag name"):
        _backend_options_to_argv({"": "x"})


def test_argv_rejects_space_in_key():
    with pytest.raises(ValueError, match="safe CLI flag name"):
        _backend_options_to_argv({"output format": "json"})


def test_argv_rejects_shell_metachar_in_key():
    with pytest.raises(ValueError, match="safe CLI flag name"):
        _backend_options_to_argv({"model;rm -rf": "x"})


def test_argv_rejects_nested_object_value():
    with pytest.raises(ValueError, match="unsupported value type"):
        _backend_options_to_argv({"config": {"nested": True}})


def test_argv_rejects_list_with_nested_object():
    with pytest.raises(ValueError, match="list items must be"):
        _backend_options_to_argv({"include": [{"path": "src"}]})


def test_argv_rejects_list_with_bool_item():
    with pytest.raises(ValueError, match="list items must be"):
        _backend_options_to_argv({"flags": [True, False]})


def test_argv_rejects_non_dict_root():
    with pytest.raises(ValueError, match="must be a JSON object"):
        _backend_options_to_argv("--search")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: _handle_emanate_cli validation + persistence
# ---------------------------------------------------------------------------


def test_emanate_cli_rejects_bad_backend_options(tmp_path):
    """A single invalid backend_options spec refuses the whole batch
    with a tool-level error mentioning the offending index."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    result = mgr.handle({
        "action": "emanate",
        "backend": "claude-code",
        "tasks": [
            {"task": "ok task", "tools": [], "backend_options": {"effort": "high"}},
            {"task": "bad task", "tools": [], "backend_options": {"-model": "x"}},
        ],
    })
    assert result["status"] == "error"
    assert "tasks[1].backend_options" in result["message"]
    # Nothing was scheduled
    assert mgr._emanations == {}


def test_emanate_cli_persists_resolved_options(tmp_path):
    """Successful CLI emanate persists user argv separately from harness argv."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    # Block the worker from actually invoking subprocess.Popen — we only
    # care that _handle_emanate_cli wired the run_dir state correctly.
    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event,
                 backend_argv=None):
        captured["em_id"] = em_id
        captured["task"] = task
        captured["backend_argv"] = list(backend_argv or [])
        captured["daemon_json_state"] = json.loads(
            run_dir.daemon_json_path.read_text()
        )
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_claude_code_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "claude-code",
            "tasks": [{
                "task": "Refactor auth.",
                "tools": [],
                "backend_options": {
                    "effort": "high",
                    "model": "claude-opus-4-7",
                    "search": True,
                },
            }],
        })
        assert result["status"] == "dispatched"

        # Wait for the fake worker to complete.
        em_id = result["ids"][0]
        fut = mgr._emanations[em_id]["future"]
        fut.result(timeout=5)

    user_argv = [
        "--effort", "high",
        "--model", "claude-opus-4-7",
        "--search",
    ]
    assert captured["backend_argv"][:len(user_argv)] == user_argv
    assert "--mcp-config" in captured["backend_argv"]
    assert "--strict-mcp-config" in captured["backend_argv"]
    state = captured["daemon_json_state"]
    assert state["backend"] == "claude-code"
    assert state["backend_options"] == {
        "effort": "high",
        "model": "claude-opus-4-7",
        "search": True,
    }
    assert state["backend_argv"] == user_argv
    assert "--mcp-config" in state["backend_harness_argv"]
    assert "--strict-mcp-config" in state["backend_harness_argv"]


def test_emanate_cli_no_options_omits_fields(tmp_path):
    """No backend_options omits user fields but records harness argv separately."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event,
                 backend_argv=None):
        captured["backend_argv"] = list(backend_argv or [])
        captured["state"] = json.loads(run_dir.daemon_json_path.read_text())
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_claude_code_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "claude-code",
            "tasks": [{"task": "no options", "tools": []}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert "--mcp-config" in captured["backend_argv"]
    assert "--strict-mcp-config" in captured["backend_argv"]
    assert "backend_options" not in captured["state"]
    assert "backend_argv" not in captured["state"]
    assert "--mcp-config" in captured["state"]["backend_harness_argv"]
    assert "--strict-mcp-config" in captured["state"]["backend_harness_argv"]


def test_lingtai_backend_ignores_backend_options(tmp_path):
    """The lingtai backend has no CLI process — backend_options must be
    silently ignored, never raised against the schema."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    # Force preset path off and mock create_session so the worker is a no-op.
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "task done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    result = mgr.handle({
        "action": "emanate",
        # backend defaults to "lingtai"
        "tasks": [{
            "task": "lingtai task",
            "tools": ["file"],
            # This must be ignored, not validated. Even an "invalid" object
            # would be accepted because the lingtai backend never reads it.
            "backend_options": {"effort": "high"},
        }],
    })
    assert result["status"] == "dispatched"


def test_unknown_backend_falls_back_to_lingtai_path(tmp_path):
    """Direct callers that bypass schema validation keep the old fallback.

    Unknown backend strings are not CLI backends; they route to the in-process
    LingTai worker and therefore do not store a CLI ``backend`` field.
    """
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict = {}

    def fake_run(em_id, run_dir, schemas, dispatch, task, cancel_event,
                 timeout_event=None, preset_llm=None, max_turns=None,
                 task_mcp_clients=None):
        captured["task"] = task
        captured["state"] = dict(run_dir._state)
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "not-real",
            "tasks": [{"task": "fall back", "tools": []}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["task"] == "fall back"
    assert captured["state"]["backend"] == "lingtai"
    assert "backend" not in mgr._emanations[em_id]


# ---------------------------------------------------------------------------
# Runner cmd construction: backend_argv lands before the task prompt
# ---------------------------------------------------------------------------


def test_claude_code_cmd_appends_backend_argv_before_task(tmp_path):
    """The Claude Code runner must put backend_argv after the required
    infrastructure flags and immediately before the task positional."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured_cmd: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        return FiniteFakeProc()

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-test",
        task="dummy task",
        tools=[],
        model="claude-code",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="claude-code",
    )

    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_claude_code_emanation(
            "em-test", run_dir, "Refactor auth.",
            cancel, timeout,
            backend_argv=["--effort", "high", "--model", "claude-opus-4-7"],
        )

    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    # Required prefix preserved
    assert cmd[0] == "claude"
    assert "--print" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert "--name" in cmd
    # backend_argv lives somewhere after --name and before the trailing task
    effort_idx = cmd.index("--effort")
    model_idx = cmd.index("--model")
    name_idx = cmd.index("--name")
    task_idx = cmd.index("Refactor auth.")
    assert name_idx < effort_idx < task_idx
    assert name_idx < model_idx < task_idx
    # The task itself is the very last token
    assert cmd[-1] == "Refactor auth."


def test_codex_cmd_appends_backend_argv_before_task(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured_cmd: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        return FiniteFakeProc()

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-codex",
        task="dummy",
        tools=[],
        model="codex",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="codex",
    )

    cancel = threading.Event()
    timeout = threading.Event()

    # Codex needs a `turn.completed` event to consider the run successful;
    # feed a minimal valid stream.
    fake_stdout_lines = [
        '{"type":"thread.started","thread_id":"thr-xyz"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        '{"type":"turn.completed"}\n',
    ]

    with patch("lingtai.core.daemon.subprocess.Popen",
               side_effect=lambda cmd, *a, **kw: (captured_cmd.append(list(cmd))
                                                  or FiniteFakeProc(
                                                      stdout_lines=fake_stdout_lines,
                                                  ))):
        mgr._run_codex_emanation(
            "em-codex", run_dir, "Find the breaking change.",
            cancel, timeout,
            backend_argv=["--model", "gpt-5", "--search"],
        )

    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    assert cmd[:4] == ["codex", "exec", "--json",
                       "--dangerously-bypass-approvals-and-sandbox"]
    # backend_argv tokens are present, in order, and before the task
    assert cmd[4:6] == ["--model", "gpt-5"]
    assert cmd[6] == "--search"
    assert cmd[-1] == "Find the breaking change."


# ---------------------------------------------------------------------------
# Schema surface
# ---------------------------------------------------------------------------


def test_schema_includes_backend_options():
    from lingtai.core.daemon import get_schema
    schema = get_schema("en")
    task_props = schema["properties"]["tasks"]["items"]["properties"]
    assert "backend_options" in task_props
    assert task_props["backend_options"]["type"] == "object"
    # The free-form description should mention discovery via --help so
    # agents know not to expect a fixed list here.
    assert "--help" in task_props["backend_options"]["description"]


def test_backend_schema_enum_matches_ordered_contract():
    from lingtai.core.daemon import get_schema

    expected = [
        "lingtai",
        "claude-p",
        "claude-code",
        "codex",
        "opencode",
        "mimocode",
        "mimo",
        "qwen-code",
        "qwen",
        "oh-my-pi",
        "omp",
        "kimicode",
        "kimi",
        "cursor",
    ]
    assert list(_BACKEND_SCHEMA_ENUM) == expected
    assert get_schema("en")["properties"]["backend"]["enum"] == expected


def test_backend_metadata_consistency_keeps_hidden_legacy_claude():
    hidden = {"claude", "claude-interactive"}
    assert set(_BACKEND_SCHEMA_ENUM) == (
        (set(_BACKEND_SPECS) - hidden) | set(_BACKEND_ALIASES)
    )
    assert hidden.isdisjoint(_BACKEND_SCHEMA_ENUM)
    assert _BACKEND_ALIASES == {
        "mimo": "mimocode",
        "qwen": "qwen-code",
        "omp": "oh-my-pi",
        "kimi": "kimicode",
    }
    assert all(target in _BACKEND_SPECS for target in _BACKEND_ALIASES.values())
    assert _BACKEND_SPECS["claude-code"].runner_attr == "_run_claude_code_emanation"
    assert _BACKEND_SPECS["claude-p"].runner_attr == "_run_claude_code_emanation"


def test_normalize_backend_aliases_only_true_aliases():
    assert _normalize_backend("mimo") == "mimocode"
    assert _normalize_backend("qwen") == "qwen-code"
    assert _normalize_backend("omp") == "oh-my-pi"
    assert _normalize_backend("kimi") == "kimicode"
    assert _normalize_backend(None) == "lingtai"
    assert _normalize_backend("") == "lingtai"
    assert _normalize_backend("claude-code") == "claude-code"
    assert _normalize_backend("not-real") == "not-real"


def test_schema_includes_mimocode_and_qwen_code_backends():
    from lingtai.core.daemon import get_schema

    backend = get_schema("en")["properties"]["backend"]
    for name in ("mimocode", "mimo", "qwen-code", "qwen"):
        assert name in backend["enum"]
    assert "MiMo Code" in backend["description"]
    assert "Qwen Code" in backend["description"]


def test_mimocode_alias_dispatches_to_canonical_backend(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["backend"] = run_dir._state["backend"]
        captured["model"] = run_dir._state["model"]
        captured["backend_argv"] = list(backend_argv or [])
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_mimocode_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "mimo",
            "tasks": [{"task": "Use MiMo Code.", "tools": [],
                       "backend_options": {"model": "mimo-auto"}}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["backend"] == "mimocode"
    assert captured["model"] == "mimocode"
    assert captured["backend_argv"] == ["--model", "mimo-auto"]


def test_cli_contexts_keep_per_task_argv_and_passive_mcp(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict[str, dict] = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured[run_dir._state["task"]] = {
            "backend_argv": list(backend_argv or []),
            "call_parameters": run_dir._state["call_parameters"],
            "state": dict(run_dir._state),
        }
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with (
        patch.object(mgr, "_run_claude_code_emanation", side_effect=fake_run),
        patch.object(
            mgr,
            "_connect_task_mcp_registrations",
            side_effect=AssertionError("CLI backend must not connect MCP clients"),
        ),
    ):
        result = mgr.handle({
            "action": "emanate",
            "backend": "claude-code",
            "tasks": [
                {
                    "task": "task with argv",
                    "tools": [],
                    "backend_options": {"model": "claude-opus-4-7"},
                },
                {
                    "task": "task with mcp",
                    "tools": [],
                    "mcp": [{
                        "name": "demo",
                        "command": "demo-mcp",
                        "args": ["--serve"],
                        "env": {"TOKEN": "secret"},
                    }],
                },
            ],
        })
        assert result["status"] == "dispatched"
        for em_id in result["ids"]:
            mgr._emanations[em_id]["future"].result(timeout=5)

    argv_with_model = captured["task with argv"]["backend_argv"]
    assert argv_with_model[:2] == ["--model", "claude-opus-4-7"]
    assert "--mcp-config" in argv_with_model
    assert "--strict-mcp-config" in argv_with_model
    assert captured["task with argv"]["call_parameters"]["mcp"][0]["name"] == "daemon_common"
    argv_with_mcp = captured["task with mcp"]["backend_argv"]
    assert "--mcp-config" in argv_with_mcp
    assert "--strict-mcp-config" in argv_with_mcp
    assert "backend_argv" not in captured["task with mcp"]["state"]
    assert "--mcp-config" in captured["task with mcp"]["state"]["backend_harness_argv"]
    assert "--strict-mcp-config" in captured["task with mcp"]["state"]["backend_harness_argv"]
    mcp_params = captured["task with mcp"]["call_parameters"]["mcp"]
    assert mcp_params[0]["name"] == "daemon_common"
    assert mcp_params[1] == {
        "name": "demo",
        "command": "demo-mcp",
        "args": ["--serve"],
        "env": {"TOKEN": "<redacted>"},
        "transport": "stdio",
    }


def test_mimocode_cmd_appends_backend_argv_before_prompt(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-mimo",
        task="dummy",
        tools=[],
        model="mimocode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="mimocode",
    )
    stdout_lines = [
        '{"type":"session.created","sessionID":"sess-mimo"}\n',
        '{"type":"message.completed","text":"done"}\n',
    ]

    with patch("lingtai.core.daemon.subprocess.Popen",
               side_effect=lambda cmd, *a, **kw: (captured_cmd.append(list(cmd))
                                                  or FiniteFakeProc(
                                                      stdout_lines=stdout_lines,
                                                  ))):
        mgr._run_mimocode_emanation(
            "em-mimo", run_dir, "Refactor with MiMo.",
            threading.Event(), threading.Event(),
            backend_argv=["--model", "mimo-auto", "--agent", "build"],
        )

    cmd = captured_cmd[0]
    assert cmd[:4] == ["mimo", "run", "--format", "json"]
    assert cmd[4:8] == ["--model", "mimo-auto", "--agent", "build"]
    assert "Refactor with MiMo." in cmd[-1]
    assert run_dir._state["mimocode_session_id"] == "sess-mimo"


def test_qwen_code_cmd_appends_backend_argv_before_prompt(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-qwen",
        task="dummy",
        tools=[],
        model="qwen-code",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="qwen-code",
    )

    with patch("lingtai.core.daemon.subprocess.Popen",
               side_effect=lambda cmd, *a, **kw: (captured_cmd.append(list(cmd))
                                                  or FiniteFakeProc(
                                                      stdout_lines=["qwen done\n"],
                                                  ))):
        mgr._run_qwen_code_emanation(
            "em-qwen", run_dir, "Refactor with Qwen.",
            threading.Event(), threading.Event(),
            backend_argv=["--model", "qwen3-coder-plus"],
        )

    cmd = captured_cmd[0]
    assert cmd[:2] == ["qwen", "--yolo"]
    assert cmd[2:4] == ["--model", "qwen3-coder-plus"]
    assert cmd[-2] == "-p"
    assert "Refactor with Qwen." in cmd[-1]
    assert run_dir._state["last_output"] == "qwen done"


def test_qwen_code_rejects_harness_owned_backend_options(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    result = mgr.handle({
        "action": "emanate",
        "backend": "qwen-code",
        "tasks": [{"task": "bad", "tools": [],
                   "backend_options": {"prompt": "override"}}],
    })

    assert result["status"] == "error"
    assert "--prompt is reserved by the qwen-code daemon backend" in result["message"]
    assert mgr._emanations == {}


def test_qwen_code_ask_is_explicitly_unsupported(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_qwen_code_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "qwen-code",
            "tasks": [{"task": "Qwen once.", "tools": []}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    ask = mgr.handle({"action": "ask", "id": em_id, "message": "follow up"})

    assert ask["status"] == "error"
    assert ask["message"] == (
        "qwen-code daemon backend does not support daemon(action='ask') yet; "
        "start a new qwen-code emanation instead."
    )


# ---------------------------------------------------------------------------
# Kimi Code backend
# ---------------------------------------------------------------------------


def test_schema_includes_kimicode_backend():
    from lingtai.core.daemon import get_schema

    backend = get_schema("en")["properties"]["backend"]
    for name in ("kimicode", "kimi"):
        assert name in backend["enum"]
    assert "Kimi Code" in backend["description"]


@pytest.mark.parametrize("backend", ["kimi", "kimicode"])
def test_kimicode_alias_and_canonical_dispatch_to_backend(tmp_path, backend):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["backend"] = run_dir._state["backend"]
        captured["model"] = run_dir._state["model"]
        captured["backend_argv"] = list(backend_argv or [])
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_kimicode_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": backend,
            "tasks": [{"task": "Use Kimi Code.", "tools": [],
                       "backend_options": {"model": "kimi-for-coding"}}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["backend"] == "kimicode"
    assert captured["model"] == "kimicode"
    assert captured["backend_argv"] == ["--model", "kimi-for-coding"]


def test_kimicode_cmd_appends_backend_argv_before_owned_flags(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []
    captured_env: list[dict] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-kimi",
        task="dummy",
        tools=[],
        model="kimicode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="kimicode",
    )

    def fake_popen(cmd, *a, **kw):
        captured_cmd.append(list(cmd))
        captured_env.append(dict(kw.get("env") or {}))
        return FiniteFakeProc(stdout_lines=["kimi done\n"])

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_kimicode_emanation(
            "em-kimi", run_dir, "Refactor with Kimi.",
            threading.Event(), threading.Event(),
            backend_argv=["--model", "kimi-for-coding"],
        )

    cmd = captured_cmd[0]
    assert cmd[0] == "kimi"
    # Free-form backend_argv comes right after the executable...
    assert cmd[1:3] == ["--model", "kimi-for-coding"]
    # ...and the harness-owned flags come last, with the prompt behind --prompt.
    assert cmd[-2:] == ["--output-format", "text"]
    assert cmd[-4] == "--prompt"
    assert "Refactor with Kimi." in cmd[-3]
    assert "--yolo" not in cmd
    assert run_dir._state["last_output"] == "kimi done"


def test_kimicode_run_env_defaults_and_home(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_env: list[dict] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-kimi-env",
        task="dummy",
        tools=[],
        model="kimicode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="kimicode",
    )

    # No canonical key set; a source key is present and must be mapped.
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_MODEL_NAME", raising=False)
    monkeypatch.setenv("KIMICODE_API_KEY", "sk-secret-kimi")

    def fake_popen(cmd, *a, **kw):
        captured_env.append(dict(kw.get("env") or {}))
        return FiniteFakeProc(stdout_lines=["ok\n"])

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_kimicode_emanation(
            "em-kimi-env", run_dir, "Do it.",
            threading.Event(), threading.Event(),
        )

    env = captured_env[0]
    # Run-private KIMI_CODE_HOME lives under the run dir.
    assert env["KIMI_CODE_HOME"].startswith(str(run_dir.path))
    assert env["KIMI_DISABLE_TELEMETRY"] == "1"
    assert env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    # Source key mapped onto the canonical var.
    assert env["KIMI_MODEL_API_KEY"] == "sk-secret-kimi"
    # Provider defaults applied when absent.
    assert env["KIMI_MODEL_NAME"] == "kimi-for-coding"
    assert env["KIMI_MODEL_PROVIDER_TYPE"] == "kimi"
    assert env["KIMI_MODEL_BASE_URL"] == "https://api.kimi.com/coding/v1"
    assert env["KIMI_MODEL_MAX_CONTEXT_SIZE"] == "262144"


def test_kimicode_run_env_respects_existing_operator_values(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_env: list[dict] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-kimi-op",
        task="dummy",
        tools=[],
        model="kimicode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="kimicode",
    )

    # Operator already set the canonical key and a model name — never override.
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "operator-key")
    monkeypatch.setenv("KIMICODE_API_KEY", "should-be-ignored")
    monkeypatch.setenv("KIMI_MODEL_NAME", "operator-model")

    def fake_popen(cmd, *a, **kw):
        captured_env.append(dict(kw.get("env") or {}))
        return FiniteFakeProc(stdout_lines=["ok\n"])

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_kimicode_emanation(
            "em-kimi-op", run_dir, "Do it.",
            threading.Event(), threading.Event(),
        )

    env = captured_env[0]
    assert env["KIMI_MODEL_API_KEY"] == "operator-key"
    assert env["KIMI_MODEL_NAME"] == "operator-model"


@pytest.mark.parametrize(
    ("present_key", "expected_value"),
    [
        ("KIMI_API_KEY", "sk-kimi-fallback"),
        ("MOONSHOT_API_KEY", "sk-moonshot-fallback"),
    ],
)
def test_kimicode_run_env_api_key_fallback_sources(
    tmp_path, monkeypatch, present_key, expected_value
):
    """When ``KIMICODE_API_KEY`` is absent, the next source in the fallback
    order (``KIMI_API_KEY`` then ``MOONSHOT_API_KEY``) maps onto the canonical
    ``KIMI_MODEL_API_KEY``."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_env: list[dict] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-kimi-fallback",
        task="dummy",
        tools=[],
        model="kimicode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="kimicode",
    )

    # No canonical key and no higher-priority source; only the parametrized
    # fallback source is present and must be mapped.
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("KIMICODE_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv(present_key, expected_value)

    def fake_popen(cmd, *a, **kw):
        captured_env.append(dict(kw.get("env") or {}))
        return FiniteFakeProc(stdout_lines=["ok\n"])

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_kimicode_emanation(
            "em-kimi-fallback", run_dir, "Do it.",
            threading.Event(), threading.Event(),
        )

    env = captured_env[0]
    assert env["KIMI_MODEL_API_KEY"] == expected_value


def test_kimicode_run_env_api_key_fallback_precedence(tmp_path, monkeypatch):
    """When multiple source keys are present, the fallback order is honored:
    ``KIMICODE_API_KEY`` beats ``KIMI_API_KEY`` beats ``MOONSHOT_API_KEY``."""
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_env: list[dict] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-kimi-precedence",
        task="dummy",
        tools=[],
        model="kimicode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="kimicode",
    )

    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("KIMICODE_API_KEY", "sk-kimicode-wins")
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-loses")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-loses")

    def fake_popen(cmd, *a, **kw):
        captured_env.append(dict(kw.get("env") or {}))
        return FiniteFakeProc(stdout_lines=["ok\n"])

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_kimicode_emanation(
            "em-kimi-precedence", run_dir, "Do it.",
            threading.Event(), threading.Event(),
        )

    env = captured_env[0]
    # Highest-priority source wins over the two lower-priority fallbacks.
    assert env["KIMI_MODEL_API_KEY"] == "sk-kimicode-wins"


def test_kimicode_not_in_common_mcp_loading_set():
    """Regression guard: kimicode ships no-MCP, so it must stay out of the
    daemon_common MCP-loading set. If a refactor accidentally added kimicode
    here, Kimi runs would be expected to emit a ``finish`` completion signal
    they cannot produce."""
    assert _source_cli_backend_loads_common_mcp("kimicode") is False
    # Sanity: the guard would actually fire — a backend that does load MCP.
    assert _source_cli_backend_loads_common_mcp("qwen-code") is True


@pytest.mark.parametrize("bad_flag", ["prompt", "output-format", "yolo", "session", "continue"])
def test_kimicode_rejects_harness_owned_backend_options(tmp_path, bad_flag):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    result = mgr.handle({
        "action": "emanate",
        "backend": "kimicode",
        "tasks": [{"task": "bad", "tools": [],
                   "backend_options": {bad_flag: "override"}}],
    })

    assert result["status"] == "error"
    assert f"--{bad_flag} is reserved by the kimicode daemon backend" in result["message"]
    assert mgr._emanations == {}


def test_kimicode_ask_is_explicitly_unsupported(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_kimicode_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "kimicode",
            "tasks": [{"task": "Kimi once.", "tools": []}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    ask = mgr.handle({"action": "ask", "id": em_id, "message": "follow up"})

    assert ask["status"] == "error"
    assert ask["message"] == (
        "kimicode daemon backend does not support daemon(action='ask') yet; "
        "start a new kimicode emanation instead."
    )


# ---------------------------------------------------------------------------
# Oh-My-Pi backend
# ---------------------------------------------------------------------------


def test_schema_includes_oh_my_pi_backend():
    from lingtai.core.daemon import get_schema

    backend = get_schema("en")["properties"]["backend"]
    for name in ("oh-my-pi", "omp"):
        assert name in backend["enum"]
    assert "Oh-My-Pi" in backend["description"]


@pytest.mark.parametrize("backend", ["omp", "oh-my-pi"])
def test_oh_my_pi_alias_and_canonical_dispatch_to_backend(tmp_path, backend):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["backend"] = run_dir._state["backend"]
        captured["model"] = run_dir._state["model"]
        captured["backend_argv"] = list(backend_argv or [])
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    with patch.object(mgr, "_run_oh_my_pi_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": backend,
            "tasks": [{"task": "Use Oh-My-Pi.", "tools": [],
                       "backend_options": {"provider": "anthropic"}}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["backend"] == "oh-my-pi"
    assert captured["model"] == "oh-my-pi"
    assert captured["backend_argv"] == ["--provider", "anthropic"]


def test_oh_my_pi_cmd_includes_mode_json_and_session_id_from_header(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []

    # Oh-My-Pi JSON mode: a `type:session` header (bare top-level id)
    # followed by agent events.
    stdout_lines = [
        '{"type":"session","id":"omp-sess-1","cwd":"/tmp"}\n',
        # Event ids that arrive after the session header must not overwrite
        # the resumable session id.
        '{"type":"session.updated","id":"not-the-session-id"}\n',
        '{"type":"message.completed","text":"all done"}\n',
    ]
    run_dir = make_daemon_run_dir(
        agent,
        handle="em-omp",
        task="dummy",
        tools=[],
        model="oh-my-pi",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="oh-my-pi",
    )

    with patch("lingtai.core.daemon.subprocess.Popen",
               side_effect=lambda cmd, *a, **kw: (captured_cmd.append(list(cmd))
                                                  or FiniteFakeProc(
                                                      stdout_lines=stdout_lines,
                                                  ))):
        mgr._run_oh_my_pi_emanation(
            "em-omp", run_dir, "Refactor with Oh-My-Pi.",
            threading.Event(), threading.Event(),
            backend_argv=["--provider", "anthropic", "--model", "claude-x"],
        )

    cmd = captured_cmd[0]
    # `omp --mode json --approval-mode yolo` prefix, then backend_argv, then prompt.
    assert cmd[:5] == ["omp", "--mode", "json", "--approval-mode", "yolo"]
    assert cmd[5:9] == ["--provider", "anthropic", "--model", "claude-x"]
    assert "Refactor with Oh-My-Pi." in cmd[-1]
    # Session id captured from the `type:session` header, stored under the
    # Oh-My-Pi-specific key.
    assert run_dir._state["oh_my_pi_session_id"] == "omp-sess-1"


def test_oh_my_pi_ask_resume_uses_session_flag(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-omp-ask",
        task="dummy",
        tools=[],
        model="oh-my-pi",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="oh-my-pi",
    )
    run_dir._state["oh_my_pi_session_id"] = "omp-sess-9"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)

    em_id = "em-omp-ask"
    entry = register_daemon_entry(
        mgr,
        em_id,
        run_dir,
        future=completed_future("[fake done]"),
        task="x",
        backend="oh-my-pi",
        ask_in_flight=False,
    )

    with patch("lingtai.core.daemon.subprocess.Popen",
               side_effect=lambda cmd, *a, **kw: (captured_cmd.append(list(cmd))
                                                  or FiniteFakeProc(
                                                      stdout_lines=[
                                                          '{"type":"message.completed","text":"resumed"}\n',
                                                      ],
                                                  ))):
        resp = mgr.handle({"action": "ask", "id": em_id, "message": "keep going"})
        # ask is async; wait for the ask worker to finish before asserting.
        fut = entry.get("ask_future")
        if fut is not None:
            fut.result(timeout=5)

    assert resp["status"] == "sent"
    cmd = captured_cmd[0]
    assert cmd == [
        "omp", "--mode", "json", "--approval-mode", "yolo",
        "--session", "omp-sess-9", "keep going",
    ]


def test_oh_my_pi_ask_before_session_id_returns_initializing_error(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    run_dir = make_daemon_run_dir(
        agent,
        handle="em-omp-no-session",
        task="dummy",
        tools=[],
        model="oh-my-pi",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="oh-my-pi",
    )
    em_id = "em-omp-no-session"
    register_daemon_entry(
        mgr,
        em_id,
        run_dir,
        future=completed_future("[fake done]"),
        task="x",
        backend="oh-my-pi",
        ask_in_flight=False,
    )

    resp = mgr.handle({"action": "ask", "id": em_id, "message": "continue"})

    assert resp["status"] == "error"
    assert "No oh-my-pi session ID found" in resp["message"]
    assert "may still be initializing" in resp["message"]


def test_oh_my_pi_rejects_harness_owned_backend_options(tmp_path):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    for flag, key, value in (
        ("--mode", "mode", "text"),
        ("--print", "print", True),
        ("--approval-mode", "approval_mode", "yolo"),
        ("--auto-approve", "auto_approve", True),
        ("--yolo", "yolo", True),
        ("--session", "session", "omp-sess-1"),
        ("--resume", "resume", "omp-sess-1"),
        ("--continue", "continue", True),
        ("--no-session", "no_session", True),
        ("--session-dir", "session_dir", "/tmp/omp-session"),
    ):
        result = mgr.handle({
            "action": "emanate",
            "backend": "oh-my-pi",
            "tasks": [{"task": "bad", "tools": [],
                       "backend_options": {key: value}}],
        })
        assert result["status"] == "error", flag
        assert f"{flag} is reserved by the oh-my-pi daemon backend" in result["message"], flag
        assert mgr._emanations == {}, flag
