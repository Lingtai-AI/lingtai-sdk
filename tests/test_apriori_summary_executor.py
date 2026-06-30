"""End-to-end ToolExecutor tests for the a-priori (``summary=true``) path.

These drive the real ``ToolExecutor.execute`` with a stub dispatch and a stub
summarizer (no real LLM) to prove the contract:

* ``summary=false`` / absent → the wire result is the raw result (unchanged
  behavior) and the durable log records the raw result.
* ``summary=true`` under cap → the wire result is the generated summary; the raw
  is still durably logged (preserved by ``tool_call_id``) BEFORE replacement.
* ``summary=true`` over the 500k cap → the wire result is a refusal that names
  the cap and points at the preserved raw; the summarizer LLM is never called.
* The ``_build_apriori_summarizer_fn`` factory degrades to ``None`` when the
  service has no ``generate`` gateway.
"""
from __future__ import annotations

from lingtai_kernel.base_agent import turn
from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_executor import ToolExecutor
from lingtai_kernel.tool_result_summary import (
    APRIORI_SUMMARY_CAP,
    APRIORI_SUMMARY_MARKER,
)


def _make_executor(*, dispatch_fn, summarizer_fn, events, tmp_path):
    """Construct a ToolExecutor that records durable log events into *events*."""

    def logger_fn(event_type, **fields):
        events.append((event_type, fields))

    def make_tool_result_fn(name, result, **kw):
        # Mirror the provider factory shape just enough for assertions: the
        # model-visible content is whatever we hand the wire.
        return {"role": "tool", "name": name, "content": result, **kw}

    return ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=make_tool_result_fn,
        guard=LoopGuard(),
        known_tools={"bash", "grep", "read"},
        logger_fn=logger_fn,
        working_dir=tmp_path,
        summarizer_fn=summarizer_fn,
    )


def _wire_content(result_msg):
    return result_msg["content"]


def _raw_logged(events, *, needle):
    """True iff some durable ``tool_result`` event carried the raw needle."""
    for event_type, fields in events:
        if event_type == "tool_result":
            if needle in str(fields.get("result")):
                return True
    return False


def _event_fields(events, event_type):
    """Return the fields dict of the first event of *event_type*, or None."""
    for et, fields in events:
        if et == event_type:
            return fields
    return None


def test_summary_false_returns_raw_and_logs_raw(tmp_path):
    raw = {"stdout": "RAWMARKER-" + "x" * 50}
    events = []
    ex = _make_executor(
        dispatch_fn=lambda tc: raw,
        summarizer_fn=lambda sp, up, tn, cid: "SHOULD NOT RUN",
        events=events,
        tmp_path=tmp_path,
    )
    results, intercepted, _ = ex.execute(
        [ToolCall(name="bash", args={"command": "echo hi", "summary": False}, id="t1")]
    )
    content = _wire_content(results[0])
    # Raw reaches the wire unchanged (default behavior).
    assert "RAWMARKER" in str(content)
    assert not (isinstance(content, dict) and content.get("artifact") == APRIORI_SUMMARY_MARKER)
    assert _raw_logged(events, needle="RAWMARKER")


def test_summary_absent_returns_raw(tmp_path):
    raw = {"stdout": "RAWMARKER2"}
    events = []
    ex = _make_executor(
        dispatch_fn=lambda tc: raw,
        summarizer_fn=lambda sp, up, tn, cid: "SHOULD NOT RUN",
        events=events,
        tmp_path=tmp_path,
    )
    results, _, _ = ex.execute(
        [ToolCall(name="grep", args={"pattern": "foo"}, id="t2")]
    )
    assert "RAWMARKER2" in str(_wire_content(results[0]))


