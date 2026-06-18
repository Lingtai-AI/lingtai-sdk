"""Stage-3I wrapper bridge: host the *real* ``avatar_spawn`` / ``avatar_rules``
tools through the SDK peer-spawn bundles.

Where ``tests/test_sdk_avatar_tools.py`` proves the SDK-side declarations + host
seams with dummy handlers (and import purity), this test proves the *wrapper* half
— ``lingtai.core.avatar_bundle`` — that injects the genuine wrapper
``avatar.make_spawn_handler(agent)`` / ``avatar.make_rules_handler(agent)`` into the
SDK bundles and so runs the real behavior through the declared manifests.

The key assertion is **parity**: invoking the avatar tools through the bundle hosts
returns exactly what the live path returns, because the bridge wires the *same*
source of truth (``avatar.make_manager`` the live ``avatar.setup()`` registers),
bound to the same agent.

**Safety:** NO real avatar process is ever spawned here.

* The ``dry_run`` spawn path short-circuits in the live handler *before* any working
  dir is created or process launched (it only reads the parent ``init.json`` and
  returns a preview), so it is safe to exercise for real.
* The single confirmed-spawn parity test monkeypatches ``AvatarManager._launch`` and
  ``_wait_for_boot`` (the same technique ``tests/test_avatar_rules.py`` uses) so no
  subprocess is forked — only the local ledger / ``.rules`` filesystem bookkeeping
  runs, inside the test's ``tmp_path`` sandbox.
* The ``avatar_rules`` paths use the admin-less error case (errors before any write)
  and the admin case (writes only ``.rules`` signal files under ``tmp_path``); no
  process is spawned and no descendant exists, so nothing escapes the sandbox.
* No network, no GitHub, no real agent boot.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.agent import Agent
from lingtai.core import avatar as avatarmod
from lingtai.core import avatar_bundle


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


@pytest.fixture
def agent(tmp_path):
    # Avatars spawn as *siblings* of the agent's working dir, so nest the agent
    # one level under tmp_path to keep every avatar dir inside the sandbox.
    wd = tmp_path / "net" / "parent"
    wd.mkdir(parents=True, exist_ok=True)
    a = Agent(
        service=make_mock_service(),
        agent_name="parent",
        working_dir=wd,
        capabilities=["avatar"],
        admin={"karma": True},
    )
    try:
        yield a
    finally:
        a.stop(timeout=1.0)


@pytest.fixture
def non_admin_agent(tmp_path):
    wd = tmp_path / "net2" / "worker"
    wd.mkdir(parents=True, exist_ok=True)
    a = Agent(
        service=make_mock_service(),
        agent_name="worker",
        working_dir=wd,
        capabilities=["avatar"],
        admin={},
    )
    try:
        yield a
    finally:
        a.stop(timeout=1.0)


def _fake_launch_return(pid: int = 12345):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    return (proc, Path("/tmp/avatar_stderr.log"))


@contextmanager
def _patch_avatar_launch(*, boot_status: str = "ok", boot_error=None):
    """Patch ``_launch`` + ``_wait_for_boot`` so no subprocess is forked."""
    with patch.object(
        avatarmod.AvatarManager, "_launch", return_value=_fake_launch_return()
    ), patch.object(
        avatarmod.AvatarManager,
        "_wait_for_boot",
        return_value=(boot_status, boot_error),
    ):
        yield


# --- the bridge builds the right hosts ---------------------------------------


def test_avatar_spawn_bridge_builds_in_process_host(agent):
    host = avatar_bundle.avatar_spawn_bundle_host(agent)
    assert host.tools == ("avatar_spawn",)
    assert host.manifest.name == "avatar_spawn"
    assert host.manifest.roles.privileged is False
    assert host.manifest.transport.kind == "in_process"
    assert host.manifest.security.danger == "destructive"


def test_avatar_rules_bridge_builds_in_process_host(agent):
    host = avatar_bundle.avatar_rules_bundle_host(agent)
    assert host.tools == ("avatar_rules",)
    assert host.manifest.name == "avatar_rules"
    assert host.manifest.transport.kind == "in_process"
    assert host.manifest.security.danger == "destructive"


def test_bridge_builds_hosts_mapping(agent):
    hosts = avatar_bundle.avatar_bundle_hosts(agent)
    assert set(hosts) == {"avatar_spawn", "avatar_rules"}
    assert hosts["avatar_spawn"].tools == ("avatar_spawn",)
    assert hosts["avatar_rules"].tools == ("avatar_rules",)


# --- drift guard: SDK declared schema == live wrapper schema -----------------


def test_avatar_spawn_schema_mirrors_live_get_schema():
    """Pin the SDK avatar_spawn declaration to the live wrapper schema."""
    from lingtai_sdk import avatar_tools as at

    declared = at.avatar_spawn_manifest().metadata["schema"]
    live = avatarmod.get_schema()
    # property keys mirror the live schema (descriptions are i18n'd live and omitted).
    assert set(declared["properties"]) == set(live["properties"])
    assert declared["required"] == live["required"] == ["name"]
    # the enum on `type` is the language-neutral copy of the live enum.
    assert (
        declared["properties"]["type"]["enum"]
        == live["properties"]["type"]["enum"]
        == ["shallow", "deep"]
    )


def test_avatar_rules_schema_mirrors_live_get_rules_schema():
    from lingtai_sdk import avatar_tools as at

    declared = at.avatar_rules_manifest().metadata["schema"]
    live = avatarmod.get_rules_schema()
    assert set(declared["properties"]) == set(live["properties"])
    assert declared["required"] == live["required"] == ["rules_content"]


# --- avatar parity: the bundle path runs the real handler --------------------


def test_avatar_spawn_dry_run_parity(agent):
    """A dry_run spawn matches the live handler — no process, no files written.

    The live handler short-circuits before any working dir / process is created.
    """
    host = avatar_bundle.avatar_spawn_bundle_host(agent)
    # Parent needs an init.json for the dry-run preview to reach the short-circuit.
    (agent._working_dir / "init.json").write_text(
        '{"manifest": {"agent_name": "parent", "admin": {}}}'
    )
    args = {
        "name": "researcher",
        "_reasoning": "Investigate the open-flux discrepancy in PFSS models.",
    }
    via_bundle = host.invoke("avatar_spawn", **{**args, "dry_run": True})
    via_live = avatarmod.make_spawn_handler(agent)({**args, "dry_run": True})
    assert via_bundle == via_live
    assert via_bundle["status"] == "dry_run"
    assert via_bundle["preview"]["name"] == "researcher"
    # No sibling avatar directory was created by the dry run.
    assert not (agent._working_dir.parent / "researcher").exists()


def test_avatar_spawn_missing_name_error_parity(agent):
    host = avatar_bundle.avatar_spawn_bundle_host(agent)
    via_bundle = host.invoke("avatar_spawn")
    via_live = avatarmod.make_spawn_handler(agent)({})
    assert via_bundle == via_live
    assert "error" in via_bundle


def test_avatar_spawn_mission_gate_parity(agent):
    """A short/test-like mission trips the confirmation gate identically.

    No filesystem mutation occurs — the gate returns before any spawn work.
    """
    host = avatar_bundle.avatar_spawn_bundle_host(agent)
    (agent._working_dir / "init.json").write_text(
        '{"manifest": {"agent_name": "parent", "admin": {}}}'
    )
    args = {"name": "tmp", "_reasoning": "test"}
    via_bundle = host.invoke("avatar_spawn", **args)
    via_live = avatarmod.make_spawn_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "confirmation_needed"
    assert not (agent._working_dir.parent / "tmp").exists()


def test_avatar_spawn_confirmed_runs_through_bundle(agent):
    """A confirmed spawn runs end to end through the bundle with NO real subprocess.

    ``_launch`` / ``_wait_for_boot`` are monkeypatched, so only the local ledger +
    ``.rules`` bookkeeping runs inside the tmp_path sandbox. This proves the bundle
    host drives the real spawn pipeline (ledger append, working-dir creation),
    not just the early-return paths.
    """
    host = avatar_bundle.avatar_spawn_bundle_host(agent)
    (agent._working_dir / "init.json").write_text(
        '{"manifest": {"agent_name": "parent", "admin": {"karma": true}}}'
    )
    args = {
        "name": "scholar",
        "_reasoning": "Summarize the switchback literature for PSP encounter 9.",
        "confirm": True,
    }
    with _patch_avatar_launch():
        result = host.invoke("avatar_spawn", **args)
    assert result["status"] == "ok"
    assert result["agent_name"] == "scholar"
    assert result["address"] == "scholar"
    # The avatar working dir was created as a sibling, inside the sandbox.
    assert (agent._working_dir.parent / "scholar").is_dir()


def test_avatar_rules_non_admin_error_parity(non_admin_agent):
    """A non-admin agent is refused identically on both paths — nothing written."""
    host = avatar_bundle.avatar_rules_bundle_host(non_admin_agent)
    args = {"rules_content": "No deleting files."}
    via_bundle = host.invoke("avatar_rules", **args)
    via_live = avatarmod.make_rules_handler(non_admin_agent)(dict(args))
    assert via_bundle == via_live
    assert "error" in via_bundle
    # No .rules signal written when refused.
    assert not (non_admin_agent._working_dir / ".rules").is_file()


def test_avatar_rules_missing_content_error_parity(agent):
    host = avatar_bundle.avatar_rules_bundle_host(agent)
    via_bundle = host.invoke("avatar_rules")
    via_live = avatarmod.make_rules_handler(agent)({})
    assert via_bundle == via_live
    assert "error" in via_bundle


def test_avatar_rules_admin_writes_self_signal_through_bundle(agent):
    """An admin avatar_rules call writes a ``.rules`` signal to self via the bundle.

    Only a local signal file is written (no descendants exist) — the heartbeat
    loop, not this call, persists it. No process is spawned.
    """
    host = avatar_bundle.avatar_rules_bundle_host(agent)
    result = host.invoke("avatar_rules", rules_content="Always log actions.")
    assert result["status"] == "ok"
    assert (agent._working_dir / ".rules").read_text() == "Always log actions."
    # self is reported in the distribution.
    assert agent._working_dir.name in result["distributed_to"]


def test_avatar_make_handler_is_setup_single_source(agent):
    """``setup()`` and the bridge build handlers through the same factory.

    ``avatar.setup()`` registers handlers from a manager built by ``make_manager``,
    and the bridge hosts handlers from the *same* ``make_spawn_handler`` /
    ``make_rules_handler`` (also via ``make_manager``), so the bundle hosts cannot
    drift from the registered tools. Exercised on the admin-less rules error path
    (no side effect).
    """
    # setup() registered both tools on the agent at construction (capabilities=).
    assert "avatar_spawn" in agent._tool_handlers
    assert "avatar_rules" in agent._tool_handlers
    args = {"rules_content": "be concise"}
    setup_run = agent._tool_handlers["avatar_rules"](dict(args))
    host = avatar_bundle.avatar_rules_bundle_host(agent)
    bundle_run = host.invoke("avatar_rules", **args)
    assert bundle_run == setup_run
    assert bundle_run["status"] == "ok"


# --- the bridge does not eagerly import the SDK at wrapper module load --------


def test_bridge_does_not_import_sdk_at_wrapper_module_load():
    """Importing the wrapper bridge module must not eagerly import the SDK.

    The SDK is imported lazily inside the bridge functions (wrapper -> sdk edge),
    so a bare import of the bridge module leaves ``lingtai_sdk`` unloaded until a
    host is actually built.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    code = (
        "import sys\n"
        "import lingtai.core.avatar_bundle as ab\n"
        "assert 'lingtai_sdk' not in sys.modules, "
        "'bridge import eagerly pulled the SDK'\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
