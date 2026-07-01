"""Turn-level integration tests for the sparse ``_meta`` agent/guidance blocks.

These tests drive ``base_agent.turn._process_response`` end-to-end (with light
fakes) to verify the parent-identified blockers are actually fixed at the
boundary, not just in the helper:

  * blocker #1 — ``attach_active_runtime`` is invoked at the tool-batch
    boundary, so the latest provider-visible result gets ``_meta.agent_meta``
    and ``_meta.guidance``.
  * the sparse / update-driven invariant — when the material agent snapshot is
    unchanged across consecutive batches, ``_meta.agent_meta`` is NOT re-stamped
    onto the newer result (the prior holder keeps it as a historical update
    point); a genuinely material change re-attaches it to the newer result and
    strips the older holder.

The helper-level semantics (change signature, promotion, pending scaffolding,
guidance schema) are covered in ``tests/test_meta_block.py``; this file proves
the wiring.
"""
from __future__ import annotations

import threading
from pathlib import Path

from lingtai_kernel.base_agent.turn import _process_response
from lingtai_kernel.llm.base import LLMResponse, ToolCall
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.meta_block import stamp_meta


class _Guard:
    """Minimal guard exposing total_calls for the runtime counter."""

    def __init__(self, total_calls: int = 0) -> None:
        self.total_calls = total_calls

    def check_limit(self, count: int) -> str | None:
        return None

    def check_invalid_tool_limit(self) -> str | None:
        return None

    def record_calls(self, count: int) -> None:
        self.total_calls += count

    def clear_progress_notice(self) -> None:
        pass


class _Executor:
    """Returns one pre-stamped dict result per batch (mimics ToolExecutor)."""

    def __init__(self, contents: list[dict], guard: _Guard) -> None:
        self.guard = guard
        self._contents = list(contents)
        self._i = 0

    def execute(self, tool_calls, **kwargs):
        calls = list(tool_calls)
        content = self._contents[self._i]
        self._i += 1
        block = ToolResultBlock(id=calls[0].id or "call", name=calls[0].name, content=content)
        return [block], False, ""


class _Chat:
    def __init__(self) -> None:
        self.interface = ChatInterface()
        self.interface.add_system("system")
        self.interface.add_user_message("run tool")
        self.interface.add_assistant_message(
            [TextBlock("calling"), ToolCallBlock(id="call_1", name="bash", args={"c": "x"})]
        )
        self.committed: list[list] = []

    def commit_tool_results(self, results) -> None:
        self.committed.append(list(results))
        self.interface.add_tool_results(results)


class _Session:
    """Single-shot session: commits the batch and returns a terminal response."""

    def __init__(self, chat: _Chat) -> None:
        self.chat = chat
        self.sent = []

    def send(self, content):
        self.sent.append(content)
        self.chat.commit_tool_results(content)
        return LLMResponse(text="done", tool_calls=[])


class _Agent:
    def __init__(self, tmp_path: Path, contents: list[dict]) -> None:
        self._chat = _Chat()
        self.agent_name = "rt-agent"
        self._notification_live_holder = None
        self._notification_fp = ()
        self._notification_payload_signature = None
        self._runtime_live_holder = None
        self._intrinsics = {}
        self._working_dir = tmp_path
        self._cancel_event = threading.Event()
        self._on_tool_result_hook = None
        self._intermediate_text_streamed = True
        self._sent_tracker = object()
        self.guard = _Guard(total_calls=2)
        self._executor = _Executor(contents, self.guard)
        self._session = _Session(self._chat)
        self.saved = 0
        self.logs: list[tuple[str, dict]] = []

    def _save_chat_history(self, *, ledger_source: str | None = None) -> None:
        self.saved += 1

    def _log(self, event: str, **kwargs) -> None:
        self.logs.append((event, kwargs))


def _stamped(meta_value: str, *, molt: str | None = None) -> dict:
    """A dict tool-result content already carrying a _runtime_pending snapshot.

    ``molt`` optionally injects a material ``context.molt`` reminder so the
    caller can drive a genuine change in the agent_meta signature between
    batches (``current_time`` and ``echo`` are volatile / non-agent_meta and do
    NOT change the material signature on their own).
    """
    content = {"status": "ok", "echo": meta_value}
    meta: dict = {"current_time": meta_value}
    if molt is not None:
        meta["context"] = {"molt": molt}
    stamp_meta(content, meta, 5)
    return content


def test_runtime_block_lands_on_latest_result_at_turn_boundary(tmp_path):
    agent = _Agent(tmp_path, [_stamped("T1")])

    response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})],
    )
    _process_response(agent, response, ledger_source="test")

    holder = agent._runtime_live_holder
    assert holder is not None, "attach_active_runtime was not invoked at the boundary"
    assert "current_time" not in holder["_meta"]["agent_meta"]
    assert holder["echo"] == "T1"
    # The turn records the batch's calls on the guard (2 seeded + 1 this batch),
    # and the boundary stamps the live total under _meta.agent_meta.
    assert holder["_meta"]["agent_meta"]["active_turn_tool_calls"] == 3
    # The tail guidance is now a lightweight ref/hook pointing at the resident
    # meta_guidance system-prompt section, not the full ordered sections (those
    # moved into the system prompt so they stop riding on every tail _meta).
    guidance = holder["_meta"]["guidance"]
    assert "sections" not in guidance
    assert guidance["ref"] == "meta_guidance"
    # transient scaffolding is gone; no top-level counter repetition.
    assert "_runtime_pending" not in holder
    assert "active_turn_tool_calls" not in holder


