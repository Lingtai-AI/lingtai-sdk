"""Tests for meta_block — unified per-turn metadata injection."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from lingtai_kernel.meta_block import (
    GuidanceSchemaError,
    attach_active_notifications,
    attach_active_runtime,
    build_meta,
    build_meta_readme,
    build_runtime_guidance,
    clear_active_notification_holder,
    render_meta,
    stamp_meta,
    validate_runtime_guidance,
)
from lingtai_kernel.llm.interface import ToolResultBlock


def _fake_agent(*, time_awareness: bool = True, timezone_awareness: bool = True):
    """Minimal agent stand-in: build_meta only reads agent._config.*."""
    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=timezone_awareness,
        )
    )


def test_build_meta_time_aware_local_tz_has_offset():
    agent = _fake_agent(time_awareness=True, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" in meta
    ts = meta["current_time"]
    assert not ts.endswith("Z"), f"expected local offset, got {ts!r}"
    assert re.search(r"[+-]\d{2}:\d{2}$", ts), f"no ±HH:MM suffix in {ts!r}"


def test_build_meta_time_aware_utc_uses_z_suffix():
    agent = _fake_agent(time_awareness=True, timezone_awareness=False)
    meta = build_meta(agent)
    assert meta["current_time"].endswith("Z")


def test_build_meta_time_blind_emits_context_sentinel():
    agent = _fake_agent(time_awareness=False)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == -1


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == -1


def test_build_meta_counts_current_tool_result_chars_excluding_meta():
    formal_payload = {"payload": "X" * 1200}
    tool_block = ToolResultBlock(
        id="tc-history",
        name="bash",
        content={
            **formal_payload,
            "_meta": {
                "notifications": {"system": {"body": "N" * 1000}},
                "guidance": {"meta_readme": {}},
            },
        },
    )
    agent = _fake_agent()
    agent._config.context_limit = 1_000_000
    agent._cached_sys_prompt_tokens = 0
    agent._cached_tool_schema_tokens = 0
    agent._session = SimpleNamespace(
        _token_decomp_dirty=False,
        _system_prompt_tokens=0,
        _tools_tokens=0,
        _context_tokens=0,
        _latest_input_tokens=0,
        _tool_schema_tokens=0,
        _context_section_tokens=0,
        chat=SimpleNamespace(
            interface=SimpleNamespace(_entries=[SimpleNamespace(content=[tool_block])]),
            context_window=lambda: 1_000_000,
        ),
    )

    meta = build_meta(agent)

    current = meta["current_tool_result_chars"]
    expected = len(json.dumps(formal_payload, ensure_ascii=False, default=str))
    assert current["_readme"] == (
        "listing top 5 tool result longer than 1000 char; "
        "consider summarize if deemed useless"
    )
    assert current["total_chars"] == expected
    assert current["top_results"] == [{"id": "tc-history", "chars": expected}]


def test_build_meta_readme_mentions_tool_result_char_count_and_summarize():
    readme = build_meta_readme()

    assert "current_tool_result_chars" in readme["agent_meta"]
    assert "top" in readme["agent_meta"]
    assert "summarization" in readme["agent_meta"]


def _fake_agent_with_lang(lang: str, *, time_awareness: bool = True):
    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=True,
            language=lang,
        )
    )


def test_render_meta_empty_dict_returns_empty_string():
    agent = _fake_agent_with_lang("en")
    assert render_meta(agent, {}) == ""


def test_render_meta_en_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: 7.1% (sys 4720 + ctx 9450)]"


def test_render_meta_zh_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"


def test_render_meta_wen_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("wen")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"


def test_render_meta_non_empty_without_current_time_returns_empty():
    # Verifies render_meta ignores keys it doesn't know how to render
    # (neither current_time nor any context field). Produces '' so the
    # caller can omit the prefix entirely.
    agent = _fake_agent_with_lang("en")
    assert render_meta(agent, {"future_field": 123}) == ""


def test_render_meta_context_unknown_sentinel_en():
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        },
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: unavailable]"


def test_render_meta_context_unknown_sentinel_zh():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：未知]"


def test_render_meta_rounds_usage_to_one_decimal():
    """Usage ratios round to one decimal place, not raw float."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "T",
        "context": {
            "system_tokens": 1000,
            "history_tokens": 500,
            "usage": 0.0723456,
        },
    }
    result = render_meta(agent, meta)
    assert "7.2%" in result


