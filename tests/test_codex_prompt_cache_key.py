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
import re
from dataclasses import dataclass
from types import SimpleNamespace

from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    CodexResponsesSession,
    OpenAIResponsesSession,
    _lingtai_user_agent,
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


def test_codex_request_sends_honest_lingtai_identity_headers():
    """Every Codex request carries honest LingTai client-identity headers (#436):
    ``originator: lingtai`` and a ``LingTai/<version>`` User-Agent. We identify
    as LingTai, NOT as the Codex CLI (no ``codex_exec`` impersonation)."""
    session = _create_codex_session([_completed()], model="gpt-5.5")

    session.send("please answer via tool")

    headers = session._client.responses.kwargs[0]["extra_headers"]
    assert headers["originator"] == "lingtai"
    # Normal case resolves the installed package version: ``LingTai/<version>``.
    ua = headers["User-Agent"]
    assert re.fullmatch(r"LingTai/\d+\.\d+.*", ua), f"unexpected User-Agent: {ua!r}"
    # We do NOT impersonate the official Codex CLI.
    assert "codex_exec" not in ua
    assert headers["originator"] != "codex_exec"


def test_codex_caller_headers_override_identity_headers():
    """Caller-supplied ``extra_headers`` override the honest identity defaults
    (identity headers are the base layer; caller wins). Audit gap (#436/#437)."""
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system",
        tools=None,
        tool_choice=None,
        extra_kwargs={"extra_headers": {"originator": "caller", "User-Agent": "Caller/1"}},
    )

    session.send("hi")

    headers = session._client.responses.kwargs[0]["extra_headers"]
    assert headers["originator"] == "caller"
    assert headers["User-Agent"] == "Caller/1"


def test_lingtai_user_agent_falls_back_when_version_unresolvable(monkeypatch):
    """If the package version can't be resolved, the UA degrades to a bare
    ``LingTai`` token rather than raising (#436)."""
    import importlib.metadata as md

    def _boom(_name):
        raise md.PackageNotFoundError("lingtai")

    monkeypatch.setattr(md, "version", _boom)
    assert _lingtai_user_agent() == "LingTai"


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


def test_lone_prompt_cache_key_stays_body_only_no_headers():
    """A cache-key-only session keeps the body key and emits NO headers.

    Header carve-out (#378): ``session_id`` / ``thread_id`` route the backend
    cache slot and must be per-agent. A lone ``prompt_cache_key`` with no
    companion ``session_id``/``thread_id`` declares no per-agent identity (this
    is the bare/no-anchor adapter path, where the key is the shared model-only
    fallback), so it stays a body-only cache key and no headers are promoted —
    otherwise every agent on a model would collapse onto one session/thread.
    """
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
    # The carve-out is about the per-agent slot routers: no session_id / thread_id
    # promoted from a body-only cache key.
    headers = sent.get("extra_headers") or {}
    assert "session_id" not in headers
    assert "thread_id" not in headers
    # The legacy codex-cache-key request header is no longer sent.
    assert "codex-cache-key" not in headers


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
# Codex REST cache-affinity headers — session_id / thread_id (issue #378).
# Underscore keys are mandatory: the Codex backend matches the literal key, so a
# hyphenated session-id / thread-id would lose cache affinity (cache/cost blowup).
# ---------------------------------------------------------------------------


def _create_codex_session_cfg(events, *, model="gpt-5.5", **adapter_kw):
    """Build a Codex session through the adapter with extra config kwargs.

    The Codex cache-affinity id is a PURE hash of the agent path anchor
    (no epoch / no time dependence), so derived ids are deterministic without
    pinning any clock.
    """
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
    assert "session_id" not in headers
    assert "thread_id" not in headers
    # prompt_cache_key behavior is untouched.
    assert sent["prompt_cache_key"] == "lingtai-codex:gpt-5.5:v1"


