"""Tests for the optional Codex endpoint pool (``codex_base_urls``).

PR #495 made the ``codex`` provider honor a single configurable ``base_url``
(absent/blank -> the official endpoint). This adds an OPTIONAL pool: when
``codex_base_urls`` carries 2+ valid endpoints, the adapter chooses one at
*request time* by agent identity + the current ``molt_count`` from
``<working_dir>/.agent.json``. The choice is stable while ``molt_count`` is
unchanged and rotates at a molt boundary (deterministic
``(offset + molt_count) % len``). This shuffles only at molt because a molt
already wipes the wire session, so swapping endpoints crosses no live
continuation state.

Hard invariant: the Codex cache identity
``prompt_cache_key == session_id == thread_id`` (the pure agent-path hash) must
NOT change with endpoint or molt_count — the pool routes off that stable
identity, it does not perturb it.

These tests make NO network calls — ``CodexTokenManager`` is mocked and we
inspect the constructed OpenAI client ``base_url`` (the real end-to-end value).
The ``.agent.json`` is a real temp file (no real molt machinery needed).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import lingtai  # noqa: F401  (registers adapters / loads service module)
from lingtai.llm.service import LLMService

_OFFICIAL_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

_POOL = [
    "http://127.0.0.1:8811/backend-api/codex",
    "http://127.0.0.1:8812/backend-api/codex",
    "http://127.0.0.1:8813/backend-api/codex",
]


def _client_base_url(adapter) -> str:
    """The adapter's effective endpoint, trailing slash normalized away."""
    return str(adapter._client.base_url).rstrip("/")


def _session_base_url(adapter) -> str:
    """The endpoint a freshly created Codex chat session would actually use.

    Creating the chat is what re-points the client at the molt-selected
    endpoint, so read the session the same way a real turn would.
    """
    chat = adapter.create_chat(
        "gpt-5.5", "system prompt", tools=None,
        force_tool_call=False, thinking="high",
    )
    session = getattr(chat, "_session", chat)
    return str(session._ws_base_url).rstrip("/")


def _mock_mgr():
    mgr = mock.patch("lingtai.auth.codex.CodexTokenManager")
    cls = mgr.start()
    cls.return_value.get_access_token.return_value = "fake-token"
    cls.return_value.get_account_id.return_value = None
    return mgr, cls


def _write_agent_json(tmp_path: Path, molt_count) -> Path:
    """Write ``<dir>/.agent.json`` with ``molt_count`` and return the anchor.

    The anchor is the agent's ``init.json`` path (what the host wiring passes as
    ``codex_session_anchor``); ``.agent.json`` is its sibling.
    """
    (tmp_path / ".agent.json").write_text(
        json.dumps({"molt_count": molt_count}), encoding="utf-8"
    )
    return tmp_path / "init.json"


def _adapter_with(tmp_path, *, anchor=None, **defaults):
    """Build a codex adapter via the real service/factory path."""
    bucket = {}
    if anchor is not None:
        bucket["codex_session_anchor"] = str(anchor)
    bucket.update(defaults)
    svc = LLMService(
        provider="codex", model="gpt-5.5",
        provider_defaults={"codex": bucket},
    )
    return svc.get_adapter("codex")


# --- (a) single base_url unchanged ----------------------------------------


def test_no_pool_falls_back_to_single_base_url():
    """Absent ``codex_base_urls`` -> existing single ``base_url`` behavior."""
    mgr, _ = _mock_mgr()
    try:
        pool_url = "http://127.0.0.1:8810/backend-api/codex"
        svc = LLMService(provider="codex", model="gpt-5.5", base_url=pool_url)
        adapter = svc.get_adapter("codex", pool_url)
        assert _client_base_url(adapter) == pool_url
    finally:
        mgr.stop()


def test_no_pool_and_no_base_url_uses_official(tmp_path):
    mgr, _ = _mock_mgr()
    try:
        adapter = _adapter_with(tmp_path)
        assert _client_base_url(adapter) == _OFFICIAL_CODEX_BASE_URL
    finally:
        mgr.stop()


# --- (d) invalid / blank pool entries fall back ----------------------------


def test_blank_pool_entries_fall_back_to_base_url(tmp_path):
    """A pool of only blank entries is ignored -> single ``base_url`` wins."""
    mgr, _ = _mock_mgr()
    try:
        base = "http://127.0.0.1:8810/backend-api/codex"
        svc = LLMService(
            provider="codex", model="gpt-5.5", base_url=base,
            provider_defaults={"codex": {"codex_base_urls": ["", "   ", "\n"]}},
        )
        adapter = svc.get_adapter("codex", base)
        assert _client_base_url(adapter) == base
        assert _session_base_url(adapter) == base
    finally:
        mgr.stop()