def test_stamp_meta_records_pending_snapshot_not_runtime_block():
    # stamp_meta records a transient _runtime_pending snapshot. The real
    # _meta.agent_meta/_meta.guidance is promoted only at the tool-batch boundary by
    # attach_active_runtime (latest-only), so stamp_meta itself never writes
    # _runtime or flat top-level keys.
    result = {"status": "ok"}
    out = stamp_meta(result, {"current_time": "2026-04-20T10:15:23-07:00"}, 42)
    assert out is result  # in-place
    pending = out["_runtime_pending"]
    assert pending["current_time"] == "2026-04-20T10:15:23-07:00"
    assert pending["elapsed_ms"] == 42
    assert out["status"] == "ok"
    # No real _meta envelope and no legacy flat keys at the top level.
    assert "_runtime" not in out
    assert "current_time" not in out
    assert "_elapsed_ms" not in out


def test_stamp_meta_empty_meta_records_nothing():
    # Time-blind case: empty meta ⇒ no pending snapshot, no live _meta block.
    result = {"status": "ok"}
    out = stamp_meta(result, {}, 42)
    assert out is result
    assert "_runtime" not in out
    assert "_runtime_pending" not in out
    assert "current_time" not in out
    assert "_elapsed_ms" not in out
    assert out == {"status": "ok"}


def test_stamp_meta_future_fields_are_carried_in_pending():
    # Forward-compatibility: every key in meta lands in _runtime_pending.
    result = {"status": "ok"}
    meta = {"current_time": "2026-04-20T10:15:23-07:00", "future_field": 123}
    stamp_meta(result, meta, 7)
    pending = result["_runtime_pending"]
    assert pending["future_field"] == 123
    assert pending["current_time"] == "2026-04-20T10:15:23-07:00"
    assert pending["elapsed_ms"] == 7


def test_stamp_meta_elapsed_ms_key_under_pending():
    # elapsed_ms is written as pending["elapsed_ms"] (not _elapsed_ms).
    result = {}
    stamp_meta(result, {"current_time": "T"}, 7)
    assert result["_runtime_pending"]["elapsed_ms"] == 7
    assert "_elapsed_ms" not in result


def _fake_agent_with_session(
    *,
    time_awareness=True,
    timezone_awareness=True,
    language="en",
    system_prompt_tokens=0,
    tools_tokens=0,
    history_tokens=0,
    context_limit=100000,
    decomp_ran=True,
):
    """Agent stand-in that exposes the session state build_meta reads."""
    class _Chat:
        def context_window(self_):
            return 200000  # model default

        class _iface:
            @staticmethod
            def estimate_context_tokens():
                # Real interface.estimate_context_tokens() returns
                # system + tools + conversation — match that contract.
                return system_prompt_tokens + tools_tokens + history_tokens

        interface = _iface()

    chat_obj = _Chat() if decomp_ran else None
    # Server-authoritative wire-count: system + tools + history.
    # This is the invariant our production code relies on
    # (history = latest_input - system - tools).
    latest_input = system_prompt_tokens + tools_tokens + history_tokens

    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=timezone_awareness,
            language=language,
            context_limit=context_limit,
        ),
        _session=SimpleNamespace(
            _system_prompt_tokens=system_prompt_tokens,
            _tools_tokens=tools_tokens,
            _latest_input_tokens=latest_input,
            _token_decomp_dirty=not decomp_ran,
            _chat=chat_obj,
            chat=chat_obj,
        ),
    )


def test_build_meta_emits_context_fields_when_decomp_ran():
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
        context_limit=100000,
    )
    meta = build_meta(agent)
    # system = system_prompt + tools = 5000 + 500 = 5500
    assert meta["context"]["system_tokens"] == 5500
    # history = 200
    assert meta["context"]["history_tokens"] == 200
    # usage = (5500 + 200) / 100000 = 0.057
    assert abs(meta["context"]["usage"] - 0.057) < 1e-6


def test_build_meta_emits_sentinels_before_decomp_runs():
    # When decomposition has never run (dirty flag True) and no chat yet,
    # we cannot compute any of the three fields honestly.
    agent = _fake_agent_with_session(decomp_ran=False)
    meta = build_meta(agent)
    assert meta["context"]["system_tokens"] == -1
    assert meta["context"]["history_tokens"] == -1
    assert meta["context"]["usage"] == -1.0


