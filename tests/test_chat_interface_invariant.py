"""Tests for ChatInterface tool-pairing invariant.

DeepSeek V4 and strict OpenAI reject chat-completions requests where an
assistant message with tool_calls is not immediately followed by matching
tool messages. These tests verify the canonical ChatInterface enforces
that invariant at construction time.
"""
from __future__ import annotations

import pytest

from lingtai_kernel.llm.interface import (
    ChatInterface,
    PendingToolCallsError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _iface_with_pending_tool_calls() -> ChatInterface:
    """Build an interface whose tail is assistant[tool_calls] with no results."""
    iface = ChatInterface()
    iface.add_system("system prompt")
    iface.add_user_message("hi")
    iface.add_assistant_message(
        [
            TextBlock(text="checking"),
            ToolCallBlock(id="call_A", name="noop", args={}),
        ],
    )
    return iface


class TestHasPendingToolCalls:
    def test_false_on_empty_interface(self):
        iface = ChatInterface()
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_system(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_user(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_plain_assistant(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])
        assert iface.has_pending_tool_calls() is False

    def test_true_when_tail_is_assistant_with_tool_calls(self):
        iface = _iface_with_pending_tool_calls()
        assert iface.has_pending_tool_calls() is True

    def test_false_after_tool_results_appended(self):
        iface = _iface_with_pending_tool_calls()
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="done")])
        assert iface.has_pending_tool_calls() is False


class TestClosePendingToolCalls:
    def test_noop_on_empty_interface(self):
        iface = ChatInterface()
        iface.close_pending_tool_calls("test")
        assert len(iface.entries) == 0

    def test_noop_when_tail_clean(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        before = len(iface.entries)
        iface.close_pending_tool_calls("test")
        assert len(iface.entries) == before

    def test_synthesizes_results_for_pending(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [
                TextBlock(text="running"),
                ToolCallBlock(id="call_A", name="tool1", args={}),
                ToolCallBlock(id="call_B", name="tool2", args={"k": 1}),
            ],
        )
        assert iface.has_pending_tool_calls() is True
        iface.close_pending_tool_calls("network timeout")
        # Now tail should be a user entry with two ToolResultBlocks.
        assert iface.has_pending_tool_calls() is False
        tail = iface.entries[-1]
        assert tail.role == "user"
        assert len(tail.content) == 2
        result_A, result_B = tail.content
        assert isinstance(result_A, ToolResultBlock)
        assert result_A.id == "call_A"
        assert result_A.name == "tool1"
        # Synthesized placeholders are tagged so add_tool_results can replace
        # them when the real result for the same id later arrives.
        assert result_A.synthesized is True
        # Heal content is written for the agent to read on the next turn:
        # it must signal that the call did not complete, that the side effect
        # MAY have happened, and the kernel-supplied reason must be carried
        # through so the agent can decide whether to verify or retry.
        assert "did not complete" in result_A.content
        assert "tool1" in result_A.content
        assert "network timeout" in result_A.content
        assert isinstance(result_B, ToolResultBlock)
        assert result_B.id == "call_B"
        assert result_B.name == "tool2"
        assert result_B.synthesized is True
        assert "tool2" in result_B.content

    def test_synthesizes_results_for_pending_with_tool_completed(self):
        """When tool_completed=True, the message honestly says the tool
        already executed and the LLM continuation failed, instead of
        implying the tool itself did not complete."""
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [
                ToolCallBlock(id="call_A", name="email", args={}),
            ],
        )
        iface.close_pending_tool_calls(
            "provider overload", tool_completed=True,
        )
        assert iface.has_pending_tool_calls() is False
        tail = iface.entries[-1]
        result = tail.content[0]
        assert isinstance(result, ToolResultBlock)
        assert result.synthesized is True
        # The message should say the tool completed, not that it didn't.
        assert "completed" in result.content
        assert "continuation failed" in result.content
        assert "email" in result.content
        assert "provider overload" in result.content
        # It must NOT say "did not complete" when the tool completed.
        assert "did not complete" not in result.content

    def test_idempotent(self):
        iface = _iface_with_pending_tool_calls()
        iface.close_pending_tool_calls("r1")
        entries_after_first = len(iface.entries)
        iface.close_pending_tool_calls("r2")
        # Second call is a no-op because tail is now clean.
        assert len(iface.entries) == entries_after_first