def test_single_valid_pool_entry_always_chosen(tmp_path):
    """One valid entry (after stripping blanks) is always selected."""
    mgr, _ = _mock_mgr()
    try:
        only = _POOL[1]
        anchor = _write_agent_json(tmp_path, 7)
        adapter = _adapter_with(
            tmp_path, anchor=anchor,
            codex_base_urls=["", only, "  "],
        )
        assert _session_base_url(adapter) == only
    finally:
        mgr.stop()


def test_pool_accepts_comma_and_newline_string(tmp_path):
    """A comma/newline-separated string is parsed into the pool."""
    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        joined = f"{_POOL[0]} ,\n  {_POOL[1]} ,{_POOL[2]}\n"
        adapter = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=joined)
        chosen = _session_base_url(adapter)
        assert chosen in _POOL
    finally:
        mgr.stop()


# --- (b) deterministic selection from .agent.json molt_count ----------------


def test_selection_is_deterministic_and_stable_for_same_molt(tmp_path):
    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 3)
        a1 = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))
        a2 = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))
        chosen = _session_base_url(a1)
        assert chosen in _POOL
        # Same anchor + same molt_count -> identical choice, repeatedly.
        assert _session_base_url(a2) == chosen
        assert _session_base_url(a1) == chosen
    finally:
        mgr.stop()


def test_selection_rotates_when_molt_count_changes(tmp_path):
    """A live adapter observes ``.agent.json`` molt_count changes at request time.

    The molt path does not rebuild the adapter, so the SAME adapter must pick a
    different endpoint after molt_count advances. Adjacent molts must actually
    move (``(offset + molt_count) % len`` with len == 3).
    """
    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        adapter = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))

        seen = []
        for mc in range(len(_POOL)):
            (tmp_path / ".agent.json").write_text(
                json.dumps({"molt_count": mc}), encoding="utf-8"
            )
            seen.append(_session_base_url(adapter))

        # Adjacent molts move to adjacent (distinct) slots; one full cycle of
        # molt_count hits every endpoint exactly once.
        assert len(set(seen)) == len(_POOL)
        assert seen[0] != seen[1]
        assert seen[1] != seen[2]
    finally:
        mgr.stop()


def test_different_agents_distribute(tmp_path):
    """Stable per-agent offset spreads different agents across the pool."""
    mgr, _ = _mock_mgr()
    try:
        chosen = set()
        for i in range(8):
            d = tmp_path / f"agent{i}"
            d.mkdir()
            anchor = _write_agent_json(d, 0)
            adapter = _adapter_with(d, anchor=anchor, codex_base_urls=list(_POOL))
            chosen.add(_session_base_url(adapter))
        # With 8 agents over 3 endpoints we expect more than one slot used.
        assert len(chosen) > 1
    finally:
        mgr.stop()


# --- (c) explicit codex_molt_count override (no real .agent.json) -----------


def test_explicit_molt_count_override_without_agent_json(tmp_path):
    mgr, _ = _mock_mgr()
    try:
        anchor = tmp_path / "init.json"  # no .agent.json written
        assert not (tmp_path / ".agent.json").exists()

        a0 = _adapter_with(
            tmp_path, anchor=anchor,
            codex_base_urls=list(_POOL), codex_molt_count=0,
        )
        a1 = _adapter_with(
            tmp_path, anchor=anchor,
            codex_base_urls=list(_POOL), codex_molt_count=1,
        )
        c0, c1 = _session_base_url(a0), _session_base_url(a1)
        assert c0 in _POOL and c1 in _POOL
        assert c0 != c1  # adjacent molt counts move
    finally:
        mgr.stop()


def test_override_preferred_over_agent_json(tmp_path):
    """When both are present, the explicit override wins over ``.agent.json``."""
    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        from_file = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))
        with_override = _adapter_with(
            tmp_path, anchor=anchor,
            codex_base_urls=list(_POOL), codex_molt_count=1,
        )
        assert _session_base_url(from_file) != _session_base_url(with_override)
    finally:
        mgr.stop()


def test_invalid_codex_molt_count_override_defaults_to_zero(tmp_path):
    anchor = tmp_path / "agent" / "session"
    invalid = _adapter_with(
        tmp_path, anchor=anchor,
        codex_base_urls=list(_POOL), codex_molt_count="not-an-int",
    )
    zero = _adapter_with(
        tmp_path, anchor=anchor,
        codex_base_urls=list(_POOL), codex_molt_count=0,
    )

    assert _session_base_url(invalid) == _session_base_url(zero)


