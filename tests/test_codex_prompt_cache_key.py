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

import json
from dataclasses import dataclass
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


def _create_codex_session(events: list[Event], *, model: str = "gpt-5.5"):
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
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


def test_codex_prompt_cache_key_is_stable_across_requests():
    session = _create_codex_session([_completed(), _completed()], model="gpt-5.5")

    session.send("first")
    session.send("second")

    keys = [kw["prompt_cache_key"] for kw in session._client.responses.kwargs]
    assert keys == ["lingtai-codex:gpt-5.5:v1", "lingtai-codex:gpt-5.5:v1"]


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


# ---------------------------------------------------------------------------
# Codex REST cache-affinity headers — session-id / thread-id (issue #378)
# ---------------------------------------------------------------------------


def _create_codex_session_cfg(events, *, model="gpt-5.5", **adapter_kw):
    """Build a Codex session through the adapter with extra config kwargs."""
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
        **adapter_kw,
    )
    adapter._client = FakeClient(events)
    return adapter.create_chat(
        model,
        "system prompt",
        tools=[_function_schema()],
        force_tool_call=True,
        thinking="high",
    )


def test_codex_bare_adapter_omits_session_thread_headers():
    """A bare adapter (no per-agent identity passed down) sends no headers.

    This is the test/standalone path: when nothing supplies the agent path
    (the host wiring normally does), the adapter cannot distinguish agents and
    must not collapse them onto one session/thread, so it stays silent.
    """
    session = _create_codex_session([_completed()], model="gpt-5.5")

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    headers = sent.get("extra_headers") or {}
    assert "session-id" not in headers
    assert "thread-id" not in headers
    # prompt_cache_key behavior is untouched.
    assert sent["prompt_cache_key"] == "lingtai-codex:gpt-5.5:v1"


def test_codex_sends_stable_headers_from_session_anchor():
    session = _create_codex_session_cfg(
        [_completed(), _completed()],
        model="gpt-5.5",
        codex_session_anchor="/agents/alice/init.json",
    )

    session.send("first")
    session.send("second")

    h0 = session._client.responses.kwargs[0]["extra_headers"]
    h1 = session._client.responses.kwargs[1]["extra_headers"]
    # Present, UUID-shaped, and stable across requests of the same session.
    assert h0["session-id"] and h0["thread-id"]
    assert _is_uuid(h0["session-id"]) and _is_uuid(h0["thread-id"])
    assert h0 == h1
    # prompt_cache_key still rides alongside (not broken by the headers).
    assert session._client.responses.kwargs[0]["prompt_cache_key"] == "lingtai-codex:gpt-5.5:v1"


def test_codex_headers_differ_for_different_agents():
    a = _create_codex_session_cfg(
        [_completed()], codex_session_anchor="/agents/alice/init.json"
    )
    b = _create_codex_session_cfg(
        [_completed()], codex_session_anchor="/agents/bob/init.json"
    )

    a.send("x")
    b.send("x")

    ha = a._client.responses.kwargs[0]["extra_headers"]
    hb = b._client.responses.kwargs[0]["extra_headers"]
    assert ha["session-id"] != hb["session-id"]
    assert ha["thread-id"] != hb["thread-id"]


def test_codex_thread_id_varies_by_thread_salt_session_id_stable():
    """Same agent, different last molt time -> same session, new thread."""
    molt0 = _create_codex_session_cfg(
        [_completed()],
        codex_session_anchor="/agents/alice/init.json",
        codex_thread_salt="2026-06-01T00:00:00Z",
    )
    molt1 = _create_codex_session_cfg(
        [_completed()],
        codex_session_anchor="/agents/alice/init.json",
        codex_thread_salt="2026-06-02T00:00:00Z",
    )

    molt0.send("x")
    molt1.send("x")

    h0 = molt0._client.responses.kwargs[0]["extra_headers"]
    h1 = molt1._client.responses.kwargs[0]["extra_headers"]
    assert h0["session-id"] == h1["session-id"]  # session stable across molts
    assert h0["thread-id"] != h1["thread-id"]  # thread changes per molt


def test_codex_explicit_session_id_used_verbatim():
    explicit = "11111111-2222-3333-4444-555555555555"
    session = _create_codex_session_cfg(
        [_completed()], codex_session_id=explicit
    )

    session.send("x")

    headers = session._client.responses.kwargs[0]["extra_headers"]
    assert headers["session-id"] == explicit
    assert _is_uuid(headers["thread-id"])


