"""Shared fake ChatCompletion builders for provider adapter tests."""
from __future__ import annotations

from types import SimpleNamespace


def make_raw_response(*, content=None, reasoning_content=None, tool_calls=None):
    """Build a minimal fake OpenAI ChatCompletion-like object."""
    msg = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls or [],
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    )


def make_tool_call(id_, name, args_json="{}"):
    return SimpleNamespace(
        id=id_,
        function=SimpleNamespace(name=name, arguments=args_json),
    )
