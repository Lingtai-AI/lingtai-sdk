"""Tests for `_perform_refresh` filesystem handshake and lifecycle signaling.

Three call sites reach `_perform_refresh` directly:

  1. Heartbeat — has already renamed `.refresh` → `.refresh.taken` and
     intends to call `_shutdown.set()` immediately after our return.
  2. `system(action='refresh')` intrinsic — has done neither.
  3. AED preset-fallback in `turn.py` — has done neither.

`_perform_refresh` therefore normalizes the on-disk handshake itself
(making `.refresh.taken` present and clearing `.refresh`) and signals
`_cancel_event` + `_shutdown` after the watcher subprocess is spawned,
so the lock-release phase completes regardless of caller. These tests
exercise that contract without spawning a real subprocess.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "p"
    svc.model = "m"
    return svc


def _make_agent_with_launch_cmd(tmp_path, agent_name="alice"):
    """Build a bare BaseAgent and rebind `_build_launch_cmd` so the
    refresh path proceeds past the `cmd is None` early return. The
    actual subprocess call is patched in each test to avoid relaunches.
    """
    from lingtai_kernel.base_agent import BaseAgent
    wd = tmp_path / "test"
    wd.mkdir(exist_ok=True)
    agent = BaseAgent(
        service=make_mock_service(),
        agent_name=agent_name,
        working_dir=wd,
    )
    # BaseAgent._build_launch_cmd returns None; rebind to a sentinel
    # list so the handshake/signal code runs.
    agent._build_launch_cmd = lambda: ["python", "-c", "print('relaunch sentinel')"]
    return agent


# ---------------------------------------------------------------------------
# Filesystem handshake
# ---------------------------------------------------------------------------


def test_perform_refresh_direct_call_synthesizes_taken(tmp_path):
    """Direct call with neither .refresh nor .refresh.taken on disk:
    `_perform_refresh` synthesizes `.refresh.taken` so the watcher's
    ack-phase poll finds it."""
    agent = _make_agent_with_launch_cmd(tmp_path)
    wd = agent._working_dir
    assert not (wd / ".refresh").exists()
    assert not (wd / ".refresh.taken").exists()

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
        assert mock_popen.called  # watcher subprocess spawned

    assert (wd / ".refresh.taken").exists(), \
        ".refresh.taken must exist after direct refresh — watcher polls for it"
    assert not (wd / ".refresh").exists(), \
        ".refresh must be absent so heartbeat does not spawn a duplicate watcher"


def test_perform_refresh_preserves_existing_taken(tmp_path):
    """Heartbeat path: `.refresh.taken` already exists. `_perform_refresh`
    must preserve it (not overwrite or remove) and still proceed."""
    agent = _make_agent_with_launch_cmd(tmp_path)
    wd = agent._working_dir
    taken = wd / ".refresh.taken"
    taken.write_text("preexisting body")  # heartbeat just renamed; some marker payload

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
        assert mock_popen.called

    assert taken.exists()
    assert taken.read_text() == "preexisting body", \
        "preexisting .refresh.taken contents must be preserved"


def test_perform_refresh_renames_existing_refresh(tmp_path):
    """Direct call but `.refresh` is on disk (e.g. tool-call path raced
    with heartbeat detection): rename .refresh → .refresh.taken instead
    of synthesizing, preserving any payload."""
    agent = _make_agent_with_launch_cmd(tmp_path)
    wd = agent._working_dir
    refresh = wd / ".refresh"
    refresh.write_text("refresh body")
    assert not (wd / ".refresh.taken").exists()

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
        assert mock_popen.called

    assert not refresh.exists()
    taken = wd / ".refresh.taken"
    assert taken.exists()
    assert taken.read_text() == "refresh body", \
        "rename should preserve .refresh payload as .refresh.taken"


def test_perform_refresh_removes_stale_refresh_if_both_exist(tmp_path):
    """Both .refresh and .refresh.taken on disk (rare race): preserve
    .refresh.taken (the ack the watcher polls for) and unlink .refresh
    so the heartbeat doesn't spawn a duplicate watcher next tick."""
    agent = _make_agent_with_launch_cmd(tmp_path)
    wd = agent._working_dir
    (wd / ".refresh").write_text("racey")
    (wd / ".refresh.taken").write_text("ack")

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
        assert mock_popen.called

    assert not (wd / ".refresh").exists()
    assert (wd / ".refresh.taken").exists()
    assert (wd / ".refresh.taken").read_text() == "ack"


# ---------------------------------------------------------------------------
# Lifecycle signaling
# ---------------------------------------------------------------------------



