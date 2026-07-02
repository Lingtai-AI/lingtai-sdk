"""Tests for system intrinsic — runtime, lifecycle, and synchronization."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.intrinsics import ALL_INTRINSICS


@pytest.fixture(autouse=True)
def _stub_preset_connectivity(monkeypatch):
    """Auto-mock network probes in test_system.py so _presets tests don't
    actually open sockets. Returns a fixed 42ms latency."""
    from lingtai_kernel import preset_connectivity
    monkeypatch.setattr(preset_connectivity, "_probe_host",
                        lambda host, port, timeout: 42)
    yield


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_system_in_all_intrinsics():
    assert "system" in ALL_INTRINSICS
    info = ALL_INTRINSICS["system"]
    assert "module" in info
    mod = info["module"]
    assert hasattr(mod, "get_schema")
    assert hasattr(mod, "get_description")
    assert hasattr(mod, "handle")


def test_system_wired_in_agent(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    assert "system" in agent._intrinsics




# ---------------------------------------------------------------------------
# agent.status() — internal Python API; writes .status.json for TUI/portal.
# The LLM-callable system(action="show") was removed; identity now ships in
# the cached system prompt and runtime metadata ships on every tool result via _meta.
# These tests cover the status() shape contract that TUI/portal depend on.
# ---------------------------------------------------------------------------


def test_status_returns_identity(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="alice", working_dir=tmp_path / "test")
    agent.start()
    try:
        result = agent.status()
        identity = result["identity"]
        assert identity["agent_name"] == "alice"
        assert "test" in identity["address"]
        assert identity["mail_address"] is None
    finally:
        agent.stop()


def test_status_returns_runtime(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        time.sleep(0.1)
        result = agent.status()
        runtime = result["runtime"]
        assert "T" in runtime["started_at"]
        assert runtime["uptime_seconds"] >= 0.05
    finally:
        agent.stop()


def test_status_returns_state(tmp_path):
    """status() exposes runtime.state so the TUI knows the lifecycle phase."""
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        result = agent.status()
        runtime = result["runtime"]
        assert "state" in runtime, "runtime.state missing — C1 regression"
        assert runtime["state"] in ("active", "idle", "asleep", "stuck", "suspended"), \
            f"unexpected state value: {runtime['state']!r}"
    finally:
        agent.stop()


def test_status_dict_state_matches_agent_state(tmp_path):
    """C1: agent.status() and agent._state agree."""
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        result = agent.status()
        assert result["runtime"]["state"] == agent._state.value
    finally:
        agent.stop()


def test_status_returns_tokens(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        result = agent.status()
        tokens = result["tokens"]
        assert "input_tokens" in tokens
        assert "output_tokens" in tokens
        assert "total_tokens" in tokens
        assert "api_calls" in tokens
        assert "context" in tokens
        ctx = tokens["context"]
        assert "window_size" in ctx
        assert "usage_pct" in ctx
    finally:
        agent.stop()


def test_status_with_mail_service(tmp_path):
    mock_mail = MagicMock()
    mock_mail.address = "127.0.0.1:8301"
    agent = BaseAgent(
        agent_name="test", working_dir=tmp_path / "test",
        service=make_mock_service(),
        mail_service=mock_mail,
    )
    agent.start()
    try:
        result = agent.status()
        assert result["identity"]["mail_address"] == "127.0.0.1:8301"
    finally:
        agent.stop()


def test_status_context_null_without_session(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    result = agent.status()
    ctx = result["tokens"]["context"]
    assert ctx["window_size"] is None
    assert ctx["usage_pct"] is None


def test_system_show_action_rejected(tmp_path):
    """system(action='show') was removed; calling it must error, not silently no-op."""
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    result = agent._intrinsics["system"]({"action": "show"})
    assert result["status"] == "error"
    assert "Unknown system action" in result["message"]


# ---------------------------------------------------------------------------
# nap removed — was broken (blocked notifications/soul flow while inside
# tool handler). _nap_wake / _wake_nap remain as general-purpose heartbeat
# nudges used by notification sync.
# ---------------------------------------------------------------------------


def test_system_nap_returns_unknown_action(tmp_path):
    """nap is no longer a valid system action."""
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    result = agent._intrinsics["system"]({"action": "nap", "seconds": 1})
    assert result["status"] == "error"
    assert "Unknown system action" in result["message"]


# ---------------------------------------------------------------------------
# self-sleep (go asleep)
# ---------------------------------------------------------------------------


def test_system_self_sleep(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test", admin={"karma": True})
    result = agent._intrinsics["system"]({"action": "sleep", "reason": "need bash"})
    assert result["status"] == "ok"
    assert agent._asleep.is_set()
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# refresh (restart)
# ---------------------------------------------------------------------------


def test_system_refresh(tmp_path):
    """refresh action: returns ok and writes a ``.refresh`` signal file
    that the live agent's heartbeat loop consumes to drive the deferred
    relaunch. (Older versions also set ``agent._refresh_requested`` and
    ``agent._shutdown`` synchronously — both retired in favor of the
    signal-file + watcher-subprocess pattern.)
    """
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    result = agent._intrinsics["system"]({"action": "refresh", "reason": "new tools"})
    assert result["status"] == "ok"
    # The signal file is the contract; the heartbeat loop keys off it.
    # Note: BaseAgent._build_launch_cmd returns None by default, so no
    # actual .refresh file may be written here (only Agent overrides it).
    # The OK return is the only universally-true signal.
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------


def test_system_unknown_action(tmp_path):
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    result = agent._intrinsics["system"]({"action": "bogus"})
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# refresh + preset arg, presets action
# ---------------------------------------------------------------------------


def _make_test_agent_for_presets(tmp_path, presets_path=None, active_preset=None, default_preset=None):
    """Build a BaseAgent for preset tests with init.json containing optional
    manifest.preset umbrella. The agent's _activate_preset and
    _perform_refresh are intentionally left as the BaseAgent defaults; tests
    monkeypatch them to observe call patterns.

    active_preset / default_preset: stem names; the helper resolves them to
    full paths under `presets_path` (or stores them verbatim when no
    presets_path is given — useful for tests that won't actually load the file).
    default_preset: if provided, writes manifest.preset.default to this value
    (instead of mirroring active_preset).
    """
    import json
    from pathlib import Path as _P
    wd = tmp_path / "test"
    wd.mkdir(exist_ok=True)
    manifest = {
        "agent_name": "alice",
        "language": "en",
        "llm": {"provider": "gemini", "model": "gemini-test",
                "api_key": None, "api_key_env": "GEMINI_API_KEY"},
        "capabilities": {},
        "soul": {"delay": 120}, "stamina": 3600,
        "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
        "admin": {}, "streaming": False,
    }

    def _to_path(stem):
        if not stem:
            return stem
        # If already a path-form (contains slash or .json/.jsonc), pass through.
        if "/" in stem or stem.endswith((".json", ".jsonc")):
            return stem
        if presets_path is not None:
            return str(_P(presets_path) / f"{stem}.json")
        return stem

    if active_preset:
        active_value = _to_path(active_preset)
        default_value = _to_path(default_preset if default_preset is not None else active_preset)
        # Build `allowed` from every *.json[c] in `presets_path` (the test
        # helper's old `path` field meant "scan this directory for the listing"
        # so the new schema's `allowed` is the explicit version of the same).
        allowed_paths: list[str] = []
        if presets_path is not None:
            for entry in sorted(_P(presets_path).iterdir()):
                if entry.is_file() and entry.suffix in (".json", ".jsonc") and entry.name != "_kernel_meta.json":
                    allowed_paths.append(str(entry))
        if active_value not in allowed_paths:
            allowed_paths.append(active_value)
        if default_value not in allowed_paths:
            allowed_paths.append(default_value)
        manifest["preset"] = {
            "active": active_value,
            "default": default_value,
            "allowed": allowed_paths,
        }
    init = {
        "manifest": manifest,
        "principle": "p", "covenant": "c", "pad": "", "lingtai": "",
        "soul": "",
    }
    (wd / "init.json").write_text(json.dumps(init))
    agent = BaseAgent(service=make_mock_service(), agent_name="alice",
                      working_dir=wd)
    return agent


def test_refresh_with_unauthorized_preset_returns_error(tmp_path, monkeypatch):
    """system(action='refresh', preset='ghost') with `ghost` not in `allowed`
    returns error and logs preset_swap_refused_unauthorized — the activate
    path never runs because authorization is checked first."""
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "minimax.json").write_text(json.dumps({
        "name": "minimax", "description": {"summary": "x"},
        "manifest": {"llm": {"provider": "minimax", "model": "y",
                             "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib,
                                          active_preset="minimax")

    log_events = []
    real_log = agent._log
    def log_capture(event, **kw):
        log_events.append((event, kw))
        return real_log(event, **kw)
    monkeypatch.setattr(agent, "_log", log_capture)

    # Track _perform_refresh calls — should NOT be called when swap is refused
    perform_calls = []
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    # _activate_preset must NOT be called — the allowed-gate runs first
    activate_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda name: activate_calls.append(name))

    result = agent._intrinsics["system"]({"action": "refresh", "preset": "ghost"})

    assert result["status"] == "error"
    assert "ghost" in result["message"]
    assert "presets" in result["message"]  # guidance: call system(action='presets')
    events = [e for e, _ in log_events]
    assert "preset_swap_refused_unauthorized" in events
    assert activate_calls == []  # never reached
    assert perform_calls == []  # refresh NOT triggered


def test_preset_ref_in_normalizes_tilde_and_absolute(tmp_path, monkeypatch):
    """`_preset_ref_in` must treat `~/...` and the equivalent absolute
    path as the same preset, in both directions — otherwise the
    allowed-gate refuses legitimate swaps when path forms diverge."""
    from pathlib import Path
    from lingtai_kernel.intrinsics.system import _preset_ref_in
    # Path.expanduser() reads $HOME — point it at a tempdir we can resolve.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Use the resolved absolute path so macOS's /private/var/... symlink
    # prefix doesn't cause a spurious mismatch.
    abs_path = str((home / "presets" / "alpha.json").resolve())
    tilde = "~/presets/alpha.json"
    assert _preset_ref_in(tilde, [abs_path])
    assert _preset_ref_in(abs_path, [tilde])
    assert _preset_ref_in(tilde, [tilde])
    assert not _preset_ref_in(tilde, ["/other/foo.json"])
    assert not _preset_ref_in("", [tilde])


def test_refresh_with_known_preset_calls_activate_then_perform(tmp_path, monkeypatch):
    """system(action='refresh', preset='minimax') calls _activate_preset then _perform_refresh."""
    agent = _make_test_agent_for_presets(tmp_path)

    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh",
                                           "preset": "minimax"})

    assert activate_calls == ["minimax"]
    assert perform_calls == [True]
    assert result["status"] == "ok"


def test_refresh_no_preset_arg_unchanged(tmp_path, monkeypatch):
    """system(action='refresh') with no preset arg behaves as today (no _activate_preset call)."""
    agent = _make_test_agent_for_presets(tmp_path)

    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    agent._intrinsics["system"]({"action": "refresh"})

    assert activate_calls == []  # not called
    assert perform_calls == [True]


def test_refresh_empty_preset_is_no_swap(tmp_path, monkeypatch):
    """system(action='refresh', preset='') is treated as no swap requested.

    Tool-call providers sometimes serialize optional string fields as ""
    instead of omitting them. Without normalization, an empty string flows
    into the authorization gate and returns 'preset \\'\\' is not in this
    agent's allowed list' — a confusing error for an unintended swap.
    """
    agent = _make_test_agent_for_presets(tmp_path)

    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh", "preset": ""})
    assert result["status"] == "ok"
    assert activate_calls == []  # empty string must not request a swap
    assert perform_calls == [True]


def test_refresh_whitespace_preset_is_no_swap(tmp_path, monkeypatch):
    """preset='   ' (whitespace-only) normalizes to no swap, same as ''."""
    agent = _make_test_agent_for_presets(tmp_path)

    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh", "preset": "   \t\n"})
    assert result["status"] == "ok"
    assert activate_calls == []
    assert perform_calls == [True]


def test_presets_action_lists_full_library(tmp_path):
    """system(action='presets') returns full library with descriptions and capabilities."""
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "alpha.json").write_text(json.dumps({
        "name": "alpha", "description": {"summary": "alpha desc"},
        "manifest": {"llm": {"provider": "p1", "model": "m1",
                             "api_key": None, "api_key_env": "X"},
                     "capabilities": {"file": {}, "vision": {"provider": "p1"}}},
    }))
    (plib / "beta.json").write_text(json.dumps({
        "name": "beta", "description": {"summary": "structured", "gains": ["a"]},
        "manifest": {"llm": {"provider": "p2", "model": "m2",
                             "api_key": None, "api_key_env": "Y"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib,
                                          active_preset="alpha")

    result = agent._intrinsics["system"]({"action": "presets"})

    assert result["status"] == "ok"
    # In the path-as-name model, `active` and entry names are full paths.
    alpha_path = str(plib / "alpha.json")
    beta_path = str(plib / "beta.json")
    assert result["active"] == alpha_path
    names = [p["name"] for p in result["available"]]
    assert sorted(names) == sorted([alpha_path, beta_path])

    alpha = next(p for p in result["available"] if p["name"] == alpha_path)
    assert alpha["description"] == {"summary": "alpha desc"}
    assert alpha["llm"] == {"provider": "p1", "model": "m1"}
    assert "vision" in alpha["capabilities"]

    beta = next(p for p in result["available"] if p["name"] == beta_path)
    assert beta["description"] == {"summary": "structured", "gains": ["a"]}

    for entry in result["available"]:
        assert "connectivity" in entry
        assert entry["connectivity"]["status"] in ("ok", "no_credentials", "unreachable")
        assert "checked_at" in entry["connectivity"]


def test_presets_action_strips_credentials(tmp_path):
    """presets action does not surface api_key, base_url, or api_compat in the llm summary.

    api_key_env names (e.g. 'OPENAI_API_KEY') are not credentials — they are just
    environment variable names. The connectivity field may mention the env var name
    in its 'no_credentials' error message, which is intentional and non-sensitive.
    Only the actual key value ('SECRET') must never appear in the output.
    """
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "secret.json").write_text(json.dumps({
        "name": "secret", "description": {"summary": "x"},
        "manifest": {"llm": {"provider": "p", "model": "m",
                             "api_key": "SECRET", "api_key_env": "ENVKEY",
                             "base_url": "https://example.com",
                             "api_compat": "openai"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib,
                                          active_preset="secret")

    result = agent._intrinsics["system"]({"action": "presets"})

    secret = result["available"][0]
    assert set(secret["llm"].keys()) == {"provider", "model"}
    # The actual api_key value must never appear — it is a true credential.
    assert "SECRET" not in json.dumps(result)
    # The base_url must not appear in the llm summary (connectivity uses it
    # internally for probing but does not store it in the output).
    assert "example.com" not in json.dumps(result)


def test_presets_action_empty_library(tmp_path):
    """Empty library returns empty available[] without error."""
    plib = tmp_path / "presets"
    plib.mkdir()
    # Must provide active_preset so the preset umbrella block is written with the
    # explicit path — otherwise resolve_presets_path defaults to ~/.lingtai-tui/presets/
    # and would read the user's real presets library.
    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib,
                                          active_preset="nonexistent")

    result = agent._intrinsics["system"]({"action": "presets"})

    assert result["status"] == "ok"
    assert result["available"] == []


def test_refresh_with_preset_handles_not_implemented(tmp_path):
    """When _activate_preset is the BaseAgent stub (raises NotImplementedError),
    _refresh returns a clean error dict instead of letting the exception escape."""
    agent = _make_test_agent_for_presets(tmp_path)
    # Don't monkeypatch _activate_preset — let the BaseAgent stub fire.
    # _perform_refresh must NOT be called when activation fails.
    perform_calls = []
    import unittest.mock as _mock
    with _mock.patch.object(agent, "_perform_refresh",
                            lambda: perform_calls.append(True)):
        result = agent._intrinsics["system"](
            {"action": "refresh", "preset": "anything"})
    assert result["status"] == "error"
    assert "anything" in result["message"]
    assert perform_calls == []  # refresh NOT triggered


# ---------------------------------------------------------------------------
# revert_preset flag
# ---------------------------------------------------------------------------


def test_refresh_revert_preset_swaps_to_default(tmp_path, monkeypatch):
    """system(refresh, revert_preset=True) calls _activate_default_preset then _perform_refresh."""
    # Make active != default by editing init.json after agent construction
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "boring.json").write_text(json.dumps({
        "name": "boring", "description": {"summary": "default"},
        "manifest": {"llm": {"provider": "p1", "model": "m1",
                             "api_key": None, "api_key_env": "P1KEY"},
                     "capabilities": {"file": {}}},
    }))
    (plib / "fancy.json").write_text(json.dumps({
        "name": "fancy", "description": {"summary": "non-default"},
        "manifest": {"llm": {"provider": "p2", "model": "m2",
                             "api_key": None, "api_key_env": "P2KEY"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(tmp_path,
                                          presets_path=plib,
                                          active_preset="fancy",
                                          default_preset="boring")

    activate_default_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_default_preset",
                        lambda: activate_default_calls.append(True))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh", "revert_preset": True})

    assert result["status"] == "ok"
    assert activate_default_calls == [True]
    assert perform_calls == [True]


def test_refresh_revert_preset_with_preset_arg_errors(tmp_path):
    """system(refresh, preset='x', revert_preset=True) returns error."""
    agent = _make_test_agent_for_presets(tmp_path)

    result = agent._intrinsics["system"]({
        "action": "refresh",
        "preset": "minimax",
        "revert_preset": True,
    })

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "both" in msg or "either" in msg or "one" in msg
    assert "preset" in msg
    assert "revert" in msg


def test_refresh_empty_preset_with_revert_preset_treats_empty_as_absent(
    tmp_path, monkeypatch
):
    """preset='' (empty string) plus revert_preset=True is NOT a conflict.

    Tool-call providers sometimes serialize optional string fields as ""
    instead of omitting them; normalizing empty/whitespace at the boundary
    means revert proceeds as if `preset` weren't supplied at all. The old
    behavior (return conflict error) leaked an implementation detail of
    JSON tool-call schemas into agent-facing semantics. See lifecycle
    self-refresh patch.
    """
    # Build with an explicit default preset so the revert path can resolve.
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "home.json").write_text(json.dumps({
        "name": "home", "description": {"summary": "x"},
        "manifest": {"llm": {"provider": "p", "model": "m",
                             "api_key": None, "api_key_env": "PKEY"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(
        tmp_path, presets_path=plib,
        active_preset="home", default_preset="home",
    )

    activate_default_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_default_preset",
                        lambda: activate_default_calls.append(True))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({
        "action": "refresh",
        "preset": "",
        "revert_preset": True,
    })
    assert result["status"] == "ok"
    assert activate_default_calls == [True]
    assert perform_calls == [True]


def test_refresh_revert_preset_when_no_preset_configured_errors(tmp_path, monkeypatch):
    """system(refresh, revert_preset=True) errors if manifest.preset is absent.

    The error fires upstream of _activate_default_preset: _refresh reads
    init.json directly, finds no manifest.preset.default, and returns
    the error before any activation path runs."""
    # Build an agent without a preset block
    from lingtai_kernel.base_agent import BaseAgent
    from unittest.mock import MagicMock
    import json
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "x"
    svc.model = "y"
    wd = tmp_path / "test"
    wd.mkdir()
    init = {
        "manifest": {
            "agent_name": "alice", "language": "en",
            "llm": {"provider": "x", "model": "y", "api_key": None, "api_key_env": "X"},
            "capabilities": {}, "soul": {"delay": 120}, "stamina": 3600,
            "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
            "admin": {}, "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "lingtai": "", "soul": "",
    }
    (wd / "init.json").write_text(json.dumps(init))
    agent = BaseAgent(service=svc, agent_name="alice", working_dir=wd)

    # Don't actually relaunch on _perform_refresh
    monkeypatch.setattr(agent, "_perform_refresh", lambda: None)

    result = agent._intrinsics["system"]({"action": "refresh", "revert_preset": True})
    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "default" in msg or "no preset" in msg or "configured" in msg


def test_refresh_revert_preset_false_is_noop(tmp_path, monkeypatch):
    """revert_preset=False behaves identically to omitting the flag."""
    agent = _make_test_agent_for_presets(tmp_path)

    activate_default_calls = []
    activate_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_default_preset",
                        lambda: activate_default_calls.append(True))
    monkeypatch.setattr(agent, "_activate_preset",
                        lambda n: activate_calls.append(n))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh", "revert_preset": False})

    assert result["status"] == "ok"
    assert activate_default_calls == []  # not called
    assert activate_calls == []  # not called
    assert perform_calls == [True]


def test_refresh_revert_preset_when_active_equals_default_still_succeeds(tmp_path, monkeypatch):
    """Reverting when already on default is fine — _activate_default_preset is
    effectively a no-op rewrite, refresh proceeds normally."""
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "home.json").write_text(json.dumps({
        "name": "home", "description": {"summary": "x"},
        "manifest": {"llm": {"provider": "p", "model": "m",
                             "api_key": None, "api_key_env": "PKEY"},
                     "capabilities": {"file": {}}},
    }))
    agent = _make_test_agent_for_presets(tmp_path,
                                          presets_path=plib,
                                          active_preset="home",
                                          default_preset="home")  # same as active

    activate_default_calls = []
    perform_calls = []
    monkeypatch.setattr(agent, "_activate_default_preset",
                        lambda: activate_default_calls.append(True))
    monkeypatch.setattr(agent, "_perform_refresh",
                        lambda: perform_calls.append(True))

    result = agent._intrinsics["system"]({"action": "refresh", "revert_preset": True})

    assert result["status"] == "ok"
    assert activate_default_calls == [True]  # called even though it's effectively a no-op
    assert perform_calls == [True]


# ---------------------------------------------------------------------------
# connectivity field on presets action
# ---------------------------------------------------------------------------


def test_presets_action_includes_connectivity(tmp_path, monkeypatch):
    """presets action returns a connectivity field for each preset.
    Test covers: ok (with credentials), no_credentials (env var missing)."""
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "alpha.json").write_text(json.dumps({
        "name": "alpha", "description": {"summary": "x"},
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4",
                    "api_key": None, "api_key_env": "ALPHA_KEY",
                    "base_url": "https://api.example.com"},
            "capabilities": {"file": {}},
        },
    }))
    (plib / "beta.json").write_text(json.dumps({
        "name": "beta", "description": {"summary": "y"},
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-3",
                    "api_key": None, "api_key_env": "BETA_KEY",
                    "base_url": "https://api.example.com"},
            "capabilities": {"file": {}},
        },
    }))
    # alpha has credentials, beta doesn't
    monkeypatch.setenv("ALPHA_KEY", "sk-test")
    monkeypatch.delenv("BETA_KEY", raising=False)

    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib, active_preset="alpha")
    result = agent._intrinsics["system"]({"action": "presets"})

    assert result["status"] == "ok"
    by_name = {p["name"]: p for p in result["available"]}
    alpha_path = str(plib / "alpha.json")
    beta_path = str(plib / "beta.json")

    # alpha — has credentials, mocked probe succeeds → ok
    assert by_name[alpha_path]["connectivity"]["status"] == "ok"
    assert by_name[alpha_path]["connectivity"]["latency_ms"] == 42

    # beta — no credentials → no_credentials, no network call
    assert by_name[beta_path]["connectivity"]["status"] == "no_credentials"
    assert by_name[beta_path]["connectivity"]["latency_ms"] is None


def test_presets_action_marks_unreachable_when_probe_fails(tmp_path, monkeypatch):
    """If the network probe raises, connectivity status is 'unreachable'."""
    import json
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "broken.json").write_text(json.dumps({
        "name": "broken", "description": {"summary": "x"},
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4",
                    "api_key": None, "api_key_env": "BROKEN_KEY",
                    "base_url": "https://api.example.com"},
            "capabilities": {"file": {}},
        },
    }))
    monkeypatch.setenv("BROKEN_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity
    # Override the autouse fixture's stub for this test
    monkeypatch.setattr(preset_connectivity, "_probe_host",
                        lambda host, port, timeout: (_ for _ in ()).throw(OSError("DNS fail")))

    agent = _make_test_agent_for_presets(tmp_path, presets_path=plib, active_preset="broken")
    result = agent._intrinsics["system"]({"action": "presets"})

    by_name = {p["name"]: p for p in result["available"]}
    broken_path = str(plib / "broken.json")
    assert by_name[broken_path]["connectivity"]["status"] == "unreachable"
    assert "DNS fail" in by_name[broken_path]["connectivity"]["error"]


# ---------------------------------------------------------------------------
# CPR failure propagation
# ---------------------------------------------------------------------------


def test_cpr_propagates_launch_failure_instead_of_resuscitated(tmp_path):
    """A failed CPR launch must not be reported as resuscitated."""
    from lingtai_kernel.intrinsics.system.karma import _cpr

    target = tmp_path / "target"
    target.mkdir()
    (target / ".agent.json").write_text('{"admin": {}}')

    class FakeAgent:
        _admin = {"karma": True}
        _working_dir = tmp_path / "self"

        def __init__(self):
            self.logs = []

        def _cpr_agent(self, address):
            return {"error": True, "message": "launch failed before heartbeat"}

        def _log(self, event, **kwargs):
            self.logs.append((event, kwargs))

    agent = FakeAgent()
    result = _cpr(agent, {"address": str(target)})

    assert result["error"] is True
    assert "launch failed" in result["message"]
    assert any(event == "karma_cpr_failed" for event, _ in agent.logs)
