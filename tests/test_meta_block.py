"""Tests for meta_block — unified per-turn metadata injection."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
import lingtai_kernel.meta_block as meta_block

from lingtai_kernel.meta_block import (
    GUIDANCE_KEY,
    TOOL_META_TOKEN_USAGE_PENDING_KEY,
    GuidanceSchemaError,
    attach_active_notifications,
    attach_active_runtime,
    build_cache_miss_budget_context,
    build_meta,
    build_meta_guidance,
    build_meta_readme,
    build_molt_context,
    build_notification_payload,
    build_synthetic_meta_envelope,
    build_tool_meta_token_usage,
    build_guidance_with_meta_readme,
    build_runtime_guidance,
    clear_active_notification_holder,
    current_tool_result_chars,
    render_meta,
    slim_adapter_comment_for_tail,
    stamp_meta,
    static_adapter_comment,
    dynamic_adapter_comment,
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


def test_build_meta_time_blind_omits_context_without_warning():
    agent = _fake_agent(time_awareness=False)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert "context" not in meta


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert "context" not in meta


def test_build_meta_includes_adapter_comment_when_chat_provides_one():
    agent = _fake_agent()
    calls = {"legacy": 0, "dynamic": 0}

    def legacy_comment():
        calls["legacy"] += 1
        return {
            "adapter": "fake",
            "summary": "legacy static provider note",
            "cache_note": "legacy static cache prose",
        }

    def dynamic_comment():
        calls["dynamic"] += 1
        return {
            "adapter": "fake",
            "summary": "dynamic summary is not kernel-guessed static",
            "turns_since_epoch_reset": 2,
        }

    agent._session = SimpleNamespace(
        chat=SimpleNamespace(
            adapter_comment=legacy_comment,
            dynamic_adapter_comment=dynamic_comment,
        ),
        _token_decomp_dirty=True,
        _system_prompt_tokens=0,
        _tools_tokens=0,
        _latest_input_tokens=0,
        _update_token_decomposition=lambda: None,
    )

    meta = build_meta(agent)

    tail = meta["adapter_comment"]
    assert calls == {"legacy": 0, "dynamic": 1}
    assert tail["adapter"] == "fake"
    assert tail["summary"] == "dynamic summary is not kernel-guessed static"
    assert tail["turns_since_epoch_reset"] == 2
    assert "cache_note" not in tail
    assert "meta_guidance_ref" not in tail

def test_build_meta_omits_empty_adapter_comment():
    agent = _fake_agent()
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(adapter_comment=lambda: None),
        _token_decomp_dirty=True,
        _system_prompt_tokens=0,
        _tools_tokens=0,
        _latest_input_tokens=0,
        _update_token_decomposition=lambda: None,
    )

    meta = build_meta(agent)

    assert "adapter_comment" not in meta


def test_build_meta_counts_current_tool_result_chars_excluding_meta():
    formal_payload = {"payload": "X" * 1200}
    tool_block = ToolResultBlock(
        id="tc-history",
        name="bash",
        content={
            **formal_payload,
            "_meta": {
                "notifications": {"system": {"body": "N" * 1000}},
                "guidance": {
                    "sections": [
                        {"id": "meta_readme", "title": "_meta envelope readme", "body": ""}
                    ]
                },
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
    assert "_readme" not in current
    assert current["total_chars"] == expected
    assert current["top_results"] == [
        {
            "id": "tc-history",
            "tool_name": "bash",
            "chars": expected,
        }
    ]


def _agent_with_history(blocks):
    """Agent stand-in whose chat history yields the given tool-result blocks."""
    agent = _fake_agent()
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(
            interface=SimpleNamespace(
                _entries=[SimpleNamespace(content=list(blocks))]
            ),
        ),
    )
    return agent


def test_current_tool_result_chars_lists_top_5():
    # 15 prior results of strictly decreasing length; expect the 5 longest.
    blocks = [
        ToolResultBlock(id=f"tc-{i}", name="bash", content="X" * (1500 - i))
        for i in range(15)
    ]
    agent = _agent_with_history(blocks)

    current = current_tool_result_chars(agent)

    assert len(current["top_results"]) == 5
    ids = [entry["id"] for entry in current["top_results"]]
    assert ids == [f"tc-{i}" for i in range(5)]
    assert all(entry["tool_name"] == "bash" for entry in current["top_results"])
    assert all("preview" not in entry for entry in current["top_results"])


def test_current_tool_result_chars_filters_results_at_or_below_1000_chars():
    blocks = [
        ToolResultBlock(id="tc-short", name="bash", content="A" * 1000),
        ToolResultBlock(id="tc-long", name="read", content="B" * 1001),
    ]
    agent = _agent_with_history(blocks)

    current = current_tool_result_chars(agent)

    assert current["top_results"] == [
        {"id": "tc-long", "tool_name": "read", "chars": 1001}
    ]


def test_current_tool_result_chars_entries_include_tool_name_and_no_preview():
    block = ToolResultBlock(id="tc-preview", name="bash", content="Z" * 1200)
    agent = _agent_with_history([block])

    current = current_tool_result_chars(agent)

    assert current["top_results"] == [
        {"id": "tc-preview", "tool_name": "bash", "chars": 1200}
    ]


def test_current_tool_result_chars_tail_omits_readme_and_resident_readme_describes_fields():
    agent = SimpleNamespace(_conversation=[])

    current = current_tool_result_chars(agent)

    assert current["total_chars"] == 0
    assert current["top_results"] == []
    assert "_readme" not in current
    readme = json.dumps(build_meta_readme())
    assert "top_results" in readme
    assert "no preview" in readme
    assert "top 5" not in readme

def test_current_tool_result_chars_readme_is_resident_not_tail_state():
    agent = SimpleNamespace(_conversation=[])

    current = current_tool_result_chars(agent)

    assert "_readme" not in current
    readme = json.dumps(build_meta_readme())
    assert "proactive summarization" in readme
    assert "top_results" in readme
    assert "ids/previews" not in readme

def test_build_meta_readme_mentions_tool_result_char_count_and_summarize():
    readme = build_meta_readme()

    assert "token_usage" in readme["tool_meta"]
    assert "provider-round token/cache snapshot" in readme["tool_meta"]
    # The unified block documents both halves: provider-round + current-session.
    assert "session_cache_rate" in readme["tool_meta"]
    assert "api_calls" in readme["tool_meta"]
    assert "tool_meta.token_usage" in readme["agent_meta"]


def test_build_meta_readme_documents_cache_miss_budget_guard():
    """The resident tool_meta readme must document the cache-miss budget guard:
    the "molt now" warning at context.molt and the cache_miss_budget field."""
    readme = build_meta_readme()
    tool_meta_doc = readme["tool_meta"]
    assert "cache_miss_budget" in tool_meta_doc
    assert "molt now" in tool_meta_doc
    # agent_meta no longer carries a token_efficiency block of its own.
    assert "token_efficiency block" not in readme["agent_meta"]
    assert "current_tool_result_chars" in readme["agent_meta"]
    assert "top" in readme["agent_meta"]
    assert "proactive summarization candidates" in readme["agent_meta"]
    assert "adapter_comment" in readme["agent_meta"]


def test_build_meta_readme_documents_always_on_session_cache_miss_telemetry():
    """The tool_meta readme must tell agents that token_usage carries always-on
    current-session cache-miss/budget fields, and to molt proactively (not
    summarize/reconstruct) when at/nearing budget."""
    tool_meta_doc = build_meta_readme()["tool_meta"]
    # The three always-on field names are documented.
    assert "cache_miss_tokens" in tool_meta_doc
    assert "cache_miss_budget" in tool_meta_doc
    assert "cache_miss_remaining_tokens" in tool_meta_doc
    # And they are described as riding on the session half of token_usage.
    assert "ALWAYS-ON" in tool_meta_doc
    # Jason's proactive-molt guidance is present in spirit.
    lowered = tool_meta_doc.lower()
    assert "molt proactively" in lowered
    assert "reconstruct" in lowered


def test_build_guidance_with_meta_readme_keeps_section_shape_without_packaged_guidance():
    guidance = build_guidance_with_meta_readme({})

    assert guidance["schema_version"] == 1
    assert guidance["guidance_version"] == "runtime-meta-readme"
    assert guidance["render_mode"] == "latest_tool_result_only"
    assert "meta_readme" not in guidance
    assert [section["id"] for section in guidance["sections"]] == ["meta_readme"]


# ---------------------------------------------------------------------------
# meta_guidance — resident system-prompt section + slimmed tail _meta.
# ---------------------------------------------------------------------------


def _meta_guidance_agent(static_comment=None):
    """Agent stand-in whose chat exposes static_adapter_comment()."""
    chat = SimpleNamespace(static_adapter_comment=lambda: static_comment)
    return SimpleNamespace(_session=SimpleNamespace(chat=chat))


def test_static_adapter_comment_reads_chat_static_method():
    agent = _meta_guidance_agent(static_comment={"summary": "adapter rules"})

    comment = static_adapter_comment(agent)

    assert comment == {"summary": "adapter rules"}


def test_dynamic_adapter_comment_prefers_chat_dynamic_method():
    agent = _fake_agent()
    calls = {"legacy": 0, "dynamic": 0}

    def legacy_comment():
        calls["legacy"] += 1
        return {"adapter": "fake", "summary": "legacy static"}

    def dynamic_comment():
        calls["dynamic"] += 1
        return {"adapter": "fake", "next_reset_in": 7}

    agent._session = SimpleNamespace(
        chat=SimpleNamespace(
            adapter_comment=legacy_comment,
            dynamic_adapter_comment=dynamic_comment,
        )
    )

    assert dynamic_adapter_comment(agent) == {"adapter": "fake", "next_reset_in": 7}
    assert calls == {"legacy": 0, "dynamic": 1}

def test_static_adapter_comment_none_without_method():
    agent = SimpleNamespace(_session=SimpleNamespace(chat=SimpleNamespace()))
    assert static_adapter_comment(agent) is None


def test_build_meta_guidance_renders_guidance_meta_readme_and_adapter():
    static_comment = {
        "adapter": "codex",
        "summary": "Codex plans turns as full or incremental.",
        "summarize_note": (
            "Summarize breaks the incremental prefix and opens a fresh full epoch; "
            "it is an investment, so keep the full:incremental ratio at or below "
            "1:10 and defer non-urgent summarize until the savings justify the "
            "cache miss; summarize immediately under high context pressure."
        ),
    }
    agent = _meta_guidance_agent(static_comment)

    section = build_meta_guidance(agent)

    assert isinstance(section, str) and section.strip()
    # Packaged guidance section body is present.
    assert "progressive disclosure" in section
    assert "Delayed summarization reconstruction threshold" in section
    assert "0.75" in section
    assert "Do not call `refresh` just to apply a summarize" in section
    assert "does not mean the active provider-side context" in section
    # meta_readme content (the _meta envelope explanation) is present.
    assert "_meta envelope" in section or "_meta` envelope" in section
    assert "tool_meta" in section
    assert "agent_meta" in section
    assert "Token efficiency state" in section
    assert "Notification handling hook" in section
    assert "Review delegation instruction check" in section
    assert "recent human-channel instructions" in section
    # Static adapter rules are present (the 4 required Codex points).
    assert "full epoch" in section
    assert "1:10" in section


def test_build_meta_guidance_without_adapter_comment_still_renders():
    agent = _meta_guidance_agent(None)
    section = build_meta_guidance(agent)
    assert isinstance(section, str) and section.strip()
    assert "tool_meta" in section


def test_slim_adapter_comment_for_tail_trims_ledger_without_static_key_guessing():
    comment = {
        "adapter": "codex",
        "turns_since_epoch_reset": 3,
        "last_full_api_calls_ago": 2,
        "summary": "dynamic summary that should survive",
        "cache_note": "adapter-owned dynamic value that should survive",
        "summarize_full_note": "adapter-owned dynamic value that should survive",
        "cache_ledger": {
            "rows": [[0, "F", 0.5, 100.0, 50.0, "sum"]],
            "summary": {"api_calls": 1, "cache_rate": 0.5},
        },
        "maintenance_hint": {
            "summarize_economy": "reduce_summarize_frequency",
            "full_to_incremental_ratio": "1:1",
            "reason": "long prose reason",
        },
    }

    slim = slim_adapter_comment_for_tail(comment)

    # Dynamic scalars and arbitrary adapter keys survive: the kernel no longer
    # guesses static-vs-dynamic from Codex-specific key names.
    assert slim["turns_since_epoch_reset"] == 3
    assert slim["last_full_api_calls_ago"] == 2
    assert slim["summary"] == "dynamic summary that should survive"
    assert slim["cache_note"] == "adapter-owned dynamic value that should survive"
    assert slim["summarize_full_note"] == "adapter-owned dynamic value that should survive"
    # The heavy 20-call cache history rows are size-trimmed generically.
    assert "cache_ledger" not in slim
    assert "rows" not in json.dumps(slim)
    assert slim["cache_ledger_summary"] == {"api_calls": 1, "cache_rate": 0.5}
    # maintenance decision survives, long prose reason dropped.
    assert slim["maintenance_hint"]["summarize_economy"] == "reduce_summarize_frequency"
    assert "reason" not in slim["maintenance_hint"]
    # A hook points at the resident meta_guidance section.
    assert "meta_guidance_ref" not in slim

def test_attach_active_runtime_tail_guidance_is_ref_not_full_sections():
    agent = _runtime_agent(total_calls=1)
    content = _stamped_result({"current_time": "T"}, 12)
    block = ToolResultBlock(id="t1", name="x", content=content)

    attach_active_runtime(agent, [block], prior_holder=None)

    guidance = block.content["_meta"][GUIDANCE_KEY]
    # Tail guidance is a lightweight ref/hook, not the full ordered sections.
    assert "sections" not in guidance
    assert "meta_guidance" in guidance.get("ref", "") + json.dumps(guidance)


def test_attach_active_runtime_tail_adapter_comment_has_no_ledger_rows():
    calls = {"legacy": 0, "dynamic": 0}

    def legacy_comment():
        calls["legacy"] += 1
        return {
            "adapter": "codex",
            "summary": "legacy static summary",
            "cache_note": "legacy static prose",
        }

    def dynamic_comment():
        calls["dynamic"] += 1
        return {
            "adapter": "codex",
            "turns_since_epoch_reset": 4,
            "cache_ledger": {
                "rows": [[0, "F", 0.5, 100.0, 50.0, "sum"]],
                "summary": {"api_calls": 1},
            },
            "maintenance_hint": {"non_urgent_summarize": "wait", "reason": "long"},
        }

    agent = _runtime_agent(total_calls=1)
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(
            adapter_comment=legacy_comment,
            dynamic_adapter_comment=dynamic_comment,
        )
    )
    block = ToolResultBlock(
        id="t-adapter", name="x", content=_stamped_result({"current_time": "T"}, 12)
    )

    attach_active_runtime(agent, [block])

    tail = block.content["_meta"]["agent_meta"]["adapter_comment"]
    assert calls == {"legacy": 0, "dynamic": 1}
    assert tail["adapter"] == "codex"
    assert tail["turns_since_epoch_reset"] == 4
    assert "summary" not in tail
    assert "cache_note" not in tail
    assert "cache_ledger" not in tail
    assert "rows" not in json.dumps(tail)
    assert tail["cache_ledger_summary"] == {"api_calls": 1}
    assert "reason" not in tail["maintenance_hint"]
    assert "meta_guidance_ref" not in tail

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


def test_build_meta_omits_numeric_context_fields_when_decomp_ran():
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
        context_limit=100000,
    )
    meta = build_meta(agent)
    assert "context" not in meta
    assert meta_block._current_context_usage(agent) == pytest.approx(0.057)


def test_build_meta_carries_latest_token_usage_for_tool_meta_only():
    # The full provider-round snapshot is the source; only the compact subset is
    # placed into the transit key destined for _meta.tool_meta.token_usage. With
    # no get_token_usage on the agent, only the provider-round half is emitted.
    snapshot = {
        "scope": "provider_round",
        "api_call_index": 3,
        "input_tokens": 190_000,
        "cache_miss_tokens": 22_000,
        "output_tokens": 636,
        "thinking_tokens": 40,
        "cached_tokens": 168_000,
        "cache_rate": 0.882,
        "context_tokens": 190_000,
        "context_window": 250_000,
        "context_usage": 0.759,
        "estimated": False,
        "api_call_id": "call-abc",
    }
    agent = _fake_agent()
    agent._session = SimpleNamespace(
        _token_decomp_dirty=True,
        latest_token_usage_snapshot=lambda: snapshot,
    )

    meta = build_meta(agent)

    assert meta[TOOL_META_TOKEN_USAGE_PENDING_KEY] == {
        "input": 190_000,
        "cache_miss": 22_000,
        "cache_rate": 0.882,
        "context_usage": 0.759,
        "window": 250_000,
        "output": 636,
        "thinking": 40,
        "ref": "See meta_guidance.token_efficiency for details.",
    }
    # The unified token_usage block is the sole token diagnostics carrier; the
    # separate token_efficiency block must be gone.
    assert "token_efficiency" not in meta


# Provider-round half of the unified token_usage block (snapshot-derived).
_PROVIDER_TOKEN_USAGE_KEYS = {
    "input",
    "cache_miss",
    "cache_rate",
    "context_usage",
    "window",
    "output",
    "thinking",
}
# Current-session half of the unified token_usage block (get_token_usage-derived).
# ``cache_miss_tokens`` is always present with the session half (derivable from
# the session counters); ``cache_miss_budget`` / ``cache_miss_remaining_tokens``
# ride along only when a positive-int budget is resolvable from agent._config.
_SESSION_TOKEN_USAGE_KEYS = {
    "session_cache_rate",
    "api_calls",
    "input_tokens",
    "cached_tokens",
    "avg_input_tokens_per_api_call",
    "cache_miss_tokens",
}
# The two budget-derived always-on fields (present only with a configured budget).
_SESSION_CACHE_MISS_BUDGET_KEYS = {
    "cache_miss_budget",
    "cache_miss_remaining_tokens",
}


def test_build_tool_meta_token_usage_compacts_full_snapshot_to_exact_keys():
    # A full provider-round snapshot (the internal-logging shape) must compact to
    # exactly the seven provider keys, dropping scope/api_call_index/
    # cached_tokens/context_tokens/context_window/estimated/api_call_id and the
    # long names. With no get_token_usage, the session half is omitted.
    snapshot = {
        "scope": "provider_round",
        "api_call_index": 3,
        "input_tokens": 190_000,
        "cache_miss_tokens": 22_000,
        "output_tokens": 636,
        "thinking_tokens": 40,
        "cached_tokens": 168_000,
        "cache_rate": 0.882,
        "context_tokens": 190_000,
        "context_window": 250_000,
        "context_usage": 0.759,
        "estimated": False,
        "api_call_id": "call-abc",
    }
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: snapshot)
    )

    compact = build_tool_meta_token_usage(agent)

    assert set(compact) == _PROVIDER_TOKEN_USAGE_KEYS | {"ref"}
    assert compact == {
        "input": 190_000,
        "cache_miss": 22_000,
        "cache_rate": 0.882,
        "context_usage": 0.759,
        "window": 250_000,
        "output": 636,
        "thinking": 40,
        "ref": "See meta_guidance.token_efficiency for details.",
    }


def test_build_tool_meta_token_usage_merges_session_aggregate_into_one_block():
    # The unified block carries BOTH the provider-round half (from the snapshot)
    # and the current-session half (from get_token_usage), in one flat dict —
    # there is no separate token_efficiency block anywhere.
    snapshot = {
        "input_tokens": 190_000,
        "cache_miss_tokens": 22_000,
        "output_tokens": 636,
        "thinking_tokens": 40,
        "cache_rate": 0.882,
        "context_window": 250_000,
        "context_usage": 0.759,
    }
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: snapshot),
        get_token_usage=lambda: {
            "api_calls": 4,
            "input_tokens": 22_000,
            "cached_tokens": 5_500,
        },
    )

    compact = build_tool_meta_token_usage(agent)

    assert set(compact) == _PROVIDER_TOKEN_USAGE_KEYS | _SESSION_TOKEN_USAGE_KEYS | {"ref"}
    assert compact == {
        # provider-round half
        "input": 190_000,
        "cache_miss": 22_000,
        "cache_rate": 0.882,
        "context_usage": 0.759,
        "window": 250_000,
        "output": 636,
        "thinking": 40,
        # current-session half
        "session_cache_rate": 0.25,
        "api_calls": 4,
        "input_tokens": 22_000,
        "cached_tokens": 5_500,
        "avg_input_tokens_per_api_call": 5_500,
        # always-on cache-miss telemetry (no _config -> no budget-derived fields,
        # but cache_miss_tokens is always present: 22_000 - 5_500 = 16_500)
        "cache_miss_tokens": 16_500,
        # short guidance hook
        "ref": "See meta_guidance.token_efficiency for details.",
    }
    # No dropped/noisy keys leak into the unified block — and the hook is the
    # short `ref`, never the long `guidance_ref`.
    for noisy in ("scope", "guidance_ref", "context_tokens", "context_window", "estimated", "api_call_id"):
        assert noisy not in compact


def test_build_tool_meta_token_usage_session_only_when_no_snapshot():
    # When no provider-round snapshot exists but session data does, only the
    # session half is emitted (the block is never invented from nothing).
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: None),
        get_token_usage=lambda: {
            "api_calls": 2,
            "input_tokens": 1_000,
            "cached_tokens": 1_200,  # cached > input clamps to 1.0
        },
    )

    compact = build_tool_meta_token_usage(agent)

    assert set(compact) == _SESSION_TOKEN_USAGE_KEYS | {"ref"}
    assert compact["session_cache_rate"] == 1.0
    assert compact["avg_input_tokens_per_api_call"] == 500


def test_build_tool_meta_token_usage_preserves_zero_and_sentinel_values():
    # Existing numeric zero / sentinel values are kept, not dropped or invented.
    snapshot = {
        "input_tokens": 0,
        "cache_miss_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_rate": 0.0,
        "context_window": 0,
        "context_usage": -1.0,
    }
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: snapshot)
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact == {
        "input": 0,
        "cache_miss": 0,
        "cache_rate": 0.0,
        "context_usage": -1.0,
        "window": 0,
        "output": 0,
        "thinking": 0,
        "ref": "See meta_guidance.token_efficiency for details.",
    }


def test_build_tool_meta_token_usage_robust_to_missing_fields():
    # Partial snapshot: only present fields are emitted; absent ones are omitted
    # rather than invented.
    snapshot = {"input_tokens": 100, "cache_rate": 0.5}
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: snapshot)
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact == {"input": 100, "cache_rate": 0.5, "ref": "See meta_guidance.token_efficiency for details."}


def test_build_meta_folds_session_economy_into_token_usage_not_efficiency():
    agent = _fake_agent_with_session(
        system_prompt_tokens=1000,
        tools_tokens=500,
        history_tokens=5500,
        context_limit=10000,
    )
    agent.get_token_usage = lambda: {
        "api_calls": 4,
        "input_tokens": 22000,
        "cached_tokens": 5500,
        "ctx_total_tokens": 99999,
    }

    meta = build_meta(agent)

    assert "context" not in meta
    # There is NO token_efficiency block anywhere — the session economy now lives
    # inside the unified token_usage transit block (destined for tool_meta).
    assert "token_efficiency" not in meta
    usage = meta[TOOL_META_TOKEN_USAGE_PENDING_KEY]
    assert usage["api_calls"] == 4
    assert usage["input_tokens"] == 22000
    assert usage["cached_tokens"] == 5500
    assert usage["session_cache_rate"] == 0.25
    assert usage["avg_input_tokens_per_api_call"] == 5500
    # Dropped noisy/invalid fields never reappear.
    for noisy in ("scope", "guidance_ref", "context_tokens", "context_window"):
        assert noisy not in usage


def test_build_meta_session_cache_rate_clamps_to_fraction():
    agent = _fake_agent_with_session(
        system_prompt_tokens=100,
        tools_tokens=0,
        history_tokens=900,
        context_limit=2000,
    )
    agent.get_token_usage = lambda: {
        "api_calls": 1,
        "input_tokens": 1000,
        "cached_tokens": 1200,
        "ctx_total_tokens": 1000,
    }

    meta = build_meta(agent)

    assert meta[TOOL_META_TOKEN_USAGE_PENDING_KEY]["session_cache_rate"] == 1.0


def test_synthetic_meta_envelope_shows_token_usage_in_tool_meta_not_agent_meta():
    # /notification synthetic raw meta carries token diagnostics under
    # tool_meta.token_usage when pending/session data is available; agent_meta
    # never carries token diagnostics.
    snapshot = {
        "input_tokens": 190_000,
        "cache_miss_tokens": 22_000,
        "cache_rate": 0.882,
        "context_window": 250_000,
        "context_usage": 0.759,
        "output_tokens": 636,
        "thinking_tokens": 40,
    }
    agent = _fake_agent_with_session()
    agent._session.latest_token_usage_snapshot = lambda: snapshot
    agent.get_token_usage = lambda: {
        "api_calls": 4,
        "input_tokens": 22_000,
        "cached_tokens": 5_500,
    }
    payload = build_notification_payload({"system": {"events": [{"body": "ping"}]}})

    envelope = build_synthetic_meta_envelope(agent, payload, call_id="c1")

    tool_meta = envelope["tool_meta"]
    assert tool_meta["synthetic"] is True
    assert tool_meta["token_usage"]["input"] == 190_000
    assert tool_meta["token_usage"]["session_cache_rate"] == 0.25
    assert tool_meta["token_usage"]["api_calls"] == 4
    # agent_meta must not carry token diagnostics in any form.
    agent_meta = envelope["agent_meta"]
    assert "token_efficiency" not in agent_meta
    assert "token_usage" not in agent_meta
    assert TOOL_META_TOKEN_USAGE_PENDING_KEY not in agent_meta


def test_synthetic_meta_envelope_omits_token_usage_when_no_data():
    # No snapshot and no session usage → no token_usage key on synthetic tool_meta.
    agent = _fake_agent_with_session()
    agent._session.latest_token_usage_snapshot = lambda: None
    payload = build_notification_payload({"system": {"events": [{"body": "ping"}]}})

    envelope = build_synthetic_meta_envelope(agent, payload, call_id="c1")

    assert "token_usage" not in envelope["tool_meta"]


def test_session_economy_prefers_current_session_over_lifetime_totals():
    # The session-stat half MUST come from get_current_session_token_usage()
    # (current runtime deltas), never the lifetime get_token_usage() totals.
    # Lifetime get_token_usage carries giant restored numbers; the injected
    # session stats must be the small current-runtime deltas instead.
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: None),
        get_token_usage=lambda: {
            "api_calls": 27_863,
            "input_tokens": 5_000_000_000,
            "cached_tokens": 4_000_000_000,
        },
        get_current_session_token_usage=lambda: {
            "api_calls": 2,
            "input_tokens": 200,
            "cached_tokens": 40,
            "session_cache_rate": 0.2,
            "avg_input_tokens_per_api_call": 100,
        },
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["api_calls"] == 2
    assert compact["input_tokens"] == 200
    assert compact["cached_tokens"] == 40
    assert compact["avg_input_tokens_per_api_call"] == 100
    assert compact["session_cache_rate"] == 0.2
    # The lifetime giants never leak in.
    assert compact["api_calls"] != 27_863
    assert compact["input_tokens"] != 5_000_000_000


def test_session_economy_falls_back_to_lifetime_getter_when_no_current_session():
    # Compatibility fallback: stubs without get_current_session_token_usage still
    # populate the session half from get_token_usage().
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: None),
        get_token_usage=lambda: {
            "api_calls": 4,
            "input_tokens": 22_000,
            "cached_tokens": 5_500,
        },
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["api_calls"] == 4
    assert compact["input_tokens"] == 22_000
    assert compact["cached_tokens"] == 5_500


def test_token_usage_block_carries_short_guidance_ref():
    # The unified token_usage block always carries a short `ref` hook (NOT
    # `guidance_ref`) — a short sentence, not a bare path — pointing at the
    # resident guidance section.
    snapshot = {"input_tokens": 100, "cache_rate": 0.5}
    agent = SimpleNamespace(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: snapshot)
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["ref"] == "See meta_guidance.token_efficiency for details."
    assert "guidance_ref" not in compact


# ---------------------------------------------------------------------------
# Always-on current-session cache-miss/budget telemetry in the session half of
# token_usage (Jason's follow-up to PR #641).  Distinct from the
# tool_meta.context guard (build_cache_miss_budget_context), which surfaces only
# at/above budget: these three fields ride on EVERY result whenever the session
# aggregate is available so agents can always read current cache miss + budget.
# ---------------------------------------------------------------------------


def _session_agent_with_budget(
    *, input_tokens, cached_tokens, api_calls=1, budget=1_000_000, with_config=True
):
    """SimpleNamespace agent exposing the current-session getter and a budget.

    ``with_config=False`` drops ``_config`` entirely so the config-less-stub
    path (cache_miss_tokens present; budget-derived fields omitted) is exercised.
    """
    kwargs = dict(
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: None),
        get_current_session_token_usage=lambda: {
            "api_calls": api_calls,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
        },
    )
    if with_config:
        kwargs["_config"] = SimpleNamespace(cache_miss_budget=budget)
    return SimpleNamespace(**kwargs)


def test_session_half_always_carries_cache_miss_tokens_and_budget_fields():
    # With a configured budget, all three always-on fields appear even though the
    # cache-miss total is far below budget (contrast the context guard, which
    # would stay silent here).
    agent = _session_agent_with_budget(
        input_tokens=300_000, cached_tokens=100_000, budget=1_000_000
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_tokens"] == 200_000  # 300k - 100k
    assert compact["cache_miss_budget"] == 1_000_000
    assert compact["cache_miss_remaining_tokens"] == 800_000  # 1M - 200k
    # The full session half plus the two budget-derived fields are all present.
    assert (_SESSION_TOKEN_USAGE_KEYS | _SESSION_CACHE_MISS_BUDGET_KEYS) <= set(compact)


def test_session_half_cache_miss_tokens_clamps_to_zero():
    # cached > input (odd provider accounting) -> cache_miss clamps to 0, and
    # remaining is the full budget.
    agent = _session_agent_with_budget(
        input_tokens=100, cached_tokens=500, budget=1_000_000
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_tokens"] == 0
    assert compact["cache_miss_remaining_tokens"] == 1_000_000


def test_session_half_remaining_clamps_to_zero_above_budget():
    # cache_miss above budget -> remaining floors at 0, never negative.  The
    # always-on fields keep reporting even past the guard trip point.
    agent = _session_agent_with_budget(
        input_tokens=1_500_000, cached_tokens=200_000, budget=1_000_000
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_tokens"] == 1_300_000
    assert compact["cache_miss_remaining_tokens"] == 0


def test_session_half_omits_budget_fields_without_config():
    # A config-less stub still gets cache_miss_tokens (session-derivable) but the
    # budget-derived fields are omitted, never invented.
    agent = _session_agent_with_budget(
        input_tokens=300_000, cached_tokens=100_000, with_config=False
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_tokens"] == 200_000
    assert "cache_miss_budget" not in compact
    assert "cache_miss_remaining_tokens" not in compact


def test_session_half_omits_budget_fields_for_nonpositive_budget():
    # A non-positive / non-int / bool budget disables the budget-derived fields,
    # matching build_cache_miss_budget_context semantics; cache_miss_tokens stays.
    for bad in (0, -5, None, True, "1000000"):
        agent = _session_agent_with_budget(
            input_tokens=300_000, cached_tokens=100_000, budget=bad
        )
        compact = build_tool_meta_token_usage(agent)
        assert compact["cache_miss_tokens"] == 200_000
        assert "cache_miss_budget" not in compact
        assert "cache_miss_remaining_tokens" not in compact


def test_session_half_honors_custom_budget():
    agent = _session_agent_with_budget(
        input_tokens=300_000, cached_tokens=100_000, budget=250_000
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_budget"] == 250_000
    assert compact["cache_miss_remaining_tokens"] == 50_000  # 250k - 200k


def test_session_half_cache_miss_uses_current_session_not_lifetime():
    # The always-on cache-miss telemetry MUST derive from the current-session
    # getter, never the lifetime get_token_usage() giants.
    agent = SimpleNamespace(
        _config=SimpleNamespace(cache_miss_budget=1_000_000),
        _session=SimpleNamespace(latest_token_usage_snapshot=lambda: None),
        get_token_usage=lambda: {
            "api_calls": 27_863,
            "input_tokens": 5_000_000_000,
            "cached_tokens": 4_000_000_000,
        },
        get_current_session_token_usage=lambda: {
            "api_calls": 2,
            "input_tokens": 200,
            "cached_tokens": 40,
        },
    )

    compact = build_tool_meta_token_usage(agent)

    assert compact["cache_miss_tokens"] == 160  # 200 - 40, not the lifetime giant
    assert compact["cache_miss_remaining_tokens"] == 999_840


def test_build_meta_token_usage_carries_always_on_cache_miss_below_budget():
    # Through build_meta: below the budget there is NO context guard, but the
    # always-on session-half telemetry still reports current cache miss + budget.
    agent = _budget_agent(budget=1_000_000, input_tokens=300_000, cached_tokens=100_000)

    meta = build_meta(agent)

    # No context guard below budget.
    assert meta_block.TOOL_META_CONTEXT_PENDING_KEY not in meta
    usage = meta[TOOL_META_TOKEN_USAGE_PENDING_KEY]
    assert usage["cache_miss_tokens"] == 200_000
    assert usage["cache_miss_budget"] == 1_000_000
    assert usage["cache_miss_remaining_tokens"] == 800_000


def test_build_meta_omits_context_before_decomp_runs():
    # When decomposition has never run (dirty flag True) and no chat yet,
    # we do not emit stale/unknown numeric context diagnostics in agent_meta.
    agent = _fake_agent_with_session(decomp_ran=False)
    meta = build_meta(agent)
    assert "context" not in meta
    assert meta_block._current_context_usage(agent) == -1.0


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
    # The local usage helper still falls back to interface.estimate_context_tokens(),
    # but the numeric breakdown is no longer duplicated in agent_meta.
    assert "context" not in meta
    assert meta_block._current_context_usage(agent) == pytest.approx(0.555)


def test_build_meta_time_blind_still_omits_numeric_context_fields():
    agent = _fake_agent_with_session(
        time_awareness=False,
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert "context" not in meta
    assert meta_block._current_context_usage(agent) == pytest.approx(0.057)


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
    # The numeric context breakdown is no longer duplicated in agent_meta, but
    # the local warning/reconstruction estimate must still avoid double-counting
    # system+tools. usage = (5500 + 200) / 100000 = 0.057.
    assert "context" not in meta
    assert meta_block._current_context_usage(agent) == pytest.approx(0.057)


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

    # The numeric context breakdown is no longer duplicated in agent_meta.
    assert "context" not in meta

    # The local usage helper must still match get_context_pressure():
    # pressure = estimate_context_tokens() / limit = (sys+tools+history) / limit
    expected_pressure = (sys_prompt + tools + history) / limit
    assert meta_block._current_context_usage(agent) == pytest.approx(expected_pressure)


# ---------------------------------------------------------------------------
# build_reconstruction_tool_meta — one-shot delayed-summarize reconstruction
# event (channel A), permanent evidence on _meta.tool_meta.
#
# The adapter records the before-context (A) when an actual reconstruction
# fires; the kernel pops it once, fills the after-context (B) from the live
# context decomposition, and attaches the A->B event to the next visible tool
# result. If B is still >= the 0.6 recovery target, a molt reminder is
# included; otherwise the A->B event is attached without a warning.
# ---------------------------------------------------------------------------


def _recon_agent(
    *,
    raw_event,
    after_usage,
    context_limit=100000,
    local_usage=None,
):
    """Agent stand-in whose session yields a pending reconstruction event.

    ``after_usage`` drives the PROVIDER-reported after-context (B): it is set as
    ``_latest_input_tokens`` (the post-reconstruction provider request input).
    ``local_usage`` (defaults to ``after_usage``) drives the local
    compacted-history estimate via ``interface.estimate_context_tokens()``. When
    the two differ, tests can prove which source B prefers; setting
    ``after_usage`` semantics:
      * ``>= 0``  -> _latest_input_tokens reflects that provider usage.
      * ``None``  -> _latest_input_tokens = 0 (provider input unavailable),
                     forcing the local-estimate fallback.
    """
    if local_usage is None:
        local_usage = after_usage if after_usage is not None else 0.0
    local_history = int(round(local_usage * context_limit))
    provider_input = (
        0 if after_usage is None else int(round(after_usage * context_limit))
    )
    fake_iface = SimpleNamespace(estimate_context_tokens=lambda: local_history)

    class _Chat:
        interface = fake_iface

        def context_window(self_):
            return context_limit

    taken = {"count": 0}

    def _take():
        taken["count"] += 1
        return raw_event if taken["count"] == 1 else None

    chat = _Chat()
    chat.take_pending_reconstruction_event = _take

    session = SimpleNamespace(
        _token_decomp_dirty=False,
        _system_prompt_tokens=0,
        _tools_tokens=0,
        _latest_input_tokens=provider_input,
        chat=chat,
        context_pressure_warning_active=False,
        context_pressure_streak=0,
    )
    agent = SimpleNamespace(
        _intrinsics={"psyche": object()},
        _config=SimpleNamespace(
            context_limit=context_limit, time_awareness=True, timezone_awareness=True
        ),
        _session=session,
        _uptime_anchor=None,
    )
    return agent


_RAW_EVENT = {
    "type": "delayed_summarize_reconstruction",
    "reason": "delayed_summarize_reconstruction",
    "trigger_threshold": 0.75,
    "recovery_target": 0.60,
    "context_window": 100000,
    "before": {"context_tokens": 85000, "usage": 0.85},
}


def test_reconstruction_tool_meta_none_when_no_pending_event():
    agent = _recon_agent(raw_event=None, after_usage=0.40)
    assert meta_block.build_reconstruction_tool_meta(agent) is None


def test_reconstruction_tool_meta_below_recovery_target_no_warning():
    # B usage 0.40 < 0.60 recovery target -> A->B event, NO molt warning.
    agent = _recon_agent(raw_event=dict(_RAW_EVENT), after_usage=0.40)
    event = meta_block.build_reconstruction_tool_meta(agent)
    assert event is not None
    assert event["type"] == "delayed_summarize_reconstruction"
    assert event["trigger_threshold"] == 0.75
    assert event["recovery_target"] == 0.60
    assert event["context_window"] == 100000
    assert event["before"]["usage"] == 0.85
    assert event["after"]["usage"] == pytest.approx(0.40)
    assert event["after"]["context_tokens"] == 40000
    assert event["after"]["source"] == "provider_input_tokens"
    assert "molt" not in event


def test_reconstruction_tool_meta_after_prefers_provider_input_tokens():
    """B must be the PROVIDER-reported post-reconstruction input
    (_latest_input_tokens / window), not the local compacted-history estimate.
    Here provider says 0.70 while the local estimate says 0.30; B must be 0.70."""
    agent = _recon_agent(
        raw_event=dict(_RAW_EVENT), after_usage=0.70, local_usage=0.30
    )
    event = meta_block.build_reconstruction_tool_meta(agent)
    assert event["after"]["usage"] == pytest.approx(0.70)
    assert event["after"]["context_tokens"] == 70000
    assert event["after"]["source"] == "provider_input_tokens"
    # 0.70 >= 0.60 recovery target -> warning, proving provider value (not the
    # local 0.30) decides the molt warning.
    assert "molt" in event


def test_reconstruction_tool_meta_after_falls_back_to_local_estimate():
    """When the provider input is unavailable (_latest_input_tokens == 0), B
    falls back to the local compacted-history estimate and records that source."""
    agent = _recon_agent(
        raw_event=dict(_RAW_EVENT), after_usage=None, local_usage=0.55
    )
    event = meta_block.build_reconstruction_tool_meta(agent)
    assert event["after"]["usage"] == pytest.approx(0.55)
    assert event["after"]["context_tokens"] == 55000
    assert event["after"]["source"] == "local_estimate"
    assert "molt" not in event  # 0.55 < 0.60


def test_reconstruction_tool_meta_at_or_above_recovery_target_warns():
    # B usage 0.70 >= 0.60 recovery target -> A->B event WITH molt reminder.
    agent = _recon_agent(raw_event=dict(_RAW_EVENT), after_usage=0.70)
    event = meta_block.build_reconstruction_tool_meta(agent)
    assert event["after"]["usage"] == pytest.approx(0.70)
    assert "molt" in event
    molt = event["molt"]
    assert isinstance(molt, str)
    # Wording: reconstruction/summarize was attempted; pressure still above the
    # recovery target; consider molt.
    assert "runtime already rebuilt the provider context" in molt
    assert "70%" in molt
    assert "60%" in molt
    assert "one batch" in molt
    assert "molt deliberately" in molt
    assert "psyche-manual" in molt

def test_reconstruction_tool_meta_still_above_high_context_threshold_says_to_molt():
    # B usage still >= 0.75 after reconstruction: do not loop summarize forever.
    agent = _recon_agent(raw_event=dict(_RAW_EVENT), after_usage=0.80)
    event = meta_block.build_reconstruction_tool_meta(agent)
    molt = event["molt"]
    assert isinstance(molt, str)
    assert "80%" in molt
    assert "above the 75% high-context threshold" in molt
    assert "substantially hurt token efficiency" in molt
    assert "stop repeating summarize" in molt
    assert "molt deliberately" in molt


def test_reconstruction_tool_meta_exactly_at_recovery_target_warns():
    agent = _recon_agent(raw_event=dict(_RAW_EVENT), after_usage=0.60)
    event = meta_block.build_reconstruction_tool_meta(agent)
    assert "molt" in event  # >= recovery target is inclusive


def test_reconstruction_tool_meta_is_one_shot():
    agent = _recon_agent(raw_event=dict(_RAW_EVENT), after_usage=0.40)
    first = meta_block.build_reconstruction_tool_meta(agent)
    assert first is not None
    # The session's take_pending_reconstruction_event already returned None on
    # the second call, so the kernel must not re-emit.
    assert meta_block.build_reconstruction_tool_meta(agent) is None


# ---------------------------------------------------------------------------
# notifications field removed 2026-05-02 (Task 11 of system-notification-as-
# tool-call redesign). System-source notifications are now delivered as
# synthetic notification(action="check") tool-call pairs spliced by
# BaseAgent._inject_notification_pair (the legacy tc_inbox splice path is
# dormant); see docs/plans/2026-05-02-system-notification-as-tool-call.md. Tests for the
# old inbox-drain path lived here and have been removed alongside the field.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# attach_active_notifications — moving single-slot, SPARSE / update-driven
# stamping.  The payload attaches on first appearance and re-attaches only when
# it materially changes (or on a deliberate notification(action=check) read);
# an unchanged payload is NOT chased onto every newest ordinary tool result.
# ---------------------------------------------------------------------------


def _notif_agent(working_dir):
    """Minimal agent stand-in. ``attach_active_notifications`` reads
    ``agent._working_dir`` and, on successful stamping, commits the
    current notification fingerprint to ``agent._notification_fp`` so
    the IDLE-path synthesized pair does not re-deliver the same state.

    ``_notification_payload_signature`` starts ``None`` (no payload emitted yet)
    so the first active payload always attaches; the sparse change-gate in
    ``attach_active_notifications`` updates it thereafter."""
    return SimpleNamespace(
        _working_dir=working_dir,
        _notification_fp=(),
        _notification_payload_signature=None,
    )


def _write_email_notif(tmp_path):
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "email.json").write_text(
        '{"header": "1 unread", "icon": "📬", "priority": "normal", '
        '"data": {"digest": "Email preview line"}}'
    )


def test_attach_active_notifications_first_payload_attaches(tmp_path):
    from lingtai_kernel.notifications import notification_fingerprint

    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)
    assert agent._notification_fp == ()

    # First batch: a single dict-shaped tool result, no prior holder.  The very
    # first active payload always attaches (no prior signature to compare).
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
        }
    }
    assert first.content["_meta"]["notification_guidance"] == {
        "ref": "meta_guidance.notification_handling",
        "sources": ["email"],
    }
    assert "notification_guidance" not in first.content["_meta"]["notifications"]["email"]
    # The sparse change-gate recorded a non-null signature for this payload.
    assert agent._notification_payload_signature is not None
    # Successful stamping must commit the fingerprint, so the IDLE-path
    # synthesized pair will treat this same state as already delivered.
    expected_fp = notification_fingerprint(tmp_path)
    assert expected_fp != ()
    assert agent._notification_fp == expected_fp


def test_attach_active_notifications_unchanged_payload_not_restamped(tmp_path):
    # Sparse contract: an UNCHANGED notification payload must NOT be chased onto
    # a newer ordinary tool result merely because that result is the latest.
    # The prior holder keeps the payload as the current-state carrier.
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert "notifications" in first.content["_meta"]

    # Second batch: the notification files are unchanged.  An ordinary tool
    # result must NOT receive the payload; the prior holder keeps it.
    second = ToolResultBlock(id="t2", name="x", content={"ok": False})
    new_holder = attach_active_notifications(agent, [second], prior_holder=holder)

    assert new_holder is holder
    # Newer ordinary result carries no notification payload.
    assert "_meta" not in second.content or "notifications" not in second.content["_meta"]
    # Prior holder still carries it — it was NOT skeletonized.
    assert "notifications" in first.content["_meta"]
    assert first.content["_meta"]["notifications"]["email"]["data"] == {
        "digest": "Email preview line"
    }


def test_attach_active_notifications_changed_payload_reattaches_and_strips_prior(tmp_path):
    # When the notification payload materially changes, it re-attaches to the
    # newest result and the prior holder sheds its now-stale payload.
    from lingtai_kernel.notifications import notification_fingerprint

    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert "notifications" in first.content["_meta"]
    first_sig = agent._notification_payload_signature

    # Materially change the email channel payload.
    (tmp_path / ".notification" / "email.json").write_text(
        '{"header": "3 unread", "icon": "📬", "priority": "normal", '
        '"data": {"digest": "Three new emails"}}'
    )

    second = ToolResultBlock(id="t2", name="x", content={"ok": False})
    new_holder = attach_active_notifications(agent, [second], prior_holder=holder)

    assert new_holder is second.content
    # The signature advanced with the material change.
    assert agent._notification_payload_signature != first_sig
    # First holder shed its notification keys (and its now-empty _meta envelope).
    assert "_meta" not in first.content or "notifications" not in first.content["_meta"]
    assert second.content["_meta"]["notifications"]["email"]["data"] == {
        "digest": "Three new emails"
    }
    assert agent._notification_fp == notification_fingerprint(tmp_path)


def test_attach_active_notifications_unchanged_commits_fp_to_avoid_retry(tmp_path):
    # Even when an unchanged payload is not restamped, the fingerprint is
    # committed so an equivalent rewrite / same-material payload does not retry
    # forever against the IDLE-path synthesized pair.
    from lingtai_kernel.notifications import notification_fingerprint

    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)

    # Rewrite the same material payload (fingerprint changes: mtime/size may
    # differ), but the canonical payload signature is identical.
    (tmp_path / ".notification" / "email.json").write_text(
        '{"header": "1 unread", "icon": "📬", "priority": "normal", '
        '"data": {"digest": "Email preview line"}}'
    )
    agent._notification_fp = (("stale.json", 1, 1),)

    second = ToolResultBlock(id="t2", name="x", content={"ok": False})
    new_holder = attach_active_notifications(agent, [second], prior_holder=holder)

    # Not restamped (unchanged material), but the fingerprint IS committed.
    assert new_holder is holder
    assert "_meta" not in second.content or "notifications" not in second.content["_meta"]
    assert agent._notification_fp == notification_fingerprint(tmp_path)


def test_attach_active_notifications_unchanged_signature_without_holder_reattaches(tmp_path):
    # Defensive regression: if the signature says "unchanged" but the live
    # holder was lost (e.g. after unusual recovery), do NOT commit an invisible
    # notification state. Fall through and attach the payload to the target.
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert holder is first.content
    assert "notifications" in first.content["_meta"]

    # Simulate holder loss while the material signature remains recorded.
    agent._notification_live_holder = None
    second = ToolResultBlock(id="t2", name="x", content={"ok": False})
    new_holder = attach_active_notifications(agent, [second], prior_holder=None)

    assert new_holder is second.content
    assert "notifications" in second.content["_meta"]



def test_attach_active_notifications_check_read_receives_unchanged_payload(tmp_path):
    # A deliberate notification(action=check) placeholder result is a read
    # request: it must receive the current payload even when unchanged.
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    # First, an ordinary batch establishes the holder + signature.
    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert "notifications" in first.content["_meta"]

    # Now the agent voluntarily calls notification(action=check): its result is
    # the placeholder dict.  Even though the payload is unchanged, the check
    # result must receive the payload (deliberate read) and become the holder.
    check_result = ToolResultBlock(
        id="t2",
        name="notification",
        content={"_notification_placeholder": True, "message": "voluntary check"},
    )
    new_holder = attach_active_notifications(agent, [check_result], prior_holder=holder)

    assert new_holder is check_result.content
    assert "notifications" in check_result.content["_meta"]
    assert check_result.content["_meta"]["notifications"]["email"]["data"] == {
        "digest": "Email preview line"
    }
    # The prior ordinary holder shed its payload when the check took over.
    assert "_meta" not in first.content or "notifications" not in first.content["_meta"]


def test_attach_active_notifications_empty_resets_signature_for_reappearance(tmp_path):
    # When notifications go empty the signature resets to None, so a later
    # reappearance of the SAME payload attaches again as the first active one.
    _write_email_notif(tmp_path)
    agent = _notif_agent(tmp_path)

    first = ToolResultBlock(id="t1", name="x", content={"ok": True})
    holder = attach_active_notifications(agent, [first], prior_holder=None)
    assert agent._notification_payload_signature is not None

    # Notifications cleared.
    (tmp_path / ".notification" / "email.json").unlink()
    empty_batch = ToolResultBlock(id="t2", name="x", content={"ok": False})
    result = attach_active_notifications(agent, [empty_batch], prior_holder=holder)
    assert result is None
    assert agent._notification_payload_signature is None
    # Prior holder shed its payload.
    assert "_meta" not in first.content or "notifications" not in first.content["_meta"]

    # Same payload reappears — must attach afresh (first-active semantics).
    _write_email_notif(tmp_path)
    third = ToolResultBlock(id="t3", name="x", content={"ok": True})
    new_holder = attach_active_notifications(agent, [third], prior_holder=None)
    assert new_holder is third.content
    assert "notifications" in third.content["_meta"]


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
    assert "notification_guidance" not in payload
    assert block.content["_meta"]["notification_guidance"] == {
        "ref": "meta_guidance.notification_handling",
        "sources": ["mcp.telegram"],
    }


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
    result = {"payload": "B" * 1200}
    stamp_meta(result, build_meta(agent), elapsed_ms=12)
    block = ToolResultBlock(id="tc-batch", name="bash", content=result)

    attach_active_runtime(agent, [block])

    agent_meta = block.content["_meta"]["agent_meta"]
    current = agent_meta["current_tool_result_chars"]
    expected = len(json.dumps({"payload": "B" * 1200}, ensure_ascii=False, default=str))
    assert current["total_chars"] == expected
    assert current["top_results"] == [
        {
            "id": "tc-batch",
            "tool_name": "bash",
            "chars": expected,
        }
    ]


def test_attach_active_runtime_does_not_leak_tool_meta_token_usage_to_agent_meta():
    agent = _runtime_agent(total_calls=1)
    snapshot = {"scope": "provider_round", "input_tokens": 100}
    block = ToolResultBlock(
        id="tc-token",
        name="bash",
        content=_stamped_result(
            {"current_time": "T", TOOL_META_TOKEN_USAGE_PENDING_KEY: snapshot},
            12,
        ),
    )

    attach_active_runtime(agent, [block])

    agent_meta = block.content["_meta"]["agent_meta"]
    assert TOOL_META_TOKEN_USAGE_PENDING_KEY not in agent_meta
    assert "_runtime_pending" not in block.content

def test_attach_active_runtime_keeps_no_token_efficiency_in_agent_meta():
    # agent_meta must NOT carry any token diagnostics — those live in
    # _meta.tool_meta.token_usage only. Even if a stale token_efficiency snapshot
    # somehow rode along in pending, it is not promoted into agent_meta.
    agent = _runtime_agent(total_calls=3)
    result = _stamped_result(
        {"current_time": "T"},
        elapsed_ms=12,
    )
    block = ToolResultBlock(id="tc-eff", name="bash", content=result)

    attach_active_runtime(agent, [block])

    agent_meta = block.content["_meta"]["agent_meta"]
    assert "token_efficiency" not in agent_meta
    assert agent_meta["active_turn_tool_calls"] == 3


def test_attach_active_runtime_stamps_latest_with_state_and_guidance():
    agent = _runtime_agent(total_calls=3)
    content = _stamped_result({"current_time": "T", "context": {"usage": 0.1}}, 12)
    block = ToolResultBlock(id="t1", name="x", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)

    assert holder is block.content
    meta = block.content["_meta"]
    agent_meta = meta["agent_meta"]
    assert "current_time" not in agent_meta
    assert agent_meta["elapsed_ms"] == 12
    # active_turn_tool_calls is sourced from the guard and lives under agent_meta.
    assert agent_meta["active_turn_tool_calls"] == 3
    # Tail guidance is now a lightweight ref/hook pointing at the resident
    # meta_guidance system-prompt section — NOT the full ordered sections,
    # which moved into the system prompt to stop riding on every tail _meta.
    guidance = meta["guidance"]
    assert "sections" not in guidance
    assert "meta_guidance" in json.dumps(guidance)
    # The transient scaffolding is consumed.
    assert "_runtime_pending" not in block.content
    # No top-level active_turn_tool_calls repetition, and no legacy _runtime key.
    assert "active_turn_tool_calls" not in block.content
    assert "_runtime" not in block.content



def test_attach_active_runtime_refreshes_adapter_comment_at_batch_boundary():
    agent = _runtime_agent(total_calls=1)

    def dynamic_comment():
        return {"adapter": "fake", "next_reset_in": 5}

    agent._session = SimpleNamespace(
        chat=SimpleNamespace(
            adapter_comment=lambda: {"adapter": "fake", "summary": "legacy provider note"},
            dynamic_adapter_comment=dynamic_comment,
        )
    )
    block = ToolResultBlock(
        id="t-adapter", name="x", content=_stamped_result({"current_time": "T"}, 12)
    )

    attach_active_runtime(agent, [block])

    agent_meta = block.content["_meta"]["agent_meta"]
    tail = agent_meta["adapter_comment"]
    assert tail["adapter"] == "fake"
    assert tail["next_reset_in"] == 5
    assert "summary" not in tail
    assert "meta_guidance_ref" not in tail

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
    assert "current_time" not in second.content["_meta"]["agent_meta"]
    assert second.content["_meta"]["agent_meta"]["active_turn_tool_calls"] == 2


def test_attach_active_runtime_picks_latest_dict_in_batch():
    agent = _runtime_agent(total_calls=4)
    earlier = ToolResultBlock(id="t1", name="x", content=_stamped_result({"current_time": "E"}, 1))
    middle = ToolResultBlock(id="t2", name="x", content=_stamped_result({"current_time": "M"}, 2))
    string_tail = ToolResultBlock(id="t3", name="x", content="plain text")

    holder = attach_active_runtime(agent, [earlier, middle, string_tail], prior_holder=None)

    assert holder is middle.content
    assert "current_time" not in middle.content["_meta"]["agent_meta"]
    assert middle.content["_meta"]["agent_meta"]["elapsed_ms"] == 2
    # The earlier dict gets no agent_meta, and its pending scaffolding is stripped.
    assert "_meta" not in earlier.content
    assert "_runtime_pending" not in earlier.content
    assert string_tail.content == "plain text"


def test_attach_active_runtime_empty_meta_keeps_prior_snapshot():
    # A time-blind agent's results carry no _runtime_pending (stamp_meta no-op).
    agent = _runtime_agent(total_calls=1)
    prior_content = _stamped_result({"current_time": "T1"}, 5)
    prior = ToolResultBlock(id="t1", name="x", content=prior_content)
    holder = attach_active_runtime(agent, [prior], prior_holder=None)
    assert "agent_meta" in prior.content["_meta"]

    # Next batch: result was NOT stamped (no pending) — there is no new snapshot
    # to emit. Under the sparse contract the prior holder's agent_meta stays put
    # as the most recent emitted update point rather than being stripped with no
    # replacement.
    blind = ToolResultBlock(id="t2", name="x", content={"status": "ok"})
    new_holder = attach_active_runtime(agent, [blind], prior_holder=holder)

    assert new_holder is holder
    assert "agent_meta" in prior.content["_meta"]
    assert "_meta" not in blind.content


# ---------------------------------------------------------------------------
# Sparse / update-driven agent_meta: agent_meta is attached only when the
# material snapshot changes since the last emitted agent_meta, not re-stamped
# onto every latest tool result when unchanged.
# ---------------------------------------------------------------------------


def test_attach_active_runtime_first_snapshot_is_attached():
    # The very first material snapshot always attaches — there is no prior
    # signature to compare against.
    agent = _runtime_agent(total_calls=1)
    content = _stamped_result({"current_time": "T1"}, 5)
    block = ToolResultBlock(id="t1", name="x", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)

    assert holder is block.content
    assert "agent_meta" in block.content["_meta"]
    assert "guidance" in block.content["_meta"]


def test_attach_active_runtime_unchanged_snapshot_not_restamped_on_latest():
    # Same agent, a second batch whose MATERIAL snapshot is identical (only
    # volatile bookkeeping — elapsed_ms, active_turn_tool_calls, current_time,
    # total_chars — differs). agent_meta must NOT move onto the new latest
    # result, and the prior holder keeps its snapshot as a historical update
    # point.
    agent = _runtime_agent(total_calls=1)
    first = ToolResultBlock(id="t1", name="x", content=_stamped_result({"current_time": "T1"}, 5))
    holder = attach_active_runtime(agent, [first], prior_holder=None)
    assert "agent_meta" in first.content["_meta"]

    # Volatile-only change: counter ticks, elapsed differs, time differs.
    agent._executor.guard.total_calls = 2
    second = ToolResultBlock(id="t2", name="x", content=_stamped_result({"current_time": "T2"}, 6))
    new_holder = attach_active_runtime(agent, [second], prior_holder=holder)

    # No material change → do not attach to the latest, do not move the holder.
    assert "_meta" not in second.content or "agent_meta" not in second.content["_meta"]
    # Prior snapshot stays as a historical update point (not stripped).
    assert "agent_meta" in first.content["_meta"]
    assert new_holder is holder
    # Transient scaffolding is still cleared from the un-stamped result.
    assert "_runtime_pending" not in second.content


def test_attach_active_runtime_material_change_reattaches():
    # After an unchanged batch, a genuinely material change (here: a new
    # adapter_comment scalar) re-attaches agent_meta to the newest result and
    # strips the older holder.  (The sustained-pressure molt reminder is NO longer
    # an agent_meta signal — it lives on permanent tool_meta.context.molt now — so
    # a neutral agent_meta material field drives this mechanism test.)
    agent = _runtime_agent(total_calls=1)
    first = ToolResultBlock(id="t1", name="x", content=_stamped_result({"current_time": "T1"}, 5))
    holder = attach_active_runtime(agent, [first], prior_holder=None)

    # Unchanged batch — no re-attach.
    agent._executor.guard.total_calls = 2
    second = ToolResultBlock(id="t2", name="x", content=_stamped_result({"current_time": "T2"}, 6))
    holder2 = attach_active_runtime(agent, [second], prior_holder=holder)
    assert holder2 is holder
    assert "agent_meta" not in second.content.get("_meta", {})

    # Material change: a new adapter_comment scalar appears in the snapshot.
    agent._executor.guard.total_calls = 3
    third = ToolResultBlock(
        id="t3",
        name="x",
        content=_stamped_result(
            {"current_time": "T3", "adapter_comment": {"note": "materially new"}}, 7
        ),
    )
    new_holder = attach_active_runtime(agent, [third], prior_holder=holder)

    assert new_holder is third.content
    assert "agent_meta" in third.content["_meta"]
    assert third.content["_meta"]["agent_meta"]["adapter_comment"] == {"note": "materially new"}
    # The older holder now sheds its agent_meta/guidance.
    assert "_meta" not in first.content or "agent_meta" not in first.content["_meta"]


def test_attach_active_runtime_new_large_result_is_material():
    # A new large tool result appearing in current_tool_result_chars.top_results
    # is a material change worth re-surfacing agent_meta, even if nothing else
    # changed.
    agent = _fake_agent()
    small = ToolResultBlock(
        id="t1", name="x", content=_stamped_result(build_meta(agent), 5)
    )
    holder = attach_active_runtime(agent, [small], prior_holder=None)
    assert "agent_meta" in small.content["_meta"]

    # A big result enters the batch — top_results changes materially.
    big_content = {"payload": "B" * 5000}
    stamp_meta(big_content, build_meta(agent), elapsed_ms=6)
    big = ToolResultBlock(id="t2", name="bash", content=big_content)
    new_holder = attach_active_runtime(agent, [big], prior_holder=holder)

    assert new_holder is big.content
    assert "agent_meta" in big.content["_meta"]
    top = big.content["_meta"]["agent_meta"]["current_tool_result_chars"]["top_results"]
    assert any(entry["id"] == "t2" for entry in top)


def test_agent_meta_signature_ignores_volatile_bookkeeping():
    # The material signature must be identical when only volatile fields differ.
    from lingtai_kernel.meta_block import agent_meta_signature

    base = {
        "elapsed_ms": 5,
        "active_turn_tool_calls": 1,
        "current_time": "T1",
        "current_tool_result_chars": {
            "total_chars": 100,
            "threshold": 3000,
            "over_threshold_count": 0,
            "top_results": [],
        },
        "context": {"molt": "reminder"},
    }
    volatile_changed = {
        "elapsed_ms": 999,
        "active_turn_tool_calls": 42,
        "current_time": "T2",
        "current_tool_result_chars": {
            "total_chars": 999999,
            "threshold": 3000,
            "over_threshold_count": 0,
            "top_results": [],
        },
        "context": {"molt": "reminder"},
    }
    assert agent_meta_signature(base) == agent_meta_signature(volatile_changed)

    material_changed = dict(base)
    material_changed["context"] = {"molt": "different reminder"}
    assert agent_meta_signature(base) != agent_meta_signature(material_changed)


def test_attach_active_runtime_no_dict_target_keeps_prior_snapshot():
    # A batch with no dict-shaped result has nowhere to attach a new snapshot.
    # Under the sparse contract the prior holder's agent_meta remains as the
    # most recent emitted update point rather than being stripped.
    agent = _runtime_agent(total_calls=1)
    prior_content = _stamped_result({"current_time": "T1"}, 5)
    prior = ToolResultBlock(id="t1", name="x", content=prior_content)
    holder = attach_active_runtime(agent, [prior], prior_holder=None)

    string_only = ToolResultBlock(id="t2", name="x", content="text")
    new_holder = attach_active_runtime(agent, [string_only], prior_holder=holder)

    assert new_holder is holder
    assert "agent_meta" in prior.content["_meta"]
    assert string_only.content == "text"


def test_attach_active_runtime_omits_counter_when_no_guard():
    agent = _runtime_agent(total_calls=None)  # no executor/guard
    content = _stamped_result({"current_time": "T"}, 9)
    block = ToolResultBlock(id="t1", name="x", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)

    assert holder is block.content
    agent_meta = block.content["_meta"]["agent_meta"]
    assert "current_time" not in agent_meta
    assert "active_turn_tool_calls" not in agent_meta


# ---------------------------------------------------------------------------
# Runtime guidance payload/catalog schema validation.
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
    # The shipped guidance catalog must validate — this is the test that catches a
    # malformed packaged resource (build_runtime_guidance degrades silently).
    guidance = build_runtime_guidance()
    assert guidance != {}, "packaged guidance catalog failed to load/validate"
    validate_runtime_guidance(guidance)  # must not raise
    ids = [s["id"] for s in guidance["sections"]]
    assert len(ids) == len(set(ids)), "section ids must be unique"
    titles = [s["title"] for s in guidance["sections"]]
    assert len(titles) == len(set(titles)), "section titles must be unique"
    assert "summarize_reconstruction_threshold" in ids
    assert "Delayed summarization reconstruction threshold" in titles
    body = "\n".join(section["body"] for section in guidance["sections"])
    assert "summarize completed tool results" in body
    assert "raw text no longer needs inspection" in body
    assert "carrying more into each provider request" in body
    assert "Apply the token-efficiency principle" in body
    assert "do not molt automatically" in body
    assert "api_calls > 100" in body
    assert "mini molt for consumed tool results" in body
    assert "stronger whole-conversation boundary" in body
    assert "skip pre-molt summarize" in body
    assert "0.75" in body
    assert "Do not call `refresh` just to apply a summarize" in body
    assert "does not mean the active provider-side context" in body
    assert "0.6 * context_window" in body
    # Unified contract: token diagnostics live in tool_meta.token_usage; the
    # guidance points there and describes the current-session aggregate half.
    assert "token_usage" in body
    assert "current-session" in body
    assert "session_cache_rate" in body
    assert "guiding_avg_input_tokens_per_api_call" not in body
    assert "recent human-channel instructions" in body
    assert "last 30 Telegram messages" in body
    assert "not a personal standing rule file" in body


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


# ---------------------------------------------------------------------------
# build_molt_context / context.molt — SUSTAINED context-pressure warning
# surfaced under permanent _meta.tool_meta.context.molt (persists on every
# result while active; routed via the _tool_meta_context transit key), not a
# dismissible notification.
#
# Corrected contract (channel B): the warning is NOT the old immediate
# ``usage >= 0.60`` trip-wire.  It is driven by the SessionManager
# sustained-pressure streak — context must be high (>= 0.75) for
# CONTEXT_PRESSURE_WARN_AFTER_ROUNDS (3) consecutive *fresh provider rounds*
# before the warning appears, giving summarize/reconstruction time to relieve
# pressure first.  A drop below 0.75 resets the streak and clears the warning.
# Wording directs: summarize first; if context cannot be brought below the 0.6
# recovery target, consider/perform molt.
# ---------------------------------------------------------------------------


def _molt_agent(*, warning_active=False, streak=0, psyche=True):
    """Minimal agent stand-in for build_molt_context.

    build_molt_context reads agent._intrinsics (must contain 'psyche') and the
    session's sustained-pressure streak state (set by SessionManager).
    """
    return SimpleNamespace(
        _intrinsics={"psyche": object()} if psyche else {},
        _config=SimpleNamespace(
            context_limit=None,
            time_awareness=True,
            timezone_awareness=True,
        ),
        _session=SimpleNamespace(
            context_pressure_warning_active=warning_active,
            context_pressure_streak=streak,
        ),
    )


def test_build_molt_context_absent_without_psyche():
    agent = _molt_agent(warning_active=True, streak=5, psyche=False)
    # Even with a fully-armed streak, no molt context when psyche is absent.
    assert build_molt_context(agent, 0.95) is None


def test_build_molt_context_absent_for_first_two_high_rounds():
    # Streak below the warn threshold (3) -> no warning yet, even at high usage.
    assert build_molt_context(_molt_agent(warning_active=False, streak=1), 0.90) is None
    assert build_molt_context(_molt_agent(warning_active=False, streak=2), 0.92) is None


def test_build_molt_context_old_immediate_0_60_no_longer_trips():
    """Regression: 0.61 (above the retired 0.60 trip-wire) with no sustained
    streak must NOT produce a warning anymore."""
    assert build_molt_context(_molt_agent(warning_active=False, streak=1), 0.61) is None


def test_build_molt_context_warns_from_third_high_round():
    agent = _molt_agent(warning_active=True, streak=3)
    molt = build_molt_context(agent, 0.90)
    assert isinstance(molt, str)
    assert "Context has stayed high" in molt
    assert "3 consecutive fresh model calls" in molt
    assert "90%" in molt
    assert "recovery target is 60%" in molt
    assert "batch tool results" in molt
    assert "Repeated summarize calls while context stays above 75%" in molt
    assert "substantially hurt token efficiency" in molt
    assert "batched summarize/reconstruction pass" in molt
    assert "stop repeating summarize" in molt
    assert "molt deliberately" in molt
    assert "psyche-manual" in molt


def test_build_molt_context_keeps_warning_while_streak_sustained():
    for streak in (3, 4, 7):
        molt = build_molt_context(_molt_agent(warning_active=True, streak=streak), 0.95)
        assert molt is not None
        assert f"{streak} consecutive fresh model calls" in molt
        assert "95%" in molt


def test_build_molt_context_is_natural_language_not_tag_payload():
    molt = build_molt_context(_molt_agent(warning_active=True, streak=3), 0.90)

    assert isinstance(molt, str)
    assert "stage" not in molt
    assert '"threshold"' not in molt
    assert "recovery_target" not in molt
    assert "summarize_then_molt" not in molt
    assert "procedures.md#performing-a-molt" not in molt
    serialized = json.dumps({"molt": molt})
    assert len(serialized) < 650


def test_build_molt_context_handles_missing_session_gracefully():
    agent = SimpleNamespace(_intrinsics={"psyche": object()})
    # No _session attribute at all -> no warning, no crash.
    assert build_molt_context(agent, 0.90) is None


def test_build_meta_attaches_context_molt_only_when_streak_armed():
    """build_meta integrates build_molt_context: context.molt is absent while the
    streak is below the warn threshold and present once the streak is armed,
    independent of the instantaneous usage on this particular build_meta call."""
    fake_iface = SimpleNamespace(estimate_context_tokens=lambda: 90)
    fake_session = SimpleNamespace(
        _token_decomp_dirty=False,
        _system_prompt_tokens=10,
        _tools_tokens=0,
        _latest_input_tokens=0,
        chat=SimpleNamespace(interface=fake_iface, context_window=lambda: 100),
        context_pressure_warning_active=False,
        context_pressure_streak=2,
    )
    agent = _molt_agent(warning_active=False, streak=2)
    agent._session = fake_session
    agent._uptime_anchor = None

    meta = build_meta(agent)
    assert meta_block._current_context_usage(agent) == pytest.approx(0.9)
    # Streak not yet armed -> no context reminder is emitted even though usage is
    # 0.9.  The molt reminder now rides on a transit key destined for the
    # PERMANENT tool_meta.context block (not the sparse agent_meta.context), so
    # neither the transit key nor a plain "context" key is present.
    assert meta_block.TOOL_META_CONTEXT_PENDING_KEY not in meta
    assert "context" not in meta

    # Arm the streak; same high usage now surfaces the warning under the transit
    # key (ToolExecutor._attach_tool_block promotes it into tool_meta.context).
    fake_session.context_pressure_warning_active = True
    fake_session.context_pressure_streak = 3
    meta = build_meta(agent)
    context_transit = meta[meta_block.TOOL_META_CONTEXT_PENDING_KEY]
    assert isinstance(context_transit["molt"], str)
    assert "Context has stayed high" in context_transit["molt"]
    assert "3 consecutive fresh model calls" in context_transit["molt"]
    # It must NOT land in a plain agent-facing "context" key on the meta dict.
    assert "context" not in meta


def _molt_agent_with_reminder(reminder):
    """Agent stand-in whose session exposes a real ContextPressureReminder plus
    the live token-decomposition attributes build_meta reads for usage."""
    fake_iface = SimpleNamespace(estimate_context_tokens=lambda: 90)
    session = SimpleNamespace(
        _token_decomp_dirty=False,
        _system_prompt_tokens=10,
        _tools_tokens=0,
        _latest_input_tokens=0,
        chat=SimpleNamespace(interface=fake_iface, context_window=lambda: 100),
        context_pressure_reminder=reminder,
    )
    return SimpleNamespace(
        _intrinsics={"psyche": object()},
        _config=SimpleNamespace(
            context_limit=None, time_awareness=True, timezone_awareness=True
        ),
        _session=session,
        _uptime_anchor=None,
    )


def test_build_meta_current_molt_carries_reminder_and_event_payload():
    from lingtai_kernel.reminders.context_pressure import ContextPressureReminder

    reminder = ContextPressureReminder()
    for rid in (1, 2, 3):
        reminder.note_round(0.90, round_id=rid)
    agent = _molt_agent_with_reminder(reminder)

    # build_meta is SIDE-EFFECT-FREE and always carries the reminder text (transit
    # key, destined for permanent tool_meta.context.molt) AND the emission-event
    # payload while the warning is active — the DEDUP happens later, in
    # ToolExecutor._attach_tool_block (keyed on payload.last_round_id), not here.
    meta1 = build_meta(agent)
    assert "molt" in meta1[meta_block.TOOL_META_CONTEXT_PENDING_KEY]
    assert meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY in meta1
    payload = meta1[meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY]["payload"]
    assert payload["last_round_id"] == 3

    # Called again in the same round, build_meta STILL carries the payload (no
    # mutation / no dedup at this layer — the render-path text-prefix call and the
    # per-result stamp call must both be pure).
    meta2 = build_meta(agent)
    assert meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY in meta2
    assert (
        meta2[meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY]["payload"]["last_round_id"]
        == 3
    )


# ---------------------------------------------------------------------------
# build_cache_miss_budget_context / cache-miss budget guard.
#
# A per-molt/runtime-session soft cap on total cache-miss (uncached input)
# tokens. The current-session cache-miss total is derived from
# agent.get_current_session_token_usage() as max(input_tokens - cached_tokens, 0).
# Once it reaches/exceeds agent._config.cache_miss_budget, build_meta restamps a
# "cache miss budget {budget} reached, molt now" reminder into the
# _tool_meta_context transit sub-object (promoted to permanent
# tool_meta.context.molt) and surfaces cache_miss_budget / cache_miss_tokens
# under tool_meta.context. It is a soft signal, not a new event route.
# ---------------------------------------------------------------------------


def _budget_agent(
    *,
    budget=1_000_000,
    input_tokens=0,
    cached_tokens=0,
    psyche=True,
    warning_active=False,
    streak=0,
    has_getter=True,
):
    """Minimal agent stand-in for build_cache_miss_budget_context / build_meta.

    Carries a get_current_session_token_usage() returning the given
    input/cached token deltas, plus the streak fields build_molt_context reads
    (so the "both warnings active" case can be exercised through build_meta).
    """
    session = SimpleNamespace(
        _token_decomp_dirty=True,
        context_pressure_warning_active=warning_active,
        context_pressure_streak=streak,
    )
    agent = SimpleNamespace(
        _intrinsics={"psyche": object()} if psyche else {},
        _config=SimpleNamespace(
            cache_miss_budget=budget,
            context_limit=None,
            time_awareness=True,
            timezone_awareness=True,
        ),
        _session=session,
    )
    if has_getter:
        agent.get_current_session_token_usage = lambda: {
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "api_calls": 1,
        }
    return agent


def test_cache_miss_budget_context_none_below_budget():
    # cache_miss = 900k - 100k = 800k < 1M -> no context.
    agent = _budget_agent(budget=1_000_000, input_tokens=900_000, cached_tokens=100_000)
    assert build_cache_miss_budget_context(agent) is None


def test_cache_miss_budget_context_present_at_budget():
    # cache_miss = 1.0M - 0 = 1.0M == budget -> reminder (inclusive >=).
    agent = _budget_agent(budget=1_000_000, input_tokens=1_000_000, cached_tokens=0)
    ctx = build_cache_miss_budget_context(agent)
    assert isinstance(ctx, dict)
    assert ctx["molt"] == "cache miss budget 1000000 reached, molt now"
    assert ctx["cache_miss_budget"] == 1_000_000
    assert ctx["cache_miss_tokens"] == 1_000_000


def test_cache_miss_budget_context_present_above_budget_with_cache():
    # cache_miss = 1.5M - 200k = 1.3M >= 1M budget.
    agent = _budget_agent(budget=1_000_000, input_tokens=1_500_000, cached_tokens=200_000)
    ctx = build_cache_miss_budget_context(agent)
    assert ctx["cache_miss_tokens"] == 1_300_000
    assert ctx["cache_miss_budget"] == 1_000_000
    assert ctx["molt"] == "cache miss budget 1000000 reached, molt now"


def test_cache_miss_budget_context_clamps_negative_cache_miss_to_zero():
    # cached > input (odd provider accounting) -> cache_miss clamps to 0, no warn.
    agent = _budget_agent(budget=1, input_tokens=100, cached_tokens=500)
    assert build_cache_miss_budget_context(agent) is None


def test_cache_miss_budget_context_honors_custom_budget():
    agent = _budget_agent(budget=250_000, input_tokens=250_000, cached_tokens=0)
    ctx = build_cache_miss_budget_context(agent)
    assert ctx["molt"] == "cache miss budget 250000 reached, molt now"
    assert ctx["cache_miss_budget"] == 250_000


def test_cache_miss_budget_context_absent_without_psyche():
    # Consistent with build_molt_context: no psyche intrinsic -> no reminder.
    agent = _budget_agent(input_tokens=2_000_000, cached_tokens=0, psyche=False)
    assert build_cache_miss_budget_context(agent) is None


def test_cache_miss_budget_context_graceful_without_getter():
    agent = _budget_agent(input_tokens=2_000_000, cached_tokens=0, has_getter=False)
    assert build_cache_miss_budget_context(agent) is None


def test_cache_miss_budget_context_absent_for_nonpositive_budget():
    # Defensive: a non-positive / non-int budget disables the guard, never warns.
    for bad in (0, -1, None, "1000000"):
        agent = _budget_agent(budget=bad, input_tokens=5_000_000, cached_tokens=0)
        assert build_cache_miss_budget_context(agent) is None


def test_build_meta_attaches_budget_context_at_budget():
    """build_meta integrates the budget guard: at/above budget the transit
    sub-object carries the molt warning plus the budget fields."""
    agent = _budget_agent(budget=1_000_000, input_tokens=1_200_000, cached_tokens=0)
    meta = build_meta(agent)
    ctx = meta[meta_block.TOOL_META_CONTEXT_PENDING_KEY]
    assert ctx["molt"] == "cache miss budget 1000000 reached, molt now"
    assert ctx["cache_miss_budget"] == 1_000_000
    assert ctx["cache_miss_tokens"] == 1_200_000
    # Budget guard is not a new event route: no emission-event payload.
    assert meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY not in meta


def test_build_meta_no_budget_context_below_budget():
    agent = _budget_agent(budget=1_000_000, input_tokens=500_000, cached_tokens=0)
    meta = build_meta(agent)
    assert meta_block.TOOL_META_CONTEXT_PENDING_KEY not in meta


def test_build_meta_preserves_both_warnings_when_context_pressure_also_active():
    """When the sustained context-pressure warning AND the cache-miss budget
    warning are both active, both must survive in tool_meta.context.molt — the
    budget line is appended and the context-pressure prose is preserved — and the
    budget fields ride alongside."""
    from lingtai_kernel.reminders.context_pressure import ContextPressureReminder

    # Drive a real context decomposition (usage 0.9) with an armed real reminder,
    # plus a cache-miss total over budget.
    fake_iface = SimpleNamespace(estimate_context_tokens=lambda: 90)
    reminder = ContextPressureReminder()
    reminder.streak = 3  # >= warn_after_rounds (3) -> active
    reminder.last_round_id = 7
    agent = _budget_agent(budget=1_000_000, input_tokens=1_200_000, cached_tokens=0)
    agent._session._token_decomp_dirty = False
    agent._session._system_prompt_tokens = 10
    agent._session._tools_tokens = 0
    agent._session._latest_input_tokens = 0
    agent._session.context_pressure_reminder = reminder
    agent._session.chat = SimpleNamespace(
        interface=fake_iface, context_window=lambda: 100
    )

    meta = build_meta(agent)
    ctx = meta[meta_block.TOOL_META_CONTEXT_PENDING_KEY]
    molt = ctx["molt"]
    # Context-pressure prose preserved.
    assert "Context has stayed high" in molt
    assert "3 consecutive fresh model calls" in molt
    # Budget warning also present, appended on its own line.
    assert "cache miss budget 1000000 reached, molt now" in molt
    assert molt.endswith("cache miss budget 1000000 reached, molt now")
    # Budget fields present alongside.
    assert ctx["cache_miss_budget"] == 1_000_000
    assert ctx["cache_miss_tokens"] == 1_200_000
    # The context-pressure emission event still hashes ONLY the pressure message,
    # not the combined text (channel-B dedup/logging semantics are unchanged).
    from lingtai_kernel.reminders.context_pressure import reminder_message_hash
    pressure_only = reminder.current_molt_context(0.9)
    payload = meta[meta_block.TOOL_META_CONTEXT_EVENT_PENDING_KEY]["payload"]
    assert payload["message_hash"] == reminder_message_hash(pressure_only)


def test_attach_tool_block_promotes_budget_context_and_pops_transit_key():
    """_attach_tool_block promotes the budget sub-object (molt + budget fields)
    into permanent tool_meta.context and pops the transit key from
    _runtime_pending so it never lands on the wire tool_meta."""
    from lingtai_kernel.loop_guard import LoopGuard
    from lingtai_kernel.tool_executor import _DEFAULT_MAX_RESULT_CHARS, ToolExecutor

    agent = _budget_agent(budget=1_000_000, input_tokens=1_200_000, cached_tokens=0)
    meta = build_meta(agent)
    result = {"ok": True}
    stamp_meta(result, meta, elapsed_ms=5)
    # Sanity: the transit key is present before _attach_tool_block consumes it.
    assert meta_block.TOOL_META_CONTEXT_PENDING_KEY in result["_runtime_pending"]

    executor = ToolExecutor(
        dispatch_fn=lambda name, args: {},
        make_tool_result_fn=lambda name, result, **kw: result,
        guard=LoopGuard(max_total_calls=50),
        working_dir="/tmp",
        max_result_chars=_DEFAULT_MAX_RESULT_CHARS,
    )
    wire = executor._attach_tool_block(result, tool_call_id="tc1", elapsed_ms=5)
    context = wire["_meta"]["tool_meta"]["context"]
    assert context["molt"] == "cache miss budget 1000000 reached, molt now"
    assert context["cache_miss_budget"] == 1_000_000
    assert context["cache_miss_tokens"] == 1_200_000
    # The transit key was popped out of _runtime_pending (the batch boundary
    # strips whatever remains of _runtime_pending; the key itself must not
    # survive into the promoted tool_meta.context beyond molt + budget fields).
    assert meta_block.TOOL_META_CONTEXT_PENDING_KEY not in result["_runtime_pending"]