def test_build_meta_history_falls_back_to_interface_estimate_after_restore():
    """After start() rehydrates the wire ChatInterface from chat_history.jsonl,
    _latest_input_tokens is still 0 until the first LLM call completes. The
    meta-line must fall back to interface.estimate_context_tokens() so the
    first post-refresh text_input shows the restored history, not '对话 0'."""
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=50000,  # restored from JSONL
    )
    # Simulate pre-first-LLM-call state: interface has history but server
    # has not reported an input count yet.
    agent._session._latest_input_tokens = 0
    meta = build_meta(agent)
    # history should come from interface.estimate_context_tokens(), not 0
    assert meta["context"]["history_tokens"] == 50000
    assert meta["context"]["system_tokens"] == 5500


def test_build_meta_time_blind_still_emits_context_fields():
    agent = _fake_agent_with_session(
        time_awareness=False,
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == 5500
    assert meta["context"]["history_tokens"] == 200


def test_render_meta_time_blind_with_context_present_emits_empty_time_slot():
    """Known edge case (documented in spec): a time-blind agent whose session
    has context data produces '[Current time:  | context: ...]' with an empty
    time slot. This is intentional — the spec accepts this and defers a
    time-blind-specific template to a follow-up. If future work changes the
    behavior, this test must be updated together with the spec."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[Current time:  | context: 7.1% (sys 4720 + ctx 9450)]"


def test_build_meta_history_tokens_does_not_double_count_system_and_tools():
    """Regression: history_tokens must NOT include the system prompt or tool
    schema tokens (they belong to system_tokens). Computed from the server's
    authoritative input count minus system + tools, mirroring
    SessionManager.get_token_usage's ctx_history_tokens."""
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    # system_tokens = 5000 + 500 = 5500
    assert meta["context"]["system_tokens"] == 5500
    # history_tokens = history only = 200
    assert meta["context"]["history_tokens"] == 200
    # usage = (5500 + 200) / 100000 = 0.057
    assert abs(meta["context"]["usage"] - 0.057) < 1e-6


def test_build_meta_usage_matches_get_context_pressure_after_restore():
    """Regression: on the very first turn after a restore (before the first
    LLM call returns), the meta-prefix usage% must match what
    SessionManager.get_context_pressure() would report for the same state.
    Otherwise the molt warning and the injected '[... | context: X%]'
    prefix show different numbers on the same turn, confusing the agent.

    Pre-fix bug: build_meta treated estimate_context_tokens() as
    history-only, but the real method returns system + tools + conversation.
    That made history_tokens = full estimate, which then double-counted
    system + tools when added to system_tokens in the usage calculation.
    """
    sys_prompt = 5000
    tools = 500
    history = 50000
    limit = 100000
    agent = _fake_agent_with_session(
        system_prompt_tokens=sys_prompt,
        tools_tokens=tools,
        history_tokens=history,
        context_limit=limit,
    )
    # Simulate post-restore state: wire chat rehydrated from JSONL,
    # but no LLM response has landed yet for this run.
    agent._session._latest_input_tokens = 0
    meta = build_meta(agent)

    # history_tokens must be history-only, not the full estimate
    assert meta["context"]["history_tokens"] == history
    assert meta["context"]["system_tokens"] == sys_prompt + tools

    # meta usage% must equal what get_context_pressure() would return:
    # pressure = estimate_context_tokens() / limit = (sys+tools+history) / limit
    expected_pressure = (sys_prompt + tools + history) / limit
    assert abs(meta["context"]["usage"] - expected_pressure) < 1e-9


# ---------------------------------------------------------------------------
# notifications field removed 2026-05-02 (Task 11 of system-notification-as-
# tool-call redesign). System-source notifications are now delivered as
# synthetic notification(action="check") tool-call pairs spliced by
# BaseAgent._inject_notification_pair (the legacy tc_inbox splice path is
# dormant); see docs/plans/2026-05-02-system-notification-as-tool-call.md. Tests for the
# old inbox-drain path lived here and have been removed alongside the field.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# attach_active_notifications — moving single-slot, latest-result-only stamping.
# ---------------------------------------------------------------------------


def _notif_agent(working_dir):
    """Minimal agent stand-in. ``attach_active_notifications`` reads
    ``agent._working_dir`` and, on successful stamping, commits the
    current notification fingerprint to ``agent._notification_fp`` so
    the IDLE-path synthesized pair does not re-deliver the same state."""
    return SimpleNamespace(_working_dir=working_dir, _notification_fp=())


def _write_email_notif(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "email.json").write_text(
        '{"header": "1 unread", "icon": "📬", "priority": "normal", '
        '"data": {"digest": "Email preview line"}}'
    )


def test_attach_active_notifications_moves_to_latest_and_clears_prior(tmp_path):
    from lingtai_kernel.notifications import notification_fingerprint

    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)
    assert agent._notification_fp == ()

    # First batch: a single dict-shaped tool result, no prior holder.
    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert holder is first.content
    assert "_notifications" not in first.content
    # The canonical notification payload nests under the _meta envelope.
    assert "notifications" not in first.content  # not top-level anymore
    assert first.content["_meta"]["notifications"] == {
        "email": {
            "header": "1 unread",
            "icon": "📬",
            "priority": "normal",
            "data": {"digest": "Email preview line"},
            "notification_guidance": (
                "This notification block comes from the 'email' notification "
                "channel. It is kernel-synchronized state, not necessarily a "
                "human instruction. Identify the source, interpret the channel "
                "payload, and verify intent before deciding whether to act. If "
                "this channel payload is a human message whose preview is "
                "truncated, ambiguous, includes media, or needs exact anchoring, "
                "use the producer channel's normal read action before long work; "
                "acknowledgements and replies go through the communication tool "
                "directly."
            ),
        }
    }
    assert "email" in first.content["_meta"]["notification_guidance"]
    assert "normal read tool" in first.content["_meta"]["notification_guidance"]
    assert "secondary" not in first.content["_meta"]["notification_guidance"]
    # Successful stamping must commit the fingerprint, so the IDLE-path
    # synthesized pair will treat this same state as already delivered.
    expected_fp = notification_fingerprint(tmp_path)
    assert expected_fp != ()
    assert agent._notification_fp == expected_fp

    # Second batch: a new dict result. The old dict must shed the canonical
    # notification payload; only the new dict carries it.
    second = ToolResultBlock(id="t2", name="x", content={"ok": False})
    new_holder = attach_active_notifications(
        agent, [second], prior_holder=holder
    )
    assert new_holder is second.content
    # First holder shed its notification keys (and its now-empty _meta envelope).
    assert "_meta" not in first.content or "notifications" not in first.content["_meta"]
    assert "notifications" in second.content["_meta"]
    assert second.content["_meta"]["notifications"]["email"]["data"] == {"digest": "Email preview line"}