def test_codex_explicit_session_id_wins_over_anchor():
    explicit = "11111111-2222-3333-4444-555555555555"
    session = _create_codex_session_cfg(
        [_completed()],
        codex_session_id=explicit,
        codex_session_anchor="/agents/alice/init.json",
    )

    session.send("x")

    assert session._client.responses.kwargs[0]["extra_headers"]["session-id"] == explicit


def test_codex_session_headers_can_be_set_directly_on_session():
    """The session accepts session_id/thread_id directly (adapter-independent)."""
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        prompt_cache_key="custom-key:v2",
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        thread_id="ffffffff-0000-1111-2222-333333333333",
    )

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert sent["extra_headers"]["session-id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert sent["extra_headers"]["thread-id"] == "ffffffff-0000-1111-2222-333333333333"
    # prompt_cache_key still sent independently.
    assert sent["prompt_cache_key"] == "custom-key:v2"


def test_codex_bare_session_omits_headers():
    """A directly-constructed session with no ids sends no header (bare/test path)."""
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )

    session.send("hi")

    assert "extra_headers" not in session._client.responses.kwargs[0]


# ---------------------------------------------------------------------------
# Manifest config seam — per-agent identity flows factory -> adapter (#378).
# This is the internal override / testing escape hatch; the default path
# (agent path + last molt time) is covered in the section after this one.
# ---------------------------------------------------------------------------


def test_manifest_config_keys_pass_through_to_provider_defaults():
    """codex_session_id/anchor/thread_salt survive the manifest->defaults map."""
    import lingtai  # noqa: F401  (registers adapters / loads service module)
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    d = build_provider_defaults_from_manifest_llm(
        {
            "provider": "codex",
            "codex_session_anchor": "/agents/alice/init.json",
            "codex_thread_salt": "explicit-salt",
        },
        max_rpm=0,
    )
    assert d["codex"]["codex_session_anchor"] == "/agents/alice/init.json"
    assert d["codex"]["codex_thread_salt"] == "explicit-salt"

    # No codex config and no working_dir -> nothing leaks (historical None).
    assert build_provider_defaults_from_manifest_llm({"provider": "codex"}, max_rpm=0) is None


def test_codex_factory_builds_adapter_with_per_agent_ids():
    """The registered codex factory wires manifest config into resolved ids."""
    from unittest import mock

    import lingtai  # noqa: F401
    from lingtai.llm.service import LLMService

    with mock.patch("lingtai.auth.codex.CodexTokenManager") as mgr_cls:
        mgr_cls.return_value.get_access_token.return_value = "fake-token"

        svc = LLMService(
            provider="codex",
            model="gpt-5.5",
            provider_defaults={
                "codex": {
                    "codex_session_anchor": "/agents/alice/init.json",
                    "codex_thread_salt": "2026-06-03T00:00:00Z",
                }
            },
        )
        sid, tid = svc.get_adapter("codex")._resolve_codex_ids("gpt-5.5")
        assert _is_uuid(sid) and _is_uuid(tid) and sid != tid

        # No config -> the safe default: no per-agent identity, no headers.
        svc2 = LLMService(provider="codex", model="gpt-5.5")
        assert svc2.get_adapter("codex")._resolve_codex_ids("gpt-5.5") == (None, None)


# ---------------------------------------------------------------------------
# Default wiring — agent path + last molt time passed down automatically (#378)
# ---------------------------------------------------------------------------


def _write_molt_summary(working_dir, *, count, ts, created_at):
    """Write a system/summaries/molt_<count>_<ts>.md like the molt machinery.

    ``count`` only shapes the on-disk filename/frontmatter (the real format
    written by _snapshots._write_molt_summary); the thread salt is the
    ``created_at`` last-molt time, never the count.
    """
    summaries = working_dir / "system" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / f"molt_{count}_{ts}.md").write_text(
        f"---\nmolt_count: {count}\ncreated_at: {created_at}\n---\n\nbody",
        encoding="utf-8",
    )


def test_latest_molt_time_reads_created_at_from_newest_summary(tmp_path):
    from lingtai.llm.service import _latest_molt_time

    _write_molt_summary(tmp_path, count=1, ts=1000, created_at="2026-01-01T00:00:00Z")
    _write_molt_summary(tmp_path, count=2, ts=2000, created_at="2026-06-01T12:00:00Z")

    # Newest by filename ts wins, and we read its frontmatter created_at.
    assert _latest_molt_time(tmp_path) == "2026-06-01T12:00:00Z"