def _second_batch(agent, call_id: str = "call_2"):
    """Stage and process a second assistant turn + tool call on ``agent``."""
    agent._chat.interface.add_assistant_message(
        [TextBlock("again"), ToolCallBlock(id=call_id, name="bash", args={"c": "y"})]
    )
    agent._session = _Session(agent._chat)
    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id=call_id, name="bash", args={"c": "y"})]),
        ledger_source="test",
    )


def test_unchanged_snapshot_not_restamped_on_newer_result(tmp_path):
    # Two batches whose MATERIAL agent snapshot is identical (only the volatile
    # current_time / non-agent_meta echo differ). agent_meta must stay on the
    # first holder as a historical update point; the newer result must NOT get
    # re-stamped merely because it is the latest.
    agent = _Agent(tmp_path, [_stamped("T1"), _stamped("T2")])

    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})]),
        ledger_source="test",
    )
    first_holder = agent._runtime_live_holder
    assert "agent_meta" in first_holder["_meta"]
    assert first_holder["echo"] == "T1"

    _second_batch(agent)

    # Sparse: the live holder did not move; the new result carries no agent_meta.
    assert agent._runtime_live_holder is first_holder
    assert "agent_meta" in first_holder["_meta"]
    second_result = agent._chat.committed[-1][0].content
    assert "agent_meta" not in second_result.get("_meta", {})


def test_material_change_reattaches_and_strips_prior(tmp_path):
    # First batch has no molt; second batch surfaces a sustained-pressure molt
    # reminder — a material change. agent_meta re-attaches to the newer result
    # and the older holder sheds its agent_meta/guidance.
    agent = _Agent(tmp_path, [_stamped("T1"), _stamped("T2", molt="sustained pressure")])

    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})]),
        ledger_source="test",
    )
    first_holder = agent._runtime_live_holder
    assert "agent_meta" in first_holder["_meta"]

    _second_batch(agent)

    second_holder = agent._runtime_live_holder
    assert second_holder is not first_holder
    assert second_holder["echo"] == "T2"
    assert second_holder["_meta"]["agent_meta"]["context"] == {"molt": "sustained pressure"}
    # The previous holder sheds its agent_meta/guidance now that a newer live
    # holder carries the changed snapshot.
    assert "_meta" not in first_holder or "agent_meta" not in first_holder["_meta"]


# ---------------------------------------------------------------------------
# Sparse / update-driven notifications at the turn boundary.  Mirrors the
# agent_meta sparse invariant above but for ``_meta.notifications``: an
# unchanged notification payload is NOT chased onto every newest ordinary tool
# result; a material change re-attaches it and strips the prior holder.
# ---------------------------------------------------------------------------


def _write_email_notif(tmp_path: Path, *, digest: str = "Email preview line") -> None:
    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "email.json").write_text(
        '{"header": "1 unread", "icon": "M", "priority": "normal", '
        '"data": {"digest": "' + digest + '"}}'
    )


def test_notification_unchanged_not_restamped_on_newer_result_at_boundary(tmp_path):
    _write_email_notif(tmp_path)
    agent = _Agent(tmp_path, [_stamped("T1"), _stamped("T2")])

    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})]),
        ledger_source="test",
    )
    first_holder = agent._notification_live_holder
    assert first_holder is not None
    assert "notifications" in first_holder["_meta"]

    _second_batch(agent)

    # Sparse: the notification holder did not move; the newer ordinary result
    # carries no notification payload, and the prior holder keeps it.
    assert agent._notification_live_holder is first_holder
    assert "notifications" in first_holder["_meta"]
    second_result = agent._chat.committed[-1][0].content
    assert "notifications" not in second_result.get("_meta", {})


def test_notification_material_change_reattaches_at_boundary(tmp_path):
    _write_email_notif(tmp_path)
    agent = _Agent(tmp_path, [_stamped("T1"), _stamped("T2")])

    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})]),
        ledger_source="test",
    )
    first_holder = agent._notification_live_holder
    assert "notifications" in first_holder["_meta"]

    # Materially change the notification payload before the second batch.
    _write_email_notif(tmp_path, digest="Three new emails")

    _second_batch(agent)

    new_holder = agent._notification_live_holder
    assert new_holder is not first_holder
    assert new_holder["_meta"]["notifications"]["email"]["data"] == {
        "digest": "Three new emails"
    }
    # The previous holder sheds its notification payload.
    assert "_meta" not in first_holder or "notifications" not in first_holder["_meta"]