class TestAddUserMessageGuard:
    def test_raises_when_tail_has_pending_tool_calls(self):
        iface = _iface_with_pending_tool_calls()
        with pytest.raises(PendingToolCallsError):
            iface.add_user_message("new message")

    def test_succeeds_after_close(self):
        iface = _iface_with_pending_tool_calls()
        iface.close_pending_tool_calls("test")
        # Should not raise.
        iface.add_user_message("recovery message")
        tail = iface.entries[-1]
        assert tail.role == "user"
        assert len(tail.content) == 1
        assert isinstance(tail.content[0], TextBlock)
        assert tail.content[0].text == "recovery message"

    def test_succeeds_after_tool_results(self):
        iface = _iface_with_pending_tool_calls()
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="done")])
        # Should not raise.
        iface.add_user_message("next")

    def test_clean_interface_not_affected(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("first")  # should not raise


class TestAddUserBlocksGuard:
    def test_raises_for_text_blocks_when_pending(self):
        iface = _iface_with_pending_tool_calls()
        with pytest.raises(PendingToolCallsError):
            iface.add_user_blocks([TextBlock(text="hi")])

    def test_tool_results_allowed_when_pending(self):
        """ToolResultBlocks ARE the legitimate closing operation."""
        iface = _iface_with_pending_tool_calls()
        # Should not raise.
        iface.add_user_blocks([ToolResultBlock(id="call_A", name="noop", content="ok")])
        assert iface.has_pending_tool_calls() is False

    def test_mixed_blocks_rejected_when_pending(self):
        iface = _iface_with_pending_tool_calls()
        with pytest.raises(PendingToolCallsError):
            iface.add_user_blocks([
                ToolResultBlock(id="call_A", name="noop", content="ok"),
                TextBlock(text="extra"),
            ])


class TestRestoreDanglingToolCalls:
    def test_rehydrate_closes_pending_tool_calls(self):
        """A chat_history.jsonl persisted mid-tool-loop (process crashed
        between tool_call emission and tool_result arrival) should
        rehydrate with synthetic tool_results so the first send after
        restore is well-formed."""
        persisted = [
            {"id": 0, "role": "system", "system": "prompt", "timestamp": 0.0},
            {"id": 1, "role": "user",
             "content": [{"type": "text", "text": "go"}],
             "timestamp": 1.0},
            {"id": 2, "role": "assistant",
             "content": [
                 {"type": "text", "text": "checking"},
                 {"type": "tool_call", "id": "call_X", "name": "tool1", "args": {}},
             ],
             "timestamp": 2.0},
        ]
        iface = ChatInterface.from_dict(persisted)
        assert iface.has_pending_tool_calls() is True

        # The restore path will call these two methods in sequence.
        iface.enforce_tool_pairing()
        if iface.has_pending_tool_calls():
            iface.close_pending_tool_calls(
                reason="restored from disk — prior session ended mid-tool-loop"
            )

        # After recovery, no pending tool_calls and a synthetic tool_result entry.
        assert iface.has_pending_tool_calls() is False
        tail = iface.entries[-1]
        assert tail.role == "user"
        assert len(tail.content) == 1
        assert isinstance(tail.content[0], ToolResultBlock)
        assert tail.content[0].id == "call_X"
        assert "restored from disk" in tail.content[0].content

    def test_bash_heal_notice_includes_recovery_metadata(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("run")
        iface.add_assistant_message([
            ToolCallBlock(
                id="call_bash_1",
                name="bash",
                args={
                    "command": "cd /repo && git status --short",
                    "working_dir": "/agent",
                    "timeout": 30,
                },
            )
        ])

        iface.close_pending_tool_calls("heal:idle_inject_blocked")

        content = iface.entries[-1].content[0].content
        assert "tool call did not complete" in content
        assert "Reason recorded by the kernel: heal:idle_inject_blocked" in content
        assert "Recovery metadata:" in content
        assert "Tool call id: call_bash_1" in content
        assert "bash action: run" in content
        assert "bash working_dir: /agent" in content
        assert "bash timeout: 30" in content
        assert "bash command preview: cd /repo && git status --short" in content


# ---------------------------------------------------------------------------
# Real result arriving after a heal — replace-in-place behavior
# ---------------------------------------------------------------------------

class TestRealResultReplacesSynthesized:
    """When a tool call was healed (synthesized placeholder appended) and
    the real tool_result later arrives via add_tool_results, the placeholder
    is overwritten in place. This keeps a single tool_result per id in the
    canonical interface — strict providers reject duplicate tool_call_ids
    on the wire.

    Note: in current production paths the real result never arrives after
    a heal (heal fires only when the prior send was interrupted, so the
    execution that would have produced the real result died). This is
    defensive coverage for future async-tool-result patterns.
    """

    def test_real_result_replaces_synthesized_in_place(self):
        iface = ChatInterface()
        iface.add_system("s")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [ToolCallBlock(id="call_X", name="tool1", args={})],
        )
        iface.close_pending_tool_calls("network timeout")
        # Tail is now user[ToolResultBlock(synthesized=True, id=call_X)]
        synth_entry = iface.entries[-1]
        assert synth_entry.content[0].synthesized is True
        n_entries_before = len(iface.entries)

        # Real result arrives. add_tool_results should overwrite in place,
        # not append a new entry (which would create a duplicate tool_call_id
        # on the wire).
        real = ToolResultBlock(id="call_X", name="tool1", content="real output")
        iface.add_tool_results([real])

        # No new entry appended — the placeholder slot was overwritten.
        assert len(iface.entries) == n_entries_before
        # The block at the placeholder's slot is now the real one.
        replaced = synth_entry.content[0]
        assert replaced.content == "real output"
        assert replaced.synthesized is False  # real result is not synthesized

    def test_real_result_appends_when_no_synthesized_match(self):
        """Normal (non-heal) flow — add_tool_results appends a fresh entry."""
        iface = ChatInterface()
        iface.add_system("s")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [ToolCallBlock(id="call_Y", name="tool2", args={})],
        )
        n_entries_before = len(iface.entries)

        real = ToolResultBlock(id="call_Y", name="tool2", content="ok")
        iface.add_tool_results([real])

        assert len(iface.entries) == n_entries_before + 1
        tail = iface.entries[-1]
        assert tail.role == "user"
        assert tail.content[0].content == "ok"

    def test_partial_overlap_replaces_some_appends_rest(self):
        """Mix of incoming results: some replace placeholders, the rest
        are appended together as one new entry."""
        iface = ChatInterface()
        iface.add_system("s")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [
                ToolCallBlock(id="call_A", name="tool1", args={}),
                ToolCallBlock(id="call_B", name="tool2", args={}),
            ],
        )
        iface.close_pending_tool_calls("timeout")
        synth_entry = iface.entries[-1]
        n_entries_before = len(iface.entries)

        real_A = ToolResultBlock(id="call_A", name="tool1", content="A real")
        real_C = ToolResultBlock(id="call_C", name="tool3", content="C real")  # new id
        iface.add_tool_results([real_A, real_C])

        # call_A overwrote the placeholder; call_C had no match, so a new
        # entry was appended carrying just real_C.
        assert len(iface.entries) == n_entries_before + 1
        assert synth_entry.content[0].content == "A real"
        assert synth_entry.content[0].synthesized is False
        # Placeholder for call_B is untouched (still synthesized).
        assert synth_entry.content[1].id == "call_B"
        assert synth_entry.content[1].synthesized is True
        # New entry has only the leftover (call_C).
        new_entry = iface.entries[-1]
        assert len(new_entry.content) == 1
        assert new_entry.content[0].id == "call_C"