def test_latest_molt_time_falls_back_to_filename_ts(tmp_path):
    from datetime import datetime, timezone

    from lingtai.llm.service import _latest_molt_time

    summaries = tmp_path / "system" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    # No frontmatter created_at -> fall back to the filename unix ts.
    (summaries / "molt_3_1750000000.md").write_text("no frontmatter", encoding="utf-8")

    expected = datetime.fromtimestamp(1750000000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _latest_molt_time(tmp_path) == expected


def test_latest_molt_time_falls_back_to_agent_json_created_at(tmp_path):
    from lingtai.llm.service import _latest_molt_time

    # No molt summaries yet -> use .agent.json created_at (birth-stable thread).
    (tmp_path / ".agent.json").write_text(
        json.dumps({"created_at": "2026-05-05T05:05:05Z"}), encoding="utf-8"
    )
    assert _latest_molt_time(tmp_path) == "2026-05-05T05:05:05Z"


def test_latest_molt_time_none_when_no_source(tmp_path):
    from lingtai.llm.service import _latest_molt_time

    assert _latest_molt_time(tmp_path) is None


def test_default_wiring_injects_agent_path_and_last_molt_salt(tmp_path):
    """Codex defaults: session anchor = resolved init.json path; salt = molt time."""
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    _write_molt_summary(tmp_path, count=1, ts=1000, created_at="2026-06-01T12:00:00Z")

    d = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )
    assert d["codex"]["codex_session_anchor"] == str((tmp_path / "init.json").resolve())
    assert d["codex"]["codex_thread_salt"] == "2026-06-01T12:00:00Z"


def test_default_wiring_uses_birth_salt_before_first_molt(tmp_path):
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    # No molt summaries, no birth manifest -> stable "birth" salt.
    d = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )
    assert d["codex"]["codex_thread_salt"] == "birth"


def test_default_wiring_only_applies_to_codex(tmp_path):
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    d = build_provider_defaults_from_manifest_llm(
        {"provider": "openai"}, max_rpm=0, working_dir=tmp_path
    )
    # Non-codex providers get no codex identity injected (None when otherwise empty).
    assert d is None


def test_manifest_salt_overrides_default_last_molt_time(tmp_path):
    """Explicit manifest config wins (internal override / testing escape hatch)."""
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    _write_molt_summary(tmp_path, count=1, ts=1000, created_at="2026-06-01T12:00:00Z")

    d = build_provider_defaults_from_manifest_llm(
        {
            "provider": "codex",
            "codex_session_anchor": "/custom/anchor",
            "codex_thread_salt": "override-salt",
        },
        max_rpm=0,
        working_dir=tmp_path,
    )
    assert d["codex"]["codex_session_anchor"] == "/custom/anchor"
    assert d["codex"]["codex_thread_salt"] == "override-salt"


def test_default_wiring_different_agent_paths_yield_different_session_ids(tmp_path):
    """Different agent paths -> different session-id; same path + different last
    molt time -> same session-id but different thread-id."""
    import lingtai  # noqa: F401
    from lingtai.llm.openai.adapter import _codex_session_id, _codex_thread_id
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    for p in (alice, bob):
        p.mkdir()
        _write_molt_summary(p, count=1, ts=1000, created_at="2026-06-01T12:00:00Z")

    da = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=alice
    )["codex"]
    db = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=bob
    )["codex"]

    sid_a = _codex_session_id(da["codex_session_anchor"])
    sid_b = _codex_session_id(db["codex_session_anchor"])
    assert sid_a != sid_b  # different agent paths -> different session-id

    # Same agent, two different last-molt times -> same session, new thread.
    _write_molt_summary(alice, count=2, ts=2000, created_at="2026-07-01T00:00:00Z")
    da2 = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=alice
    )["codex"]
    sid_a2 = _codex_session_id(da2["codex_session_anchor"])
    assert sid_a2 == sid_a  # session-id stable across molts (same path)
    tid_a = _codex_thread_id(sid_a, da["codex_thread_salt"])
    tid_a2 = _codex_thread_id(sid_a2, da2["codex_thread_salt"])
    assert tid_a != tid_a2  # thread-id rotates with last molt time


def _is_uuid(value: str) -> bool:
    import uuid as _uuid

    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False