def test_attach_active_notifications_uses_canonical_mcp_payload(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "mcp.telegram.json").write_text(
        '{"header": "2 new events", "icon": "💬", "priority": "normal", '
        '"data": {"previews": ['
        '{"from": "alice", "subject": "hello", "preview": "first body"}, '
        '{"from": "bob", "subject": "status", "preview": "second body"}'
        ']}}'
    )
    agent = _notif_agent(tmp_path)
    block = ToolResultBlock(id="t1", name="x", content={"ok": True})

    attach_active_notifications(agent, [block], prior_holder=None)

    payload = block.content["_meta"]["notifications"]["mcp.telegram"]
    assert "_notifications" not in block.content
    assert payload["data"]["previews"] == [
        {"from": "alice", "subject": "hello", "preview": "first body"},
        {"from": "bob", "subject": "status", "preview": "second body"},
    ]
    assert "'mcp.telegram' notification channel" in payload["notification_guidance"]
    assert "normal read action" in payload["notification_guidance"]
    assert "secondary" not in payload["notification_guidance"]


def test_attach_active_notifications_uses_canonical_system_payload(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "system.json").write_text(
        '{"header": "1 system notification", "icon": "🔔", "priority": "normal", '
        '"data": {"events": ['
        '{"source": "daemon", "body": "Daemon finished with useful details"}'
        ']}}'
    )
    agent = _notif_agent(tmp_path)
    block = ToolResultBlock(id="t1", name="x", content={"ok": True})

    attach_active_notifications(agent, [block], prior_holder=None)

    payload = block.content["_meta"]["notifications"]["system"]
    assert "_notifications" not in block.content
    assert payload["data"]["events"] == [
        {"source": "daemon", "body": "Daemon finished with useful details"}
    ]


def test_attach_active_notifications_uses_canonical_soul_payload(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "soul.json").write_text(
        '{"header": "soul flow", "icon": "🌊", "priority": "normal", '
        '"data": {"voices": ['
        '{"source": "insights", "voice": "Remember to verify by email."}'
        ']}}'
    )
    agent = _notif_agent(tmp_path)
    block = ToolResultBlock(id="t1", name="x", content={"ok": True})

    attach_active_notifications(agent, [block], prior_holder=None)

    payload = block.content["_meta"]["notifications"]["soul"]
    assert "_notifications" not in block.content
    assert payload["data"]["voices"] == [
        {"source": "insights", "voice": "Remember to verify by email."}
    ]


