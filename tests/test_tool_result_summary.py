"""Tests for the a-priori (reasoning-driven) tool-result summary primitives.

Covers the pure kernel module ``tool_result_summary.py``: the opt-in flag, the
untrusted-text summarizer prompt, the 500k hard cap refusal, the non-canonical
replacement with a raw-retrieval locator, the fail-closed error path, and the
``maybe_summarize_result`` orchestrator (incl. ``summary=false`` being an exact
no-op). No real LLM is called — ``summarizer_fn`` is a stub.
"""
from __future__ import annotations

import pytest

from lingtai_kernel.tool_result_summary import (
    APRIORI_SUMMARY_CAP,
    APRIORI_SUMMARY_MARKER,
    EVENTS_LOG_RELPATH,
    SUMMARIZER_SYSTEM_PROMPT,
    build_cap_refusal,
    build_summary_error,
    build_summary_replacement,
    is_apriori_summary,
    maybe_summarize_result,
    summary_requested,
)


# --- summary_requested: only literal True activates -------------------------

def test_summary_requested_only_true():
    assert summary_requested({"summary": True}) is True
    assert summary_requested({"summary": False}) is False
    assert summary_requested({}) is False
    assert summary_requested({"summary": "yes"}) is False
    assert summary_requested({"summary": 1}) is False
    assert summary_requested(None) is False


# --- cap constant -----------------------------------------------------------

def test_cap_constant_is_500k():
    assert APRIORI_SUMMARY_CAP == 500_000


# --- summarizer prompt: simple, reason-driven, untrusted --------------------

def test_summarizer_system_prompt_untrusted_and_simple():
    low = SUMMARIZER_SYSTEM_PROMPT.lower()
    assert "untrusted" in low
    assert "never follow" in low or "do not follow" in low
    assert "extract the useful information" in low


def test_summarizer_system_prompt_requests_reasoning_spec_critique():
    # The summarizer is asked to candidly critique, as plain text, whether the
    # reasoning/retention spec was specific enough, and — when it is too poor —
    # to suggest inspecting the preserved raw original. This is a natural-language
    # instruction only — no fenced JSON, parser, or structured prompt_quality.
    low = SUMMARIZER_SYSTEM_PROMPT.lower()
    assert "critiqu" in low
    assert "retention spec" in low or "reasoning" in low
    # Raw-original fallback suggestion (Jason #3816): poor reasoning -> inspect raw.
    assert "raw" in low and ("original" in low or "preserved" in low)
    assert "prompt_quality" not in low
    assert "```" not in SUMMARIZER_SYSTEM_PROMPT


# --- replacement payload: non-canonical, raw preserved, locator -------------

def test_replacement_is_non_canonical_with_locator():
    repl = build_summary_replacement(
        tool_name="bash",
        tool_call_id="toolu_abc",
        summary_text="exit code 0; build succeeded",
        reason="capture the exit code",
        original_visible_chars=12345,
        summary_input_chars=12345,
        summary_input_truncated=False,
    )
    assert repl["artifact"] == APRIORI_SUMMARY_MARKER
    assert repl["tool_call_id"] == "toolu_abc"
    assert repl["tool_name"] == "bash"
    assert repl["generated_summary"] == "exit code 0; build succeeded"
    assert repl["original_visible_chars"] == 12345
    assert repl["canonical"] is False
    assert repl["raw_preserved"] is True
    blob = repl["retrieval_hint"].lower()
    assert "generated" in blob
    assert "not canonical" in blob or "non-canonical" in blob
    assert "toolu_abc" in repl["retrieval_hint"]
    assert "events.jsonl" in repl["retrieval_hint"]
    assert is_apriori_summary(repl)


# --- cap refusal: states cap, preserves raw, no raw payload -----------------

