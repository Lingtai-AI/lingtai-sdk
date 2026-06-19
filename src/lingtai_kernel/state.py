"""AgentState — lifecycle state enum for 灵台 agents."""

from __future__ import annotations

import enum


class AgentState(enum.Enum):
    """Lifecycle state of an agent.

    ACTIVE --(completed)--------> IDLE
    ACTIVE --(timeout/exception)-> STUCK
    IDLE   --(inbox message)----> ACTIVE
    STUCK  --(AED)--------------> ACTIVE  (session reset, fresh run loop)
    STUCK  --(AED timeout)------> ASLEEP  (sleep, listeners alive)
    ACTIVE/IDLE --(sleep)-------> ASLEEP
    ASLEEP --(inbox message)---> ACTIVE  (wake from sleep)
    ASLEEP --(.suspend/SIGINT)-> SUSPENDED (process exits)
    SUSPENDED --(lingtai-agent run)---> IDLE    (reconstructed from working dir)
    """

    ACTIVE = "active"
    IDLE = "idle"
    STUCK = "stuck"
    ASLEEP = "asleep"
    SUSPENDED = "suspended"
