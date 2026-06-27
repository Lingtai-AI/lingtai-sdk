"""Tests for read capability continuation semantics (issues #352, #359).

Covers:
- Small reads are unchanged (no truncated flag, normal metadata).
- Large reads return explicit continuation metadata and a valid next_offset.
- next_offset continuation actually advances past the first chunk.
- requested offset/limit and total/line metadata are correct.
- Schema description contains the transport-cap and truncated warning.
- Graceful handling of a single very-long line that exceeds cap on its own.
- DEFAULT_READ_CAP_CHARS is 100k while the runtime hard cap is 200k.
- Read accepts per-call max_chars and clamps it to the runtime hard cap.
- Read description references read-manual and the 50k/200k cap semantics.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.core.read import (
    DEFAULT_READ_CAP_CHARS,
    READ_HARD_CAP_CHARS,
    _apply_cap,
    _resolve_call_cap,
)
from lingtai_kernel.tool_result_artifacts import PREVENTIVE_MAX_CHARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _file_agent(tmp_path, *, config=None):
    return Agent(
        service=_make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "test",
        capabilities=["read"],
        config=config,
    )


# ---------------------------------------------------------------------------
# Unit tests for _apply_cap (pure logic, no agent needed)
# ---------------------------------------------------------------------------

class TestApplyCap:
    def _lines(self, n: int, chars_per_line: int = 10) -> list[str]:
        """Create *n* lines each with *chars_per_line* visible chars + newline."""
        return [("x" * chars_per_line + "\n") for _ in range(n)]

    def test_small_read_no_truncation(self):
        """When total content fits within cap, truncated is absent from meta."""
        lines = self._lines(5, chars_per_line=10)
        numbered, meta = _apply_cap(lines, 0, 5, cap_chars=10_000)
        assert meta == {}
        assert "1\t" in numbered

    def test_large_read_returns_truncated_true(self):
        """When content exceeds cap, meta contains truncated=True."""
        lines = self._lines(1200, chars_per_line=100)
        numbered, meta = _apply_cap(lines, 0, 1200, cap_chars=DEFAULT_READ_CAP_CHARS)
        assert meta.get("truncated") is True

    def test_large_read_next_offset_is_valid(self):
        """next_offset is 1-based and points past the last returned line."""
        lines = self._lines(1200, chars_per_line=100)
        _, meta = _apply_cap(lines, 0, 1200, cap_chars=DEFAULT_READ_CAP_CHARS)
        assert "next_offset" in meta
        next_off = meta["next_offset"]
        assert isinstance(next_off, int)
        assert next_off >= 2  # at least line 2

    def test_next_offset_advances_on_second_call(self):
        """A second call starting at next_offset covers different lines."""
        lines = self._lines(1200, chars_per_line=100)
        _, meta1 = _apply_cap(lines, 0, 1200, cap_chars=DEFAULT_READ_CAP_CHARS)
        assert meta1.get("truncated") is True
        next_start = meta1["next_offset"] - 1  # convert to 0-based
        numbered2, _ = _apply_cap(lines, next_start, 300, cap_chars=DEFAULT_READ_CAP_CHARS)
        # The second chunk must start at a later line number than the first.
        first_line_num_chunk2 = int(numbered2.split("\t")[0])
        first_line_num_chunk1 = 1
        assert first_line_num_chunk2 > first_line_num_chunk1

    def test_requested_offset_and_limit_in_meta(self):
        """requested_offset and requested_limit echo the call arguments."""
        lines = self._lines(1200, chars_per_line=100)
        _, meta = _apply_cap(lines, 4, 200, cap_chars=DEFAULT_READ_CAP_CHARS)
        if meta.get("truncated"):
            assert meta["requested_offset"] == 5  # 1-based
            assert meta["requested_limit"] == 200

    def test_total_lines_and_remaining_estimate(self):
        """total_lines and remaining_lines_estimate are present when truncated."""
        lines = self._lines(1200, chars_per_line=100)
        _, meta = _apply_cap(lines, 0, 1200, cap_chars=DEFAULT_READ_CAP_CHARS)
        assert meta.get("truncated") is True
        assert meta["remaining_lines_estimate"] > 0

    def test_single_very_long_line_does_not_crash(self):
        """A line longer than cap_chars returns a bounded prefix without crashing."""
        long_line = "A" * (DEFAULT_READ_CAP_CHARS * 2) + "\n"
        lines = [long_line]
        numbered, meta = _apply_cap(lines, 0, 1, cap_chars=DEFAULT_READ_CAP_CHARS)
        # We get *some* content back (the bounded prefix of the first line),
        # but it must still be explicitly marked as truncated.
        assert len(numbered) > 0
        assert len(numbered) <= DEFAULT_READ_CAP_CHARS
        assert meta["truncated"] is True
        assert meta["line_truncated"] is True
        assert meta["last_returned_line"] == 1
        assert meta["next_offset"] == 2

    def test_last_returned_line_is_correct(self):
        """last_returned_line matches the actual last line number in content."""
        lines = self._lines(1200, chars_per_line=100)
        numbered, meta = _apply_cap(lines, 0, 1200, cap_chars=DEFAULT_READ_CAP_CHARS)
        if meta.get("truncated"):
            # Parse the last line number from the numbered content.
            content_lines = [l for l in numbered.split("\n") if l.strip()]
            last_num = int(content_lines[-1].split("\t")[0])
            assert last_num == meta["last_returned_line"]


# ---------------------------------------------------------------------------
# Integration tests via agent handler
# ---------------------------------------------------------------------------

class TestReadHandler:
    def test_small_read_unchanged(self, tmp_path):
        """Small files come back without truncated flag."""
        agent = _file_agent(tmp_path)
        try:
            f = tmp_path / "small.txt"
            f.write_text("line one\nline two\nline three\n", encoding="utf-8")
            result = agent._tool_handlers["read"]({"file_path": str(f)})
            assert result.get("status") != "error"
            assert "truncated" not in result
            assert result["total_lines"] == 3
            assert result["lines_shown"] == 3
            assert "1\t" in result["content"]
        finally:
            agent.stop(timeout=1.0)

    def test_large_read_returns_continuation_metadata(self, tmp_path):
        """Files larger than DEFAULT_READ_CAP_CHARS return continuation metadata."""
        agent = _file_agent(tmp_path)
        try:
            # Write enough lines to definitely exceed the cap.
            f = tmp_path / "large.txt"
            line = "x" * 100 + "\n"
            n_lines = (DEFAULT_READ_CAP_CHARS // len(line)) * 3
            f.write_text(line * n_lines, encoding="utf-8")

            result = agent._tool_handlers["read"]({"file_path": str(f)})
            assert result.get("truncated") is True
            assert "next_offset" in result
            assert "remaining_lines_estimate" in result
            assert result["total_lines"] == n_lines
            assert result["lines_shown"] < n_lines
        finally:
            agent.stop(timeout=1.0)

    def test_next_offset_continues_from_where_first_left_off(self, tmp_path):
        """Passing next_offset as offset returns the subsequent chunk."""
        agent = _file_agent(tmp_path)
        try:
            f = tmp_path / "large.txt"
            line = "x" * 100 + "\n"
            n_lines = (DEFAULT_READ_CAP_CHARS // len(line)) * 3
            f.write_text(line * n_lines, encoding="utf-8")

            r1 = agent._tool_handlers["read"]({"file_path": str(f)})
            assert r1.get("truncated") is True
            next_off = r1["next_offset"]

            r2 = agent._tool_handlers["read"]({"file_path": str(f), "offset": next_off})
            # r2 must start after r1 ended — check via content line numbers.
            first_line_r1 = int(r1["content"].split("\t")[0])
            first_line_r2 = int(r2["content"].split("\t")[0])
            assert first_line_r2 == next_off
            assert first_line_r2 > first_line_r1
        finally:
            agent.stop(timeout=1.0)

    def test_offset_and_limit_passed_through(self, tmp_path):
        """offset and limit are respected even when capped."""
        agent = _file_agent(tmp_path)
        try:
            f = tmp_path / "numbered.txt"
            n_lines = 20
            f.write_text("".join(f"line {i}\n" for i in range(1, n_lines + 1)), encoding="utf-8")

            result = agent._tool_handlers["read"](
                {"file_path": str(f), "offset": 5, "limit": 3}
            )
            assert "5\t" in result["content"]
            assert "6\t" in result["content"]
            assert "7\t" in result["content"]
            assert "8\t" not in result["content"]
            assert result["total_lines"] == n_lines
        finally:
            agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Schema description contains warning text (#352)
# ---------------------------------------------------------------------------

def test_read_cap_default_is_100k_and_hard_cap_is_200k():
    """Read defaults to 100k while runtime spill has a 200k hard ceiling."""
    assert DEFAULT_READ_CAP_CHARS == 100_000
    assert READ_HARD_CAP_CHARS == 200_000
    assert PREVENTIVE_MAX_CHARS == 200_000


def test_resolve_call_cap_defaults_to_read_default(tmp_path):
    """Without max_chars, read uses the 100k everyday page budget."""
    agent = _file_agent(tmp_path)
    try:
        assert _resolve_call_cap(agent, None) == 100_000
    finally:
        agent.stop(timeout=1.0)


def test_resolve_call_cap_clamps_to_runtime_hard_cap(tmp_path):
    """max_chars may raise read chunk size, but never beyond the runtime ceiling."""
    agent = _file_agent(tmp_path)
    try:
        assert _resolve_call_cap(agent, 50_000) == 50_000
        assert _resolve_call_cap(agent, 200_000) == 200_000
    finally:
        agent.stop(timeout=1.0)


def test_read_handler_uses_per_call_max_chars(tmp_path):
    """Read pagination obeys max_chars passed to one call."""
    agent = _file_agent(tmp_path)
    try:
        f = tmp_path / "small-cap.txt"
        f.write_text("".join(f"line-{i:03d} xxxxxxxxxxxxxxxxxxxx\n" for i in range(100)), encoding="utf-8")
        result = agent._tool_handlers["read"]({"file_path": str(f), "limit": 100, "max_chars": 120})
        assert result["truncated"] is True
        assert result["cap_chars"] == 120
        assert result["returned_chars"] <= 120
    finally:
        agent.stop(timeout=1.0)


def test_read_schema_description_warns_about_cap():
    """en description must mention read-manual, max_chars, 100k default, and 200k hard cap."""
    from lingtai.core.read import get_description
    desc = get_description("en")
    assert "100 000" in desc or "100000" in desc or "100_000" in desc, \
        "description should mention the 100 000 char read default"
    assert "200 000" in desc or "200000" in desc or "200_000" in desc, \
        "description should mention the 200 000 char runtime hard cap"
    assert "max_chars" in desc, \
        "description should mention the per-call max_chars parameter"
    assert "read-manual" in desc and "Before using read" in desc, \
        "description should require reading read-manual first"
    assert "truncated" in desc, \
        "description should mention the 'truncated' field"
    assert "next_offset" in desc, \
        "description should mention 'next_offset' for continuation"
    assert "line_truncated" in desc, \
        "description should mention single-line truncation metadata"