def test_summary_true_under_cap_replaces_with_summary_and_preserves_raw(tmp_path):
    raw = {"stdout": "RAWSECRET-" + "y" * 200}
    seen = {}

    def summarizer(system_prompt, user_prompt, tool_name, tool_call_id):
        seen["user_prompt"] = user_prompt
        seen["tool_name"] = tool_name
        return "GENSUMMARY: command printed 200 ys"

    events = []
    ex = _make_executor(
        dispatch_fn=lambda tc: raw,
        summarizer_fn=summarizer,
        events=events,
        tmp_path=tmp_path,
    )
    results, _, _ = ex.execute(
        [ToolCall(
            name="bash",
            args={"command": "yes | head", "summary": True,
                  "reasoning": "How many ys were printed?"},
            id="t3",
        )]
    )
    content = _wire_content(results[0])
    # Wire result is the generated summary, NOT the raw.
    assert isinstance(content, dict)
    assert content["artifact"] == APRIORI_SUMMARY_MARKER
    assert content["generated_summary"] == "GENSUMMARY: command printed 200 ys"
    assert "RAWSECRET" not in str(content)
    # Locator points at the preserved raw by tool_call_id.
    assert "t3" in content["retrieval_hint"]
    assert "events.jsonl" in content["retrieval_hint"]
    # The reasoning drove the summary, and the raw was fed to the summarizer.
    assert "How many ys were printed?" in seen["user_prompt"]
    assert "RAWSECRET" in seen["user_prompt"]
    # The RAW result was durably logged before replacement (preservation).
    assert _raw_logged(events, needle="RAWSECRET")
    # Structured machine-readable locator points at the preserved raw, and the
    # summary-input metadata records the full untruncated input.
    loc = content["raw_locator"]
    assert loc["tool_call_id"] == "t3"
    assert loc["log"] == "logs/events.jsonl"
    assert loc["event_type"] == "tool_result"
    assert "t3" in loc["query"]
    assert content["summary_input_chars"] == content["original_visible_chars"]
    assert content["summary_input_truncated"] is False
    # The success lifecycle event records the actual model-visible summary text
    # (not just its char count) so event-log replay / TUI can render it. The raw
    # is NOT carried in this event.
    gen = _event_fields(events, "apriori_summary_generated")
    assert gen is not None
    assert gen["generated_summary"] == "GENSUMMARY: command printed 200 ys"
    assert gen["summary_chars"] == len("GENSUMMARY: command printed 200 ys")
    assert gen["tool_call_id"] == "t3"
    assert gen["tool_name"] == "bash"
    assert "RAWSECRET" not in str(gen)


def test_summary_true_over_cap_refuses_without_llm_and_hides_raw(tmp_path):
    raw = {"stdout": "BIGRAW-" + "z" * (APRIORI_SUMMARY_CAP + 100)}
    called = {"n": 0}

    def summarizer(system_prompt, user_prompt, tool_name, tool_call_id):
        called["n"] += 1
        return "should not happen"

    events = []
    ex = _make_executor(
        dispatch_fn=lambda tc: raw,
        summarizer_fn=summarizer,
        events=events,
        tmp_path=tmp_path,
    )
    results, _, _ = ex.execute(
        [ToolCall(name="read", args={"file_path": "/big", "summary": True,
                                     "reasoning": "r"}, id="t4")]
    )
    content = _wire_content(results[0])
    assert called["n"] == 0  # LLM never called over cap
    assert isinstance(content, dict)
    assert content["artifact"] == APRIORI_SUMMARY_MARKER
    assert content["status"] == "summary_unavailable"
    assert content["cap_chars"] == APRIORI_SUMMARY_CAP
    # The oversized raw is NOT dumped into the wire content.
    assert "BIGRAW" not in str(content)
    assert "t4" in content["retrieval_hint"]
    # Structured locator present on the refusal too; no LLM input was consumed.
    assert content["raw_locator"]["tool_call_id"] == "t4"
    assert content["raw_locator"]["log"] == "logs/events.jsonl"
    assert content["summary_input_chars"] == 0
    assert content["summary_input_truncated"] is False
    # Raw still preserved in durable log.
    assert _raw_logged(events, needle="BIGRAW")
    # The cap path never emits a success event and never invents a summary.
    assert _event_fields(events, "apriori_summary_generated") is None
    cap = _event_fields(events, "apriori_summary_cap_refused")
    assert cap is not None
    assert "generated_summary" not in cap


# --- factory: degrades to None without a one-shot session gateway ------------