def test_attach_active_notifications_no_active_clears_prior(tmp_path):
    # No `.notification/` directory at all → no active notifications.
    agent = _notif_agent(tmp_path)
    # Pre-existing fingerprint from a hypothetical earlier delivery; the
    # no-active path must NOT touch it (preserves IDLE-path semantics).
    sentinel_fp = (("sentinel.json", 1, 1),)
    agent._notification_fp = sentinel_fp

    # Seed a prior holder as if a previous batch had stamped one (under _meta).
    prior = {"ok": True, "_meta": {"notifications": {"email": {"header": "stale"}}}}
    new_block = ToolResultBlock(id="t1", name="x", content={"ok": "new"})

    result = attach_active_notifications(
        agent, [new_block], prior_holder=prior
    )
    assert result is None
    # Prior shed its notification keys; the empty _meta envelope is dropped.
    assert "_meta" not in prior or "notifications" not in prior["_meta"]
    assert "_meta" not in new_block.content
    # Crucially: with no active notifications, we leave the fp alone so
    # the IDLE-path synthesized pair retains whatever guard state it had.
    assert agent._notification_fp == sentinel_fp


def test_attach_active_notifications_no_target_preserves_fp(tmp_path):
    # Active notifications exist, but no dict-shaped tool result is
    # available to stamp onto (e.g. all results were strings, or the
    # batch is empty). Must NOT commit `_notification_fp` — otherwise
    # the IDLE-path would silently skip delivering this never-seen state.
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)
    sentinel_fp = (("sentinel.json", 1, 1),)
    agent._notification_fp = sentinel_fp

    # Case A: empty batch.
    assert attach_active_notifications(agent, [], prior_holder=None) is None
    assert agent._notification_fp == sentinel_fp

    # Case B: batch with only string-content blocks (no dict target).
    string_only = ToolResultBlock(id="t1", name="x", content="plain text")
    result = attach_active_notifications(
        agent, [string_only], prior_holder=None
    )
    assert result is None
    assert agent._notification_fp == sentinel_fp
    assert string_only.content == "plain text"


def test_attach_active_notifications_picks_latest_dict_in_batch(tmp_path):
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    # A batch with multiple ToolResultBlocks: a dict, then another dict,
    # then a string-content block at the tail. The walk-backward logic
    # should skip the string and land on the *latest* dict (`middle`).
    earlier = ToolResultBlock(id="t1", name="x", content={"k": "earlier"})
    middle = ToolResultBlock(id="t2", name="x", content={"k": "middle"})
    string_tail = ToolResultBlock(id="t3", name="x", content="plain text")

    holder = attach_active_notifications(
        agent, [earlier, middle, string_tail], prior_holder=None
    )

    assert holder is middle.content
    assert "notifications" in middle.content["_meta"]
    assert "_meta" not in earlier.content
    # String content is untouched — and it certainly didn't grow a key.
    assert string_tail.content == "plain text"


# ---------------------------------------------------------------------------
# skeletonize_notification_holder / clear_active_notification_holder — strip
# stale live notification payload while preserving history structure.  Old
# synthesized notification pairs remain as placeholder skeletons; normal tool
# results only lose notification-specific keys.
# ---------------------------------------------------------------------------


def test_clear_active_notification_holder_strips_normal_live_holder():
    # Notification keys live under _meta; stripping them leaves tool_meta and
    # drops the envelope only if it becomes empty.
    stamped = {
        "ok": True,
        "_meta": {
            "tool_meta": {"id": "t1"},
            "notifications": {"email": {"data": {}}},
            "notification_guidance": "live guidance",
        },
    }
    agent = SimpleNamespace(_notification_live_holder=stamped)

    clear_active_notification_holder(agent)

    # tool_meta survives; notification keys are gone.
    assert stamped == {"ok": True, "_meta": {"tool_meta": {"id": "t1"}}}
    assert agent._notification_live_holder is None


def test_clear_active_notification_holder_drops_empty_meta_envelope():
    # When _meta carried only notification keys, the whole envelope is removed.
    stamped = {
        "ok": True,
        "_meta": {
            "notifications": {"email": {"data": {}}},
            "notification_guidance": "live guidance",
        },
    }
    agent = SimpleNamespace(_notification_live_holder=stamped)

    clear_active_notification_holder(agent)

    assert stamped == {"ok": True}
    assert agent._notification_live_holder is None


