"""Tests for token_ledger.append_token_entry — including the optional extra kwarg
that lets daemon writes carry attribution tags into the parent's ledger."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lingtai_kernel.services.logging import (
    doctor_sqlite_event_index,
    query_sqlite_event_index,
)
from lingtai_kernel.token_ledger import (
    append_token_entry,
    is_daemon_entry,
    sum_token_ledger,
)


def test_sum_empty_ledger(tmp_path):
    """Sum of a non-existent ledger returns zeros."""
    result = sum_token_ledger(tmp_path / "token_ledger.jsonl")
    assert result == {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_tokens": 0,
        "api_calls": 0,
    }


def test_append_and_sum(tmp_path):
    """Appending entries and summing them returns correct totals."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    append_token_entry(path, input=200, output=100, thinking=30, cached=40)

    result = sum_token_ledger(path)
    assert result == {
        "input_tokens": 300,
        "output_tokens": 150,
        "thinking_tokens": 40,
        "cached_tokens": 60,
        "api_calls": 2,
    }


def test_sum_ignores_corrupt_lines(tmp_path):
    """Corrupt JSONL lines are skipped without error."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    with open(path, "a") as f:
        f.write("not valid json\n")
    append_token_entry(path, input=200, output=100, thinking=30, cached=40)

    result = sum_token_ledger(path)
    assert result == {
        "input_tokens": 300,
        "output_tokens": 150,
        "thinking_tokens": 40,
        "cached_tokens": 60,
        "api_calls": 2,
    }


def test_append_creates_parent_dirs(tmp_path):
    """append_token_entry creates parent directories if missing."""
    path = tmp_path / "logs" / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    assert path.is_file()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["input"] == 100
    assert "ts" in entry


def test_append_standard_token_ledger_mirrors_to_sqlite(tmp_path):
    """Standard logs/token_ledger.jsonl writes get a best-effort SQLite sidecar."""
    path = tmp_path / "logs" / "token_ledger.jsonl"
    append_token_entry(
        path,
        input=100,
        output=20,
        thinking=5,
        cached=40,
        model="gpt-test",
        endpoint="https://api.example",
        extra={"source": "main", "api_call_id": "api_1"},
    )

    rows = query_sqlite_event_index(
        tmp_path,
        """
        SELECT input_tokens, output_tokens, thinking_tokens, cached_tokens,
               model, endpoint, source, api_call_id, source_kind, scope, run_id
        FROM token_entries
        """,
    )
    assert rows == [
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 5,
            "cached_tokens": 40,
            "model": "gpt-test",
            "endpoint": "https://api.example",
            "source": "main",
            "api_call_id": "api_1",
            "source_kind": "agent_token_ledger",
            "scope": "agent",
            "run_id": None,
        }
    ]
    doctor = doctor_sqlite_event_index(tmp_path)
    assert doctor["token_entry_count"] == 1


def test_append_token_sqlite_mirror_fail_open(tmp_path, monkeypatch):
    """SQLite mirror errors must not break the authoritative JSONL append."""
    from lingtai_kernel.services import logging as logging_service

    def boom(self, *args, **kwargs):
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(logging_service.SQLiteEventIndex, "log_token_entry", boom)
    path = tmp_path / "logs" / "token_ledger.jsonl"
    append_token_entry(path, input=7, output=3, thinking=1, cached=2)

    assert path.is_file()
    assert sum_token_ledger(path)["input_tokens"] == 7


def test_append_entry_has_timestamp(tmp_path):
    """Each entry has a ts field with ISO 8601 UTC timestamp."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=1, output=2, thinking=3, cached=4)
    entry = json.loads(path.read_text().strip())
    assert "ts" in entry
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
    assert dt.tzinfo is not None


def test_append_token_entry_basic(tmp_path):
    """Default behavior: writes ts + 4 numeric fields."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["input"] == 10
    assert entry["output"] == 5
    assert entry["thinking"] == 2
    assert entry["cached"] == 1
    assert "ts" in entry
    assert "source" not in entry  # no extras


def test_append_token_entry_with_extra(tmp_path):
    """`extra` dict keys merge into the entry before serialization."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        extra={"source": "daemon", "em_id": "em-3",
               "run_id": "em-3-20260427-094215-a1b2c3"},
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["source"] == "daemon"
    assert entry["em_id"] == "em-3"
    assert entry["run_id"] == "em-3-20260427-094215-a1b2c3"
    # numeric fields still present
    assert entry["input"] == 10


