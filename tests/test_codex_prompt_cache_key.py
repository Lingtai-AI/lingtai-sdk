"""Tests for Codex/OpenAI Responses ``prompt_cache_key`` plumbing.

Codex's ``/backend-api/codex/responses`` endpoint accepts ``prompt_cache_key``
to opt into cross-request prompt caching, but rejects ``prompt_cache_retention``
(``Unsupported parameter``) and content-block ``cache_control`` (``Unknown
parameter``). These tests assert the wire kwargs the session sends:

  * Codex Responses requests carry a stable ``prompt_cache_key``.
  * They never carry ``prompt_cache_retention``.
  * No Anthropic-style ``cache_control`` leaks into input/tools/instructions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    CodexResponsesSession,
    OpenAIResponsesSession,
)
from lingtai_kernel.llm.base import FunctionSchema


@dataclass
class Event:
    type: str
    delta: str | None = None
    item: object | None = None
    response: object | None = None
    item_id: str | None = None
    text: str | None = None


class FakeResponses:
    def __init__(self, events: list[Event]):
        self.events = events
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        yield from self.events


class FakeClient:
    def __init__(self, events: list[Event]):
        self.responses = FakeResponses(events)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed() -> Event:
    return Event(
        "response.completed",
        response=SimpleNamespace(id="resp_fake", usage=_usage()),
    )


def _function_schema() -> FunctionSchema:
    return FunctionSchema(
        name="report_answer",
        description="Report answer",
        parameters={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )


def _expected_init_key(init_path: Path, *, model: str = "gpt-5.5") -> str:
    init_hash = hashlib.sha256(str(init_path.resolve(strict=False)).encode("utf-8")).hexdigest()[:16]
    return f"lingtai-codex:{model}:init-{init_hash}:v2"


def _create_codex_session(
    events: list[Event],
    *,
    model: str = "gpt-5.5",
    agent_init_path: Path | None = None,
):
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
        agent_init_path=agent_init_path,
    )
    adapter._client = FakeClient(events)
    return adapter.create_chat(
        model,
        "system prompt",
        tools=[_function_schema()],
        force_tool_call=True,
        thinking="high",
    )


def _no_cache_control(payload) -> bool:
    """Return True iff ``cache_control`` appears nowhere in ``payload``."""
    return "cache_control" not in json.dumps(payload, default=str)


def test_codex_request_includes_default_prompt_cache_key():
    session = _create_codex_session([_completed()], model="gpt-5.5")

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == "lingtai-codex:gpt-5.5:v1"


def test_codex_request_uses_agent_init_path_hash_when_available(tmp_path):
    init_path = tmp_path / "agent-a" / "init.json"
    session = _create_codex_session(
        [_completed()],
        model="gpt-5.5",
        agent_init_path=init_path,
    )

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == _expected_init_key(init_path)
    assert str(init_path) not in sent["prompt_cache_key"]


def test_codex_agent_init_path_hash_distinguishes_agents(tmp_path):
    init_a = tmp_path / "agent-a" / "init.json"
    init_b = tmp_path / "agent-b" / "init.json"

    session_a = _create_codex_session([_completed()], agent_init_path=init_a)
    session_b = _create_codex_session([_completed()], agent_init_path=init_b)
    session_a.send("hi")
    session_b.send("hi")

    key_a = session_a._client.responses.kwargs[0]["prompt_cache_key"]
    key_b = session_b._client.responses.kwargs[0]["prompt_cache_key"]
    assert key_a == _expected_init_key(init_a)
    assert key_b == _expected_init_key(init_b)
    assert key_a != key_b


def test_codex_request_omits_prompt_cache_retention():
    session = _create_codex_session([_completed()])

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    assert "prompt_cache_retention" not in sent


def test_codex_request_has_no_cache_control_anywhere():
    session = _create_codex_session([_completed()])

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    assert _no_cache_control(sent)


def test_codex_prompt_cache_key_is_stable_across_requests(tmp_path):
    init_path = tmp_path / "agent-a" / "init.json"
    session = _create_codex_session(
        [_completed(), _completed()],
        model="gpt-5.5",
        agent_init_path=init_path,
    )

    session.send("first")
    session.send("second")

    keys = [kw["prompt_cache_key"] for kw in session._client.responses.kwargs]
    assert keys == [_expected_init_key(init_path), _expected_init_key(init_path)]


def test_explicit_prompt_cache_key_overrides_default():
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key="custom-key:v2",
    )

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == "custom-key:v2"


def test_responses_session_omits_cache_key_when_unset():
    """Non-Codex Responses sessions don't send prompt_cache_key unless asked."""
    session = OpenAIResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )

    session.send_stream("hi")

    sent = session._client.responses.kwargs[0]
    assert "prompt_cache_key" not in sent
    assert "prompt_cache_retention" not in sent