def test_codex_sends_stable_headers_from_session_anchor():
    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg(
        [_completed(), _completed()],
        model="gpt-5.5",
        codex_session_anchor=anchor,
    )

    session.send("first")
    session.send("second")

    s0 = session._client.responses.kwargs[0]
    s1 = session._client.responses.kwargs[1]
    h0 = s0["extra_headers"]
    h1 = s1["extra_headers"]
    expected = _expected_codex_hash(anchor)
    # Root/main path: session_id == thread_id == prompt_cache_key == 8-char hash.
    assert h0["session_id"] == expected
    assert h0["thread_id"] == expected
    assert s0["prompt_cache_key"] == expected
    # Stable across multiple ordinary sends of the same session.
    assert h0 == h1
    assert s1["prompt_cache_key"] == expected


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
    assert ha["session_id"] != hb["session_id"]
    assert ha["thread_id"] != hb["thread_id"]
    # Each agent's three values are internally identical (the agent-path hash).
    assert ha["session_id"] == ha["thread_id"]
    assert hb["session_id"] == hb["thread_id"]


def test_codex_thread_salt_does_not_split_thread_from_session():
    """A legacy ``codex_thread_salt`` no longer derives a separate thread.

    The salt is accepted as a manifest pass-through for backward compatibility,
    but the root/main thread tracks the session id exactly, so two different
    salts on the same anchor still produce byte-identical session/thread/key.
    """
    anchor = "/agents/alice/init.json"
    salt0 = _create_codex_session_cfg(
        [_completed()],
        codex_session_anchor=anchor,
        codex_thread_salt="api_1000aaaa",
    )
    salt1 = _create_codex_session_cfg(
        [_completed()],
        codex_session_anchor=anchor,
        codex_thread_salt="api_2000bbbb",
    )

    salt0.send("x")
    salt1.send("x")

    expected = _expected_codex_hash(anchor)
    h0 = salt0._client.responses.kwargs[0]["extra_headers"]
    h1 = salt1._client.responses.kwargs[0]["extra_headers"]
    assert h0["session_id"] == h0["thread_id"] == expected
    assert h1["session_id"] == h1["thread_id"] == expected  # salt is irrelevant
    assert salt0._client.responses.kwargs[0]["prompt_cache_key"] == expected
    assert salt1._client.responses.kwargs[0]["prompt_cache_key"] == expected


def test_codex_rest_omits_previous_response_id_with_anchored_thread():
    """Codex REST stays stateless: no previous_response_id even with headers on."""
    anchor = "/agents/alice/init.json"
    session = _create_codex_session_cfg(
        [_completed()],
        model="gpt-5.5",
        codex_session_anchor=anchor,
    )

    session.send("please answer via tool")

    sent = session._client.responses.kwargs[0]
    expected = _expected_codex_hash(anchor)
    assert "previous_response_id" not in sent
    assert sent.get("store") is False
    assert sent["extra_headers"]["thread_id"] == expected  # anchored thread still sent
    # prompt_cache_key matches the same hash alongside the stateless REST contract.
    assert sent["prompt_cache_key"] == expected


def test_codex_explicit_session_id_used_verbatim_for_all_three():
    explicit = "11111111-2222-3333-4444-555555555555"
    session = _create_codex_session_cfg(
        [_completed()], codex_session_id=explicit
    )

    session.send("x")

    sent = session._client.responses.kwargs[0]
    headers = sent["extra_headers"]
    # Explicit id is used byte-identically for session, thread, and cache key.
    assert headers["session_id"] == explicit
    assert headers["thread_id"] == explicit
    assert sent["prompt_cache_key"] == explicit


def test_codex_explicit_prompt_cache_key_override_drives_all_three_with_anchor():
    """An adapter-level ``prompt_cache_key`` override + anchor → all three equal.

    This is the mismatch leak Jason flagged: previously an explicit
    ``prompt_cache_key`` override would diverge from the anchor-derived
    ``session_id``/``thread_id``. Now the override (highest priority) becomes the
    single effective id carried byte-identically by all three levers, so the
    operator's explicit choice can never split the cache slot from the headers.
    """
    override = "operator-chosen-key:v9"
    session = _create_codex_session_cfg(
        [_completed()],
        codex_session_anchor="/agents/alice/init.json",
        prompt_cache_key=override,
    )

    session.send("x")

    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == override
    assert sent["extra_headers"]["session_id"] == override
    assert sent["extra_headers"]["thread_id"] == override


def test_codex_explicit_session_id_wins_over_anchor():
    explicit = "11111111-2222-3333-4444-555555555555"
    session = _create_codex_session_cfg(
        [_completed()],
        codex_session_id=explicit,
        codex_session_anchor="/agents/alice/init.json",
    )

    session.send("x")

    assert session._client.responses.kwargs[0]["extra_headers"]["session_id"] == explicit


