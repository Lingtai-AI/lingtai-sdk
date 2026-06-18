"""Stage-3H wrapper bridge: host the *real* ``bash`` tool through the SDK
shell-execution bundle.

Where ``tests/test_sdk_bash_tools.py`` proves the SDK-side declaration + host seam
with a dummy handler (and import purity), this test proves the *wrapper* half —
``lingtai.core.bash_bundle`` — that injects the genuine wrapper
``bash.make_handler(agent)`` into the SDK bundle and so runs the real behavior
through the declared manifest.

The key assertion is **parity**: invoking ``bash`` through the bundle host returns
exactly what the live path returns, because the bridge wires the *same* source of
truth (``bash.make_manager`` / ``make_handler`` the live ``bash.setup()`` registers,
with the same default-policy resolution), bound to the same agent.

**Safety:** every command exercised here is a harmless, no-op temp command
(``echo`` / ``true``) run inside the agent's working-directory sandbox under the
bundled **default denylist** policy (which blocks ``rm`` / ``sudo`` / ``kill`` /
…). The ``poll`` / ``cancel`` paths are exercised against a missing ``job_id`` (they
error before touching any process). No dangerous command, no external side effect,
no real process kill of anything but the test's own short ``echo`` async job.
"""
from __future__ import annotations

import os

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai.core import bash as bashmod
from lingtai.core import bash_bundle
from lingtai_sdk import bash_tools as bt


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


@pytest.fixture
def agent(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    a = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=wd)
    try:
        yield a
    finally:
        a.stop(timeout=1.0)


# --- the bridge builds the right host ---------------------------------------


def test_bash_bridge_builds_in_process_host(agent):
    host = bash_bundle.bash_exec_bundle_host(agent)
    assert host.tools == ("bash",)
    assert host.manifest.name == "bash"
    assert host.manifest.roles.privileged is False
    assert host.manifest.transport.kind == "in_process"
    assert host.manifest.security.danger == "destructive"


def test_bridge_builds_hosts_mapping(agent):
    hosts = bash_bundle.bash_exec_bundle_hosts(agent)
    assert set(hosts) == {"bash"}
    assert hosts["bash"].tools == ("bash",)


def _schema_actions(schema: dict) -> set[str]:
    return set(schema["properties"]["action"]["enum"])


# --- drift guard: SDK declared action set == live schema action enum ---------


def test_bash_manifest_actions_match_live_schema():
    """Pin the SDK bash declaration to the live wrapper schema action enum."""
    declared = set(bt.bash_exec_manifest().metadata["actions"])
    live = _schema_actions(bashmod.get_schema())
    assert declared == live == {"run", "poll", "cancel"}


def test_bash_manifest_schema_mirrors_live_property_keys():
    """The declared schema property keys mirror the live ``get_schema`` keys."""
    declared = set(bt.bash_exec_manifest().metadata["schema"]["properties"])
    live = set(bashmod.get_schema()["properties"])
    assert declared == live


# --- bash parity: the bundle path runs the real handler, byte-identical -------


def test_bash_run_sync_parity(agent):
    """A harmless sync ``echo`` matches the live handler, byte-identically.

    Both go through ``bash.make_handler`` → ``BashManager.handle`` against the same
    agent sandbox and default policy. ``echo`` is allowed by the default denylist.
    """
    host = bash_bundle.bash_exec_bundle_host(agent)
    args = {"action": "run", "command": "echo bash-bundle-parity-ok"}
    via_bundle = host.invoke("bash", **args)
    via_live = bashmod.make_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "ok"
    assert via_bundle["exit_code"] == 0
    assert "bash-bundle-parity-ok" in via_bundle["stdout"]


def test_bash_run_empty_command_error_parity(agent):
    host = bash_bundle.bash_exec_bundle_host(agent)
    args = {"action": "run", "command": ""}
    via_bundle = host.invoke("bash", **args)
    via_live = bashmod.make_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "command is required" in via_bundle["message"]


def test_bash_run_policy_denied_parity(agent):
    """The default denylist blocks ``rm`` identically on both paths.

    No filesystem is touched — the policy rejects the command before execution.
    """
    host = bash_bundle.bash_exec_bundle_host(agent)
    args = {"action": "run", "command": "rm -rf /tmp/does-not-exist-xyz"}
    via_bundle = host.invoke("bash", **args)
    via_live = bashmod.make_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "not allowed" in via_bundle["message"]


def test_bash_poll_missing_job_error_parity(agent):
    """``poll`` of a non-existent job errors identically — before any work."""
    host = bash_bundle.bash_exec_bundle_host(agent)
    args = {"action": "poll", "job_id": "job-deadbeef"}
    via_bundle = host.invoke("bash", **args)
    via_live = bashmod.make_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "Job not found" in via_bundle["message"]


def test_bash_cancel_missing_job_error_parity(agent):
    """``cancel`` of a non-existent job errors identically — kills nothing."""
    host = bash_bundle.bash_exec_bundle_host(agent)
    args = {"action": "cancel", "job_id": "job-deadbeef"}
    via_bundle = host.invoke("bash", **args)
    via_live = bashmod.make_handler(agent)(dict(args))
    assert via_bundle == via_live
    assert via_bundle["status"] == "error"
    assert "Job not found" in via_bundle["message"]


# --- async job behavior through the bundle (safe, no-op echo) ----------------


def test_bash_async_job_lifecycle_through_bundle(agent):
    """A safe async ``echo`` runs to completion through the bundle host.

    Exercises the ``run(async=True)`` → ``poll`` path end to end with a harmless
    command. Job ids differ per manager instance, so this is a single-host
    lifecycle assertion (not cross-handler parity).
    """
    import time

    host = bash_bundle.bash_exec_bundle_host(agent)
    started = host.invoke(
        "bash", action="run", command="echo async-ok", **{"async": True}
    )
    assert started["status"] == "ok"
    job_id = started["job_id"]
    assert job_id

    # Poll until the job completes (short, harmless command).
    result = None
    for _ in range(50):
        result = host.invoke("bash", action="poll", job_id=job_id)
        if result["status"] == "done":
            break
        assert result["status"] == "running"
        time.sleep(0.05)

    assert result is not None
    assert result["status"] == "done"
    assert result["exit_code"] == 0
    assert "async-ok" in result["stdout"]


def test_bash_make_handler_is_setup_single_source(agent):
    """``setup()`` and the bridge build the handler through the same factory.

    ``bash.setup()`` registers a manager built by ``make_manager`` via ``add_tool``,
    and the bridge hosts a handler from the *same* ``make_handler`` (also via
    ``make_manager``), so the bundle host cannot drift from the registered tool.
    """
    bashmod.setup(agent)
    assert "bash" in agent._tool_handlers
    args = {"action": "run", "command": "echo single-source"}
    setup_run = agent._tool_handlers["bash"](dict(args))
    host = bash_bundle.bash_exec_bundle_host(agent)
    bundle_run = host.invoke("bash", **args)
    assert bundle_run == setup_run
    assert bundle_run["status"] == "ok"
    assert "single-source" in bundle_run["stdout"]


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
        "import lingtai.core.bash_bundle as bb\n"
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
