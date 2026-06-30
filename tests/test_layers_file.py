"""Tests for file I/O capabilities (read, write, edit, glob, grep)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def test_file_sugar_expands_to_five(tmp_path):
    """capabilities=["file"] should register all 5 file tools."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    for name in ("read", "write", "edit", "glob", "grep"):
        assert name in agent._tool_handlers, f"{name} not registered"
    agent.stop(timeout=1.0)


def test_file_sugar_dict_form(tmp_path):
    """capabilities={"file": {}} (dict form) should also expand."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities={"file": {}},
    )
    for name in ("read", "write", "edit", "glob", "grep"):
        assert name in agent._tool_handlers, f"{name} not registered (dict form)"
    agent.stop(timeout=1.0)


def test_individual_file_capability(tmp_path):
    """Each file capability can be disabled individually via `disable=[...]`.

    The `lingtai.core.*` file caps are default-on; `disable` is the opt-out
    channel for hosts that want a narrower surface.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        disable=["edit", "glob", "grep"],
    )
    assert "read" in agent._tool_handlers
    assert "write" in agent._tool_handlers
    assert "edit" not in agent._tool_handlers
    assert "glob" not in agent._tool_handlers
    assert "grep" not in agent._tool_handlers
    agent.stop(timeout=1.0)


def test_write_and_read_via_capability(tmp_path):
    """Write and read files through capability handlers."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    # Write
    write_result = agent._tool_handlers["write"](
        {"file_path": str(agent.working_dir / "test.txt"), "content": "hello world"}
    )
    assert write_result["status"] == "ok"

    # Read
    read_result = agent._tool_handlers["read"](
        {"file_path": str(agent.working_dir / "test.txt")}
    )
    assert "hello world" in read_result["content"]
    agent.stop(timeout=1.0)


def test_edit_via_capability(tmp_path):
    """Edit files through capability handler."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    (agent.working_dir / "test.txt").write_text("hello world")
    result = agent._tool_handlers["edit"](
        {"file_path": str(agent.working_dir / "test.txt"), "old_string": "hello", "new_string": "goodbye"}
    )
    assert result["status"] == "ok"
    assert (agent.working_dir / "test.txt").read_text() == "goodbye world"
    agent.stop(timeout=1.0)


def test_glob_via_capability(tmp_path):
    """Glob files through capability handler."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    (agent.working_dir / "a.py").write_text("pass")
    (agent.working_dir / "b.py").write_text("pass")
    (agent.working_dir / "c.txt").write_text("text")
    result = agent._tool_handlers["glob"](
        {"pattern": "*.py", "path": str(agent.working_dir)}
    )
    # Agent init may create library files; assert user files are present.
    matched_names = {Path(p).name for p in result["matches"]}
    assert "a.py" in matched_names
    assert "b.py" in matched_names
    assert "c.txt" not in matched_names
    agent.stop(timeout=1.0)


def test_grep_via_capability(tmp_path):
    """Grep files through capability handler."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    (agent.working_dir / "test.py").write_text("def hello():\n    pass\n")
    result = agent._tool_handlers["grep"](
        {"pattern": "def hello", "path": str(agent.working_dir)}
    )
    assert result["count"] >= 1
    agent.stop(timeout=1.0)


def test_file_capability_relative_paths_resolve_under_workdir(tmp_path):
    """Relative file tool paths are rooted under the agent working directory."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    target = agent.working_dir / "nested" / "target.py"
    try:
        write_result = agent._tool_handlers["write"](
            {"file_path": "nested/target.py", "content": "needle\nold\n"}
        )
        assert write_result == {
            "status": "ok",
            "path": str(target),
            "bytes": len("needle\nold\n".encode("utf-8")),
        }
        assert target.read_text() == "needle\nold\n"

        read_result = agent._tool_handlers["read"]({"file_path": "nested/target.py"})
        assert "needle" in read_result["content"]

        edit_result = agent._tool_handlers["edit"](
            {"file_path": "nested/target.py", "old_string": "old", "new_string": "new"}
        )
        assert edit_result["status"] == "ok"
        assert target.read_text() == "needle\nnew\n"

        glob_result = agent._tool_handlers["glob"]({"pattern": "*.py", "path": "nested"})
        assert str(target) in glob_result["matches"]

        grep_result = agent._tool_handlers["grep"]({"pattern": "needle", "path": "nested"})
        assert any(match["file"] == str(target) for match in grep_result["matches"])
    finally:
        agent.stop(timeout=1.0)


def test_base_agent_has_no_file_intrinsics(tmp_path):
    """BaseAgent should NOT have file intrinsics after phase 2."""
    from lingtai_kernel.base_agent import BaseAgent
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    for name in ("read", "write", "edit", "glob", "grep"):
        assert name not in agent._intrinsics, f"{name} should not be in BaseAgent intrinsics"
    agent.stop(timeout=1.0)


def test_base_agent_kernel_only(tmp_path):
    """BaseAgent should have exactly 5 intrinsics: email, system, psyche, soul, notification."""
    from lingtai_kernel.base_agent import BaseAgent
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    assert set(agent._intrinsics.keys()) == {"email", "system", "psyche", "soul", "notification"}
    agent.stop(timeout=1.0)


def test_file_capability_uses_file_io_service(tmp_path):
    """File capabilities should use the agent's FileIOService."""
    from lingtai.services.file_io import LocalFileIOService
    svc = LocalFileIOService(root=tmp_path)
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        file_io=svc,
        capabilities=["file"],
    )
    result = agent._tool_handlers["write"](
        {"file_path": str(tmp_path / "test.txt"), "content": "via service"}
    )
    assert result["status"] == "ok"
    assert (tmp_path / "test.txt").read_text() == "via service"
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# C2 — error format consistency across file tools.
# Before fix: read returned {"status": "error", "message": ...} but
# write/edit/glob/grep returned {"error": ...}. tool_executor checks
# result.get("status") == "error" to populate collected_errors, so the
# four bare-error tools were silently dropped from kernel error tracking.
# ---------------------------------------------------------------------------


