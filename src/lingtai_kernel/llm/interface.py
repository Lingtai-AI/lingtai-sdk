"""Canonical LLM interaction interface.

Provides a provider-agnostic representation of the full program-LLM
interaction.  This is the single source of truth for conversation history.
Adapters rebuild provider-specific message formats from this on each API call.

Each ChatInterface instance is owned by one agent thread.  Not thread-safe.
Do not share across threads.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Union


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


class PendingToolCallsError(Exception):
    """Raised when a user entry would be appended while tool_calls are unanswered.

    Callers should close the pending tool_calls first — either by appending
    the real ToolResultBlocks via ``add_tool_results(...)``, or by calling
    ``close_pending_tool_calls(reason)`` to synthesize placeholder results
    (used by recovery paths like AED and session restore).
    """


@dataclass
class TextBlock:
    text: str

    def to_dict(self) -> dict:
        return {"type": "text", "text": self.text}


@dataclass
class ToolCallBlock:
    id: str
    name: str
    args: dict

    def to_dict(self) -> dict:
        return {"type": "tool_call", "id": self.id, "name": self.name, "args": self.args}


@dataclass
class ToolResultBlock:
    id: str
    name: str
    content: Any  # str or dict
    # True iff this block was created by close_pending_tool_calls (heal path),
    # not by a real tool execution. add_tool_results will overwrite a
    # synthesized block in place when the real result for the same id arrives.
    synthesized: bool = False

    def to_dict(self) -> dict:
        return {"type": "tool_result", "id": self.id, "name": self.name, "content": self.content}


def _tool_call_context(tool_call: "ToolCallBlock | None") -> str:
    """Short, safe context for a synthesized tool-result recovery notice."""
    if tool_call is None:
        return ""
    lines = [f"Tool call id: {tool_call.id}"]
    args = tool_call.args if isinstance(tool_call.args, dict) else {}
    if tool_call.name == "bash":
        action = args.get("action", "run")
        lines.append(f"bash action: {action}")
        if "working_dir" in args:
            lines.append(f"bash working_dir: {args.get('working_dir')}")
        if "timeout" in args:
            lines.append(f"bash timeout: {args.get('timeout')}")
        if "async" in args:
            lines.append(f"bash async: {args.get('async')}")
        if "job_id" in args:
            lines.append(f"bash job_id: {args.get('job_id')}")
        command = args.get("command")
        if isinstance(command, str) and command:
            preview = command.replace("\n", "\\n")
            if len(preview) > 240:
                preview = preview[:240] + "..."
            lines.append(f"bash command preview: {preview}")
    elif args:
        keys = ", ".join(sorted(str(k) for k in args.keys())[:12])
        if keys:
            lines.append(f"tool args present: {keys}")
    return "\n".join(lines)


def _synthesized_abort_message(
    tool_name: str,
    reason: str,
    *,
    tool_completed: bool = False,
    tool_call: "ToolCallBlock | None" = None,
) -> str:
    """Content for a heal-path placeholder ToolResultBlock.

    Written FOR THE AGENT to read on the next turn — not for log-readers.
    The agent needs three things from this message: (1) clear signal that
    the tool did NOT complete normally, (2) honest acknowledgement that
    the side effect MAY have happened (the failure could have been after
    the side effect committed but before the result returned), (3) actionable
    guidance to verify state before retrying. Tone matches the kernel's
    other system-injected notices the agent already knows how to read.

    When ``tool_completed`` is True, the caller knows the tool already
    executed and the real failure was the LLM continuation *after* the
    tool result.  In that case the message says so honestly instead of
    implying the tool itself failed.
    """
    context = _tool_call_context(tool_call)
    context_block = f"\n\nRecovery metadata:\n{context}" if context else ""
    if tool_completed:
        return (
            f"[kernel notice — tool call completed but LLM continuation failed]\n"
            f"\n"
            f"A prior call to `{tool_name}` already executed, but the LLM "
            f"continuation after the tool result failed. The adapter reverted "
            f"the committed result from the conversation so the interface "
            f"could recover.\n"
            f"\n"
            f"**Do not blindly retry the tool.** Its side effect (file write, "
            f"email send, mail reply, state change, daemon spawn, etc.) very "
            f"likely took effect. Check the actual state before deciding — "
            f"read the file, check the inbox, list the daemon, etc. "
            f"If the side effect is confirmed, continue from where you "
            f"left off without re-executing the tool.\n"
            f"\n"
            f"Reason recorded by the kernel: {reason}"
            f"{context_block}"
        )
    return (
        f"[kernel notice — tool call did not complete]\n"
        f"\n"
        f"A prior call to `{tool_name}` was left without a result. The tool "
        f"execution may have timed out, errored, or been interrupted before "
        f"its result could be committed to the conversation.\n"
        f"\n"
        f"**Important:** the side effect of `{tool_name}` MAY OR MAY NOT have "
        f"happened. The kernel cannot tell. Do not assume the action took "
        f"effect. If the action mattered (file write, email send, mail reply, "
        f"state change, daemon spawn, etc.), verify the actual state before "
        f"retrying — read the file, check the inbox, list the daemon, etc. "
        f"Only retry the call if you've confirmed it didn't take effect.\n"
        f"\n"
        f"Reason recorded by the kernel: {reason}"
        f"{context_block}"
    )


@dataclass
class ThinkingBlock:
    text: str
    provider_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"type": "thinking", "text": self.text}
        if self.provider_data:
            d["provider_data"] = self.provider_data
        return d


ContentBlock = Union[TextBlock, ToolCallBlock, ToolResultBlock, ThinkingBlock]


def content_block_from_dict(d: dict) -> ContentBlock:
    """Deserialize a content block from its dict representation."""
    btype = d["type"]
    if btype == "text":
        return TextBlock(text=d["text"])
    elif btype == "tool_call":
        return ToolCallBlock(id=d["id"], name=d["name"], args=d["args"])
    elif btype == "tool_result":
        return ToolResultBlock(id=d["id"], name=d["name"], content=d["content"])
    elif btype == "thinking":
        return ThinkingBlock(text=d["text"], provider_data=d.get("provider_data", {}))
    else:
        raise ValueError(f"Unknown content block type: {btype}")


# ---------------------------------------------------------------------------
# InterfaceEntry
# ---------------------------------------------------------------------------


@dataclass
class InterfaceEntry:
    id: int
    role: str  # "system" | "user" | "assistant"
    content: list[ContentBlock]
    timestamp: float
    provider_data: dict = field(default_factory=dict)
    model: str | None = None       # which model produced this (assistant only)
    provider: str | None = None    # which provider (assistant only)
    usage: dict = field(default_factory=dict)  # per-message token usage
    _tools: list[dict] | None = field(default=None, repr=False)  # tools snapshot (system entries)

    def to_dict(self) -> dict:
        if self.role == "system":
            d: dict = {
                "id": self.id,
                "role": self.role,
                "system": self.content[0].text if self.content else "",
                "timestamp": self.timestamp,
            }
            if self._tools is not None:
                d["tools"] = self._tools
            return d
        d = {
            "id": self.id,
            "role": self.role,
            "content": [b.to_dict() for b in self.content],
            "timestamp": self.timestamp,
        }
        if self.provider_data:
            d["provider_data"] = self.provider_data
        if self.model is not None:
            d["model"] = self.model
        if self.provider is not None:
            d["provider"] = self.provider
        if self.usage:
            d["usage"] = self.usage
        return d

    @staticmethod
    def from_dict(d: dict) -> InterfaceEntry:
        if d["role"] == "system" and "system" in d:
            entry = InterfaceEntry(
                id=d["id"],
                role="system",
                content=[TextBlock(text=d["system"])],
                timestamp=d["timestamp"],
            )
            entry._tools = d.get("tools")
            return entry
        return InterfaceEntry(
            id=d["id"],
            role=d["role"],
            content=[content_block_from_dict(b) for b in d["content"]],
            timestamp=d["timestamp"],
            provider_data=d.get("provider_data", {}),
            model=d.get("model"),
            provider=d.get("provider"),
            usage=d.get("usage", {}),
        )


# ---------------------------------------------------------------------------
# ChatInterface
# ---------------------------------------------------------------------------


class ChatInterface:
    """Append-only log of canonical LLM interaction entries.

    Single source of truth for conversation history.  Adapters rebuild
    provider-specific formats from this on each API call.

    Not thread-safe.  Each instance is owned by one agent thread.
    """

    def __init__(self) -> None:
        self._entries: list[InterfaceEntry] = []
        self._next_id: int = 0
        self._current_system_text: str | None = None
        self._current_tools: list[dict] | None = None
        # Deferred system update — stashed by add_system when the tail has a
        # pending tool_call, flushed by enforce_tool_pairing at the start of
        # the next send. Prevents an interleaved system entry from splitting
        # an assistant[tool_calls] from its tool_results on the wire.
        self._pending_system: tuple[str, list[dict] | None] | None = None

    @property
    def entries(self) -> list[InterfaceEntry]:
        return self._entries

    @property
    def current_system_prompt(self) -> str | None:
        return self._current_system_text

    @property
    def current_tools(self) -> list[dict] | None:
        return self._current_tools

    def _append(self, role: str, content: list[ContentBlock], provider_data: dict | None = None) -> InterfaceEntry:
        entry = InterfaceEntry(
            id=self._next_id,
            role=role,
            content=content,
            timestamp=time.time(),
            provider_data=provider_data or {},
        )
        self._entries.append(entry)
        self._next_id += 1
        return entry

    # -- Sanitization ---------------------------------------------------------

    def enforce_tool_pairing(self) -> None:
        """Ensure every ToolCallBlock has a matching ToolResultBlock and vice versa.

        Walks all entries and:
        - Strips ToolCallBlocks from EARLIER assistant entries that have no
          matching ToolResultBlock anywhere in subsequent user entries.
        - Strips ToolResultBlocks from user entries that have no matching
          ToolCallBlock in any preceding assistant entry.
        - If stripping leaves an entry with no content blocks, inserts a
          placeholder TextBlock.

        **Leaves tail-dangling assistant[tool_calls] intact** — those represent
        a turn that was emitted but whose tool_results never arrived (typically
        because the process crashed or the send raised mid-loop). The canonical
        repair for that case is ``close_pending_tool_calls(reason)``, which
        synthesizes placeholder tool_results preserving the assistant turn and
        the error context. Repairing it here would destroy that signal.

        Mutates entries in place.  Idempotent.

        Also flushes any system entry that was deferred by ``add_system`` while
        a tool_call was pending — by the time we're about to serialize, the
        tool_results have landed and the deferred system can be appended
        safely after them.
        """
        self._flush_pending_system()

        if not self._entries:
            return

        # Collect all tool call IDs and all answered IDs
        all_call_ids: set[str] = set()
        answered_ids: set[str] = set()
        for entry in self._entries:
            for block in entry.content:
                if isinstance(block, ToolCallBlock):
                    all_call_ids.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    answered_ids.add(block.id)

        # Nothing to fix if sets match
        if all_call_ids == answered_ids:
            return

        # Identify the tail assistant-with-tool-calls (if any) so we can skip
        # stripping it — close_pending_tool_calls owns that repair.
        tail_idx = len(self._entries) - 1
        tail_is_pending_assistant = (
            self._entries[tail_idx].role == "assistant"
            and any(isinstance(b, ToolCallBlock) for b in self._entries[tail_idx].content)
        )

        for i, entry in enumerate(self._entries):
            if entry.role == "assistant":
                if i == tail_idx and tail_is_pending_assistant:
                    continue  # leave the tail for close_pending_tool_calls
                stripped_names: list[str] = []
                new_content: list[ContentBlock] = []
                for block in entry.content:
                    if isinstance(block, ToolCallBlock) and block.id not in answered_ids:
                        stripped_names.append(block.name)
                        continue
                    new_content.append(block)
                if not new_content:
                    new_content.append(TextBlock(
                        text=f"[Tool calls {', '.join(stripped_names)} were cancelled before completion.]"
                    ))
                entry.content = new_content

            elif entry.role == "user":
                new_content_u: list[ContentBlock] = []
                orphaned_names: list[str] = []
                for block in entry.content:
                    if isinstance(block, ToolResultBlock) and block.id not in all_call_ids:
                        orphaned_names.append(block.name)
                        continue
                    new_content_u.append(block)
                if orphaned_names:
                    new_content_u.append(TextBlock(
                        text=f"[Tool results ignored (no matching tool call): {', '.join(orphaned_names)}]"
                    ))
                if new_content_u:
                    entry.content = new_content_u

    def has_pending_tool_calls(self) -> bool:
        """True iff the tail entry is an assistant with unanswered ToolCallBlocks.

        "Unanswered" is defined positionally: if the very next entry contains
        ToolResultBlocks, the calls are considered answered. The canonical
        pattern is ``assistant[tool_calls] -> user[tool_results]``; anything
        else leaves the calls pending.
        """
        if not self._entries:
            return False
        last = self._entries[-1]
        if last.role != "assistant":
            return False
        return any(isinstance(b, ToolCallBlock) for b in last.content)

    def close_pending_tool_calls(
        self, reason: str, *, tool_completed: bool = False,
    ) -> None:
        """Synthesize placeholder ToolResultBlocks for any unanswered tool_calls
        on the tail assistant entry. No-op if the tail has no pending calls.

        Used by recovery paths — AED retry, session restore from crashed
        process — to bring the interface into a valid state before appending
        a new user entry. Each placeholder carries the reason string so the
        model has context on the next turn.

        When ``tool_completed`` is True, the caller knows the tool already
        executed successfully and the real failure was the LLM continuation
        after the result was produced (e.g. provider timeout / overload
        during the post-tool-continuation round-trip).  The synthesized
        message will say so honestly, guiding the agent to verify side
        effects rather than blindly retrying.

        Idempotent: after one call, has_pending_tool_calls() returns False,
        and a second call no-ops.
        """
        if not self.has_pending_tool_calls():
            return
        last = self._entries[-1]
        pending = [b for b in last.content if isinstance(b, ToolCallBlock)]
        placeholders = [
            ToolResultBlock(
                id=b.id,
                name=b.name,
                content=_synthesized_abort_message(
                    b.name,
                    reason,
                    tool_completed=tool_completed,
                    tool_call=b,
                ),
                synthesized=True,
            )
            for b in pending
        ]
        self._append("user", placeholders)

    # -- Add methods ----------------------------------------------------------

    def add_system(self, text: str, tools: list[dict] | None = None) -> None:
        """Record a system prompt + tools.  Only adds entry if either changed.

        If the tail entry is an assistant turn with unanswered tool_calls
        (e.g. a tool that mutates the system prompt — psyche, codex, library —
        is running mid-loop), the new system entry is stashed in
        ``_pending_system`` instead of appended. Inserting it now would split
        the assistant[tool_calls] from its tool_results on the wire and break
        strict providers (DeepSeek, OpenAI). The stash is flushed by
        ``enforce_tool_pairing`` at the start of the next send, after the tool
        results have landed and the tail is no longer assistant[tool_calls].
        Last write wins — repeat calls overwrite the pending entry.
        """
        if text == self._current_system_text and tools == self._current_tools:
            self._pending_system = None
            return
        self._current_system_text = text
        self._current_tools = tools
        if self.has_pending_tool_calls():
            self._pending_system = (text, tools)
            return
        self._pending_system = None
        entry = self._append("system", [TextBlock(text=text)])
        entry._tools = tools

    def _flush_pending_system(self) -> None:
        """Append a deferred system entry if one is queued. Idempotent.

        Skips appending when the pending text/tools match the most recent
        system entry already on disk — this handles the case where a tool
        mutates the prompt and then reverts it back to the prior value while
        still mid-tool-loop. Without this, we'd append a redundant duplicate
        system entry.
        """
        if self._pending_system is None:
            return
        if self.has_pending_tool_calls():
            return
        text, tools = self._pending_system
        self._pending_system = None
        last_system = next(
            (e for e in reversed(self._entries) if e.role == "system"), None
        )
        if last_system is not None:
            last_text = last_system.content[0].text if last_system.content else None
            last_tools = getattr(last_system, "_tools", None)
            if last_text == text and last_tools == tools:
                return
        entry = self._append("system", [TextBlock(text=text)])
        entry._tools = tools

    def add_user_message(self, text: str) -> InterfaceEntry:
        if self.has_pending_tool_calls():
            raise PendingToolCallsError(
                "Cannot append user message while the tail assistant turn has "
                "unanswered tool_calls. Call close_pending_tool_calls(reason) "
                "or add_tool_results(...) first."
            )
        return self._append("user", [TextBlock(text=text)])

    def add_assistant_message(
        self,
        content: list[ContentBlock],
        provider_data: dict | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
        usage: dict | None = None,
    ) -> InterfaceEntry:
        entry = self._append("assistant", content, provider_data)
        entry.model = model
        entry.provider = provider
        entry.usage = usage or {}
        return entry

    def add_user_blocks(self, blocks: list[ContentBlock]) -> InterfaceEntry:
        """Record a user entry with pre-built content blocks (for converters).

        ToolResultBlocks are the legitimate closing op for pending tool_calls
        and are allowed through. Anything else (text, mixed) is rejected when
        the tail has unanswered tool_calls.
        """
        is_tool_result_only = bool(blocks) and all(
            isinstance(b, ToolResultBlock) for b in blocks
        )
        if self.has_pending_tool_calls() and not is_tool_result_only:
            raise PendingToolCallsError(
                "Cannot append non-tool-result user blocks while the tail "
                "assistant turn has unanswered tool_calls."
            )
        return self._append("user", blocks)

    def add_tool_results(self, results: list[ToolResultBlock]) -> InterfaceEntry:
        """Record tool results as a user-role entry.

        If a synthesized placeholder for the same tool_call_id already
        exists in the canonical history (created by close_pending_tool_calls
        when the original send was interrupted), replace that placeholder
        in place instead of appending a duplicate. The real result wins;
        the synthesized abort note is overwritten so the wire payload has
        a single tool message per id (strict providers reject duplicates).

        Returns the entry that received the new (or last) block: the existing
        entry if any replacements happened, or the newly appended entry
        otherwise. If all incoming results replaced existing placeholders,
        no new entry is appended.
        """
        results = list(results)
        leftover: list[ToolResultBlock] = []
        last_touched_entry: InterfaceEntry | None = None

        for incoming in results:
            replaced = False
            for entry in self._entries:
                if entry.role != "user":
                    continue
                for idx, block in enumerate(entry.content):
                    if (
                        isinstance(block, ToolResultBlock)
                        and block.id == incoming.id
                        and block.synthesized
                    ):
                        # Real result arrived after the heal — overwrite the
                        # placeholder in place. Synthesized flag clears so a
                        # second real arrival would NOT replace this one (it
                        # would fall through to the leftover path and append,
                        # which the wire-layer dedup catches as a true bug).
                        entry.content[idx] = incoming
                        last_touched_entry = entry
                        replaced = True
                        break
                if replaced:
                    break
            if not replaced:
                leftover.append(incoming)

        if leftover:
            return self._append("user", leftover)
        return last_touched_entry  # type: ignore[return-value]

    def remove_pair_by_call_id(self, call_id: str) -> bool:
        """Remove a strict ``(assistant{tool_call}, user{tool_result})`` pair.

        Scans for an assistant entry whose content is exactly one
        ``ToolCallBlock`` with the given ``call_id``, immediately followed
        by a user entry whose content is exactly one ``ToolResultBlock``
        with the same id. Removes both entries and returns True. Returns
        False if no such strict pair exists.

        The strict-shape requirement is intentional: this helper exists
        to maintain the single-slot invariant for synthesized appendix
        pairs (soul flow), which always have exactly that shape. Refusing
        to operate on mixed-content entries protects regular tool-call
        history from being corrupted by accidental id collisions.

        The assistant entry may contain one ``ToolCallBlock`` plus any
        number of ``TextBlock``s (the synthesized notification pair
        carries a leading text summary alongside the tool call).  The
        user entry must still be exactly one ``ToolResultBlock``.
        """
        for i in range(len(self._entries) - 1):
            a = self._entries[i]
            u = self._entries[i + 1]
            if a.role != "assistant" or u.role != "user":
                continue
            if len(u.content) != 1:
                continue
            # Assistant entry: exactly one ToolCallBlock, rest must be TextBlocks
            cblock = None
            for blk in a.content:
                if isinstance(blk, ToolCallBlock):
                    if cblock is not None:
                        cblock = None  # multiple tool calls — skip
                        break
                    cblock = blk
                elif not isinstance(blk, TextBlock):
                    cblock = None
                    break
            if cblock is None:
                continue
            rblock = u.content[0]
            if not isinstance(rblock, ToolResultBlock):
                continue
            if cblock.id != call_id or rblock.id != call_id:
                continue
            del self._entries[i:i + 2]
            return True
        return False

    def remove_pair_by_notif_id(self, notif_id: str) -> bool:
        """Remove a synthetic notification pair matched by ``args.notif_id``.

        Scans for an assistant entry whose content is exactly one
        ``ToolCallBlock`` with ``args.get("action") == "notification"`` and
        ``args.get("notif_id") == notif_id``, immediately followed by a user
        entry whose content is exactly one ``ToolResultBlock`` whose ``id``
        matches the call's ``id``. Removes both entries and returns True.
        Returns False if no such strict pair exists (idempotent).

        The strict-shape requirement matches ``remove_pair_by_call_id``'s
        rationale: this helper exists to dismiss kernel-synthesized
        notification pairs only. Refusing to operate on mixed-content
        entries or non-notification calls protects regular tool-call
        history from being corrupted.

        The assistant entry may contain one ``ToolCallBlock`` plus any
        number of ``TextBlock``s (the synthesized notification pair
        carries a leading text summary alongside the tool call).  The
        user entry must still be exactly one ``ToolResultBlock``.
        """
        for i in range(len(self._entries) - 1):
            a = self._entries[i]
            u = self._entries[i + 1]
            if a.role != "assistant" or u.role != "user":
                continue
            if len(u.content) != 1:
                continue
            # Assistant entry: exactly one ToolCallBlock, rest must be TextBlocks
            cblock = None
            for blk in a.content:
                if isinstance(blk, ToolCallBlock):
                    if cblock is not None:
                        cblock = None  # multiple tool calls — skip
                        break
                    cblock = blk
                elif not isinstance(blk, TextBlock):
                    cblock = None
                    break
            if cblock is None:
                continue
            rblock = u.content[0]
            if not isinstance(rblock, ToolResultBlock):
                continue
            if cblock.args.get("action") != "notification":
                continue
            if cblock.args.get("notif_id") != notif_id:
                continue
            if cblock.id != rblock.id:
                continue
            del self._entries[i:i + 2]
            return True
        return False

    # -- Query methods --------------------------------------------------------

    def conversation_entries(self) -> list[InterfaceEntry]:
        """Return entries excluding system prompt entries."""
        return [e for e in self._entries if e.role != "system"]

    def last_assistant_entry(self) -> InterfaceEntry | None:
        """Return the most recent assistant entry, or None."""
        for e in reversed(self._entries):
            if e.role == "assistant":
                return e
        return None

    # -- Usage helpers ---------------------------------------------------------

    def total_usage(self) -> dict:
        """Sum tokens and count API calls across all assistant messages."""
        totals = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "calls": 0}
        for entry in self._entries:
            if entry.role == "assistant" and entry.usage:
                totals["input_tokens"] += entry.usage.get("input_tokens", 0)
                totals["output_tokens"] += entry.usage.get("output_tokens", 0)
                totals["thinking_tokens"] += entry.usage.get("thinking_tokens", 0)
                totals["calls"] += 1
        return totals

    def usage_by_model(self) -> dict[str, dict]:
        """Breakdown of usage per model name."""
        by_model: dict[str, dict] = {}
        for entry in self._entries:
            if entry.role == "assistant" and entry.model and entry.usage:
                if entry.model not in by_model:
                    by_model[entry.model] = {
                        "input_tokens": 0, "output_tokens": 0,
                        "thinking_tokens": 0, "calls": 0,
                    }
                by_model[entry.model]["input_tokens"] += entry.usage.get("input_tokens", 0)
                by_model[entry.model]["output_tokens"] += entry.usage.get("output_tokens", 0)
                by_model[entry.model]["thinking_tokens"] += entry.usage.get("thinking_tokens", 0)
                by_model[entry.model]["calls"] += 1
        return by_model

    # -- Orphan cleanup -------------------------------------------------------

    def pop_orphan_tool_call(self) -> bool:
        """Remove orphaned trailing tool-call assistant entry and its tool results.

        **Prefer ``close_pending_tool_calls(reason)``** for recovery paths —
        it preserves the assistant turn and attaches error context as
        synthetic tool_results rather than destroying the turn. This method
        remains available for callers that genuinely need to discard the
        turn entirely.

        When an LLM call fails mid-execution, the interface may have a trailing
        assistant entry containing ToolCallBlocks whose results were never
        followed by a successful assistant response.  This method pops that
        orphan (and any trailing tool-result user entry) so the interface is
        clean for retry.

        Returns True if anything was removed, False if the interface was
        already clean.  Idempotent and safe on an empty interface.
        """
        if not self._entries:
            return False

        removed_any = False

        # Step 1: pop trailing tool-result user entries
        while self._entries:
            last = self._entries[-1]
            if last.role == "user" and last.content and all(
                isinstance(b, ToolResultBlock) for b in last.content
            ):
                self._entries.pop()
                removed_any = True
            else:
                break

        # Step 2: pop trailing assistant entry if it contains any ToolCallBlock
        if self._entries:
            last = self._entries[-1]
            if last.role == "assistant" and any(
                isinstance(b, ToolCallBlock) for b in last.content
            ):
                self._entries.pop()
                removed_any = True

        return removed_any

    # -- Truncation -----------------------------------------------------------

    def drop_trailing(self, predicate: Callable[[InterfaceEntry], bool]) -> list[InterfaceEntry]:
        """Pop entries from the end while predicate is True.  Returns dropped entries."""
        dropped: list[InterfaceEntry] = []
        while self._entries and predicate(self._entries[-1]):
            dropped.append(self._entries.pop())
        dropped.reverse()
        return dropped

    def truncate_to(self, entry_id: int) -> list[InterfaceEntry]:
        """Remove entries with id > entry_id.  Returns removed entries."""
        idx = None
        for i, e in enumerate(self._entries):
            if e.id == entry_id:
                idx = i
                break
        if idx is None:
            return []
        removed = self._entries[idx + 1:]
        self._entries = self._entries[:idx + 1]
        return removed

    def truncate(self, max_entries: int = 20, keep_recent: int | None = None) -> None:
        """Truncate interface to max_entries, preserving system prompt.

        Args:
            max_entries: Maximum non-system entries to keep.
            keep_recent: If set, keep this many most recent non-system entries
                         at the end (for context window management). Without this,
                         keeps the first max_entries (oldest).
        """
        has_system = self._entries and self._entries[0].role == "system"
        non_system_entries = [e for e in self._entries if e.role != "system"]

        if len(non_system_entries) <= max_entries:
            return  # Nothing to truncate

        if keep_recent is not None:
            # Keep system (if any), then keep_recent entries at the end
            keep_from = len(non_system_entries) - keep_recent
            keep_from = max(0, keep_from)
            kept_non_system = non_system_entries[keep_from:]
        else:
            # Keep first max_entries non-system entries (no keep_recent)
            kept_non_system = non_system_entries[:max_entries]

        # Rebuild entries: system + kept non-system
        if has_system:
            self._entries = [self._entries[0]] + kept_non_system
        else:
            self._entries = kept_non_system

    def to_messages(self) -> list[dict]:
        """Convert to simple message list (role + content dicts).

        Used for adapters that need a basic message format.
        """
        messages = []
        for entry in self._entries:
            if entry.role == "system":
                continue  # Skip system in to_messages
            content = []
            for block in entry.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolCallBlock):
                    content.append(block.to_dict())
                elif isinstance(block, ToolResultBlock):
                    content.append(block.to_dict())
                elif isinstance(block, ThinkingBlock):
                    content.append({"type": "thinking", "text": block.text})
            messages.append({"role": entry.role, "content": content})
        return messages

    # -- Compaction helpers ----------------------------------------------------

    def estimate_context_tokens(
        self,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> int:
        """Count tokens in system + tools + all messages using Google tokenizer.

        Uses the canonical entries (not provider-specific formats), so the
        estimate is provider-agnostic.  Falls back to current_system_prompt
        and current_tools if explicit args are not provided.
        """
        from ..token_counter import count_tokens
        import json

        total = 0

        # System prompt
        sp = system_prompt if system_prompt is not None else self._current_system_text
        if sp:
            total += count_tokens(sp)

        # Tool definitions
        t = tools if tools is not None else self._current_tools
        if t:
            total += count_tokens(json.dumps(t, default=str))

        # Conversation entries (skip system — already counted above)
        for entry in self._entries:
            if entry.role == "system":
                continue
            for block in entry.content:
                if isinstance(block, TextBlock):
                    total += count_tokens(block.text)
                elif isinstance(block, ToolCallBlock):
                    total += count_tokens(
                        f"{block.name}({json.dumps(block.args, default=str)})"
                    )
                elif isinstance(block, ToolResultBlock):
                    content_str = (
                        block.content
                        if isinstance(block.content, str)
                        else json.dumps(block.content, default=str)
                    )
                    total += count_tokens(content_str)
                elif isinstance(block, ThinkingBlock):
                    total += count_tokens(block.text)

        return total

    def find_compaction_boundary(self, keep_turns: int = 3) -> int | None:
        """Find entry index where compaction should split.

        Keeps the last *keep_turns* complete user-initiated turns intact.
        A "turn" starts with a user text message (not tool results).
        Tool-use/tool-result exchanges within a turn are never split.

        Returns the entry *id* at which to split (entries [0..id) get
        summarized, entries [id..] are kept), or None if there aren't
        enough turns to compact.
        """
        conv = [e for e in self._entries if e.role != "system"]
        if len(conv) < 6:  # need meaningful history to compact
            return None

        # Walk backward counting turn boundaries.
        # A turn boundary is a user entry whose content is NOT purely
        # ToolResultBlock — i.e., it contains a TextBlock (real user message).
        turns_found = 0
        boundary_idx = None
        for i in range(len(conv) - 1, -1, -1):
            entry = conv[i]
            if entry.role == "user":
                has_text = any(isinstance(b, TextBlock) for b in entry.content)
                has_only_tool_results = all(
                    isinstance(b, ToolResultBlock) for b in entry.content
                )
                if has_text and not has_only_tool_results:
                    turns_found += 1
                    if turns_found >= keep_turns:
                        boundary_idx = i
                        break

        if boundary_idx is None or boundary_idx <= 0:
            return None

        # The boundary entry id — everything before this gets summarized
        return conv[boundary_idx].id

    def format_for_summary(self, up_to_entry_id: int) -> str:
        """Format entries [0..up_to_entry_id) as text for summarization.

        Drops thinking blocks, truncates long tool results.
        """
        import json

        parts: list[str] = []
        for entry in self._entries:
            if entry.id >= up_to_entry_id:
                break
            if entry.role == "system":
                continue

            for block in entry.content:
                if isinstance(block, TextBlock):
                    parts.append(f"[{entry.role}] {block.text}")
                elif isinstance(block, ToolCallBlock):
                    args_str = json.dumps(block.args, default=str)[:200]
                    parts.append(
                        f"[{entry.role}] tool_use: {block.name}({args_str})"
                    )
                elif isinstance(block, ToolResultBlock):
                    content_str = (
                        block.content
                        if isinstance(block.content, str)
                        else json.dumps(block.content, default=str)
                    )
                    parts.append(
                        f"[{entry.role}] tool_result({block.name}): {content_str}"
                    )
                elif isinstance(block, ThinkingBlock):
                    pass  # Drop thinking — large and not actionable

        return "\n".join(parts)

    # -- Serialization --------------------------------------------------------

    def to_dict(self) -> list[dict]:
        return [e.to_dict() for e in self._entries]

    @classmethod
    def from_dict(cls, data: list[dict]) -> ChatInterface:
        iface = cls()
        for d in data:
            entry = InterfaceEntry.from_dict(d)
            iface._entries.append(entry)
            if entry.role == "system" and entry.content:
                block = entry.content[0]
                if isinstance(block, TextBlock):
                    iface._current_system_text = block.text
                iface._current_tools = entry._tools
        if iface._entries:
            iface._next_id = max(e.id for e in iface._entries) + 1
        return iface