class _Usage:
    def __init__(self, input_tokens, output_tokens, thinking_tokens=0, cached_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.thinking_tokens = thinking_tokens
        self.cached_tokens = cached_tokens


class _NoSessionService:
    """A service with no usable one-shot gateway at all."""

    model = "m"

    def make_tool_result(self, *a, **k):  # pragma: no cover
        return {}


class _Resp:
    def __init__(self, text="ok", usage=None):
        self.text = text
        self.usage = usage or _Usage(123, 45, 6, 7)


class _FakeSession:
    """Stand-in ChatSession recording the single send it receives."""

    def __init__(self, resp, recorder):
        self._resp = resp
        self._recorder = recorder

    def send(self, content):
        self._recorder["sent"] = content
        return self._resp


class _SessionService:
    """A Codex-shaped service: ``create_session().send()`` works, and a direct
    ``generate()`` is *broken* (raises), exactly like the Codex OAuth backend
    that only serves the Responses endpoint via the session path.
    """

    model = "m"
    _base_url = "https://chatgpt.com/backend-api/codex"

    def __init__(self, resp=None):
        self._resp = resp or _Resp()
        self.create_session_calls: list[dict] = []
        self.recorder: dict = {}

    def create_session(self, *, system_prompt, tools=None, model=None,
                        thinking="default", tracked=True, interface=None,
                        provider=None, **kw):
        self.create_session_calls.append({
            "system_prompt": system_prompt,
            "tools": tools,
            "tracked": tracked,
            "provider": provider,
            "interface": interface,
        })
        return _FakeSession(self._resp, self.recorder)

    def generate(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("service.generate must not be used for Codex summary")


class _Config:
    def __init__(self, provider="codex", model="m", retry_timeout=60):
        self.provider = provider
        self.model = model
        self.retry_timeout = retry_timeout


class _AgentStub:
    def __init__(self, service, *, working_dir=None, agent_name="stub",
                 provider="codex"):
        self.service = service
        self._working_dir = working_dir
        self.agent_name = agent_name
        self._config = _Config(provider=provider)
        self._chat = None

    def _log(self, *a, **k):
        pass


def test_summarizer_factory_none_without_session_gateway():
    fn = turn._build_apriori_summarizer_fn(_AgentStub(_NoSessionService()))
    assert fn is None


def test_summarizer_uses_session_send_not_generate():
    """The a-priori summarizer must drive a one-shot ``create_session().send()``
    (the supported Responses path) — NOT ``service.generate`` (which on the
    Codex/ChatGPT OAuth backend hits ``/chat/completions`` and is rejected with
    a Cloudflare challenge / PermissionDeniedError). Regression for the live
    failure observed on PR #586.
    """
    service = _SessionService(_Resp(text="GENERATED"))
    agent = _AgentStub(service)
    fn = turn._build_apriori_summarizer_fn(agent)
    assert fn is not None

    out = fn("SYS", "USERPROMPT", "bash", "tcid")
    assert out == "GENERATED"

    # Exactly one untracked one-shot session was created, scoped to the agent's
    # provider, with no tools, and the summarizer system prompt.
    assert len(service.create_session_calls) == 1
    call = service.create_session_calls[0]
    assert call["tracked"] is False
    assert call["provider"] == "codex"
    assert call["tools"] is None
    assert call["system_prompt"] == "SYS"
    # The user prompt was sent on that session.
    assert service.recorder["sent"] == "USERPROMPT"


def _read_ledger_rows(ledger_path):
    import json

    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def test_apriori_summary_writes_main_ledger_row(tmp_path):
    """A successful a-priori summarizer call writes exactly one main-ledger row
    tagged source=summarize_apriori, with input/output matching response.usage,
    correlatable by tool_call_id, and NOT a daemon row."""
    from lingtai_kernel.token_ledger import is_daemon_entry

    agent = _AgentStub(_SessionService(), working_dir=tmp_path)
    fn = turn._build_apriori_summarizer_fn(agent)
    assert fn is not None

    out = fn("sys", "user", "grep", "toolu_ledger")
    assert out == "ok"

    ledger_path = tmp_path / "logs" / "token_ledger.jsonl"
    rows = _read_ledger_rows(ledger_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == turn.APRIORI_SUMMARY_LEDGER_SOURCE == "summarize_apriori"
    assert row["input"] == 123
    assert row["output"] == 45
    assert row["thinking"] == 6
    assert row["cached"] == 7
    assert row["tool_name"] == "grep"
    assert row["tool_call_id"] == "toolu_ledger"
    assert row.get("model") == "m"
    # It lands in the MAIN agent ledger, not a daemon ledger.
    assert not is_daemon_entry(row)


def test_apriori_summary_ledger_failure_does_not_break_summary(tmp_path):
    """A ledger write failure must not break the summary path (fail-open on
    accounting); the closure still returns the summary text."""

    class _BadWorkingDir:
        # ``working_dir / "logs" / ...`` raises, simulating a ledger failure.
        def __truediv__(self, other):
            raise OSError("ledger path explode")

    agent = _AgentStub(_SessionService(), working_dir=_BadWorkingDir())
    fn = turn._build_apriori_summarizer_fn(agent)
    assert fn is not None
    # Despite the ledger write blowing up, the summary text is returned.
    assert fn("sys", "user", "bash", "tcid") == "ok"
