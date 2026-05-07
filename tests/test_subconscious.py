"""Tests for the subconscious architecture (issue #51).

Covers:
- Step 1: Config defaults, prompt constant
- Step 2: _run_subconscious_fire → notification → TTL expiry cycle
- Step 3: Config UI + persistence round-trip
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.intrinsics.soul.consultation import (
    _SUBCONSCIOUS_SYSTEM_PROMPT,
)
from lingtai_kernel.intrinsics.soul.flow import (
    _run_subconscious_fire,
)
from lingtai_kernel.intrinsics.soul.config import (
    _handle_config,
    _persist_soul_config,
)
from lingtai_kernel.base_agent.lifecycle import _check_subconscious_ttl
from lingtai_kernel.notifications import (
    collect_notifications,
    publish,
    clear,
)
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSubconsciousConfig:
    """Config with subconscious enabled."""
    language = "en"
    consultation_past_count = 2
    context_limit = 200_000
    retry_timeout = 1.0
    model = "test-model"
    provider = None
    # Subconscious fields
    subconscious_enabled = True
    subconscious_ttl_seconds = 60.0
    subconscious_provider = None
    subconscious_model = None
    subconscious_base_url = None
    subconscious_context_window = 128000


class _FakeSubconsciousAgent:
    """Minimal stand-in for BaseAgent with subconscious enabled."""

    def __init__(self, tmp_path: Path, with_chat: bool = True):
        self._working_dir = tmp_path
        self._working_dir.mkdir(parents=True, exist_ok=True)
        self._config = _FakeSubconsciousConfig()
        self.service = MagicMock()
        self.service.model = "test-model"
        self._chat = None
        if with_chat:
            iface = ChatInterface()
            iface.add_system("test sys")
            iface.add_user_message("user said something")
            iface.add_assistant_message([
                TextBlock(text="agent reply"),
            ])
            mock_chat = MagicMock()
            mock_chat.interface = iface
            mock_chat.context_window.return_value = 200_000
            self._chat = mock_chat
        self._session = MagicMock()
        self._session._build_tool_schemas_fn.return_value = []
        self.logged: list[tuple[str, dict]] = []
        # For state check
        from lingtai_kernel.state import AgentState
        self._state = AgentState.IDLE

    def _log(self, event: str, **kw) -> None:
        self.logged.append((event, kw))

    def _wake_nap(self, reason: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Step 1: Config defaults + prompt constant
# ---------------------------------------------------------------------------


class TestSubconsciousDefaults:

    def test_config_defaults(self):
        """AgentConfig has correct subconscious defaults."""
        cfg = AgentConfig()
        assert cfg.subconscious_enabled is False
        assert cfg.subconscious_ttl_seconds == 1800.0
        assert cfg.subconscious_provider is None
        assert cfg.subconscious_model is None
        assert cfg.subconscious_base_url is None
        assert cfg.subconscious_context_window == 128000

    def test_subconscious_prompt_exists(self):
        """The subconscious prompt constant is non-empty and contains
        the key framing."""
        assert _SUBCONSCIOUS_SYSTEM_PROMPT
        assert "remind you" in _SUBCONSCIOUS_SYSTEM_PROMPT.lower() or \
               "remind" in _SUBCONSCIOUS_SYSTEM_PROMPT.lower()
        assert '"insight"' in _SUBCONSCIOUS_SYSTEM_PROMPT
        assert '"insight": null' in _SUBCONSCIOUS_SYSTEM_PROMPT
        assert "tool call" in _SUBCONSCIOUS_SYSTEM_PROMPT.lower()

    def test_subconscious_prompt_has_json_format(self):
        """The prompt instructs JSON output with the expected fields."""
        assert '"confidence"' in _SUBCONSCIOUS_SYSTEM_PROMPT
        assert '"source_memory"' in _SUBCONSCIOUS_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Step 2: Core fire + TTL
# ---------------------------------------------------------------------------


class TestSubconsciousFire:

    def test_fire_writes_notification_on_insight(self, tmp_path):
        """When the subconscious produces an insight, it writes
        .notification/subconscious.json with the correct structure."""
        agent = _FakeSubconsciousAgent(tmp_path)

        # Create a snapshot file for the consultation to read
        # Format must match ChatInterface.to_dict() / from_dict()
        snapshots_dir = tmp_path / "history" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "schema_version": 1,
            "interface": [
                {"id": 0, "role": "system", "system": "test", "timestamp": 1000.0},
                {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hello"}], "timestamp": 1001.0},
            ],
        }
        snapshot_path = snapshots_dir / "snapshot_1_1000.json"
        snapshot_path.write_text(json.dumps(snapshot))

        # Create diary entries
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        events_file = logs_dir / "events.jsonl"
        ts = time.time()
        events_file.write_text(json.dumps({
            "type": "diary", "ts": ts, "text": "working on the subconscious"
        }) + "\n")

        # Mock the LLM session to return a JSON insight
        mock_session = MagicMock()
        mock_iface = ChatInterface()
        insight_json = json.dumps({
            "insight": "This reminds me of the time I worked on the notification system",
            "confidence": 0.8,
            "source_memory": "building the notification filesystem protocol",
        })
        mock_iface.add_assistant_message([TextBlock(text=insight_json)])
        mock_session.interface = mock_iface
        agent.service.create_session.return_value = mock_session

        # Mock send_with_timeout to return a dummy response
        mock_response = MagicMock()
        mock_response.tool_calls = None

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
            return_value=mock_response,
        ):
            _run_subconscious_fire(agent)

        # Verify notification file was written
        sub_path = tmp_path / ".notification" / "subconscious.json"
        assert sub_path.is_file(), "subconscious notification should be written"

        data = json.loads(sub_path.read_text())
        assert data["header"] == "subconscious insight"
        assert data["icon"] == "🧠"
        assert "insight" in data["data"]
        assert data["data"]["insight"] == "This reminds me of the time I worked on the notification system"
        assert data["data"]["confidence"] == 0.8
        assert "expires_at" in data["data"]
        assert data["data"]["ttl_seconds"] == 60.0
        assert "source_snapshot" in data["data"]

    def test_fire_skips_on_null_insight(self, tmp_path):
        """When the subconscious returns {insight: null}, no notification
        is written."""
        agent = _FakeSubconsciousAgent(tmp_path)

        # Create a snapshot
        snapshots_dir = tmp_path / "history" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "schema_version": 1,
            "interface": [
                {"id": 0, "role": "system", "system": "test", "timestamp": 1000.0},
                {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hello"}], "timestamp": 1001.0},
            ],
        }
        (snapshots_dir / "snapshot_1_1000.json").write_text(json.dumps(snapshot))

        # Create diary
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "events.jsonl").write_text(
            json.dumps({"type": "diary", "ts": time.time(), "text": "test diary"}) + "\n"
        )

        # Mock LLM returning null insight
        mock_session = MagicMock()
        mock_iface = ChatInterface()
        mock_iface.add_assistant_message([TextBlock(text='{"insight": null}')])
        mock_session.interface = mock_iface
        agent.service.create_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.tool_calls = None

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
            return_value=mock_response,
        ):
            _run_subconscious_fire(agent)

        # Verify no notification file
        sub_path = tmp_path / ".notification" / "subconscious.json"
        assert not sub_path.exists(), "null insight should not write notification"

    def test_fire_skips_when_active_insight_exists(self, tmp_path):
        """When an active (non-expired) insight exists, the fire skips."""
        agent = _FakeSubconsciousAgent(tmp_path)

        # Write an existing active insight
        notif_dir = tmp_path / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "header": "subconscious insight",
            "icon": "🧠",
            "data": {
                "insight": "existing insight",
                "expires_at": time.time() + 300,  # still active
                "ttl_seconds": 60,
            },
        }
        (notif_dir / "subconscious.json").write_text(json.dumps(existing))

        # Fire should not overwrite — it returns early because of the active insight
        agent.service.create_session.side_effect = RuntimeError("should not be called")
        _run_subconscious_fire(agent)
        agent.service.create_session.assert_not_called()

        # Verify existing insight is unchanged
        data = json.loads((notif_dir / "subconscious.json").read_text())
        assert data["data"]["insight"] == "existing insight"

    def test_fire_overwrites_expired_insight(self, tmp_path):
        """When the existing insight is expired, the fire can overwrite."""
        agent = _FakeSubconsciousAgent(tmp_path)

        # Write an expired insight
        notif_dir = tmp_path / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        expired = {
            "header": "subconscious insight",
            "icon": "🧠",
            "data": {
                "insight": "old expired insight",
                "expires_at": time.time() - 100,  # expired
                "ttl_seconds": 60,
            },
        }
        (notif_dir / "subconscious.json").write_text(json.dumps(expired))

        # Create a snapshot
        snapshots_dir = tmp_path / "history" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "schema_version": 1,
            "interface": [
                {"id": 0, "role": "system", "system": "test", "timestamp": 1000.0},
                {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hello"}], "timestamp": 1001.0},
            ],
        }
        (snapshots_dir / "snapshot_1_1000.json").write_text(json.dumps(snapshot))

        # Create diary
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "events.jsonl").write_text(
            json.dumps({"type": "diary", "ts": time.time(), "text": "new diary"}) + "\n"
        )

        # Mock LLM returning new insight
        mock_session = MagicMock()
        mock_iface = ChatInterface()
        new_insight = json.dumps({
            "insight": "new insight after expiry",
            "confidence": 0.9,
            "source_memory": "new memory",
        })
        mock_iface.add_assistant_message([TextBlock(text=new_insight)])
        mock_session.interface = mock_iface
        agent.service.create_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.tool_calls = None

        with patch(
            "lingtai_kernel.intrinsics.soul.consultation._send_with_timeout",
            return_value=mock_response,
        ):
            _run_subconscious_fire(agent)

        # Verify new insight replaced the expired one
        data = json.loads((notif_dir / "subconscious.json").read_text())
        assert data["data"]["insight"] == "new insight after expiry"

    def test_fire_skips_when_disabled(self, tmp_path):
        """When subconscious_enabled is False, the fire skips."""
        agent = _FakeSubconsciousAgent(tmp_path)
        agent._config.subconscious_enabled = False

        # The fire should return early.  We verify by checking that no
        # session was created.
        agent.service.create_session.side_effect = RuntimeError("should not be called")

        _run_subconscious_fire(agent)
        agent.service.create_session.assert_not_called()


class TestSubconsciousTTL:

    def test_ttl_clears_expired(self, tmp_path):
        """_check_subconscious_ttl clears an expired insight."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._log = MagicMock()

        # Write an expired notification
        notif_dir = tmp_path / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        expired = {
            "header": "subconscious insight",
            "data": {
                "insight": "this is expired",
                "expires_at": time.time() - 100,
                "ttl_seconds": 60,
            },
        }
        (notif_dir / "subconscious.json").write_text(json.dumps(expired))

        _check_subconscious_ttl(agent)

        # Verify cleared
        assert not (notif_dir / "subconscious.json").exists()
        agent._log.assert_called_with(
            "subconscious_expired", insight="this is expired"
        )

    def test_ttl_preserves_active(self, tmp_path):
        """_check_subconscious_ttl preserves an active (non-expired) insight."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._log = MagicMock()

        # Write an active notification
        notif_dir = tmp_path / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        active = {
            "header": "subconscious insight",
            "data": {
                "insight": "this is still active",
                "expires_at": time.time() + 300,
                "ttl_seconds": 60,
            },
        }
        (notif_dir / "subconscious.json").write_text(json.dumps(active))

        _check_subconscious_ttl(agent)

        # Verify preserved
        assert (notif_dir / "subconscious.json").exists()
        data = json.loads((notif_dir / "subconscious.json").read_text())
        assert data["data"]["insight"] == "this is still active"
        agent._log.assert_not_called()

    def test_ttl_noop_when_no_file(self, tmp_path):
        """_check_subconscious_ttl does nothing when no file exists."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._log = MagicMock()

        _check_subconscious_ttl(agent)
        agent._log.assert_not_called()

    def test_ttl_noop_on_malformed_file(self, tmp_path):
        """_check_subconscious_ttl handles malformed JSON gracefully."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._log = MagicMock()

        notif_dir = tmp_path / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        (notif_dir / "subconscious.json").write_text("not valid json{{{")

        # Should not raise
        _check_subconscious_ttl(agent)
        # File should still exist (we don't delete malformed files)
        assert (notif_dir / "subconscious.json").exists()


# ---------------------------------------------------------------------------
# Step 3: Config UI + persistence
# ---------------------------------------------------------------------------


class TestSubconsciousConfig:

    def test_handle_config_enable(self, tmp_path):
        """soul(action='config', subconscious_enabled=True) works."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._config = AgentConfig()
        agent._soul_delay = 300.0
        agent._log = MagicMock()

        # Write a minimal init.json
        init = {"manifest": {"soul": {"delay": 300}}}
        (tmp_path / "init.json").write_text(json.dumps(init))

        result = _handle_config(agent, {"subconscious_enabled": True})
        assert result["status"] == "ok"
        assert result["new"]["subconscious_enabled"] is True
        assert result["old"]["subconscious_enabled"] is False
        assert agent._config.subconscious_enabled is True

    def test_handle_config_disable(self, tmp_path):
        """soul(action='config', subconscious_enabled=False) works."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._config = AgentConfig()
        agent._config.subconscious_enabled = True
        agent._soul_delay = 300.0
        agent._log = MagicMock()

        init = {"manifest": {"soul": {"delay": 300}}}
        (tmp_path / "init.json").write_text(json.dumps(init))

        result = _handle_config(agent, {"subconscious_enabled": False})
        assert result["status"] == "ok"
        assert result["new"]["subconscious_enabled"] is False
        assert result["old"]["subconscious_enabled"] is True

    def test_handle_config_ttl(self, tmp_path):
        """soul(action='config', subconscious_ttl_seconds=300) works."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._config = AgentConfig()
        agent._soul_delay = 300.0
        agent._log = MagicMock()

        init = {"manifest": {"soul": {"delay": 300}}}
        (tmp_path / "init.json").write_text(json.dumps(init))

        result = _handle_config(agent, {"subconscious_ttl_seconds": 300})
        assert result["status"] == "ok"
        assert result["new"]["subconscious_ttl_seconds"] == 300.0
        assert result["old"]["subconscious_ttl_seconds"] == 1800.0
        assert agent._config.subconscious_ttl_seconds == 300.0

    def test_handle_config_ttl_too_low(self, tmp_path):
        """TTL below 60 is rejected."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._config = AgentConfig()
        agent._soul_delay = 300.0
        agent._log = MagicMock()

        result = _handle_config(agent, {"subconscious_ttl_seconds": 30})
        assert "error" in result
        assert "60" in result["error"]

    def test_persist_subconscious_config(self, tmp_path):
        """Subconscious config persists to init.json under
        manifest.soul.subconscious."""
        agent = MagicMock()
        agent._working_dir = tmp_path

        init = {"manifest": {"soul": {"delay": 300}}}
        (tmp_path / "init.json").write_text(json.dumps(init))

        _persist_soul_config(agent, {
            "subconscious_enabled": True,
            "subconscious_ttl_seconds": 600.0,
        })

        data = json.loads((tmp_path / "init.json").read_text())
        sub = data["manifest"]["soul"]["subconscious"]
        assert sub["enabled"] is True
        assert sub["ttl_seconds"] == 600.0

    def test_config_roundtrip(self, tmp_path):
        """Config changes persist through init.json and survive reload."""
        agent = MagicMock()
        agent._working_dir = tmp_path
        agent._config = AgentConfig()
        agent._soul_delay = 300.0
        agent._log = MagicMock()

        init = {"manifest": {"soul": {"delay": 300}}}
        (tmp_path / "init.json").write_text(json.dumps(init))

        # Enable and set TTL
        _handle_config(agent, {
            "subconscious_enabled": True,
            "subconscious_ttl_seconds": 900,
        })

        # Read back from disk
        data = json.loads((tmp_path / "init.json").read_text())
        sub = data["manifest"]["soul"]["subconscious"]
        assert sub["enabled"] is True
        assert sub["ttl_seconds"] == 900.0

    def test_manifest_includes_subconscious(self, tmp_path):
        """The agent manifest includes subconscious config when enabled."""
        from lingtai_kernel.base_agent.identity import _build_manifest

        agent = MagicMock()
        agent._agent_id = "test-id"
        agent.agent_name = "test"
        agent.nickname = None
        agent._working_dir = tmp_path
        agent._created_at = "2026-01-01T00:00:00Z"
        agent._started_at = "2026-01-01T00:00:00Z"
        agent._admin = {}
        agent._state = MagicMock()
        agent._state.value = "IDLE"
        agent._soul_delay = 300.0
        agent._molt_count = 0
        agent._mail_service = None
        agent._config = AgentConfig()
        agent._config.subconscious_enabled = True
        agent._config.subconscious_ttl_seconds = 900.0

        manifest = _build_manifest(agent)
        assert manifest["subconscious_enabled"] is True
        assert manifest["subconscious_ttl_seconds"] == 900.0

    def test_manifest_omits_ttl_when_disabled(self, tmp_path):
        """The agent manifest omits TTL when subconscious is disabled."""
        from lingtai_kernel.base_agent.identity import _build_manifest

        agent = MagicMock()
        agent._agent_id = "test-id"
        agent.agent_name = "test"
        agent.nickname = None
        agent._working_dir = tmp_path
        agent._created_at = "2026-01-01T00:00:00Z"
        agent._started_at = "2026-01-01T00:00:00Z"
        agent._admin = {}
        agent._state = MagicMock()
        agent._state.value = "IDLE"
        agent._soul_delay = 300.0
        agent._molt_count = 0
        agent._mail_service = None
        agent._config = AgentConfig()

        manifest = _build_manifest(agent)
        assert manifest["subconscious_enabled"] is False
        assert "subconscious_ttl_seconds" not in manifest
