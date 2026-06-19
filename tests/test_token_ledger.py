"""Tests for token_ledger.append_token_entry — including the optional extra kwarg
that lets daemon writes carry attribution tags into the parent's ledger."""
from __future__ import annotations

import json
from pathlib import Path

from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger


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