def test_extra_does_not_break_summing(tmp_path):
    """sum_token_ledger ignores unknown keys — daemon tags do not affect totals."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    append_token_entry(
        path,
        input=20, output=8, thinking=3, cached=4,
        extra={"source": "daemon", "em_id": "em-1", "run_id": "x"},
    )
    totals = sum_token_ledger(path)
    assert totals["input_tokens"] == 30
    assert totals["output_tokens"] == 13
    assert totals["thinking_tokens"] == 5
    assert totals["cached_tokens"] == 5
    assert totals["api_calls"] == 2


def test_extra_cannot_override_required_fields(tmp_path):
    """Required fields (input/output/thinking/cached/ts) take precedence over `extra`.

    This protects against accidental tag conflicts. If a caller passes
    extra={"input": 999}, the explicit input=10 still wins.
    """
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        extra={"input": 999, "ts": "fake"},
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["input"] == 10
    assert entry["ts"] != "fake"


def test_append_with_model_and_endpoint(tmp_path):
    """`model` and `endpoint` kwargs are written as first-class fields."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        model="claude-opus-4-7",
        endpoint="https://api.anthropic.com",
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["model"] == "claude-opus-4-7"
    assert entry["endpoint"] == "https://api.anthropic.com"
    assert entry["input"] == 10


def test_model_and_endpoint_omitted_when_none(tmp_path):
    """When model/endpoint are None, the keys are not written (avoid noisy nulls)."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    entry = json.loads(path.read_text().splitlines()[0])
    assert "model" not in entry
    assert "endpoint" not in entry


def test_model_overrides_extra(tmp_path):
    """Top-level model/endpoint kwargs win over keys in extra."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        model="real-model",
        endpoint="https://real.example",
        extra={"model": "fake-model", "endpoint": "https://fake.example"},
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["model"] == "real-model"
    assert entry["endpoint"] == "https://real.example"


def test_model_endpoint_do_not_break_summing(tmp_path):
    """sum_token_ledger ignores model/endpoint — totals unaffected."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path, input=10, output=5, thinking=2, cached=1,
        model="m1", endpoint="https://a.example",
    )
    append_token_entry(
        path, input=20, output=8, thinking=3, cached=4,
        model="m2", endpoint="https://b.example",
    )
    totals = sum_token_ledger(path)
    assert totals == {
        "input_tokens": 30,
        "output_tokens": 13,
        "thinking_tokens": 5,
        "cached_tokens": 5,
        "api_calls": 2,
    }


# ---------------------------------------------------------------------------
# Reporting-scope contract (T5) — daemon-row identification and scoped sums.
#
# These tests lift the module-level reporting-scope contract into executable
# form: which rows a report counts depends on the question being asked. They
# also pin the intentional retention of daemon rows in a parent ledger — the
# durable rows are never rewritten; scope is a pure read-side filter.
# ---------------------------------------------------------------------------


def test_is_daemon_entry_by_source():
    """A row tagged source='daemon' is a daemon row."""
    assert is_daemon_entry({"source": "daemon", "input": 1}) is True


def test_is_daemon_entry_by_em_id_or_run_id():
    """em_id/run_id attribution marks a daemon row even without source tag."""
    assert is_daemon_entry({"em_id": "em-3", "input": 1}) is True
    assert is_daemon_entry({"run_id": "em-3-20260624-000000-abc", "input": 1}) is True


def test_tc_wake_is_not_a_daemon_entry():
    """tc_wake is an involuntary splice in the agent's own context, NOT a daemon.

    Guards against the easy mistake of treating every non-'main' source as
    daemon spend. tc_wake/heal/soul/notification_sync run in the parent's own
    context and must survive a main-agent-only filter.
    """
    for source in ("main", "tc_wake", "heal", "soul",
                   "notification_sync", "summarize"):
        assert is_daemon_entry({"source": source, "input": 1}) is False


def test_is_daemon_entry_legacy_untagged_row():
    """A legacy row with no source/em_id/run_id is not a daemon row."""
    assert is_daemon_entry({"input": 10, "output": 5}) is False


def test_sum_scope_all_includes_daemon_rows(tmp_path):
    """Default scope='all' counts daemon rows — the parent paid for that spend.

    Parent ledger lifetime totals intentionally include daemon emanations.
    """
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1,
                       extra={"source": "main"})
    append_token_entry(path, input=20, output=8, thinking=3, cached=4,
                       extra={"source": "daemon", "em_id": "em-1",
                              "run_id": "em-1-x"})

    totals = sum_token_ledger(path)  # default scope
    assert totals["input_tokens"] == 30
    assert totals["api_calls"] == 2
    # explicit scope='all' is identical to the default
    assert sum_token_ledger(path, scope="all") == totals


def test_sum_scope_main_agent_excludes_daemon_rows(tmp_path):
    """scope='main_agent' row-level excludes daemon rows but keeps tc_wake."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1,
                       extra={"source": "main"})
    append_token_entry(path, input=7, output=3, thinking=1, cached=0,
                       extra={"source": "tc_wake"})
    # daemon rows in the parent ledger — excluded from a main-agent report
    append_token_entry(path, input=20, output=8, thinking=3, cached=4,
                       extra={"source": "daemon", "em_id": "em-1",
                              "run_id": "em-1-x"})
    append_token_entry(path, input=100, output=50, thinking=10, cached=5,
                       extra={"em_id": "em-2", "run_id": "em-2-y"})

    main = sum_token_ledger(path, scope="main_agent")
    # only the main + tc_wake rows are counted (10+7 input, 2 calls)
    assert main["input_tokens"] == 17
    assert main["output_tokens"] == 8
    assert main["api_calls"] == 2

    # the durable ledger still holds all four rows; scope is read-side only
    assert sum_token_ledger(path, scope="all")["api_calls"] == 4


