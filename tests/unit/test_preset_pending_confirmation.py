import json
from pathlib import Path

from lingtai.agent import Agent
from lingtai_kernel.intrinsics.system.preset import _presets, _refresh


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def make_preset(path: Path, provider: str, model: str) -> None:
    write_json(path, {
        "name": path.stem,
        "description": {"summary": path.stem},
        "manifest": {
            "llm": {"provider": provider, "model": model},
            "capabilities": {},
        },
    })


def make_init(workdir: Path, active: str, default: str, allowed: list[str]) -> None:
    write_json(workdir / "init.json", {
        "name": "tester",
        "manifest": {
            "llm": {"provider": "openai", "model": "old"},
            "capabilities": {},
            "preset": {
                "active": active,
                "default": default,
                "allowed": allowed,
            },
        },
    })


class RefreshAgent:
    def __init__(self, workdir: Path):
        self._working_dir = workdir
        self._config = type("Config", (), {"language": "en"})()
        self.events = []
        self.refreshed = False

    def _log(self, event_type, **fields):
        self.events.append((event_type, fields))

    def _activate_preset(self, name):
        Agent._activate_preset(self, name)

    def _activate_default_preset(self):
        Agent._activate_default_preset(self)

    def _perform_refresh(self):
        self.refreshed = True

    def get_token_usage(self):
        return {"ctx_total_tokens": 0}


class ConfirmAgent:
    def __init__(self, workdir: Path):
        self._working_dir = workdir
        self.events = []
        self.notifications = []

    def _log(self, event_type, **fields):
        self.events.append((event_type, fields))

    def _enqueue_system_notification(self, **kwargs):
        self.notifications.append(kwargs)
        return "evt_test"


def test_refresh_writes_pending_marker_before_relaunch(tmp_path):
    old = str(tmp_path / "old.json")
    new = str(tmp_path / "new.json")
    make_preset(Path(old), "openai", "old")
    make_preset(Path(new), "openai", "new")
    make_init(tmp_path, active=old, default=old, allowed=[old, new])

    agent = RefreshAgent(tmp_path)
    result = _refresh(agent, {"preset": new, "reason": "test swap"})

    assert result["status"] == "ok"
    assert agent.refreshed is True
    pending = json.loads((tmp_path / ".preset.pending").read_text(encoding="utf-8"))
    assert pending["requested"] == new
    assert pending["prior_active"] == old
    assert pending["reason"] == "test swap"
    assert pending["revert"] is False
    init = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert init["manifest"]["preset"]["active"] == new


def test_presets_surfaces_pending_marker(tmp_path, monkeypatch):
    preset = str(tmp_path / "active.json")
    make_preset(Path(preset), "openai", "model")
    make_init(tmp_path, active=preset, default=preset, allowed=[preset])
    write_json(tmp_path / ".preset.pending", {"requested": preset})

    monkeypatch.setattr(
        "lingtai_kernel.preset_connectivity.check_many",
        lambda specs: [{"ok": True} for _ in specs],
    )
    agent = RefreshAgent(tmp_path)
    result = _presets(agent, {})

    assert result["status"] == "ok"
    assert result["active"] == preset
    assert result["pending"] == {"requested": preset}


def test_confirm_preset_pending_clears_marker_on_match(tmp_path):
    active = str(tmp_path / "active.json")
    make_init(tmp_path, active=active, default=active, allowed=[active])
    write_json(tmp_path / ".preset.pending", {
        "requested": active,
        "prior_active": "old",
        "requested_at": 123.0,
        "revert": False,
    })
    agent = ConfirmAgent(tmp_path)

    Agent._confirm_preset_pending(agent)

    assert not (tmp_path / ".preset.pending").exists()
    assert any(event == "preset_swap_completed" for event, _ in agent.events)
    assert agent.notifications
    assert agent.notifications[0]["source"] == "preset"
    assert agent.notifications[0]["ref_id"] == active


def test_confirm_preset_pending_leaves_marker_on_drift(tmp_path):
    requested = str(tmp_path / "requested.json")
    active = str(tmp_path / "active.json")
    make_init(tmp_path, active=active, default=active, allowed=[active, requested])
    write_json(tmp_path / ".preset.pending", {
        "requested": requested,
        "prior_active": "old",
        "requested_at": 123.0,
    })
    agent = ConfirmAgent(tmp_path)

    Agent._confirm_preset_pending(agent)

    assert (tmp_path / ".preset.pending").exists()
    drift = [fields for event, fields in agent.events if event == "preset_swap_drifted"]
    assert drift
    assert drift[0]["requested"] == requested
    assert drift[0]["active"] == active
    assert not agent.notifications
