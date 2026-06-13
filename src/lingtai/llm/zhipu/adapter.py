"""Zhipu (GLM) adapter — thin OpenAI-compat wrapper that merges
consecutive same-role messages to prevent GLM error 1214.

Zhipu GLM rejects requests containing consecutive messages with the
same role (error code 1214).  This is a provider-specific constraint —
the generic OpenAI adapter should not carry the workaround.

The fix is a single ``_build_messages`` override on the session that
post-processes the wire-format messages before they reach the API.

Everything else inherits from ``OpenAIAdapter`` / ``OpenAIChatSession``
unchanged via the ``_build_messages`` and ``_session_class`` hook points
on the parent.
"""

from __future__ import annotations

import logging

from ..openai.adapter import OpenAIAdapter, OpenAIChatSession

logger = logging.getLogger(__name__)


def _extract_text(content) -> str:
    """Extract plain text from a content value (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _merge_consecutive_same_role(messages: list[dict]) -> list[dict]:
    """Merge consecutive messages with the same role.

    Zhipu GLM (error 1214) rejects requests that contain consecutive
    messages with the same role.  This function merges adjacent
    same-role messages by concatenating their text content.

    Rules:
    - system messages are never merged (should be singular anyway).
    - tool messages are never merged (each has a distinct tool_call_id).
    - assistant messages: text content concatenated; tool_calls taken
      from the last message in the run that carries them.
    - user messages: text content concatenated.

    Idempotent — returns the list unchanged if no consecutive duplicates.
    """
    if len(messages) <= 1:
        return messages

    result: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        # Never merge system or tool messages.
        if role in ("system", "tool") or not result:
            result.append(msg)
            continue
        prev = result[-1]
        if prev.get("role") != role:
            result.append(msg)
            continue

        # --- merge into prev ---
        logger.warning(
            "[wire-sanitize] merging consecutive %s messages — "
            "GLM rejects same-role runs (error 1214)",
            role,
        )
        prev_content = prev.get("content")
        cur_content = msg.get("content")
        prev_is_list = isinstance(prev_content, list)
        cur_is_list = isinstance(cur_content, list)

        prev_text = _extract_text(prev_content)
        cur_text = _extract_text(cur_content)
        parts = [p for p in (prev_text, cur_text) if p]
        merged_text = "\n".join(parts) if parts else ""

        # Keep list format if either side used it.
        if prev_is_list or cur_is_list:
            prev["content"] = [{"type": "text", "text": merged_text}]
        else:
            prev["content"] = merged_text

        if role == "assistant":
            # Preserve tool_calls from the *last* message that has them.
            if msg.get("tool_calls"):
                prev["tool_calls"] = msg["tool_calls"]

    return result


class ZhipuChatSession(OpenAIChatSession):
    """Chat session that merges consecutive same-role messages for Zhipu GLM.

    GLM error 1214 fires when the wire-format message list has adjacent
    messages with the same role.  This override applies the merge as the
    last step of ``_build_messages``, after the parent has done all its
    standard formatting.
    """

    def _build_messages(self) -> list[dict]:
        messages = super()._build_messages()
        return _merge_consecutive_same_role(messages)


class ZhipuAdapter(OpenAIAdapter):
    """OpenAI-compat adapter for Zhipu GLM with same-role message merging."""

    _session_class = ZhipuChatSession

    def _default_prompt_cache_key(self, model: str) -> str:
        # Fixed provider identity — use a clean ``lingtai-zhipu`` namespace
        # rather than the base_url host. Zhipu/GLM Chat Completions accepts
        # ``prompt_cache_key`` (compat probe); a stable key lets successive
        # turns hit the cross-request prompt cache.
        return f"lingtai-zhipu:{model}:v1"
