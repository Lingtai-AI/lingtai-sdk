"""Tests for the avatar capability."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.core.bash import BashManager
from lingtai.core.avatar import AvatarManager, setup as setup_avatar


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


@pytest.fixture
def fake_avatar_launch():
    """Patches AvatarManager._launch and _wait_for_boot so spawn-path tests
    don't actually fork a child process.

    Also wraps lingtai.agent.Agent so that every test agent gets a minimal
    init.json written to its working_dir on construction — required by
    AvatarManager._spawn's ``parent has no init.json`` gate.

    The new _launch contract returns (Popen, stderr_path); _wait_for_boot
    returns (status, error). We synthesize a fake-but-shape-correct
    response that lets the manager's success branch run."""
    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None
    fake_stderr = Path("/tmp/avatar_stderr.log")

    from lingtai.agent import Agent as _OrigAgent

    class _AutoInitAgent(_OrigAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            init_path = self._working_dir / "init.json"
            if not init_path.is_file():
                init_path.parent.mkdir(parents=True, exist_ok=True)
                # Reflect the agent's actual capability list/kwargs into the
                # init.json manifest so AvatarManager._make_avatar_init can
                # propagate them to the spawned child.
                cap_dict = {}
                for cap_entry in (self._capabilities or []):
                    if isinstance(cap_entry, tuple) and len(cap_entry) == 2:
                        cap_dict[cap_entry[0]] = cap_entry[1] or {}
                    elif isinstance(cap_entry, str):
                        cap_dict[cap_entry] = {}
                init_path.write_text(json.dumps({
                    "manifest": {
                        "agent_name": self.agent_name,
                        "admin": dict(self._admin) if self._admin else {},
                        "capabilities": cap_dict,
                    },
                }))

    with patch.object(AvatarManager, "_launch", return_value=(proc, fake_stderr)), \
         patch.object(AvatarManager, "_wait_for_boot", return_value=("ok", None)), \
         patch("lingtai.agent.Agent", _AutoInitAgent):
        yield


class TestAvatarManager:
    @pytest.fixture(autouse=True)
    def _autopatch(self, fake_avatar_launch):
        """Apply launch patch automatically to every test in this class."""
        yield

    def test_spawn_returns_address(self, tmp_path):
        """Spawn should return a valid address."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper", "confirm": True})
        assert result["status"] == "ok"
        assert "address" in result
        assert result["address"]  # filesystem path (non-empty string)
        assert result["agent_name"] == "helper"

    def test_spawn_inherits_capabilities(self, tmp_path):
        """Spawned agent's init.json should carry all of parent's capabilities."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities={"bash": {"yolo": True}, "avatar": {}})
        result = parent._tool_handlers["avatar"]({"name": "child", "confirm": True})
        assert result["status"] == "ok"
        # New architecture: avatars run as their own processes; introspection
        # is via the avatar's on-disk init.json, not an in-process _peers map.
        child_init_path = parent._working_dir.parent / "child" / "init.json"
        assert child_init_path.is_file()
        child_init = json.loads(child_init_path.read_text())
        child_caps = child_init.get("manifest", {}).get("capabilities", {})
        assert "bash" in child_caps
        assert "avatar" in child_caps

    def test_spawn_inherits_covenant(self, tmp_path):
        """Spawned agent should inherit parent's covenant."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"], covenant="Be helpful and concise.")
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper", "confirm": True})
        assert result["status"] == "ok"

    def test_spawn_no_admin(self, tmp_path):
        """Avatar should never get admin privileges, even if parent has them."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"], admin={"karma": True})
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper", "confirm": True})
        assert result["status"] == "ok"
        child_init = json.loads((parent._working_dir.parent / "helper" / "init.json").read_text())
        child_admin = child_init.get("manifest", {}).get("admin", {})
        assert child_admin == {}

    def test_spawn_duplicate_name_error(self, tmp_path):
        """Spawning a name that already exists on disk should return an error.

        Two duplicate cases collapse to the same outward signal in the current
        implementation: (1) the directory pre-exists, (2) the peer is live.
        Both produce a non-ok result. (The "already_active" return path also
        exists for live peers, but the ledger lookup uses a basename-only
        working_dir, so is_alive() can't currently find the heartbeat —
        tracked as a separate bug.)
        """
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        r1 = mgr.handle({"name": "helper", "confirm": True})
        assert r1["status"] == "ok"
        r2 = mgr.handle({"name": "helper", "confirm": True})
        assert "error" in r2 or r2.get("status") == "already_active"

    def test_spawn_does_not_copy_identity_files(self, tmp_path):
        """Spawning an avatar should not copy parent character/pad/codex.
        (The legacy ``mirror=True`` identity-copy behavior was removed.)"""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        # Write identity files to parent
        system_dir = parent._working_dir / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "character.md").write_text("I am the parent")
        (system_dir / "pad.md").write_text("Parent pad")
        lib_dir = parent._working_dir / "codex"
        lib_dir.mkdir(parents=True, exist_ok=True)
        (lib_dir / "codex.json").write_text('{"entries": []}')

        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "blank", "confirm": True})
        assert result["status"] == "ok"
        child_dir = parent._working_dir.parent / "blank"
        # Character and codex should NOT be copied
        assert not (child_dir / "system" / "character.md").is_file()
        assert not (child_dir / "codex" / "codex.json").is_file()

    def test_spawn_missing_files_ok(self, tmp_path):
        """Spawn with no identity files in the parent should not error."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "clone", "confirm": True})
        assert result["status"] == "ok"

    def test_ledger_records_spawn(self, tmp_path):
        """Ledger should record the spawn event with name + boot_status."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        mgr.handle({"name": "clone", "confirm": True})
        ledger = (parent._working_dir / "delegates" / "ledger.jsonl").read_text().strip()
        record = json.loads(ledger)
        assert record["name"] == "clone"
        assert record["boot_status"] == "ok"