def test_cap_refusal_states_cap_and_preserves_raw():
    refusal = build_cap_refusal(
        tool_name="grep",
        tool_call_id="toolu_xyz",
        original_visible_chars=600_000,
    )
    assert refusal["artifact"] == APRIORI_SUMMARY_MARKER
    assert refusal["tool_call_id"] == "toolu_xyz"
    assert refusal["raw_preserved"] is True
    assert refusal["canonical"] is False
    assert refusal["cap_chars"] == APRIORI_SUMMARY_CAP
    msg = refusal["message"].lower()
    assert "summary=true was requested" in msg
    assert "500000" in refusal["message"]
    assert "not placed" in msg or "not be placed" in msg
    assert "toolu_xyz" in refusal["retrieval_hint"]
    assert "events.jsonl" in refusal["retrieval_hint"]
    assert is_apriori_summary(refusal)


# --- structured raw locator: machine-readable sibling of retrieval_hint -----

def test_replacement_has_structured_raw_locator():
    repl = build_summary_replacement(
        tool_name="bash",
        tool_call_id="toolu_abc",
        summary_text="ok",
        reason="r",
        original_visible_chars=100,
        summary_input_chars=100,
        summary_input_truncated=False,
    )
    loc = repl["raw_locator"]
    assert loc["tool_call_id"] == "toolu_abc"
    assert loc["log"] == EVENTS_LOG_RELPATH == "logs/events.jsonl"
    assert loc["event_type"] == "tool_result"
    # A copy-pasteable query that locates the raw by tool_call_id.
    assert "toolu_abc" in loc["query"]
    assert "events.jsonl" in loc["query"]
    # Human-readable hint is still present for compatibility.
    assert "events.jsonl" in repl["retrieval_hint"]


def test_cap_refusal_has_structured_raw_locator():
    refusal = build_cap_refusal(
        tool_name="grep",
        tool_call_id="toolu_xyz",
        original_visible_chars=600_000,
    )
    loc = refusal["raw_locator"]
    assert loc["tool_call_id"] == "toolu_xyz"
    assert loc["log"] == "logs/events.jsonl"
    assert loc["event_type"] == "tool_result"
    assert "toolu_xyz" in loc["query"]
    assert "events.jsonl" in refusal["retrieval_hint"]


def test_summary_error_has_structured_raw_locator():
    err = build_summary_error(
        tool_name="read",
        tool_call_id="toolu_err",
        original_visible_chars=42,
        error="boom",
    )
    loc = err["raw_locator"]
    assert loc["tool_call_id"] == "toolu_err"
    assert loc["log"] == "logs/events.jsonl"
    assert loc["event_type"] == "tool_result"
    assert "toolu_err" in loc["query"]


def test_raw_locator_handles_missing_tool_call_id():
    repl = build_summary_replacement(
        tool_name="bash",
        tool_call_id=None,
        summary_text="ok",
        reason="r",
        original_visible_chars=10,
        summary_input_chars=10,
        summary_input_truncated=False,
    )
    # Locator is still structured and explicit about the unknown id.
    assert repl["raw_locator"]["tool_call_id"] is None
    assert repl["raw_locator"]["log"] == "logs/events.jsonl"


# --- summary-input metadata: how much was fed to the summarizer -------------

def test_replacement_records_summary_input_metadata():
    repl = build_summary_replacement(
        tool_name="bash",
        tool_call_id="t1",
        summary_text="ok",
        reason="r",
        original_visible_chars=1234,
        summary_input_chars=1234,
        summary_input_truncated=False,
    )
    assert repl["summary_input_chars"] == 1234
    assert repl["summary_input_truncated"] is False


def test_cap_refusal_summary_input_metadata_is_zero_untruncated():
    # No LLM input exists on the cap-refusal path (LLM not called).
    refusal = build_cap_refusal(
        tool_name="grep",
        tool_call_id="t1",
        original_visible_chars=600_000,
    )
    assert refusal["summary_input_chars"] == 0
    assert refusal["summary_input_truncated"] is False


def test_orchestrator_success_records_full_untruncated_input():
    raw = {"matches": ["foo", "bar"], "count": 2}
    captured = {}

    def fake(system_prompt, user_prompt, tool_name, tool_call_id):
        captured["user_prompt"] = user_prompt
        return "SUM"

    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "r"},
        tool_name="grep",
        tool_call_id="t1",
        summarizer_fn=fake,
    )
    # The a-priori path feeds the FULL formal visible payload to the summarizer
    # (it caps-and-refuses above 500k rather than truncating), so the recorded
    # input length equals the original visible length and is never truncated.
    assert out["summary_input_chars"] == out["original_visible_chars"]
    assert out["summary_input_truncated"] is False


