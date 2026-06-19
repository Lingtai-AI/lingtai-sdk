"""Derive Z.AI mode from the agent's LLM base_url."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent


def resolve_z_ai_mode(agent: "BaseAgent") -> str:
    """Determine Z_AI_MODE from the agent's LLM base_url.

    Returns ``"ZHIPU"`` for domestic (bigmodel.cn) endpoints,
    ``"ZAI"`` for international (z.ai) endpoints.
    """
    base_url = getattr(agent.service, "_base_url", None) or ""
    if "bigmodel.cn" in base_url:
        return "ZHIPU"
    return "ZAI"