class TestMissionQualityGate:
    """Issue #33 — mission/dry_run/confirm guardrails on avatar(spawn)."""

    @pytest.fixture(autouse=True)
    def _autopatch(self, fake_avatar_launch):
        yield

    def _parent(self, tmp_path):
        from lingtai.agent import Agent
        return Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
        )

    def test_helper_rejects_empty(self):
        from lingtai.core.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("")
        assert unsafe and "empty" in reason

    def test_helper_rejects_short(self):
        from lingtai.core.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("too short")
        assert unsafe and "short" in reason

    def test_helper_rejects_test_word(self):
        from lingtai.core.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("test")
        assert unsafe

    def test_helper_rejects_test_prefix(self):
        from lingtai.core.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("debug something something something")
        assert unsafe and "placeholder" in reason

    def test_helper_accepts_real_mission(self):
        from lingtai.core.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe(
            "Investigate the heartbeat regression in the kernel and report findings"
        )
        assert not unsafe and reason == ""

    def test_spawn_with_no_mission_returns_confirmation_needed(self, tmp_path):
        """Spawn with no _reasoning and no confirm should be refused with a preview."""
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper"})
        assert result["status"] == "confirmation_needed"
        assert "warning" in result
        assert "preview" in result
        assert result["preview"]["name"] == "helper"
        # No working dir created
        assert not (parent._working_dir.parent / "helper").exists()
        # No ledger entry
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()

    def test_spawn_with_short_mission_returns_confirmation_needed(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper", "_reasoning": "test"})
        assert result["status"] == "confirmation_needed"
        assert result["preview"]["mission"] == "test"
        assert result["preview"]["mission_chars"] == 4

    def test_spawn_with_confirm_bypasses_gate(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        # No mission, but confirm=True acknowledges the risk.
        result = mgr.handle({"name": "helper", "confirm": True})
        assert result["status"] == "ok"
        assert (parent._working_dir.parent / "helper").is_dir()

    def test_spawn_with_real_mission_proceeds(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "name": "helper",
            "_reasoning": "Investigate the heartbeat regression and report back via mail",
        })
        assert result["status"] == "ok"

    def test_dry_run_returns_preview_without_spawning(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"name": "helper", "dry_run": True})
        assert result["status"] == "dry_run"
        assert result["preview"]["name"] == "helper"
        assert result["preview"]["type"] == "shallow"
        assert result["preview"]["address"] == "helper"
        # The preview reports that an empty mission would have tripped the gate.
        assert result["preview"]["mission_unsafe"] is True
        # No working dir, no ledger.
        assert not (parent._working_dir.parent / "helper").exists()
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()

    def test_dry_run_does_not_require_confirm(self, tmp_path):
        """Dry-run is preview-only; mission gate must not block it."""
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        # Empty mission + no confirm + dry_run=True → returns dry_run, not confirmation_needed.
        result = mgr.handle({"name": "helper", "dry_run": True})
        assert result["status"] == "dry_run"

    def test_dry_run_preview_reports_real_mission_safe(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "name": "helper",
            "dry_run": True,
            "_reasoning": "Investigate the heartbeat regression and report back via mail",
        })
        assert result["status"] == "dry_run"
        assert result["preview"]["mission_unsafe"] is False
        assert result["preview"]["mission_reason"] == ""

    def test_schema_exposes_dry_run_and_confirm(self):
        from lingtai.core.avatar import get_schema
        sch = get_schema("en")
        assert "dry_run" in sch["properties"]
        assert sch["properties"]["dry_run"]["type"] == "boolean"
        assert "confirm" in sch["properties"]
        assert sch["properties"]["confirm"]["type"] == "boolean"

    def test_description_warns_against_accidental_spawn(self):
        from lingtai.core.avatar import get_description
        desc = get_description("en")
        assert "WARNING" in desc
        assert "independent process" in desc.lower()


class TestSetupAvatar:
    def test_setup_avatar(self):
        agent = MagicMock()
        mgr = setup_avatar(agent)
        assert isinstance(mgr, AvatarManager)
        agent.add_tool.assert_called_once()


class TestAddCapability:
    def test_add_capability_avatar(self, tmp_path):
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities=["avatar"])
        mgr = agent.get_capability("avatar")
        assert isinstance(mgr, AvatarManager)
        assert "avatar" in agent._tool_handlers

    def test_add_capability_unknown(self, tmp_path):
        """Unknown capability is logged + skipped (not raised) so a bad name
        in init.json doesn't kill agent boot. The capability simply
        doesn't appear in the agent's tool surface."""
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test",
                      working_dir=tmp_path / "test",
                      capabilities=["nonexistent"])
        assert "nonexistent" not in agent._tool_handlers

    def test_add_multiple_capabilities_separately(self, tmp_path):
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities={"bash": {"yolo": True}, "avatar": {}})
        bash_mgr = agent.get_capability("bash")
        avatar_mgr = agent.get_capability("avatar")
        assert isinstance(bash_mgr, BashManager)
        assert isinstance(avatar_mgr, AvatarManager)

    def test_capabilities_log(self, tmp_path):
        """Agent should record (name, kwargs) in _capabilities."""
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities={"bash": {"yolo": True}, "avatar": {}})
        assert len(agent._capabilities) == 2
        assert agent._capabilities[0] == ("bash", {"yolo": True})
        assert agent._capabilities[1] == ("avatar", {})
