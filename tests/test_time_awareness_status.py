"""Integration test: status() blanks runtime fields when time_awareness=False."""
from types import SimpleNamespace


def _mk_fake_agent(time_awareness: bool):
    # Tests assume UTC output (Z suffix), so timezone_awareness=False here.
    return SimpleNamespace(_config=SimpleNamespace(
        time_awareness=time_awareness,
        timezone_awareness=False,
    ))


def test_status_runtime_blanks_when_time_blind():
    from lingtai_kernel.time_veil import scrub_time_fields, now_iso
    agent = _mk_fake_agent(False)

    runtime = scrub_time_fields(
        agent,
        {
            "current_time": now_iso(agent),
            "started_at": "2026-04-15T12:00:00Z",
            "uptime_seconds": 123.4,
            "stamina": 3600.0,
            "stamina_left": 3476.6,
        },
        keys=("current_time", "started_at", "uptime_seconds", "stamina", "stamina_left"),
    )
    assert runtime["current_time"] == ""
    assert runtime["started_at"] == ""
    assert runtime["uptime_seconds"] == ""
    assert runtime["stamina"] == ""
    assert runtime["stamina_left"] == ""


def test_status_runtime_preserved_when_time_aware():
    from lingtai_kernel.time_veil import scrub_time_fields, now_iso
    agent = _mk_fake_agent(True)

    runtime = scrub_time_fields(
        agent,
        {
            "current_time": now_iso(agent),
            "started_at": "2026-04-15T12:00:00Z",
            "uptime_seconds": 123.4,
            "stamina": 3600.0,
            "stamina_left": 3476.6,
        },
        keys=("current_time", "started_at", "uptime_seconds", "stamina", "stamina_left"),
    )
    assert runtime["current_time"].endswith("Z")
    assert runtime["started_at"] == "2026-04-15T12:00:00Z"
    assert runtime["uptime_seconds"] == 123.4
    assert runtime["stamina"] == 3600.0
    assert runtime["stamina_left"] == 3476.6
