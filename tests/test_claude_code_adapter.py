"""Tests for the claude-code provider adapter.

These mock the ``claude`` CLI subprocess so they run in CI without the binary.
A live end-to-end check against the real CLI lives in
``tests/integration_test_claude_code.py``.
"""

import json
from unittest.mock import patch

import pytest

from lingtai.llm.claude_code.adapter import (
    ClaudeCodeAdapter,
    ClaudeCodeAuthError,
    ClaudeCodeContextOverflow,
    ClaudeCodeError,
    _extract_json_object,
)
from lingtai_kernel.llm.base import FunctionSchema
from lingtai_kernel.llm.interface import TextBlock


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _envelope(result_str, *, is_error=False, usage=None, subtype="success"):
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result_str,
            "session_id": "sess-123",
            "usage": usage
            or {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 10,
            },
        }
    )


def _weather_tool():
    return FunctionSchema(
        name="get_weather",
        description="Get the current weather for a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )


# ---------------------------------------------------------------------------
# JSON action extraction
# ---------------------------------------------------------------------------


def test_extract_plain_object():
    assert _extract_json_object('{"action":"final","text":"hi"}') == {
        "action": "final",
        "text": "hi",
    }


def test_extract_fenced_object():
    raw = '```json\n{"action":"tool_call","name":"x","input":{"a":1}}\n```'
    assert _extract_json_object(raw) == {
        "action": "tool_call",
        "name": "x",
        "input": {"a": 1},
    }


def test_extract_object_with_surrounding_prose():
    raw = 'Sure, here you go: {"action":"final","text":"done"} hope that helps'
    assert _extract_json_object(raw) == {"action": "final", "text": "done"}


def test_extract_object_with_nested_braces_and_strings():
    raw = '{"action":"tool_call","name":"f","input":{"q":"a } b","n":{"x":1}}}'
    assert _extract_json_object(raw) == {
        "action": "tool_call",
        "name": "f",
        "input": {"q": "a } b", "n": {"x": 1}},
    }


def test_extract_returns_none_on_garbage():
    assert _extract_json_object("no json here at all") is None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_claude_code_is_registered():
    import lingtai.llm  # noqa: F401 — triggers register_all_adapters
    from lingtai.llm.service import LLMService

    # Both the dash and underscore spellings are registered (there is no
    # dash/underscore normalization, and preset_connectivity aliases both).
    assert "claude-code" in LLMService._adapter_registry
    assert "claude_code" in LLMService._adapter_registry


def test_service_builds_keyless():
    from lingtai.llm.service import LLMService

    svc = LLMService(provider="claude-code", model="sonnet", api_key=None)
    assert isinstance(svc.get_adapter("claude-code"), ClaudeCodeAdapter)


def test_service_builds_keyless_underscore_alias():
    from lingtai.llm.service import LLMService

    svc = LLMService(provider="claude_code", model="sonnet", api_key=None)
    assert isinstance(svc.get_adapter("claude_code"), ClaudeCodeAdapter)


# ---------------------------------------------------------------------------
# send(): tool call / final / tool results
# ---------------------------------------------------------------------------


def test_send_returns_tool_call():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    out = _envelope('{"action":"tool_call","name":"get_weather","input":{"city":"Paris"}}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        resp = sess.send("weather in paris?")
    assert resp.text == ""
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "get_weather" and tc.args == {"city": "Paris"}
    assert tc.id and tc.id.startswith("cc_")
    # usage mapping: input includes cache read + creation
    assert resp.usage.input_tokens == 160
    assert resp.usage.output_tokens == 20
    assert resp.usage.cached_tokens == 50


def test_send_returns_final_text():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    out = _envelope('{"action":"final","text":"It is sunny."}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        resp = sess.send("hi")
    assert resp.tool_calls == []
    assert resp.text == "It is sunny."


def test_non_json_reply_falls_back_to_final_text():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    out = _envelope("just some prose, no json")
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        resp = sess.send("hi")
    assert resp.tool_calls == []
    assert resp.text == "just some prose, no json"


def test_parallel_tool_calls():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    out = _envelope(
        '{"action":"tool_calls","calls":[{"name":"get_weather","input":{"city":"A"}},'
        '{"name":"get_weather","input":{"city":"B"}}]}'
    )
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        resp = sess.send("two cities")
    assert [c.args["city"] for c in resp.tool_calls] == ["A", "B"]
    assert resp.tool_calls[0].id != resp.tool_calls[1].id


def test_tool_result_roundtrip_updates_interface():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    out1 = _envelope('{"action":"tool_call","name":"get_weather","input":{"city":"Paris"}}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out1)):
        r1 = sess.send("weather?")
    tc = r1.tool_calls[0]
    tr = ad.make_tool_result_message("get_weather", {"temp_c": 18}, tool_call_id=tc.id)
    assert tr.id == tc.id
    out2 = _envelope('{"action":"final","text":"18C"}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out2)):
        r2 = sess.send([tr])
    assert r2.text == "18C"
    roles = [e.role for e in sess.interface._entries]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# Command line + environment
# ---------------------------------------------------------------------------


def test_command_includes_print_json_model_and_disallowed_tools():
    ad = ClaudeCodeAdapter(model="opus")
    sess = ad.create_chat("opus", "sys", None)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _FakeProc(stdout=_envelope('{"action":"final","text":"ok"}'))

    with patch("lingtai.llm.claude_code.adapter.subprocess.run", side_effect=fake_run):
        sess.send("hi")
    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "opus" in cmd
    assert "--disallowedTools" in cmd and "Bash" in cmd
    # prompt is piped via stdin, not argv
    assert captured["kw"]["input"]
    assert "AVAILABLE TOOLS" in captured["kw"]["input"]


def test_env_strips_api_keys_but_keeps_oauth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-keep")
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw["env"]
        return _FakeProc(stdout=_envelope('{"action":"final","text":"ok"}'))

    with patch("lingtai.llm.claude_code.adapter.subprocess.run", side_effect=fake_run):
        sess.send("hi")
    env = captured["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-keep"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_missing_cli_raises_auth_error():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ClaudeCodeAuthError):
            sess.send("hi")


def test_not_logged_in_raises_auth_error():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    proc = _FakeProc(stdout="", stderr="Please run /login to authenticate", returncode=1)
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=proc):
        with pytest.raises(ClaudeCodeAuthError):
            sess.send("hi")


def test_context_overflow_detected_from_stderr():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    proc = _FakeProc(stdout="", stderr="Error: prompt is too long for this model", returncode=1)
    # Overflow recovery will try to trim; with a tiny interface it can't, so it re-raises.
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=proc):
        with pytest.raises(ClaudeCodeContextOverflow):
            sess.send("hi")


def test_generic_cli_error_raises():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    proc = _FakeProc(stdout="", stderr="some unexpected failure", returncode=2)
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=proc):
        with pytest.raises(ClaudeCodeError):
            sess.send("hi")


def test_is_quota_error():
    ad = ClaudeCodeAdapter(model="sonnet")
    assert ad.is_quota_error(Exception("hit usage limit")) is True
    assert ad.is_quota_error(Exception("429 too many requests")) is True
    assert ad.is_quota_error(Exception("some other error")) is False


# ---------------------------------------------------------------------------
# Interface rollback: a failed turn must not leave the just-added user /
# tool-result message in the canonical history.
# ---------------------------------------------------------------------------


def _entry_kinds(interface):
    """(role, [block-type-name...]) per entry — easy to compare in asserts."""
    return [
        (e.role, [type(b).__name__ for b in e.content])
        for e in interface._entries
    ]


def test_failed_cli_rolls_back_added_user_message():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    before = _entry_kinds(sess.interface)
    before_len = len(sess.interface._entries)

    proc = _FakeProc(stdout="", stderr="some unexpected failure", returncode=2)
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=proc):
        with pytest.raises(ClaudeCodeError):
            sess.send("weather in paris?")

    # The user message added at the top of send() must be gone again.
    assert len(sess.interface._entries) == before_len
    assert _entry_kinds(sess.interface) == before
    assert not sess.interface.has_pending_tool_calls()


def test_failed_cli_rolls_back_added_tool_results():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    # Turn 1: succeed and leave a pending tool call.
    out1 = _envelope('{"action":"tool_call","name":"get_weather","input":{"city":"Paris"}}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out1)):
        r1 = sess.send("weather?")
    tc = r1.tool_calls[0]
    snapshot = _entry_kinds(sess.interface)
    snap_len = len(sess.interface._entries)

    # Turn 2: deliver the tool result, but the CLI fails. The tool-result
    # user entry must not survive the failure.
    tr = ad.make_tool_result_message("get_weather", {"temp_c": 18}, tool_call_id=tc.id)
    proc = _FakeProc(stdout="", stderr="some unexpected failure", returncode=2)
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=proc):
        with pytest.raises(ClaudeCodeError):
            sess.send([tr])

    assert len(sess.interface._entries) == snap_len
    assert _entry_kinds(sess.interface) == snapshot


def test_pre_request_hook_failure_rolls_back():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    before = _entry_kinds(sess.interface)
    before_len = len(sess.interface._entries)

    def boom(_interface):
        raise RuntimeError("hook exploded")

    sess.pre_request_hook = boom
    # subprocess.run must never be reached, but patch it so a leak would be loud.
    with patch("lingtai.llm.claude_code.adapter.subprocess.run") as run:
        with pytest.raises(RuntimeError, match="hook exploded"):
            sess.send("hi")
        run.assert_not_called()

    assert len(sess.interface._entries) == before_len
    assert _entry_kinds(sess.interface) == before


def test_successful_send_does_not_roll_back():
    """Guard: the rollback path must not fire on the happy path."""
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", [_weather_tool()])
    out = _envelope('{"action":"final","text":"sunny"}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        sess.send("hi")
    roles = [e.role for e in sess.interface._entries]
    assert roles == ["system", "user", "assistant"]


# ---------------------------------------------------------------------------
# Overflow recovery: a successful recovery must inject a [kernel] notice and
# still return the final assistant response.
# ---------------------------------------------------------------------------


def _seed_conversation(sess, turns=3):
    """Run a few successful final turns so the interface has trimmable history."""
    out = _envelope('{"action":"final","text":"ok"}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        for i in range(turns):
            sess.send(f"message {i}")


def test_successful_overflow_recovery_injects_notice():
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    _seed_conversation(sess, turns=3)
    before = len(sess.interface._entries)
    assert before > 2  # enough non-system entries that a trim can drop one

    overflow = _FakeProc(stdout="", stderr="Error: prompt is too long", returncode=1)
    success = _FakeProc(stdout=_envelope('{"action":"final","text":"recovered"}'))
    # First CLI call overflows; after the kernel trims, the retry succeeds.
    with patch(
        "lingtai.llm.claude_code.adapter.subprocess.run",
        side_effect=[overflow, success],
    ):
        resp = sess.send("the message that overflows")

    # The final response is still returned.
    assert resp.text == "recovered"
    assert resp.tool_calls == []

    # A [kernel] overflow notice was injected as a user entry, and it sits
    # before the recorded assistant response (notice, then assistant).
    texts = [
        b.text
        for e in sess.interface._entries
        for b in e.content
        if isinstance(b, TextBlock)
    ]
    notice = [t for t in texts if t.startswith("[kernel] Context exceeded")]
    assert len(notice) == 1
    # Tail is the assistant response; the entry just before it is the notice.
    assert sess.interface._entries[-1].role == "assistant"
    assert sess.interface._entries[-2].role == "user"
    assert sess.interface._entries[-2].content[0].text.startswith("[kernel] Context exceeded")


def test_no_overflow_means_no_notice():
    """Guard: a clean turn (0 rounds) injects no overflow notice."""
    ad = ClaudeCodeAdapter(model="sonnet")
    sess = ad.create_chat("sonnet", "sys", None)
    out = _envelope('{"action":"final","text":"fine"}')
    with patch("lingtai.llm.claude_code.adapter.subprocess.run", return_value=_FakeProc(stdout=out)):
        sess.send("hi")
    texts = [
        b.text
        for e in sess.interface._entries
        for b in e.content
        if isinstance(b, TextBlock)
    ]
    assert not any(t.startswith("[kernel] Context exceeded") for t in texts)
