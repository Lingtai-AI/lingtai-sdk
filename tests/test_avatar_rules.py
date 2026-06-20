"""Tests for .rules signal consumption and system/rules.md persistence."""
import json
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from lingtai.core.avatar import AvatarManager

import pytest
from tests._service_helpers import make_gemini_mock_service as make_mock_service


def _fake_launch_return(pid: int = 12345):
    """Build a (proc, stderr_path) tuple matching ``AvatarManager._launch``'s
    new signature. The proc.pid attribute is the only field consumers read."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None  # still running
    return (proc, Path("/tmp/avatar_stderr.log"))


@contextmanager
def _patch_avatar_launch(*, boot_status: str = "ok", boot_error=None):
    """Context manager: patches both _launch and _wait_for_boot so spawn-path
    tests don't actually fork a child process. Yields the launch mock so
    assertion-based tests can inspect call counts / args."""
    with patch.object(AvatarManager, "_launch", return_value=_fake_launch_return()) as launch_mock, \
         patch.object(AvatarManager, "_wait_for_boot", return_value=(boot_status, boot_error)):
        yield launch_mock


class TestRulesHeartbeatWatch:
    """Test that the heartbeat loop consumes .rules signal and persists to system/rules.md."""

    def _make_agent(self, tmp_path):
        from lingtai.agent import Agent

        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        wd = tmp_path / "agent"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)
        return agent

    def test_rules_signal_consumed_and_persisted(self, tmp_path):
        """Writing .rules should: inject section, persist to system/rules.md, delete .rules."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # No rules section initially
        assert agent._prompt_manager.read_section("rules") is None

        # Write .rules signal file
        (wd / ".rules").write_text("No deleting files.\nAlways log actions.")

        # Simulate one heartbeat tick
        agent._check_rules_file()

        # Section injected
        assert agent._prompt_manager.read_section("rules") == "No deleting files.\nAlways log actions."
        # Persisted to system/rules.md
        assert (wd / "system" / "rules.md").read_text() == "No deleting files.\nAlways log actions."
        # Signal file consumed (deleted)
        assert not (wd / ".rules").is_file()

    def test_rules_diff_skips_identical(self, tmp_path):
        """If .rules content matches system/rules.md, no prompt refresh."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load rules into section and canonical file
        agent._prompt_manager.write_section("rules", "No deleting files.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("No deleting files.")

        # Write identical .rules signal
        (wd / ".rules").write_text("No deleting files.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_not_called()

        # Signal still consumed even if content is identical
        assert not (wd / ".rules").is_file()

    def test_rules_diff_refreshes_on_change(self, tmp_path):
        """If .rules content differs from system/rules.md, prompt is refreshed."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load old rules
        agent._prompt_manager.write_section("rules", "Old rules.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Old rules.")

        # Write new .rules signal
        (wd / ".rules").write_text("New rules.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_called_once()

        assert agent._prompt_manager.read_section("rules") == "New rules."
        assert (system_dir / "rules.md").read_text() == "New rules."
        assert not (wd / ".rules").is_file()

    def test_rules_loaded_from_system_on_init(self, tmp_path):
        """If system/rules.md exists at agent start, rules section should be pre-loaded."""
        wd = tmp_path / "agent"
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Pre-existing rules.")

        from lingtai.agent import Agent
        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)

        # Rules should be loaded from system/rules.md during init
        assert agent._prompt_manager.read_section("rules") == "Pre-existing rules."

    def test_rules_unlink_failure_skips_processing(self, tmp_path, monkeypatch):
        """If .rules cannot be unlinked, the function should return WITHOUT calling flush."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir
        (wd / ".rules").write_text("Some rules.")

        # Make Path.unlink raise OSError
        original_unlink = Path.unlink
        def failing_unlink(self, *args, **kwargs):
            if self.name == ".rules":
                raise PermissionError("simulated unlink failure")
            return original_unlink(self, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", failing_unlink)

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_not_called()
        # File should still exist (we couldn't unlink it)
        assert (wd / ".rules").is_file()




class TestAvatarRulesAction:
    """Test avatar_rules distribution."""

    def test_rules_requires_admin(self, tmp_path):
        """Non-admin agent cannot set rules."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="worker",
            working_dir=tmp_path / "worker",
            capabilities=["avatar"],
            admin={},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No deleting.",
        })
        assert "error" in result

    def test_rules_writes_self_signal_then_heartbeat_persists(self, tmp_path):
        """Admin should write .rules signal to self; heartbeat persists to system/rules.md.

        After calling _rules(), the caller's own directory has a .rules signal file.
        The prompt section and system/rules.md are NOT updated synchronously — they're
        applied by the caller's own heartbeat loop on its next tick. This test simulates
        one heartbeat tick via _check_rules_file() to verify the end-to-end path.
        """
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Always log actions.",
        })
        assert result["status"] == "ok"

        # .rules signal file was written to self (not yet consumed)
        assert (agent._working_dir / ".rules").read_text() == "Always log actions."
        # Canonical file and prompt section NOT yet updated — heartbeat hasn't ticked
        assert not (agent._working_dir / "system" / "rules.md").is_file()
        assert agent._prompt_manager.read_section("rules") is None

        # Simulate one heartbeat tick
        agent._check_rules_file()

        # Now canonical file and prompt section are updated
        assert (agent._working_dir / "system" / "rules.md").read_text() == "Always log actions."
        assert agent._prompt_manager.read_section("rules") == "Always log actions."
        # Signal consumed
        assert not (agent._working_dir / ".rules").is_file()

    def test_rules_distributes_signals_to_descendants(self, tmp_path):
        """Rules should write .rules signal files to all descendant directories.

        IMPORTANT: As of v0.5.13, the ledger stores relative directory names
        (e.g. 'child_a'), not absolute paths. Descendants live as siblings of
        the parent agent in the same `.lingtai/` directory.
        """
        from lingtai.agent import Agent

        # All agents are siblings under tmp_path (mimicking .lingtai/ layout)
        parent_dir = tmp_path / "parent"
        child_a_dir = tmp_path / "child_a"
        child_b_dir = tmp_path / "child_b"
        child_a_dir.mkdir(parents=True)
        child_b_dir.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Write ledger entries with RELATIVE names (current convention)
        ledger_dir = parent_dir / "delegates"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = ledger_dir / "ledger.jsonl"
        with open(ledger_path, "w") as f:
            f.write(json.dumps({"event": "avatar", "name": "a", "working_dir": "child_a"}) + "\n")
            f.write(json.dumps({"event": "avatar", "name": "b", "working_dir": "child_b"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No external API calls.",
        })
        assert result["status"] == "ok"
        # Self + descendants uniformly get .rules signal files
        assert (parent_dir / ".rules").read_text() == "No external API calls."
        assert (child_a_dir / ".rules").read_text() == "No external API calls."
        assert (child_b_dir / ".rules").read_text() == "No external API calls."
        # distributed_to reports relative names for self + descendants
        assert set(result["distributed_to"]) == {"parent", "child_a", "child_b"}

    def test_rules_distributes_recursively(self, tmp_path):
        """Rules should propagate to grandchildren (avatars of avatars).

        All three agents are siblings under tmp_path. The ledger records use
        relative names; resolution happens against the parent's parent dir.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        child_dir = tmp_path / "child"
        grandchild_dir = tmp_path / "grandchild"
        for d in (parent_dir, child_dir, grandchild_dir):
            d.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Parent → child ledger (relative name "child")
        p_ledger = parent_dir / "delegates" / "ledger.jsonl"
        p_ledger.parent.mkdir(parents=True, exist_ok=True)
        p_ledger.write_text(json.dumps({"event": "avatar", "name": "child", "working_dir": "child"}) + "\n")

        # Child → grandchild ledger (relative name "grandchild")
        c_ledger = child_dir / "delegates" / "ledger.jsonl"
        c_ledger.parent.mkdir(parents=True, exist_ok=True)
        c_ledger.write_text(json.dumps({"event": "avatar", "name": "gc", "working_dir": "grandchild"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Be concise.",
        })
        assert result["status"] == "ok"
        # All descendants get .rules signal files
        assert (child_dir / ".rules").read_text() == "Be concise."
        assert (grandchild_dir / ".rules").read_text() == "Be concise."

    def test_rules_requires_content(self, tmp_path):
        """avatar_rules without rules_content should error."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({"action": "rules"})
        assert "error" in result

    def test_spawn_default_action(self, tmp_path):
        """Omitting action should default to spawn (backward compatible).

        NOTE: Real spawning launches a subprocess. We patch _launch to avoid
        that, and pre-create init.json so _spawn reaches the launch path.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        agent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
        )

        # _spawn requires parent to have init.json
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {}}})
        )

        mgr = agent.get_capability("avatar")
        with _patch_avatar_launch():
            result = mgr.handle({"name": "child", "confirm": True})
        assert result["status"] == "ok"
        assert result["agent_name"] == "child"
        assert result["address"] == "child"  # relative name (current convention)

    def test_rules_root_not_duplicated_via_cycle(self, tmp_path):
        """Cycles through root should not cause root to appear twice in distributed_to.

        The caller's own directory is included once (via the explicit self-write).
        _walk_avatar_tree seeds its visited set with root so that cycles pointing
        back to root from any descendant don't produce a duplicate entry.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        child_dir = tmp_path / "child"
        child_dir.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # parent → child
        p_ledger = parent_dir / "delegates" / "ledger.jsonl"
        p_ledger.parent.mkdir(parents=True, exist_ok=True)
        p_ledger.write_text(json.dumps({"event": "avatar", "name": "child", "working_dir": "child"}) + "\n")

        # child → parent (malicious cycle pointing back to root)
        c_ledger = child_dir / "delegates" / "ledger.jsonl"
        c_ledger.parent.mkdir(parents=True, exist_ok=True)
        c_ledger.write_text(json.dumps({"event": "avatar", "name": "parent", "working_dir": "parent"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Cycle test.",
        })
        assert result["status"] == "ok"
        # Both self and child receive .rules signals
        assert (parent_dir / ".rules").read_text() == "Cycle test."
        assert (child_dir / ".rules").read_text() == "Cycle test."
        # 'parent' appears exactly once in distributed_to (from self-write,
        # not duplicated by the BFS walk through the cycle)
        assert result["distributed_to"].count("parent") == 1
        assert "child" in result["distributed_to"]


class TestAutoDistributeAfterSpawn:
    """After avatar_spawn, parent's rules should be distributed to newborn.

    These tests mock _launch to avoid actually spawning subprocesses, and
    pre-create the parent's init.json so the spawn code path can proceed
    to ledger append and rules distribution.
    """

    def _setup_spawnable_parent(self, tmp_path, with_rules: bool):
        """Build a parent agent with init.json, optionally with system/rules.md."""
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {"karma": True}}})
        )
        if with_rules:
            system_dir = parent_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            (system_dir / "rules.md").write_text("Always be concise.")
        return parent, parent_dir

    def test_spawn_distributes_existing_rules(self, tmp_path):
        """If parent has system/rules.md, spawning should write .rules to new avatar."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=True)

        mgr = parent.get_capability("avatar")
        with _patch_avatar_launch():
            result = mgr.handle({"name": "child", "confirm": True})
        assert result["status"] == "ok"

        # Child dir is a sibling of parent_dir (avatar_working_dir = parent.parent / name)
        child_dir = parent_dir.parent / "child"
        # Child gets .rules signal file (heartbeat will consume and persist it)
        assert (child_dir / ".rules").read_text() == "Always be concise."

    def test_spawn_without_rules_no_distribution(self, tmp_path):
        """If parent has no system/rules.md, spawn should not create .rules in child."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=False)

        mgr = parent.get_capability("avatar")
        with _patch_avatar_launch():
            result = mgr.handle({"name": "child", "confirm": True})
        assert result["status"] == "ok"

        child_dir = parent_dir.parent / "child"
        assert not (child_dir / ".rules").is_file()

    def test_spawn_deep_clone_also_gets_rules_signal(self, tmp_path):
        """Deep clone already has system/rules.md from _prepare_deep,
        but auto-distribute still writes .rules (redundant but harmless —
        the heartbeat will diff and skip)."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=True)

        mgr = parent.get_capability("avatar")
        with _patch_avatar_launch():
            result = mgr.handle({"name": "clone", "type": "deep", "confirm": True})
        assert result["status"] == "ok"

        clone_dir = parent_dir.parent / "clone"
        # system/rules.md was copied by _prepare_deep
        assert (clone_dir / "system" / "rules.md").read_text() == "Always be concise."
        # .rules signal was also written (redundant but harmless)
        assert (clone_dir / ".rules").read_text() == "Always be concise."


class TestSpawnNameValidation:
    """Avatar name doubles as working-dir basename. It must be a bare segment:
    path separators, parent-traversal, leading dots, absolute paths, empty
    names, or oversized names are all rejected before any filesystem mutation.
    Scripts other than ASCII (e.g. CJK) are allowed — only structural chars
    are forbidden. See kernel audit C3/C4."""

    def _spawnable_parent(self, tmp_path):
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
        )
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {}}})
        )
        return parent, parent_dir

    @pytest.mark.parametrize("bad_name", [
        "avatars/scholar",      # the real-world bug from 2026-04-22
        "../evil",              # parent traversal
        "/etc/hacked",          # absolute
        "foo/bar",              # slash mid-string
        "foo\\bar",             # backslash (windows-style)
        ".hidden",              # leading dot (would shadow .tui-asset etc.)
        ".",                    # current dir
        "..",                   # parent dir
        "",                     # empty
        "foo.bar",              # dot anywhere
        "foo bar",              # space
        "a" * 65,               # over length cap
        "foo\x00bar",           # null byte
    ])
    def test_spawn_rejects_unsafe_name(self, tmp_path, bad_name):
        parent, parent_dir = self._spawnable_parent(tmp_path)
        mgr = parent.get_capability("avatar")

        with _patch_avatar_launch() as launch:
            result = mgr.handle({"name": bad_name})

        assert "error" in result, f"name={bad_name!r} should have been rejected but got {result}"
        # No subprocess launched
        launch.assert_not_called()
        # No stray directory created outside the network root
        for entry in parent_dir.parent.iterdir():
            # Only the parent dir should exist; no sibling was created
            assert entry == parent_dir, f"stray entry created: {entry}"

    @pytest.mark.parametrize("good_name", [
        "researcher",
        "scholar-reader",
        "paper_summarizer",
        "学者",            # CJK allowed
        "研究员",          # CJK allowed
        "学者-甲",         # CJK + hyphen
        "アバター",         # kana
        "한글",            # hangul
    ])
    def test_spawn_accepts_valid_name(self, tmp_path, good_name):
        parent, parent_dir = self._spawnable_parent(tmp_path)
        mgr = parent.get_capability("avatar")

        with _patch_avatar_launch():
            result = mgr.handle({"name": good_name, "confirm": True})

        assert result.get("status") == "ok", f"name={good_name!r} should have been accepted but got {result}"
        assert (parent_dir.parent / good_name).is_dir()

    def test_legacy_dir_argument_is_ignored(self, tmp_path):
        """Pre-fix callers may still pass `dir=...`. It's no longer in the
        schema, but the handler must not crash on unknown kwargs — it should
        fall through to `name`-driven placement."""
        parent, parent_dir = self._spawnable_parent(tmp_path)
        mgr = parent.get_capability("avatar")

        with _patch_avatar_launch():
            # Pass both a safe name and a malicious legacy dir; name wins.
            result = mgr.handle({"name": "safe", "dir": "avatars/evil", "confirm": True})

        assert result.get("status") == "ok"
        assert (parent_dir.parent / "safe").is_dir()
        # The malicious dir was NOT honored
        assert not (parent_dir.parent / "avatars").exists()

    def test_prepare_deep_refuses_non_sibling_dst(self, tmp_path):
        """Defense-in-depth: even if _prepare_deep is called directly with a
        dst outside the parent network, it must refuse before any rmtree."""
        src = tmp_path / "network" / "parent"
        src.mkdir(parents=True)
        (src / "system").mkdir()
        (src / "system" / "important.md").write_text("do not delete")

        # dst lives in a totally different tree
        dst = tmp_path / "elsewhere" / "victim"

        with pytest.raises(ValueError, match="not a sibling"):
            AvatarManager._prepare_deep(src, dst)

        # src untouched
        assert (src / "system" / "important.md").read_text() == "do not delete"