def test_codex_session_normalizes_mismatched_ids_to_one_effective_value():
    """Mismatched session/thread/cache-key inputs collapse to ONE effective id.

    Jason's follow-up: the three levers must never be independent. When a
    session is constructed with three different candidate ids, the session
    normalizes them to a single effective affinity id and sends that one value
    byte-identically as ``prompt_cache_key`` / ``session_id`` / ``thread_id``.
    Priority is ``prompt_cache_key`` (the explicit request-body cache-affinity
    key) > ``session_id`` > ``thread_id``.
    """
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
    # prompt_cache_key wins; all three carry that single effective value.
    assert sent["prompt_cache_key"] == "custom-key:v2"
    assert sent["extra_headers"]["session_id"] == "custom-key:v2"
    assert sent["extra_headers"]["thread_id"] == "custom-key:v2"


def test_codex_session_normalization_priority_session_then_thread():
    """With no cache key, ``session_id`` wins over ``thread_id``; both sent equal."""
    session = CodexResponsesSession(
        client=FakeClient([_completed(), _completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        session_id="session-wins",
        thread_id="thread-loses",
    )

    session.send("hi")

    sent = session._client.responses.kwargs[0]
    assert sent["prompt_cache_key"] == "session-wins"
    assert sent["extra_headers"]["session_id"] == "session-wins"
    assert sent["extra_headers"]["thread_id"] == "session-wins"

    # And ``thread_id`` alone becomes the effective id for all three.
    thread_only = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        thread_id="thread-only",
    )
    thread_only.send("hi")
    sent2 = thread_only._client.responses.kwargs[0]
    assert sent2["prompt_cache_key"] == "thread-only"
    assert sent2["extra_headers"]["session_id"] == "thread-only"
    assert sent2["extra_headers"]["thread_id"] == "thread-only"


def test_codex_bare_session_omits_cache_headers_but_sends_identity():
    """A directly-constructed session with no ids sends no cache-affinity header
    (bare/test path), but still sends honest LingTai client-identity headers (#436)."""
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )

    session.send("hi")

    headers = session._client.responses.kwargs[0].get("extra_headers", {})
    # No cache-affinity / cache-key headers on the bare path.
    assert "session_id" not in headers
    assert "thread_id" not in headers
    assert "codex-cache-key" not in headers
    # Honest identity headers are always present.
    assert headers["originator"] == "lingtai"
    assert headers["User-Agent"].startswith("LingTai")


# ---------------------------------------------------------------------------
# Manifest config seam — per-agent identity flows factory -> adapter (#378).
# This is the internal override / testing escape hatch; the default path
# (agent path hash only) is covered in the section after this one.
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
    # The salt remains a legacy pass-through key (no longer drives a thread id).
    assert d["codex"]["codex_thread_salt"] == "explicit-salt"

    # No codex config and no working_dir -> nothing leaks (historical None).
    assert build_provider_defaults_from_manifest_llm({"provider": "codex"}, max_rpm=0) is None


def test_codex_factory_builds_adapter_with_per_agent_ids():
    """The registered codex factory wires the anchor into one shared current id."""
    from unittest import mock

    import lingtai  # noqa: F401
    from lingtai.llm.openai.adapter import _codex_session_id
    from lingtai.llm.service import LLMService

    anchor = "/agents/alice/init.json"
    with mock.patch("lingtai.auth.codex.CodexTokenManager") as mgr_cls:
        mgr_cls.return_value.get_access_token.return_value = "fake-token"

        svc = LLMService(
            provider="codex",
            model="gpt-5.5",
            provider_defaults={
                "codex": {
                    "codex_session_anchor": anchor,
                    # Legacy salt is ignored for thread derivation now.
                    "codex_thread_salt": "2026-06-03T00:00:00Z",
                }
            },
        )
        adapter = svc.get_adapter("codex")
        sid, tid = adapter._resolve_codex_ids("gpt-5.5")
        # session_id == thread_id == a PURE per-agent hash of the anchor
        # (no epoch, no time). The thread tracks the session.
        assert sid == tid == _codex_session_id(anchor)

        # No config -> the safe default: no per-agent identity, no headers.
        svc2 = LLMService(provider="codex", model="gpt-5.5")
        assert svc2.get_adapter("codex")._resolve_codex_ids("gpt-5.5") == (None, None)


