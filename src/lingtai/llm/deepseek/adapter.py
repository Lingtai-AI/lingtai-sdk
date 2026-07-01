"""DeepSeek adapter — thin OpenAI-compat wrapper that satisfies the
reasoning_content round-trip contract for thinking mode.

DeepSeek V4 thinking mode rejects requests missing ``reasoning_content``
on assistant turns once thinking has been triggered. Omitting it returns
HTTP 400:

    "The `reasoning_content` in the thinking mode must be passed back
     to the API."

The actual contract (determined empirically — the docs understate it):

    Once any assistant turn in the conversation has tool_calls, ALL
    subsequent assistant turns (tool-call AND plain-text) must carry
    reasoning_content when replayed.

Assistant turns BEFORE the first tool_call don't need it. After the
first tool_call, every assistant turn needs it — including the final
plain-text reply that followed the tool loop.

Real reasoning is preserved end-to-end now. The OpenAI adapter captures
``reasoning_content`` into a ThinkingBlock on every assistant turn
(``openai/adapter.py``); ``interface_converters.to_openai`` emits the
ThinkingBlock back as ``reasoning_content`` on replay. The historical
"byte-identical placeholder" approach (commits afc7ddc → 86c2a3d)
caused DeepSeek's cache fast-path to collapse onto the placeholder
string, producing empty responses (issue #9). Real per-turn reasoning
is byte-different by construction and avoids the collapse.

The only remaining responsibility of this adapter is the **fallback**:
if an assistant turn replayed from history has no captured ThinkingBlock
(e.g. chat_history.jsonl entries written before this fix shipped, or
entries where the provider returned no reasoning text at all), inject a
per-turn-unique stub so DeepSeek's field-presence validator is satisfied
without re-introducing the cache-collapse pattern.

Everything else inherits from ``OpenAIAdapter`` / ``OpenAIChatSession``
unchanged via the ``_build_messages`` and ``_session_class`` hook points
on the parent.
"""

from __future__ import annotations

from ..openai.adapter import OpenAIAdapter, OpenAIChatSession


_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _fallback_reasoning_for(msg: dict, turn_idx: int) -> str:
    """Build a per-turn-unique reasoning stub for an assistant message.

    Used only when an assistant turn carries no real reasoning_content
    (typically: history entries that predate the ThinkingBlock-preservation
    fix, or turns where the provider returned no reasoning text). The
    string must be byte-different per turn — a constant placeholder
    triggers DeepSeek's cache fast-path to collapse onto it and emit
    empty responses (issue #9).

    DeepSeek validates field presence, not content, so any non-empty
    string is accepted. We inline tool names and the call ids to keep
    the stub naturally unique per turn.
    """
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        names = ",".join(
            (tc.get("function", {}) or {}).get("name", "") for tc in tool_calls
        )
        ids = ",".join(tc.get("id", "") for tc in tool_calls)
        return f"call {names} [{ids}] (turn {turn_idx})"
    # Plain-text post-tool-call turn — content is per-turn unique by nature.
    content = msg.get("content") or ""
    snippet = content[:64].replace("\n", " ")
    return f"reply [{snippet}] (turn {turn_idx})"


class DeepSeekChatSession(OpenAIChatSession):
    """Chat session that satisfies DeepSeek's reasoning_content round-trip contract.

    Real reasoning_content is emitted by ``interface_converters.to_openai``
    when the canonical interface has a ThinkingBlock on the assistant turn.
    This subclass only injects a per-turn-unique fallback on assistant
    turns that lack one — typically rehydrated history entries from before
    the fix shipped.
    """

    def _build_messages(self) -> list[dict]:
        messages = super()._build_messages()
        # Field-presence requirement only kicks in once thinking mode has
        # been invoked by an assistant tool_call. Earlier plain-text turns
        # must NOT carry reasoning_content — DeepSeek rejects that.
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


class DeepSeekAdapter(OpenAIAdapter):
    """OpenAI-compat adapter pinned to DeepSeek with reasoning_content round-trip."""

    _session_class = DeepSeekChatSession

    def _default_prompt_cache_key(self, model: str) -> str:
        # Fixed provider identity — use a clean ``lingtai-deepseek`` namespace
        # rather than the base_url host. DeepSeek Chat Completions accepts
        # ``prompt_cache_key`` (compat probe); a stable key lets successive
        # turns hit the cross-request prompt cache.
        return f"lingtai-deepseek:{model}:v1"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        max_rpm: int = 0,
        default_headers: dict | None = None,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url or _DEEPSEEK_BASE_URL,
            timeout_ms=timeout_ms,
            max_rpm=max_rpm,
            default_headers=default_headers,
        )