def test_clear_active_notification_holder_skeletonizes_synthesized_holder():
    synthesized = {
        "_synthesized": True,
        "_meta": {
            "notification_guidance": "live guidance",
            "notifications": {"email": {"data": {"count": 1}}},
        },
        "current_time": "2026-05-13T00:00:00Z",
    }
    agent = SimpleNamespace(_notification_live_holder=synthesized)

    clear_active_notification_holder(agent)

    assert synthesized["_synthesized"] is True
    assert synthesized["_notification_placeholder"] is True
    assert "kernel-synthesized notification(action=check)" in synthesized["message"]
    # Synthesized holder is replaced wholesale with the skeleton — _meta gone.
    assert "_meta" not in synthesized
    assert agent._notification_live_holder is None


def test_clear_active_notification_holder_handles_none_holder():
    agent = SimpleNamespace(_notification_live_holder=None)
    clear_active_notification_holder(agent)
    assert agent._notification_live_holder is None


def test_clear_active_notification_holder_handles_missing_key():
    holder = {"ok": True}  # no notification keys
    agent = SimpleNamespace(_notification_live_holder=holder)
    clear_active_notification_holder(agent)
    assert holder == {"ok": True}
    assert agent._notification_live_holder is None


# ---------------------------------------------------------------------------
# Post-molt active stamping regression.
#
# ``post-molt`` itself is an ordinary notification channel for active stamping.
# The race is narrower: the *same* ``psyche.molt`` result batch that publishes
# post-molt must skip stamping/committing it.  That per-batch deferral lives in
# ``base_agent.turn``; once a later ACTIVE tool batch exists, the post-molt
# notification may be consumed normally.
# ---------------------------------------------------------------------------


def _write_post_molt_notif(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "post-molt.json").write_text(
        '{"header": "post-molt #1 — resume work", "icon": "🌱", '
        '"priority": "high", "data": {"molt_count": 1, '
        '"reminder": "continue the task"}}'
    )


def test_attach_active_notifications_can_stamp_post_molt_after_molt_batch(tmp_path):
    """Post-molt is not globally idle-only; later ACTIVE batches may consume it."""
    from lingtai_kernel.notifications import notification_fingerprint

    _write_post_molt_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    block = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [block], prior_holder=None)

    assert holder is block.content
    assert "post-molt" in block.content["_meta"]["notifications"]
    assert agent._notification_fp == notification_fingerprint(tmp_path)


def test_attach_active_notifications_stamps_post_molt_with_other_channels(tmp_path):
    """Mixed ordinary channels and post-molt stamp together on non-molt batches."""
    from lingtai_kernel.notifications import notification_fingerprint

    _write_email_notif(tmp_path)
    _write_post_molt_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    block = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [block], prior_holder=None)

    assert holder is block.content
    assert "email" in block.content["_meta"]["notifications"]
    assert "post-molt" in block.content["_meta"]["notifications"]
    assert agent._notification_fp == notification_fingerprint(tmp_path)


# ---------------------------------------------------------------------------
# attach_active_runtime — latest-only moving agent/guidance meta (mirrors the
# notification holder).  These cover the acceptance criteria directly:
#   * latest provider-visible result has _meta.agent_meta and _meta.guidance
#   * previous results lose _runtime when a newer dict result exists
#   * active_turn_tool_calls lives under _meta.agent_meta (not top-level)
# ---------------------------------------------------------------------------


def _runtime_agent(*, total_calls: int | None = None):
    """Agent stand-in: attach_active_runtime reads agent._executor.guard.total_calls."""
    guard = SimpleNamespace(total_calls=total_calls) if total_calls is not None else None
    executor = SimpleNamespace(guard=guard) if guard is not None else None
    return SimpleNamespace(_executor=executor)


def _stamped_result(meta, elapsed_ms):
    """A dict result that has been through stamp_meta (carries _runtime_pending)."""
    result = {"status": "ok"}
    stamp_meta(result, meta, elapsed_ms)
    return result


def test_attach_active_runtime_counts_current_batch_tool_result_chars():
    agent = _fake_agent()
    result = {"payload": "batch"}
    stamp_meta(result, build_meta(agent), elapsed_ms=12)
    block = ToolResultBlock(id="tc-batch", name="bash", content=result)

    attach_active_runtime(agent, [block])

    agent_meta = block.content["_meta"]["agent_meta"]
    current = agent_meta["current_tool_result_chars"]
    assert current["total_chars"] == len(
        json.dumps({"payload": "batch"}, ensure_ascii=False, default=str)
    )
    assert current["top_results"] == []