# ---------------------------------------------------------------------------
# Default wiring — only the agent path is passed down automatically (#378).
# The adapter hashes it to one stable 8-char value used byte-identically for
# session_id, thread_id, and prompt_cache_key. The default path no longer reads
# the token ledger / last API call id or molt time, so the same working_dir
# always yields the same defaults regardless of ledger contents.
# ---------------------------------------------------------------------------


def _write_token_ledger(working_dir, entries):
    """Write logs/token_ledger.jsonl entries in chronological order."""
    logs = working_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    (logs / "token_ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ledger_entry(api_call_id=None, *, source="main", cached=0):
    entry = {
        "ts": "2026-06-19T00:00:00Z",
        "input": 10,
        "output": 2,
        "thinking": 0,
        "cached": cached,
        "source": source,
    }
    if api_call_id is not None:
        entry["api_call_id"] = api_call_id
    return entry


def test_default_wiring_injects_only_agent_path_anchor(tmp_path):
    """Codex defaults: session anchor = resolved init.json path; no salt injected."""
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    d = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )
    assert d["codex"]["codex_session_anchor"] == str((tmp_path / "init.json").resolve())
    # The default path no longer injects a thread salt at all.
    assert "codex_thread_salt" not in d["codex"]


def test_default_wiring_is_independent_of_token_ledger(tmp_path):
    """Same working_dir yields the same defaults regardless of ledger contents."""
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    # No ledger.
    d_none = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )["codex"]

    # A ledger with a fresh API call id appears — must not change anything.
    _write_token_ledger(tmp_path, [_ledger_entry("api_1000aaaa")])
    d_first = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )["codex"]

    # A newer call id rotates in — still no effect.
    _write_token_ledger(tmp_path, [_ledger_entry("api_9000zzzz")])
    d_second = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )["codex"]

    assert d_none == d_first == d_second
    assert "codex_thread_salt" not in d_none


def test_default_wiring_only_applies_to_codex(tmp_path):
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    d = build_provider_defaults_from_manifest_llm(
        {"provider": "openai"}, max_rpm=0, working_dir=tmp_path
    )
    # Non-codex providers get no codex identity injected (None when otherwise empty).
    assert d is None


def test_manifest_anchor_overrides_default(tmp_path):
    """Explicit manifest config wins (internal override / testing escape hatch)."""
    import lingtai  # noqa: F401
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    d = build_provider_defaults_from_manifest_llm(
        {
            "provider": "codex",
            "codex_session_anchor": "/custom/anchor",
            "codex_thread_salt": "override-salt",  # legacy pass-through, still survives
        },
        max_rpm=0,
        working_dir=tmp_path,
    )
    assert d["codex"]["codex_session_anchor"] == "/custom/anchor"
    assert d["codex"]["codex_thread_salt"] == "override-salt"


def test_default_wiring_session_anchor_differs_by_agent_path(tmp_path):
    """Different agent working dirs get different anchors and thus different hashes."""
    import lingtai  # noqa: F401
    from lingtai.llm.openai.adapter import _codex_session_id
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    alice.mkdir()
    bob.mkdir()

    da = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=alice
    )["codex"]
    db = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=bob
    )["codex"]

    assert da["codex_session_anchor"] != db["codex_session_anchor"]
    assert _codex_session_id(da["codex_session_anchor"]) != _codex_session_id(
        db["codex_session_anchor"]
    )


def test_default_wiring_session_id_stable_across_rebuilds(tmp_path):
    """Same agent path -> same 8-char hash on repeated builds (no rotation)."""
    import lingtai  # noqa: F401
    from lingtai.llm.openai.adapter import _codex_session_id
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    # A ledgered call id exists and changes between builds; the hash must not.
    _write_token_ledger(tmp_path, [_ledger_entry("api_1000aaaa")])
    d0 = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )["codex"]

    _write_token_ledger(tmp_path, [_ledger_entry("api_9000zzzz")])
    d1 = build_provider_defaults_from_manifest_llm(
        {"provider": "codex"}, max_rpm=0, working_dir=tmp_path
    )["codex"]

    assert d0["codex_session_anchor"] == d1["codex_session_anchor"]
    assert _codex_session_id(d0["codex_session_anchor"]) == _codex_session_id(
        d1["codex_session_anchor"]
    )