def test_missing_agent_json_defaults_molt_count_zero(tmp_path):
    """No ``.agent.json`` -> molt_count 0, no exception, deterministic pick."""
    mgr, _ = _mock_mgr()
    try:
        anchor = tmp_path / "init.json"
        no_file = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))
        with_zero = _adapter_with(
            tmp_path, anchor=anchor,
            codex_base_urls=list(_POOL), codex_molt_count=0,
        )
        assert _session_base_url(no_file) == _session_base_url(with_zero)
    finally:
        mgr.stop()


# --- (e) identity stable across endpoints / molt counts ---------------------


def test_cache_identity_stable_across_endpoints_and_molt(tmp_path):
    """prompt_cache_key/session_id/thread_id never move with endpoint/molt."""
    from lingtai.llm.openai.adapter import _codex_session_id

    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        expected_id = _codex_session_id(str(anchor))

        endpoints_seen = set()
        for mc in range(len(_POOL)):
            (tmp_path / ".agent.json").write_text(
                json.dumps({"molt_count": mc}), encoding="utf-8"
            )
            adapter = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))
            endpoints_seen.add(_session_base_url(adapter))

            sid, tid = adapter._resolve_codex_ids("gpt-5.5")
            assert sid == tid == expected_id
            assert adapter._default_prompt_cache_key("gpt-5.5") == expected_id

        # We genuinely varied the endpoint, proving identity stability is not
        # vacuous (the pick actually moved across molts).
        assert len(endpoints_seen) == len(_POOL)
    finally:
        mgr.stop()


# --- manifest-block plumbing reaches the adapter ----------------------------


def test_pool_plumbs_through_manifest_llm_block(tmp_path):
    """``codex_base_urls`` in a manifest ``llm`` block reaches the adapter.

    Exercises the host wiring safelist
    (``_PROVIDER_DEFAULTS_PASS_THROUGH_KEYS``) end to end, not just a
    hand-built ``provider_defaults`` dict.
    """
    from lingtai.llm.service import build_provider_defaults_from_manifest_llm

    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 1)
        llm = {
            "provider": "codex",
            "model": "gpt-5.5",
            "codex_base_urls": list(_POOL),
            "codex_session_anchor": str(anchor),
        }
        defaults = build_provider_defaults_from_manifest_llm(llm, max_rpm=0)
        svc = LLMService(
            provider="codex", model="gpt-5.5", provider_defaults=defaults,
        )
        adapter = svc.get_adapter("codex")
        assert _session_base_url(adapter) in _POOL
    finally:
        mgr.stop()


# --- (f) live endpoint switch drops old continuation state ------------------


def test_live_switch_repoints_client_and_drops_old(tmp_path):
    """A molt-driven endpoint change re-points the client (old one dropped).

    The new session's websocket base and the adapter client both reflect the new
    endpoint; the previous client object is no longer the adapter's client, so
    any live websocket/continuation state it owned cannot be reused.
    """
    mgr, _ = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        adapter = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))

        first = _session_base_url(adapter)
        client_before = adapter._client

        # Advance molt_count until the selection actually changes.
        changed_to = None
        for mc in range(1, len(_POOL) + 1):
            (tmp_path / ".agent.json").write_text(
                json.dumps({"molt_count": mc}), encoding="utf-8"
            )
            cur = _session_base_url(adapter)
            if cur != first:
                changed_to = cur
                break

        assert changed_to is not None and changed_to != first
        # Client re-pointed at the new endpoint; the old client is gone.
        assert _client_base_url(adapter) == changed_to
        assert adapter._client is not client_before
    finally:
        mgr.stop()


def test_live_switch_preserves_refreshed_api_key(tmp_path):
    """A switch must keep the live (refreshed) token, not revert to boot token.

    The factory's OAuth refresh hook mutates ``adapter._client.api_key`` in
    place before each call. Rebuilding the client on an endpoint switch must
    carry that live token forward.
    """
    mgr, cls = _mock_mgr()
    try:
        anchor = _write_agent_json(tmp_path, 0)
        adapter = _adapter_with(tmp_path, anchor=anchor, codex_base_urls=list(_POOL))

        first = _session_base_url(adapter)
        # Simulate the OAuth token having rotated on disk: the factory's
        # pre-call refresh hook (``get_access_token``) now returns a new value,
        # which it writes onto the live client before each create_chat.
        cls.return_value.get_access_token.return_value = "refreshed-token"

        changed = False
        for mc in range(1, len(_POOL) + 1):
            (tmp_path / ".agent.json").write_text(
                json.dumps({"molt_count": mc}), encoding="utf-8"
            )
            if _session_base_url(adapter) != first:
                changed = True
                break

        assert changed
        assert adapter._client.api_key == "refreshed-token"
        assert adapter._client_kwargs.get("api_key") == "refreshed-token"
    finally:
        mgr.stop()