def test_attach_active_runtime_stamps_latest_with_state_and_guidance():
    agent = _runtime_agent(total_calls=3)
    content = _stamped_result({"current_time": "T", "context": {"usage": 0.1}}, 12)
    block = ToolResultBlock(id="t1", name="x", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)

    assert holder is block.content
    meta = block.content["_meta"]
    agent_meta = meta["agent_meta"]
    assert agent_meta["current_time"] == "T"
    assert agent_meta["elapsed_ms"] == 12
    # active_turn_tool_calls is sourced from the guard and lives under agent_meta.
    assert agent_meta["active_turn_tool_calls"] == 3
    # guidance comes from guidance.json (package resource) and validates.
    guidance = meta["guidance"]
    assert guidance["schema_version"] == 1
    # The latest-only meta_readme self-describes the four _meta blocks.
    readme = guidance["meta_readme"]
    assert set(readme) == {"tool_meta", "agent_meta", "guidance", "notifications"}
    assert "every tool result" in readme["tool_meta"].lower()
    assert "latest" in readme["agent_meta"].lower()
    # The transient scaffolding is consumed.
    assert "_runtime_pending" not in block.content
    # No top-level active_turn_tool_calls repetition, and no legacy _runtime key.
    assert "active_turn_tool_calls" not in block.content
    assert "_runtime" not in block.content


def test_attach_active_runtime_moves_to_latest_and_clears_prior():
    agent = _runtime_agent(total_calls=1)

    first_content = _stamped_result({"current_time": "T1"}, 5)
    first = ToolResultBlock(id="t1", name="x", content=first_content)
    holder = attach_active_runtime(agent, [first], prior_holder=None)
    assert "agent_meta" in first.content["_meta"]

    # Second batch: a new dict result takes over. The prior holder must shed
    # its agent_meta/guidance; only the newest result carries them.
    agent = _runtime_agent(total_calls=2)
    second_content = _stamped_result({"current_time": "T2"}, 6)
    second = ToolResultBlock(id="t2", name="x", content=second_content)
    new_holder = attach_active_runtime(agent, [second], prior_holder=holder)

    assert new_holder is second.content
    # previous loses its agent_meta/guidance (envelope dropped when empty)
    assert "_meta" not in first.content or "agent_meta" not in first.content["_meta"]
    assert second.content["_meta"]["agent_meta"]["current_time"] == "T2"
    assert second.content["_meta"]["agent_meta"]["active_turn_tool_calls"] == 2


def test_attach_active_runtime_picks_latest_dict_in_batch():
    agent = _runtime_agent(total_calls=4)
    earlier = ToolResultBlock(id="t1", name="x", content=_stamped_result({"current_time": "E"}, 1))
    middle = ToolResultBlock(id="t2", name="x", content=_stamped_result({"current_time": "M"}, 2))
    string_tail = ToolResultBlock(id="t3", name="x", content="plain text")

    holder = attach_active_runtime(agent, [earlier, middle, string_tail], prior_holder=None)

    assert holder is middle.content
    assert middle.content["_meta"]["agent_meta"]["current_time"] == "M"
    # The earlier dict gets no agent_meta, and its pending scaffolding is stripped.
    assert "_meta" not in earlier.content
    assert "_runtime_pending" not in earlier.content
    assert string_tail.content == "plain text"


def test_attach_active_runtime_empty_meta_yields_no_runtime_but_clears_prior():
    # A time-blind agent's results carry no _runtime_pending (stamp_meta no-op).
    agent = _runtime_agent(total_calls=1)
    prior_content = _stamped_result({"current_time": "T1"}, 5)
    prior = ToolResultBlock(id="t1", name="x", content=prior_content)
    holder = attach_active_runtime(agent, [prior], prior_holder=None)
    assert "agent_meta" in prior.content["_meta"]

    # Next batch: result was NOT stamped (no pending). Prior still loses its blocks.
    blind = ToolResultBlock(id="t2", name="x", content={"status": "ok"})
    new_holder = attach_active_runtime(agent, [blind], prior_holder=holder)

    assert new_holder is None
    assert "_meta" not in prior.content
    assert "_meta" not in blind.content