def test_perform_refresh_ack_write_failure_does_not_shutdown(tmp_path):
    """If the ack file cannot be established, fail safe: do not spawn
    the watcher and do not shut the running agent down. A failed refresh
    should leave the current process alive rather than killing it without
    a relaunch path.
    """
    agent = _make_agent_with_launch_cmd(tmp_path)
    wd = agent._working_dir

    real_touch = type(wd / ".refresh.taken").touch

    def touch_side_effect(self, *args, **kwargs):
        if self.name == ".refresh.taken":
            raise OSError("simulated ack write failure")
        return real_touch(self, *args, **kwargs)

    log_events = []
    agent._log = lambda event, **kw: log_events.append((event, kw))

    with patch("pathlib.Path.touch", touch_side_effect), \
         patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()

    assert not mock_popen.called
    assert not (wd / ".refresh.taken").exists()
    assert not agent._shutdown.is_set()
    assert not agent._cancel_event.is_set()
    assert any(event == "refresh_ack_failed" for event, _ in log_events)


def test_perform_refresh_sets_shutdown_and_cancel(tmp_path):
    """Direct callers (tool-call refresh, AED preset fallback) don't go
    through the heartbeat's shutdown-set step. `_perform_refresh` must
    set both `_shutdown` and `_cancel_event` itself so the run loop
    exits, the lock releases, and the watcher's second phase completes.
    """
    agent = _make_agent_with_launch_cmd(tmp_path)
    assert not agent._shutdown.is_set()
    assert not agent._cancel_event.is_set()

    with patch("subprocess.Popen"):
        agent._perform_refresh()

    assert agent._shutdown.is_set(), \
        "_perform_refresh must set _shutdown so the run loop exits and the lock releases"
    assert agent._cancel_event.is_set(), \
        "_perform_refresh must set _cancel_event so in-flight turn work yields"


def test_perform_refresh_no_launch_cmd_skips_handshake(tmp_path):
    """When `_build_launch_cmd()` returns None (e.g. bare BaseAgent),
    `_perform_refresh` logs and returns BEFORE touching the handshake or
    signaling shutdown — those signals would orphan the agent without a
    relaunch to recover it."""
    from lingtai_kernel.base_agent import BaseAgent
    wd = tmp_path / "test"
    wd.mkdir(exist_ok=True)
    agent = BaseAgent(
        service=make_mock_service(),
        agent_name="alice",
        working_dir=wd,
    )
    # Default _build_launch_cmd returns None — do not override.

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
        assert not mock_popen.called

    assert not (wd / ".refresh.taken").exists(), \
        "no-launch-cmd path must not synthesize the ack file"
    assert not agent._shutdown.is_set(), \
        "no-launch-cmd path must not orphan the agent by setting shutdown"


def test_perform_refresh_logs_handshake_source(tmp_path):
    """`refresh_deferred_relaunch` event carries a `handshake` field so
    production telemetry can confirm which code path executed."""
    agent = _make_agent_with_launch_cmd(tmp_path)

    log_events = []
    real_log = agent._log

    def log_capture(event, **kw):
        log_events.append((event, kw))
        return real_log(event, **kw)

    agent._log = log_capture

    with patch("subprocess.Popen"):
        agent._perform_refresh()

    relaunch_events = [
        (e, kw) for e, kw in log_events if e == "refresh_deferred_relaunch"
    ]
    assert len(relaunch_events) == 1
    _, kw = relaunch_events[0]
    assert kw.get("handshake") == "synthesized_direct_call", \
        f"expected synthesized_direct_call, got {kw.get('handshake')!r}"


def test_perform_refresh_watcher_marks_env_file_overwrite(tmp_path):
    """A refresh relaunch inherits the old process environment. Mark the
    watcher process so the relaunched agent overwrites stale env_file values
    with freshly edited .env contents.
    """
    agent = _make_agent_with_launch_cmd(tmp_path)

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()

    assert mock_popen.called
    _, kwargs = mock_popen.call_args
    assert kwargs["env"]["LINGTAI_REFRESH_ENV_OVERWRITE"] == "1"


def test_refresh_watcher_script_cleans_stale_duplicate_process(tmp_path):
    """Production incident 2026-06-04: refresh watcher relaunches can be
    blocked by a stale `lingtai run <agent-dir>` process. The watcher script
    must detect the duplicate-process guard stderr and terminate only a stale
    same-agent process (no fresh heartbeat) before retrying.
    """
    agent = _make_agent_with_launch_cmd(tmp_path)

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()

    assert mock_popen.called
    args, _kwargs = mock_popen.call_args
    script = args[0][2]
    assert "another lingtai agent is already running" in script
    assert "def _cleanup_stale_duplicate" in script
    assert "('lingtai run ' + wd) in cmdline" in script
    assert "heartbeat_age" in script
    assert "signal.SIGTERM" in script
    assert "signal.SIGKILL" in script
    assert "refresh_watcher_stale_duplicate" in script
