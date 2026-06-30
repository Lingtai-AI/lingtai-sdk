import json

from lingtai_kernel.notifications import collect_notifications
from lingtai_kernel.nudge import upsert
from lingtai_kernel.nudge import kernel_version as kv


class _Agent:
    def __init__(self, workdir):
        self._working_dir = workdir
        self.logs = []

    def _log(self, event, **fields):
        self.logs.append((event, fields))


def _entries(workdir):
    return (
        collect_notifications(workdir)
        .get("nudge", {})
        .get("data", {})
        .get("nudges", [])
    )


def _reset_fast_gate(agent):
    agent._nudge_kernel_version_state["last_probe_ts"] = 0.0


def test_installed_runtime_refresh_nudge_does_not_hit_remote(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("remote should not be queried")),
    )

    kv.check(agent)

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "kernel_version"
    assert entry["source"] == "installed-distribution"
    assert entry["cadence"] == "at-most-once-per-utc-day"
    assert entry["running"] == "0.14.1"
    assert entry["installed"] == "0.14.2"
    assert entry["suggested_action"] == "read-runtime-update-skill-then-refresh-if-safe"
    assert "runtime-update-checks" in entry["skill"]
    assert "system(action='refresh')" in entry["detail"]


def test_local_refresh_mismatch_does_not_re_emit_same_utc_day(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")

    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    # Agent dismisses the nudge (deletes nudge.json).
    from lingtai_kernel.nudge import remove

    remove(agent, "kernel_version")
    assert _entries(tmp_path) == []

    # Same UTC day, same mismatch still present -> must NOT re-emit.
    _reset_fast_gate(agent)
    kv.check(agent)
    assert _entries(tmp_path) == []


def test_local_refresh_mismatch_re_emits_next_utc_day(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )

    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    from lingtai_kernel.nudge import remove

    remove(agent, "kernel_version")
    assert _entries(tmp_path) == []

    # Next UTC day, same mismatch still present -> re-emit once for that day.
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-07-01")
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1
    assert _entries(tmp_path)[0]["source"] == "installed-distribution"

    # Still the same day after another dismissal -> no re-emit.
    remove(agent, "kernel_version")
    _reset_fast_gate(agent)
    kv.check(agent)
    assert _entries(tmp_path) == []


def test_local_refresh_match_clears_mismatch_tracking_and_stale_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")

    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    # Versions now match (a refresh happened). This must clear the stale
    # nudge and the mismatch tracking so a future mismatch emits again.
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.2",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: "0.14.2")
    _reset_fast_gate(agent)
    kv.check(agent)
    assert _entries(tmp_path) == []

    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert "emitted_for_mismatch" not in state["kernel_version"]
    assert "mismatch_emitted_date" not in state["kernel_version"]

    # A fresh mismatch on the same day must emit again.
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.2",
            installed_version="0.14.3",
            dev_reason=None,
        ),
    )
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1
    assert _entries(tmp_path)[0]["installed"] == "0.14.3"


def test_remote_update_check_is_daily_and_persistent(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.1",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")

    def latest():
        calls["n"] += 1
        return "0.14.2"

    monkeypatch.setattr(kv, "_fetch_latest_version", latest)

    kv.check(agent)

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "pypi-json"
    assert entry["cadence"] == "at-most-once-per-utc-day"
    assert entry["latest"] == "0.14.2"
    assert entry["suggested_action"] == "read-runtime-update-skill-and-ask-human"
    assert "human" in entry["detail"].lower()
    assert calls["n"] == 1

    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["last_remote_check_date"] == "2026-06-24"
    assert state["kernel_version"]["checked_installed_version"] == "0.14.1"
    assert state["kernel_version"]["latest_seen"] == "0.14.2"

    _reset_fast_gate(agent)
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("daily throttle failed")),
    )
    kv.check(agent)
    assert calls["n"] == 1
    assert _entries(tmp_path)[0]["latest"] == "0.14.2"


def test_dev_or_editable_runtime_skips_and_clears_kernel_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    upsert(agent, "kernel_version", {"title": "old", "source": "pypi-json"})
    assert _entries(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1.dev0",
            installed_version="0.14.1.dev0",
            dev_reason="editable-install",
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("dev mode should skip remote")),
    )

    kv.check(agent)

    assert "nudge" not in collect_notifications(tmp_path)
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["last_skip_date"] == "2026-06-24"
    assert state["kernel_version"]["skip_reason"] == "editable-install"


def test_current_remote_version_clears_existing_kernel_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    upsert(agent, "kernel_version", {"title": "old", "source": "pypi-json"})
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.1",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: "0.14.1")

    kv.check(agent)

    assert "nudge" not in collect_notifications(tmp_path)
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["latest_seen"] == "0.14.1"
    assert state["kernel_version"]["emitted_for_latest"] is None