class TestAddSystemDefersDuringPendingToolCall:
    """Regression: psyche / codex / library mutate the system prompt as a
    side effect of running. When the model emits assistant[tool_calls] and
    such a tool runs mid-loop, the synchronous add_system call used to insert
    a system entry between the assistant turn and the tool_results, breaking
    the wire-level invariant that tool messages must immediately follow
    assistant[tool_calls]. The fix: defer the new system entry until the
    pending tool_call is resolved, then flush at enforce_tool_pairing time.
    """

    def test_add_system_during_pending_tool_call_does_not_append(self):
        iface = _iface_with_pending_tool_calls()
        n_before = len(iface.entries)

        iface.add_system("system prompt v2")

        # No new system entry while the tool_call is unanswered.
        assert len(iface.entries) == n_before
        # current_system_prompt still tracks the latest text.
        assert iface.current_system_prompt == "system prompt v2"

    def test_pending_system_flushes_after_tool_results_via_enforce(self):
        iface = _iface_with_pending_tool_calls()
        iface.add_system("system prompt v2")
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="ok")])

        # Tail is now user[tool_results]; deferred system not yet appended.
        assert iface.entries[-1].role == "user"

        # enforce_tool_pairing flushes the deferred system.
        iface.enforce_tool_pairing()

        # Layout: ..., assistant[tc], user[tool_results], system(v2)
        assert iface.entries[-1].role == "system"
        assert iface.entries[-1].content[0].text == "system prompt v2"
        assert iface.entries[-2].role == "user"
        assert iface.entries[-3].role == "assistant"

    def test_repeat_add_system_overwrites_pending_last_write_wins(self):
        iface = _iface_with_pending_tool_calls()
        iface.add_system("v2")
        iface.add_system("v3")
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="ok")])
        iface.enforce_tool_pairing()

        assert iface.entries[-1].role == "system"
        assert iface.entries[-1].content[0].text == "v3"
        # Only ONE system entry was appended (v3), not two.
        system_count = sum(1 for e in iface.entries if e.role == "system")
        # _iface_with_pending_tool_calls already added one system at start.
        assert system_count == 2

    def test_add_system_no_change_clears_pending(self):
        """If add_system is called with text that matches the current prompt,
        any deferred entry is cleared — there is nothing to flush."""
        iface = _iface_with_pending_tool_calls()
        iface.add_system("v2")
        # Now pretend something reverts the prompt back to the original.
        iface.add_system("system prompt")  # matches the current prompt set by helper

        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="ok")])
        iface.enforce_tool_pairing()

        # Tail is the user[tool_results] — no system flushed.
        assert iface.entries[-1].role == "user"

    def test_add_system_with_no_pending_calls_appends_immediately(self):
        """Sanity: the fix only changes behavior when a tool_call is pending."""
        iface = ChatInterface()
        iface.add_system("v1")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="ok")])
        n_before = len(iface.entries)

        iface.add_system("v2")

        assert len(iface.entries) == n_before + 1
        assert iface.entries[-1].role == "system"
        assert iface.entries[-1].content[0].text == "v2"

    def test_wire_layout_has_no_system_between_assistant_and_tool(self):
        """End-to-end: simulate the exact production failure shape and confirm
        the OpenAI wire serialization no longer interleaves system between
        assistant[tool_calls] and tool[tool_result]."""
        from lingtai.llm.interface_converters import to_openai

        iface = _iface_with_pending_tool_calls()
        iface.add_system("system prompt v2")  # psyche-style mid-tool mutation
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="ok")])
        iface.enforce_tool_pairing()

        msgs = to_openai(iface)
        # Find the assistant turn that carries tool_calls.
        idx = next(i for i, m in enumerate(msgs) if m.get("tool_calls"))
        # The very next message must be role=tool with the matching id.
        nxt = msgs[idx + 1]
        assert nxt["role"] == "tool"
        assert nxt["tool_call_id"] == "call_A"
