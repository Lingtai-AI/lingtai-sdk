"""Mimo (Xiaomi) adapter — satisfies MiMo thinking-mode's reasoning_content
round-trip contract, analogous to DeepSeek.

MiMo speaks the OpenAI Chat Completions protocol. Once thinking mode has
been invoked by an assistant ``tool_calls`` turn, MiMo (like DeepSeek)
requires every subsequent assistant turn — tool-call AND plain-text —
to carry ``reasoning_content`` on replay. Assistant turns BEFORE the
first tool_call must NOT carry it.

Earlier, this adapter stripped ``reasoning_content`` from every replayed
assistant turn as a workaround for a model loop: when the *same* thinking
block was echoed back unchanged on every turn, MiMo treated it as
authoritative and parroted it verbatim, eventually tripping the 120s LLM
hang watchdog. Real per-turn reasoning from ``ThinkingBlock``s is
byte-different by construction (and so is the per-turn-unique fallback
below), which avoids that pathology while satisfying the protocol.

Real reasoning is preserved end-to-end now: ``OpenAIChatSession`` captures
``reasoning_content`` into a ``ThinkingBlock`` on each assistant turn, and
``interface_converters.to_openai`` emits the block back as
``reasoning_content`` on replay. This adapter only injects a per-turn-unique
fallback for rehydrated/historical assistant turns that have no captured
``ThinkingBlock`` (e.g. ``chat_history.jsonl`` entries written before this
fix, or turns where the provider returned no reasoning text).
"""
from __future__ import annotations

from ..openai.adapter import OpenAIAdapter, OpenAIChatSession


def _fallback_reasoning_for(msg: dict, turn_idx: int) -> str:
    """Build a per-turn-unique reasoning stub for an assistant message.

    The string must be byte-different per turn — a constant placeholder
    re-introduces the original loop pathology (model echoes the same
    thinking block back). Inlining tool names, call ids, and a content
    snippet keeps the stub naturally unique per turn.
    """
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        names = ",".join(
            (tc.get("function", {}) or {}).get("name", "") for tc in tool_calls
        )
        ids = ",".join(tc.get("id", "") for tc in tool_calls)
        return f"call {names} [{ids}] (turn {turn_idx})"
    content = msg.get("content") or ""
    snippet = content[:64].replace("\n", " ")
    return f"reply [{snippet}] (turn {turn_idx})"


class MimoChatSession(OpenAIChatSession):
    """Chat session that satisfies MiMo's reasoning_content round-trip contract.

    Real ``reasoning_content`` produced by ``interface_converters.to_openai``
    from captured ``ThinkingBlock``s is preserved verbatim. Assistant turns
    after the first tool_call that lack a ThinkingBlock get a per-turn-unique
    fallback. Pre-tool-call plain-text assistant turns are left alone.
    """

    def _build_messages(self) -> list[dict]:
        messages = super()._build_messages()
        seen_tool_call = False
        turn_idx = 0
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            if msg.get("tool_calls"):
                seen_tool_call = True
            if seen_tool_call:
                turn_idx += 1
                if not msg.get("reasoning_content"):
                    msg["reasoning_content"] = _fallback_reasoning_for(msg, turn_idx)
        return messages


class MimoAdapter(OpenAIAdapter):
    """OpenAI-compat adapter pinned to MiMo with reasoning_content round-trip."""

    _session_class = MimoChatSession

    def _default_prompt_cache_key(self, model: str) -> str:
        # Fixed provider identity — use a clean ``lingtai-mimo`` namespace
        # rather than the base_url host. MiMo Chat Completions accepts
        # ``prompt_cache_key`` (compat probe); a stable key lets successive
        # turns hit the cross-request prompt cache.
        return f"lingtai-mimo:{model}:v1"