def _file_agent(tmp_path):
    return Agent(
        service=make_mock_service(), agent_name="test",
        working_dir=tmp_path / "test", capabilities=["file"],
    )


def _assert_error_shape(result, *, expect_substring=None):
    assert result.get("status") == "error", \
        f"expected status='error', got {result!r}"
    assert "message" in result, f"missing 'message' key in {result!r}"
    assert "error" not in result, \
        f"legacy 'error' key still present: {result!r}"
    if expect_substring:
        assert expect_substring in result["message"], \
            f"{expect_substring!r} not in message {result['message']!r}"


def test_read_error_shape(tmp_path):
    """read returns {status: error, message: ...} on failure."""
    agent = _file_agent(tmp_path)
    try:
        _assert_error_shape(agent._tool_handlers["read"]({}),
                            expect_substring="file_path")
        _assert_error_shape(
            agent._tool_handlers["read"]({"file_path": "/nonexistent/x"}),
            expect_substring="not found",
        )
    finally:
        agent.stop(timeout=1.0)


def test_write_error_shape(tmp_path):
    """write returns {status: error, message: ...} on failure (was {error: ...})."""
    agent = _file_agent(tmp_path)
    try:
        _assert_error_shape(agent._tool_handlers["write"]({"content": "x"}),
                            expect_substring="file_path")
    finally:
        agent.stop(timeout=1.0)


def test_edit_error_shape(tmp_path):
    """edit returns {status: error, message: ...} on each of its failure paths."""
    agent = _file_agent(tmp_path)
    try:
        _assert_error_shape(agent._tool_handlers["edit"]({}),
                            expect_substring="file_path")
        _assert_error_shape(
            agent._tool_handlers["edit"](
                {"file_path": "/nonexistent/x", "old_string": "a", "new_string": "b"}
            ),
            expect_substring="not found",
        )
        target = tmp_path / "edit_target.txt"
        target.write_text("hello")
        _assert_error_shape(
            agent._tool_handlers["edit"](
                {"file_path": str(target), "old_string": "missing", "new_string": "x"}
            ),
            expect_substring="not found",
        )
        target.write_text("aaa")
        _assert_error_shape(
            agent._tool_handlers["edit"](
                {"file_path": str(target), "old_string": "a", "new_string": "b"}
            ),
            expect_substring="replace_all",
        )
    finally:
        agent.stop(timeout=1.0)


def test_glob_error_shape(tmp_path):
    """glob returns {status: error, message: ...} on missing pattern."""
    agent = _file_agent(tmp_path)
    try:
        _assert_error_shape(agent._tool_handlers["glob"]({}),
                            expect_substring="pattern")
    finally:
        agent.stop(timeout=1.0)


def test_grep_error_shape(tmp_path):
    """grep returns {status: error, message: ...} on missing pattern."""
    agent = _file_agent(tmp_path)
    try:
        _assert_error_shape(agent._tool_handlers["grep"]({}),
                            expect_substring="pattern")
    finally:
        agent.stop(timeout=1.0)


def test_file_tool_errors_match_executor_predicate(tmp_path):
    """C2 hidden P1: tool_executor predicate (status=='error') now catches them."""
    agent = _file_agent(tmp_path)
    try:
        for tool in ("write", "edit", "glob", "grep"):
            args = {"content": "x"} if tool == "write" else {}
            result = agent._tool_handlers[tool](args)
            assert isinstance(result, dict) and result.get("status") == "error", \
                f"{tool}: tool_executor would silently drop {result!r}"
    finally:
        agent.stop(timeout=1.0)