def test_sum_scope_main_agent_legacy_untagged_rows_kept(tmp_path):
    """Legacy rows missing source/model/endpoint/schema_version are kept as
    main-agent spend (conservative: not attributed to a daemon)."""
    path = tmp_path / "ledger.jsonl"
    # legacy row: numeric fields only, no source/model/endpoint
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    append_token_entry(path, input=20, output=8, thinking=3, cached=4,
                       extra={"source": "daemon", "em_id": "em-1",
                              "run_id": "em-1-x"})

    main = sum_token_ledger(path, scope="main_agent")
    assert main["input_tokens"] == 10  # legacy row kept, daemon row dropped
    assert main["api_calls"] == 1


def test_sum_unknown_scope_raises(tmp_path):
    """An unknown scope raises rather than silently falling through to 'all'."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    with pytest.raises(ValueError):
        sum_token_ledger(path, scope="parent_only")


def test_parent_child_aggregation_must_dedup(tmp_path):
    """Summing parent + daemon-own ledgers naively DOUBLE-counts daemon spend.

    The daemon mirrors each call into both its own ledger and the parent's,
    tagged with the same run_id. This test documents the hazard and shows the
    dedup-aware fix (sum the parent ledger alone, which already contains the
    daemon rows, OR key on run_id+ts).
    """
    parent = tmp_path / "parent.jsonl"
    daemon = tmp_path / "daemon.jsonl"

    # parent's own main call
    append_token_entry(parent, input=10, output=5, thinking=2, cached=1,
                       extra={"source": "main"})
    # one daemon call, mirrored into BOTH ledgers with the same run_id
    daemon_extra = {"source": "daemon", "em_id": "em-1",
                    "run_id": "em-1-20260624-000000-abc"}
    append_token_entry(parent, input=20, output=8, thinking=3, cached=4,
                       extra=dict(daemon_extra))
    append_token_entry(daemon, input=20, output=8, thinking=3, cached=4,
                       extra=dict(daemon_extra))

    # NAIVE parent + child sum double-counts the daemon's 20 input tokens.
    naive = (sum_token_ledger(parent)["input_tokens"]
             + sum_token_ledger(daemon)["input_tokens"])
    assert naive == 50  # 10 (main) + 20 (parent's daemon row) + 20 (daemon own)

    # DEDUP-AWARE: the parent ledger alone already contains the daemon spend.
    correct = sum_token_ledger(parent)["input_tokens"]
    assert correct == 30  # 10 (main) + 20 (daemon), counted once

    # Equivalent dedup keyed on (run_id, ts, input) across both files: the
    # mirrored daemon entries are identical on those fields, so the second copy
    # collapses while the parent's distinct main row is kept.
    seen = set()
    deduped = 0
    for p in (parent, daemon):
        for line in p.read_text().splitlines():
            entry = json.loads(line)
            key = (entry.get("run_id"), entry["ts"], entry["input"])
            if entry.get("run_id") and key in seen:
                continue
            seen.add(key)
            deduped += entry["input"]
    assert deduped == 30