# --- orchestrator: summary=false is an exact no-op --------------------------

def test_summary_false_is_noop():
    raw = {"stdout": "x" * 1000}
    out = maybe_summarize_result(
        raw,
        args={"summary": False},
        tool_name="bash",
        tool_call_id="t1",
        summarizer_fn=lambda sp, up, tn, cid: "should not be called",
    )
    assert out is raw


def test_missing_summary_is_noop():
    raw = {"stdout": "x" * 1000}
    out = maybe_summarize_result(
        raw,
        args={},
        tool_name="bash",
        tool_call_id="t1",
        summarizer_fn=lambda sp, up, tn, cid: "should not be called",
    )
    assert out is raw


def test_no_summarizer_wired_fails_closed():
    # summary=true but no summarizer wired → fail closed to a summary-layer
    # error (NOT the raw). The raw must never reach the wire under summary=true.
    raw = {"stdout": "RAWNOWIRE-" + "x" * 1000}
    out = maybe_summarize_result(
        raw,
        args={"summary": True},
        tool_name="bash",
        tool_call_id="t1",
        summarizer_fn=None,
    )
    assert out is not raw
    assert is_apriori_summary(out)
    assert out["status"] == "summary_unavailable"
    # Raw is not leaked into the model-visible error.
    assert "RAWNOWIRE" not in str(out)
    # The locator points at the preserved raw by tool_call_id.
    assert "t1" in out["retrieval_hint"]
    assert "events.jsonl" in out["retrieval_hint"]


# --- orchestrator: summary=true under cap → generated replacement -----------

def test_summary_true_under_cap_generates_replacement():
    captured = {}

    def fake_summarizer(system_prompt, user_prompt, tool_name, tool_call_id):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        captured["tool_name"] = tool_name
        return "SUMMARY: 3 matches in 2 files"

    raw = {"matches": [{"file": "a.py", "line": 1, "text": "foo"}], "count": 1}
    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "Find where foo is defined"},
        tool_name="grep",
        tool_call_id="toolu_1",
        summarizer_fn=fake_summarizer,
    )
    # The raw is NOT what reaches the wire.
    assert out is not raw
    assert is_apriori_summary(out)
    assert out["generated_summary"] == "SUMMARY: 3 matches in 2 files"
    # The reason drove the prompt, and raw payload was fenced as untrusted.
    assert "Find where foo is defined" in captured["user_prompt"]
    assert "UNTRUSTED" in captured["user_prompt"]
    assert "untrusted" in captured["system_prompt"].lower()
    # The user prompt also asks for a brief critique of the reasoning/retention
    # spec, as plain text — no structured prompt_quality field is introduced —
    # and to suggest inspecting the raw original when the reason is too poor.
    up_low = captured["user_prompt"].lower()
    assert "critiqu" in up_low
    assert "raw original" in up_low
    assert "prompt_quality" not in str(out)
    # Raw content text is present in the prompt (so the LLM can summarize it),
    # but NOT in the model-visible replacement.
    assert "foo" in captured["user_prompt"]
    assert "foo" not in str(out.get("generated_summary"))


# --- orchestrator: over cap → refusal, no LLM call --------------------------

def test_summary_true_over_cap_refuses_without_llm():
    called = {"n": 0}

    def fake_summarizer(system_prompt, user_prompt, tool_name, tool_call_id):
        called["n"] += 1
        return "should not happen"

    raw = {"stdout": "y" * (APRIORI_SUMMARY_CAP + 10)}
    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "r"},
        tool_name="bash",
        tool_call_id="toolu_big",
        summarizer_fn=fake_summarizer,
    )
    assert called["n"] == 0  # LLM never called
    assert is_apriori_summary(out)
    assert out["status"] == "summary_unavailable"
    assert out["cap_chars"] == APRIORI_SUMMARY_CAP
    assert "toolu_big" in out["retrieval_hint"]


