"""Retrieval tests for subconscious Architecture A (event-driven + meta block).

End-to-end scenario: a mock snapshot directory contains a past conversation
where the agent solved an async Python race condition with asyncio.gather().
The agent is now working on a SIMILAR async bug. The subconscious fires,
loads the snapshot, consults a (mocked) cheap model, and produces an insight
referencing the past solution.

Tests cover:
- Worker retrieval with mocked LLM (core happy path)
- Real snapshot loading from disk (no mock on discovery/parsing)
- _fire_subconscious thread spawning with real snapshot dir
- Confidence filtering on async-related insights
- No snapshots -> no insights
- Null insight response -> no insights
- Insight rendering in meta block
- Multiple snapshot random selection
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Snapshot factory ────────────────────────────────────────────────────

def _build_async_bug_snapshot() -> list[dict]:
    """Build a ChatInterface-compatible entry list representing a past
    conversation where the agent solved an async race condition with
    asyncio.gather().
    """
    ts = time.time() - 86400  # yesterday
    return [
        {
            "id": 0,
            "role": "system",
            "system": "You are a helpful coding assistant.",
            "timestamp": ts,
        },
        {
            "id": 1,
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "I have an async Python service that processes incoming "
                        "webhooks. Sometimes two webhooks arrive almost at the same "
                        "time and the second one overwrites the first one's result "
                        "in the database. I think it's a race condition."
                    ),
                }
            ],
            "timestamp": ts + 1,
        },
        {
            "id": 2,
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "This is a classic async race condition. The two coroutines "
                        "are running concurrently but accessing shared state without "
                        "coordination. The fix is to use asyncio.gather() to group "
                        "the coroutines so they execute with proper sequencing, and "
                        "add an asyncio.Lock around the critical database write "
                        "section. Here's the pattern:\n\n"
                        "```python\n"
                        "import asyncio\n\n"
                        "lock = asyncio.Lock()\n\n"
                        "async def process_webhooks(hooks):\n"
                        "    results = await asyncio.gather(\n"
                        "        *[handle_hook(h) for h in hooks]\n"
                        "    )\n"
                        "    async with lock:\n"
                        "        for r in results:\n"
                        "            await db.write(r)\n"
                        "```\n\n"
                        "asyncio.gather() ensures all coroutines complete before "
                        "the write phase, and the lock serialises the DB writes."
                    ),
                }
            ],
            "timestamp": ts + 2,
        },
        {
            "id": 3,
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "That fixed it! No more race conditions. Thanks!",
                }
            ],
            "timestamp": ts + 3,
        },
        {
            "id": 4,
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Great! The key insight is that asyncio.gather() coordinates "
                        "the concurrent execution so you get all results before "
                        "touching shared state. This pattern applies whenever you "
                        "have fire-and-forget tasks that later need to merge."
                    ),
                }
            ],
            "timestamp": ts + 4,
        },
    ]


def _write_snapshot_file(directory: Path, entries: list[dict]) -> Path:
    """Write a snapshot_*.json file that _load_snapshot_interface can parse."""
    snapshots_dir = directory / "history" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots_dir / "snapshot_0_2026-05-01T12-00-00Z.json"
    payload = {
        "schema_version": 1,
        "molt_count": 0,
        "created_at": "2026-05-01T12:00:00Z",
        "before_tokens": 4200,
        "agent_name": "async-debug-agent",
        "interface": entries,
    }
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
    return snapshot_path


def _write_diary_log(directory: Path) -> None:
    """Write a minimal events.jsonl with diary entries about the current
    async bug the agent is working on.
    """
    logs_dir = directory / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "events.jsonl"
    now = time.time()
    entries = [
        {
            "event": "diary",
            "ts": now - 60,
            "text": (
                "User reports that their async Python web scraper has tasks "
                "that stomp on each other — results from one task get "
                "overwritten by another. Looks like another async race condition "
                "where concurrent coroutines share a result dict without "
                "synchronization."
            ),
        },
        {
            "event": "diary",
            "ts": now - 30,
            "text": (
                "Looking at the code: multiple asyncio.create_task() calls "
                "writing to the same dict. Need a way to coordinate them "
                "before the final merge step."
            ),
        },
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


CURRENT_DIARY_TEXT = (
    "[08:30:00] diary\n"
    "User's async Python scraper has tasks stomping on each other — "
    "concurrent coroutines share a result dict without synchronization.\n"
    "[08:30:30] diary\n"
    "Multiple asyncio.create_task() calls writing to the same dict. "
    "Need a way to coordinate them before the final merge step."
)


def _make_retrieval_agent(working_dir: Path, **overrides):
    """Create a mock agent configured for subconscious retrieval testing."""
    agent = MagicMock()
    agent._config = MagicMock()
    agent._config.subconscious_enabled = True
    agent._config.subconscious_provider = overrides.get(
        "subconscious_provider", "test-provider",
    )
    agent._config.subconscious_model = overrides.get(
        "subconscious_model", "test-model",
    )
    agent._config.subconscious_base_url = None
    agent._config.subconscious_context_window = 128000
    agent._config.subconscious_confidence_threshold = overrides.get(
        "subconscious_confidence_threshold", 0.5,
    )
    agent._config.subconscious_sample_n = overrides.get(
        "subconscious_sample_n", 1,
    )
    agent._config.provider = "primary-provider"
    agent._config.model = "primary-model"
    agent._config.retry_timeout = 30.0
    agent._config.language = "en"
    agent._shutdown = MagicMock()
    agent._shutdown.is_set.return_value = False
    agent._subconscious_insights = []
    agent._working_dir = working_dir
    agent.agent_name = "retrieval-test-agent"
    agent._log = MagicMock()
    agent.service = MagicMock()
    return agent


def _mock_llm_session(insight_json: str) -> MagicMock:
    """Build a mock session whose interface returns an assistant response
    containing the given JSON text.
    """
    mock_text_block = MagicMock()
    mock_text_block.text = insight_json
    mock_tail = MagicMock()
    mock_tail.role = "assistant"
    mock_tail.content = [mock_text_block]
    mock_session_iface = MagicMock()
    mock_session_iface.entries = [mock_tail]
    mock_session = MagicMock()
    mock_session.interface = mock_session_iface
    return mock_session


# ── Tests ───────────────────────────────────────────────────────────────


class TestSubconsciousRetrieval:
    """End-to-end retrieval: mock snapshot dir with async-Python-bug
    conversation, fire subconscious, verify insight references past
    asyncio.gather() solution.
    """

    def test_worker_retrieves_async_bug_insight(self):
        """Full worker flow with a mocked LLM response returning an
        insight about the past asyncio.gather() solution.
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _subconscious_fire_worker,
            _get_subconscious_insights,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            entries = _build_async_bug_snapshot()
            _write_snapshot_file(workdir, entries)
            _write_diary_log(workdir)

            agent = _make_retrieval_agent(workdir)

            insight_json = json.dumps({
                "insight": (
                    "You solved a nearly identical async race condition before — "
                    "the fix was asyncio.gather() to collect all coroutine results "
                    "before writing to shared state, plus an asyncio.Lock for the "
                    "write phase."
                ),
                "confidence": 0.85,
                "source_memory": "async webhook race condition fix",
            })
            mock_session = _mock_llm_session(insight_json)
            agent.service.create_session.return_value = mock_session

            mock_fitted = MagicMock()
            mock_fitted.entries = [MagicMock()]

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                return_value=MagicMock(),
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                return_value=mock_fitted,
            ):
                _subconscious_fire_worker(agent)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 1

            insight = insights[0]
            assert "asyncio.gather" in insight["insight"]
            assert insight["confidence"] == 0.85
            assert "snapshot:" in insight["source"]

    def test_worker_loads_real_snapshot_from_disk(self):
        """Verify the worker discovers and loads the snapshot file from
        the real filesystem (no mock on _list_snapshot_paths or
        _load_snapshot_interface).
        """
        from lingtai_kernel.intrinsics.soul.consultation import (
            _list_snapshot_paths,
            _load_snapshot_interface,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            entries = _build_async_bug_snapshot()
            _write_snapshot_file(workdir, entries)

            agent = _make_retrieval_agent(workdir)

            # Verify the helpers actually find the snapshot.
            paths = _list_snapshot_paths(agent)
            assert len(paths) == 1
            assert paths[0].name == "snapshot_0_2026-05-01T12-00-00Z.json"

            # Verify it parses into a ChatInterface with entries.
            iface = _load_snapshot_interface(paths[0])
            assert iface is not None
            assert len(iface.entries) == 5  # system + 2 user + 2 assistant

            # Verify the async content is in the loaded interface.
            texts = []
            for entry in iface.entries:
                for block in entry.content:
                    if hasattr(block, "text"):
                        texts.append(block.text)
            full_text = " ".join(texts)
            assert "asyncio.gather" in full_text
            assert "race condition" in full_text

    def test_fire_spawns_worker_with_real_snapshot_dir(self):
        """_fire_subconscious spawns threads; the worker thread picks up
        the real snapshot and produces an insight (mocked LLM).
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _fire_subconscious,
            _get_subconscious_insights,
        )
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            entries = _build_async_bug_snapshot()
            _write_snapshot_file(workdir, entries)
            _write_diary_log(workdir)

            agent = _make_retrieval_agent(workdir, subconscious_sample_n=1)

            insight_json = json.dumps({
                "insight": (
                    "Previously solved an async race condition using "
                    "asyncio.gather() — same pattern applies here."
                ),
                "confidence": 0.9,
                "source_memory": "async coordination pattern",
            })
            mock_session = _mock_llm_session(insight_json)
            agent.service.create_session.return_value = mock_session

            mock_fitted = MagicMock()
            mock_fitted.entries = [MagicMock()]

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                return_value=MagicMock(),
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                return_value=mock_fitted,
            ):
                _fire_subconscious(agent)

                # Wait for the worker threads to finish.
                for t in threading.enumerate():
                    if t.name.startswith("sub-retrieval-test-agent"):
                        t.join(timeout=5.0)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 1
            assert "asyncio.gather" in insights[0]["insight"]

    def test_low_confidence_async_insight_filtered(self):
        """If the LLM returns a low-confidence insight about the async
        bug, it should be filtered out by the confidence threshold.
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _subconscious_fire_worker,
            _get_subconscious_insights,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            entries = _build_async_bug_snapshot()
            _write_snapshot_file(workdir, entries)
            _write_diary_log(workdir)

            agent = _make_retrieval_agent(
                workdir, subconscious_confidence_threshold=0.9,
            )

            insight_json = json.dumps({
                "insight": "Maybe related to some async thing?",
                "confidence": 0.4,
                "source_memory": "vague",
            })
            mock_session = _mock_llm_session(insight_json)
            agent.service.create_session.return_value = mock_session

            mock_fitted = MagicMock()
            mock_fitted.entries = [MagicMock()]

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                return_value=MagicMock(),
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                return_value=mock_fitted,
            ):
                _subconscious_fire_worker(agent)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 0

            agent._log.assert_any_call(
                "subconscious_insight_filtered",
                confidence=0.4,
                threshold=0.9,
                insight="Maybe related to some async thing?",
            )

    def test_no_snapshots_produces_no_insights(self):
        """Worker returns early when the snapshot directory is empty."""
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _subconscious_fire_worker,
            _get_subconscious_insights,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            _write_diary_log(workdir)

            agent = _make_retrieval_agent(workdir)

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ):
                _subconscious_fire_worker(agent)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 0
            agent._log.assert_any_call("subconscious_fire_no_snapshots")

    def test_null_insight_response_produces_no_insights(self):
        """When the LLM responds with {"insight": null}, no insight
        should be stored.
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _subconscious_fire_worker,
            _get_subconscious_insights,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            entries = _build_async_bug_snapshot()
            _write_snapshot_file(workdir, entries)
            _write_diary_log(workdir)

            agent = _make_retrieval_agent(workdir)

            mock_session = _mock_llm_session('{"insight": null}')
            agent.service.create_session.return_value = mock_session

            mock_fitted = MagicMock()
            mock_fitted.entries = [MagicMock()]

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                return_value=MagicMock(),
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                return_value=mock_fitted,
            ):
                _subconscious_fire_worker(agent)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 0

    def test_insight_renders_in_meta_block(self):
        """After retrieval, the async-bug insight should appear in the
        meta block with the brain emoji and confidence percentage.
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _render_subconscious_insights,
        )

        agent = _make_retrieval_agent(Path("/tmp/fake"))
        agent._subconscious_insights = [
            {
                "insight": (
                    "Previously solved async race condition with asyncio.gather()"
                ),
                "confidence": 0.85,
                "source": "snapshot:snapshot_0_2026-05-01T12-00-00Z",
                "ts": time.time(),
            },
        ]

        rendered = _render_subconscious_insights(agent)
        assert "🧠" in rendered
        assert "85%" in rendered
        assert "asyncio.gather()" in rendered

    def test_multiple_snapshots_random_selection(self):
        """With multiple snapshots, the worker picks one at random.
        Both should be valid candidates.
        """
        from lingtai_kernel.intrinsics.soul.subconscious import (
            _subconscious_fire_worker,
            _get_subconscious_insights,
        )
        from lingtai_kernel.intrinsics.soul.consultation import (
            _list_snapshot_paths,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            snapshots_dir = workdir / "history" / "snapshots"
            snapshots_dir.mkdir(parents=True)

            # Write two snapshots — both about async bugs.
            for i, topic in enumerate([
                "webhook race condition",
                "scraper race condition",
            ]):
                ts = time.time() - 86400 * (i + 1)
                payload = {
                    "schema_version": 1,
                    "molt_count": 0,
                    "created_at": f"2026-05-0{i+1}T12:00:00Z",
                    "before_tokens": 3000,
                    "agent_name": "test-agent",
                    "interface": [
                        {
                            "id": 0,
                            "role": "system",
                            "system": "You are a helpful assistant.",
                            "timestamp": ts,
                        },
                        {
                            "id": 1,
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"Help with {topic}"},
                            ],
                            "timestamp": ts + 1,
                        },
                        {
                            "id": 2,
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"Used asyncio.gather() to fix {topic}."
                                    ),
                                },
                            ],
                            "timestamp": ts + 2,
                        },
                    ],
                }
                path = (
                    snapshots_dir
                    / f"snapshot_{i}_2026-05-0{i+1}T12-00-00Z.json"
                )
                path.write_text(json.dumps(payload), encoding="utf-8")

            _write_diary_log(workdir)
            agent = _make_retrieval_agent(workdir, subconscious_sample_n=1)

            # Verify both snapshots are discovered.
            paths = _list_snapshot_paths(agent)
            assert len(paths) == 2

            insight_json = json.dumps({
                "insight": "asyncio.gather() pattern from past async bug fix",
                "confidence": 0.8,
                "source_memory": "async pattern",
            })
            mock_session = _mock_llm_session(insight_json)
            agent.service.create_session.return_value = mock_session

            mock_fitted = MagicMock()
            mock_fitted.entries = [MagicMock()]

            with patch(
                "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
                return_value=CURRENT_DIARY_TEXT,
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
                return_value=MagicMock(),
            ), patch(
                "lingtai_kernel.intrinsics.soul.consultation._fit_interface_to_window",
                return_value=mock_fitted,
            ):
                _subconscious_fire_worker(agent)

            insights = _get_subconscious_insights(agent)
            assert len(insights) == 1
            assert "asyncio.gather" in insights[0]["insight"]
            assert insights[0]["source"].startswith("snapshot:snapshot_")