def test_codex_session_id_is_8_char_lowercase_hex_sha256_prefix():
    """The shared id is sha256(anchor).hexdigest()[:8], lowercase hex."""
    import hashlib

    from lingtai.llm.openai.adapter import _codex_session_id

    anchor = "/agents/alice/init.json"
    expected = hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:8]
    got = _codex_session_id(anchor)
    assert got == expected
    assert len(got) == 8
    assert got == got.lower()
    assert all(c in "0123456789abcdef" for c in got)


# ---------------------------------------------------------------------------
# Codex cache-affinity ids ride with usage so token_ledger.jsonl can record them
# (Jason's follow-up to #392). This is not an events.jsonl event.
# ---------------------------------------------------------------------------


def test_codex_usage_extra_carries_cache_affinity_ids_for_token_ledger():
    """Usage metadata exposes the actual sent ids for token-ledger writes.

    After normalization all three record the SAME effective id (no longer
    independent), so the ledger never logs mismatched values.
    """
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

    result = session.send("hi")

    sent = session._client.responses.kwargs[0]
    headers = sent["extra_headers"]
    # The actual ids used this request ride in usage.extra so token_ledger.jsonl
    # can record them — all three the same normalized effective id, so a
    # cache-affinity rotation (Jason's follow-up) is visible too.
    assert result.usage.extra == {
        "codex_session_id": "custom-key:v2",
        "codex_thread_id": "custom-key:v2",
        "codex_prompt_cache_key": "custom-key:v2",
    }
    assert headers["session_id"] == headers["thread_id"] == sent["prompt_cache_key"]
    # The affinity ids are short, non-secret derived values; the request body,
    # messages, and OAuth secret never ride in the usage extra payload.
    blob = json.dumps(result.usage.extra, default=str)
    assert "Bearer" not in blob and "Authorization" not in blob
    assert "system prompt" not in blob
    assert "input" not in result.usage.extra and "messages" not in result.usage.extra


def test_codex_usage_extra_empty_when_no_cache_affinity_headers():
    """Bare/test Codex sessions without ids add no token-ledger id fields."""
    session = CodexResponsesSession(
        client=FakeClient([_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )

    result = session.send("hi")

    # Identity headers are always sent (#436), but no cache-affinity headers and
    # therefore no token-ledger id fields on the bare path.
    headers = session._client.responses.kwargs[0].get("extra_headers", {})
    assert "session_id" not in headers and "thread_id" not in headers
    assert result.usage.extra == {}


def test_token_ledger_entry_merges_usage_extra(tmp_path):
    """BaseAgent token-ledger writes preserve safe provider metadata."""
    from types import SimpleNamespace as _SimpleNamespace

    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.llm.base import UsageMetadata

    class _Workdir:
        def write_manifest(self, manifest):
            pass

    agent = _SimpleNamespace(
        _working_dir=tmp_path,
        _workdir=_Workdir(),
        agent_name="agent",
        get_chat_state=lambda: {"messages": []},
        _build_manifest=lambda: {},
        _write_status_snapshot=lambda: None,
        _last_usage=UsageMetadata(
            input_tokens=10,
            output_tokens=2,
            thinking_tokens=1,
            cached_tokens=8,
            extra={
                "codex_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "codex_thread_id": "ffffffff-0000-1111-2222-333333333333",
            },
        ),
        _session=_SimpleNamespace(_model="gpt-5.5"),
        service=_SimpleNamespace(model="fallback", _base_url="https://chatgpt.com/backend-api/codex"),
    )

    BaseAgent._save_chat_history(agent)

    entry = json.loads((tmp_path / "logs" / "token_ledger.jsonl").read_text())
    assert entry["source"] == "main"
    assert entry["codex_session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert entry["codex_thread_id"] == "ffffffff-0000-1111-2222-333333333333"
    assert entry["input"] == 10 and entry["cached"] == 8


def _expected_codex_hash(anchor: str) -> str:
    """The expected root/main Codex id for a per-agent anchor.

    The id is a PURE ``hash(anchor)`` (8-char lowercase-hex sha256 prefix), used
    byte-identically for ``session_id``, ``thread_id``, and the default
    ``prompt_cache_key``. No epoch, no time dependence — the same anchor always
    yields the same id.
    """
    from lingtai.llm.openai.adapter import _codex_session_id

    return _codex_session_id(anchor)