# --- orchestrator: tool errors are not summarized ---------------------------

def test_error_result_not_summarized():
    raw = {"status": "error", "message": "boom: file not found"}
    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "r"},
        tool_name="read",
        tool_call_id="t_err",
        summarizer_fn=lambda sp, up, tn, cid: "nope",
    )
    assert out is raw  # exact error text preserved for recovery


# --- orchestrator: summarizer failure is fail-closed (never leaks raw) ------

def test_summarizer_exception_fails_closed():
    def boom(system_prompt, user_prompt, tool_name, tool_call_id):
        raise RuntimeError("provider down")

    raw = {"stdout": "secret raw content"}
    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "r"},
        tool_name="bash",
        tool_call_id="t_fail",
        summarizer_fn=boom,
    )
    assert is_apriori_summary(out)
    assert out["status"] == "summary_unavailable"
    # The raw must not be present in the model-visible error.
    assert "secret raw content" not in str(out)
    assert "t_fail" in out["retrieval_hint"]


def test_summarizer_empty_output_fails_closed():
    raw = {"stdout": "raw"}
    out = maybe_summarize_result(
        raw,
        args={"summary": True, "_reasoning": "r"},
        tool_name="bash",
        tool_call_id="t_empty",
        summarizer_fn=lambda sp, up, tn, cid: "   ",
    )
    assert is_apriori_summary(out)
    assert out["status"] == "summary_unavailable"


def test_summarizer_exception_error_text_is_bounded_and_sanitized():
    """A provider exception carrying a huge HTML challenge page (e.g. a
    Cloudflare block on a PermissionDeniedError) must NOT be dumped verbatim
    into model-visible context. The fail-closed error keeps the exception class
    plus a short, length-capped preview — enough to diagnose, without leaking
    the full HTML or ballooning the context. Regression for the live PR #586
    observation (visible error ~23k–35k chars of provider HTML).
    """
    from lingtai_kernel.tool_result_summary import APRIORI_SUMMARY_ERROR_MAX_CHARS

    # A challenge page is a huge run of repeated tokens — the live shape.
    html_blob = "<html>" + ("CLOUDFLARE-CHALLENGE " * 5000) + "</html>"

    class PermissionDeniedError(Exception):
        pass

    def boom(system_prompt, user_prompt, tool_name, tool_call_id):
        raise PermissionDeniedError(html_blob)

    out = maybe_summarize_result(
        {"stdout": "x" * 100},
        args={"summary": True, "_reasoning": "r"},
        tool_name="bash",
        tool_call_id="t_html",
        summarizer_fn=boom,
    )
    assert is_apriori_summary(out)
    assert out["status"] == "summary_unavailable"
    error_text = out["error"]
    # The exception class survives for diagnosis.
    assert "PermissionDeniedError" in error_text
    # Bounded length — at most the cap, and far smaller than the raw blob, so a
    # tens-of-thousands-char HTML dump never reaches model-visible context.
    assert len(error_text) <= APRIORI_SUMMARY_ERROR_MAX_CHARS
    assert len(error_text) < 4000
    assert len(error_text) < len(html_blob)
    # The wall of identical repeated tokens is collapsed, not dumped verbatim:
    # at most a couple of literal copies survive (boundary tokens), never the
    # thousands that were raised.
    assert error_text.count("CLOUDFLARE-CHALLENGE") <= 3


# --- orchestrator: already-summarized is idempotent -------------------------

def test_already_summarized_passes_through():
    already = build_summary_replacement(
        tool_name="bash",
        tool_call_id="t1",
        summary_text="prior",
        reason="r",
        original_visible_chars=10,
        summary_input_chars=10,
        summary_input_truncated=False,
    )
    out = maybe_summarize_result(
        already,
        args={"summary": True, "_reasoning": "r"},
        tool_name="bash",
        tool_call_id="t1",
        summarizer_fn=lambda sp, up, tn, cid: "again",
    )
    assert out is already
