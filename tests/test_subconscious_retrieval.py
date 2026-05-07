"""Retrieval test for subconscious architectures.

Tests whether the subconscious can actually retrieve relevant past experiences
when the current context is related to a past snapshot.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_agent(**config_overrides):
    """Create a mock agent with subconscious config."""
    agent = MagicMock()
    agent._working_dir = Path(tempfile.mkdtemp())
    agent._shutdown = MagicMock(is_set=MagicMock(return_value=False))
    agent._subconscious_insights = []
    agent._log = MagicMock()

    config = MagicMock()
    config.subconscious_enabled = True
    config.subconscious_provider = "openrouter"
    config.subconscious_model = "mimo-2-cheap"
    config.subconscious_confidence_threshold = 0.6
    config.subconscious_sample_n = 2
    config.subconscious_context_window = 128000
    config.retry_timeout = 300
    config.provider = "openrouter"
    config.model = "mimo-2-cheap"
    for k, v in config_overrides.items():
        setattr(config, k, v)
    agent._config = config

    agent.service = MagicMock()
    agent.agent_name = "test-agent"
    return agent


def _make_snapshot(stem: str, content: str):
    """Create a mock snapshot path and interface."""
    path = MagicMock()
    path.stem = stem

    iface = MagicMock()
    entry = MagicMock()
    entry.role = "assistant"
    entry.content = [MagicMock(text=content, spec=["text"])]
    iface.entries = [entry]
    return path, iface


# ── Scenario 1: Async Python bug ──────────────────────────────────────

ASYNC_SNAPSHOT_CONTENT = (
    "I solved the async Python bug. The issue was a race condition in the "
    "data pipeline — multiple coroutines were writing to the same file "
    "without proper locking. I used asyncio.gather() with a semaphore to "
    "limit concurrency and added file locking with fcntl.flock(). The fix "
    "was to wrap the file write in an async context manager that acquires "
    "the lock before writing."
)

ASYNC_CURRENT_DIARY = (
    "Working on an async Python bug in the ingestion pipeline. Multiple "
    "coroutines are corrupting the output file. Looks like a race condition."
)


class TestRetrievalAsyncBug:
    """Test: can the subconscious retrieve the async bug fix from a snapshot?"""

    def test_retrieval_with_mocked_llm(self):
        """When LLM returns a relevant insight, it should be stored."""
        agent = _make_agent()

        # Mock the LLM to return a relevant insight.
        mock_response = MagicMock()
        mock_tail = MagicMock()
        mock_tail.role = "assistant"
        text_block = MagicMock()
        text_block.text = json.dumps({
            "insight": "Past self solved a similar async race condition using asyncio.gather() with semaphore and fcntl.flock(). Consider the same pattern here.",
            "confidence": 0.85,
            "source_memory": "async bug fix in data pipeline",
        })
        mock_tail.content = [text_block]
        mock_response.interface = MagicMock(entries=[mock_tail])

        snap_path, snap_iface = _make_snapshot("snap_async_bug", ASYNC_SNAPSHOT_CONTENT)

        with patch("lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                   return_value=ASYNC_CURRENT_DIARY), \
             patch("lingtai_kernel.intrinsics.soul.consultation._list_snapshot_paths",
                   return_value=[snap_path]), \
             patch("lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                   return_value=MagicMock()), \
             patch.object(agent.service, "create_session", return_value=mock_response):

            from lingtai_kernel.intrinsics.soul.subconscious import _subconscious_fire_worker
            _subconscious_fire_worker(agent)

        # Should have stored the insight.
        assert len(agent._subconscious_insights) == 1
        insight = agent._subconscious_insights[0]
        assert "asyncio.gather" in insight["insight"]
        assert insight["confidence"] == 0.85
        assert "snap_async_bug" in insight["source"]

    def test_retrieval_no_match(self):
        """When LLM returns null insight, nothing should be stored."""
        agent = _make_agent()

        mock_response = MagicMock()
        mock_tail = MagicMock()
        mock_tail.role = "assistant"
        text_block = MagicMock()
        text_block.text = json.dumps({"insight": None})
        mock_tail.content = [text_block]
        mock_response.interface = MagicMock(entries=[mock_tail])

        snap_path, snap_iface = _make_snapshot("snap_unrelated", "Cooking recipe for pasta.")

        with patch("lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                   return_value=ASYNC_CURRENT_DIARY), \
             patch("lingtai_kernel.intrinsics.soul.consultation._list_snapshot_paths",
                   return_value=[snap_path]), \
             patch("lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                   return_value=MagicMock()), \
             patch.object(agent.service, "create_session", return_value=mock_response):

            from lingtai_kernel.intrinsics.soul.subconscious import _subconscious_fire_worker
            _subconscious_fire_worker(agent)

        # Should NOT have stored anything.
        assert len(agent._subconscious_insights) == 0


# ── Scenario 2: MCP debugging ─────────────────────────────────────────

MCP_SNAPSHOT_CONTENT = (
    "Debugged an MCP server issue. The problem was that the stdio transport "
    "was buffering output, causing the client to hang waiting for responses. "
    "Fix: flush stdout after each JSON-RPC response, and set PYTHONUNBUFFERED=1 "
    "in the environment. Also added a health check endpoint."
)

MCP_CURRENT_DIARY = (
    "Debugging a different MCP server that uses SSE transport. The client "
    "sometimes misses events. Might be a similar buffering issue."
)


class TestRetrievalMCPDebug:
    """Test: can the subconscious retrieve the MCP debugging pattern?"""

    def test_cross_domain_transfer(self):
        """LLM should recognize the MCP debugging pattern across different transports."""
        agent = _make_agent()

        mock_response = MagicMock()
        mock_tail = MagicMock()
        mock_tail.role = "assistant"
        text_block = MagicMock()
        text_block.text = json.dumps({
            "insight": "Past self debugged MCP buffering issues with stdio transport. SSE might have similar buffering — check if events are being buffered before reaching the client.",
            "confidence": 0.75,
            "source_memory": "MCP stdio buffering fix",
        })
        mock_tail.content = [text_block]
        mock_response.interface = MagicMock(entries=[mock_tail])

        snap_path, snap_iface = _make_snapshot("snap_mcp_debug", MCP_SNAPSHOT_CONTENT)

        with patch("lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                   return_value=MCP_CURRENT_DIARY), \
             patch("lingtai_kernel.intrinsics.soul.consultation._list_snapshot_paths",
                   return_value=[snap_path]), \
             patch("lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                   return_value=MagicMock()), \
             patch.object(agent.service, "create_session", return_value=mock_response):

            from lingtai_kernel.intrinsics.soul.subconscious import _subconscious_fire_worker
            _subconscious_fire_worker(agent)

        assert len(agent._subconscious_insights) == 1
        insight = agent._subconscious_insights[0]
        assert "buffering" in insight["insight"].lower() or "MCP" in insight["insight"]
        assert insight["confidence"] == 0.75


# ── Scenario 3: Confidence filtering ──────────────────────────────────

class TestRetrievalConfidenceFiltering:
    """Test: low-confidence insights are filtered out."""

    def test_low_confidence_filtered(self):
        """Insight with confidence < threshold should be dropped."""
        agent = _make_agent(subconscious_confidence_threshold=0.6)

        mock_response = MagicMock()
        mock_tail = MagicMock()
        mock_tail.role = "assistant"
        text_block = MagicMock()
        text_block.text = json.dumps({
            "insight": "Vaguely similar pattern maybe?",
            "confidence": 0.3,
            "source_memory": "uncertain match",
        })
        mock_tail.content = [text_block]
        mock_response.interface = MagicMock(entries=[mock_tail])

        snap_path, snap_iface = _make_snapshot("snap_vague", "Some unrelated content.")

        with patch("lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                   return_value=ASYNC_CURRENT_DIARY), \
             patch("lingtai_kernel.intrinsics.soul.consultation._list_snapshot_paths",
                   return_value=[snap_path]), \
             patch("lingtai_kernel.intrinsics.soul.consultation._load_snapshot_interface",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                   return_value=snap_iface), \
             patch("lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                   return_value=MagicMock()), \
             patch.object(agent.service, "create_session", return_value=mock_response):

            from lingtai_kernel.intrinsics.soul.subconscious import _subconscious_fire_worker
            _subconscious_fire_worker(agent)

        # Should be filtered out.
        assert len(agent._subconscious_insights) == 0
        agent._log.assert_any_call(
            "subconscious_insight_filtered",
            confidence=0.3,
            threshold=0.6,
            insight="Vaguely similar pattern maybe?",
        )


# ── Scenario 4: K=2 sampling ──────────────────────────────────────────

class TestRetrievalK2Sampling:
    """Test: K=2 sampling picks 2 different snapshots."""

    def test_two_workers_fired(self):
        """_fire_subconscious should spawn K=2 daemon threads."""
        agent = _make_agent(subconscious_sample_n=2)

        threads_started = []
        original_thread = __import__("threading").Thread

        class TrackingThread(original_thread):
            def start(self):
                threads_started.append(self.name)
                # Don't actually start — we just want to count.
                pass

        with patch("threading.Thread", TrackingThread), \
             patch("lingtai_kernel.intrinsics.soul.subconscious._subconscious_fire_worker"):
            from lingtai_kernel.intrinsics.soul.subconscious import _fire_subconscious
            _fire_subconscious(agent)

        # Should have started 2 threads.
        assert len(threads_started) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
