from lingtai_kernel.state import AgentState


def test_agent_state_values():
    assert AgentState.ACTIVE.value == "active"
    assert AgentState.IDLE.value == "idle"
    assert AgentState.STUCK.value == "stuck"
    assert AgentState.ASLEEP.value == "asleep"


def test_suspended_state():
    assert AgentState.SUSPENDED.value == "suspended"