def test_attach_active_runtime_no_dict_target_clears_prior():
    agent = _runtime_agent(total_calls=1)
    prior_content = _stamped_result({"current_time": "T1"}, 5)
    prior = ToolResultBlock(id="t1", name="x", content=prior_content)
    holder = attach_active_runtime(agent, [prior], prior_holder=None)

    string_only = ToolResultBlock(id="t2", name="x", content="text")
    new_holder = attach_active_runtime(agent, [string_only], prior_holder=holder)

    assert new_holder is None
    assert "_meta" not in prior.content
    assert string_only.content == "text"


def test_attach_active_runtime_omits_counter_when_no_guard():
    agent = _runtime_agent(total_calls=None)  # no executor/guard
    content = _stamped_result({"current_time": "T"}, 9)
    block = ToolResultBlock(id="t1", name="x", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)

    assert holder is block.content
    agent_meta = block.content["_meta"]["agent_meta"]
    assert agent_meta["current_time"] == "T"
    assert "active_turn_tool_calls" not in agent_meta


# ---------------------------------------------------------------------------
# guidance.json schema validation.
# ---------------------------------------------------------------------------


def _valid_guidance():
    return {
        "schema_version": 1,
        "guidance_version": "0.1.0",
        "priority": "tail",
        "render_mode": "latest_tool_result_only",
        "sections": [
            {"id": "a", "title": "A", "body": "body a"},
            {"id": "b", "title": "B", "body": "body b"},
        ],
    }


def test_packaged_guidance_resource_is_valid():
    # The shipped guidance.json must validate — this is the test that catches a
    # malformed packaged resource (build_runtime_guidance degrades silently).
    guidance = build_runtime_guidance()
    assert guidance != {}, "packaged guidance.json failed to load/validate"
    validate_runtime_guidance(guidance)  # must not raise
    ids = [s["id"] for s in guidance["sections"]]
    assert len(ids) == len(set(ids)), "section ids must be unique"
    titles = [s["title"] for s in guidance["sections"]]
    assert len(titles) == len(set(titles)), "section titles must be unique"


def test_validate_runtime_guidance_accepts_well_formed():
    data = _valid_guidance()
    assert validate_runtime_guidance(data) is data


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("schema_version"),
    lambda d: d.pop("sections"),
    lambda d: d.update(schema_version="1"),   # wrong type
    lambda d: d.update(schema_version=True),  # bool is not a valid int here
    lambda d: d.update(priority=""),          # empty string
    lambda d: d.update(sections=[]),          # empty list
    lambda d: d.update(sections="nope"),      # wrong type
])
def test_validate_runtime_guidance_rejects_malformed_top_level(mutate):
    data = _valid_guidance()
    mutate(data)
    with pytest.raises(GuidanceSchemaError):
        validate_runtime_guidance(data)


def test_validate_runtime_guidance_rejects_section_missing_field():
    data = _valid_guidance()
    data["sections"][0].pop("body")
    with pytest.raises(GuidanceSchemaError):
        validate_runtime_guidance(data)


def test_validate_runtime_guidance_rejects_duplicate_section_id():
    data = _valid_guidance()
    data["sections"][1]["id"] = "a"  # duplicate of sections[0].id
    with pytest.raises(GuidanceSchemaError):
        validate_runtime_guidance(data)


def test_validate_runtime_guidance_rejects_duplicate_section_title():
    data = _valid_guidance()
    data["sections"][1]["title"] = "A"  # duplicate of sections[0].title
    with pytest.raises(GuidanceSchemaError):
        validate_runtime_guidance(data)


def test_validate_runtime_guidance_rejects_non_dict():
    with pytest.raises(GuidanceSchemaError):
        validate_runtime_guidance(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# Regression guard for the parent-identified blocker #1: move_runtime_block was
# defined but had NO call site, so _runtime was never injected. attach_active_runtime
# replaces it and MUST be wired into the tool-batch boundary in base_agent.turn.
# This catches a future "function defined but never called" regression cheaply
# without standing up a full turn harness.
# ---------------------------------------------------------------------------


def test_attach_active_runtime_is_wired_into_turn_boundary():
    import inspect
    from lingtai_kernel.base_agent import turn as _turn

    src = inspect.getsource(_turn)
    assert "attach_active_runtime(" in src, (
        "attach_active_runtime must be CALLED at the tool-batch boundary in "
        "base_agent/turn.py — otherwise _runtime is never injected (blocker #1)."
    )
    # The holder attribute the boundary mutates must be referenced too.
    assert "_runtime_live_holder" in src
