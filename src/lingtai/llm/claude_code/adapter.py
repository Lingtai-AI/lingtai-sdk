"""Claude Code adapter — drives the local ``claude`` CLI as a stateless brain.

See ``__init__.py`` for the high-level design. This module implements the
``LLMAdapter`` / ``ChatSession`` contract by spawning ``claude -p --output-format
json`` once per turn and parsing a single JSON *action* out of the result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
    UsageMetadata,
)
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.logging import get_logger

from lingtai.llm.base import LLMAdapter

logger = get_logger()


# Claude Code built-in tools we disable so the CLI behaves as a pure reasoning
# core: it must only emit our JSON action, never go off and read/edit/run things.
# Unknown names here are harmless (the CLI just warns on stderr).
DEFAULT_DISALLOWED_TOOLS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Edit",
    "Write",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)

# Stripped from the child env so the subprocess can never bill an API key —
# forcing the subscription / OAuth path. ``CLAUDE_CODE_OAUTH_TOKEN`` is kept on
# purpose: it is the supported headless subscription credential.
DEFAULT_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_DEFAULT_TIMEOUT_S = 600
_DEFAULT_CONTEXT_WINDOW = 200_000

_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context window",
    "context length",
    "too many tokens",
    "maximum context",
    "input is too long",
)


class ClaudeCodeError(RuntimeError):
    """A ``claude`` CLI invocation failed (non-zero exit, no output, etc.)."""


class ClaudeCodeAuthError(ClaudeCodeError):
    """The ``claude`` CLI is not logged in. Run ``claude`` or ``claude setup-token``."""


class ClaudeCodeContextOverflow(ClaudeCodeError):
    """The serialised conversation exceeded the model's context window."""


# Protocol the CLI must follow. Kept terse — it is re-sent every turn.
_PROTOCOL = """\
You are the REASONING CORE of an external agent runtime called LingTai. You do \
NOT execute tools yourself; the runtime executes them and returns their results \
to you on the next turn.

On every turn, read the whole conversation and the agent's own system \
instructions below, then decide the SINGLE next action and output EXACTLY ONE \
JSON object on a single line — no prose before or after, no markdown code fences.

Choose exactly one form:
  {"action": "tool_call", "name": "<tool_name>", "input": { <arguments> }}
  {"action": "final", "text": "<your reply to the user / agent>"}

Rules:
- Only call a tool listed under AVAILABLE TOOLS, and match its input schema exactly.
- To call several tools at once, use {"action": "tool_calls", "calls": [ {"name": ..., "input": {...}}, ... ]}.
- When you have enough information to respond, use the "final" form.
- Output ONLY the JSON object. Never wrap it in ``` fences and never add commentary.\
"""


def _map_usage(usage: dict | None) -> UsageMetadata:
    """Map the CLI's ``usage`` block to the kernel's UsageMetadata."""
    usage = usage or {}
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return UsageMetadata(
        input_tokens=int(usage.get("input_tokens", 0) or 0) + cache_read + cache_create,
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        thinking_tokens=0,
        cached_tokens=cache_read,
    )


def _extract_json_object(text: str) -> dict | None:
    """Best-effort: pull the first balanced ``{...}`` JSON object out of *text*.

    Tolerates leading/trailing prose and ``json`` markdown fences that a model
    might emit despite instructions. Returns None when no object parses.
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = re.sub(r"^\s*json\s*", "", s, flags=re.IGNORECASE)
        s = s.strip()
    # Fast path: the whole string is the object.
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Scan for the first balanced object, respecting string literals.
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    return None


class ClaudeCodeChatSession(ChatSession):
    """Multi-turn session backed by repeated ``claude -p`` invocations.

    Holds the canonical ChatInterface; each ``send`` re-serialises it into one
    CLI prompt. Stateless on the CLI side — LingTai owns all context, so its
    molt / trim / pairing invariants stay authoritative.
    """

    def __init__(
        self,
        *,
        adapter: "ClaudeCodeAdapter",
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema],
        interface: ChatInterface,
        context_window: int,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools
        self._interface = interface
        self._context_window = context_window

    # -- ChatSession contract -------------------------------------------------

    @property
    def interface(self) -> ChatInterface:
        return self._interface

    def send(self, message) -> LLMResponse:
        # Snapshot the canonical history *before* we mutate it, so a failure
        # anywhere downstream (pre_request_hook, tool pairing, the CLI call, or
        # response parsing) does not leave the just-added user / tool-result
        # message stranded in history. The agent retries from a clean state.
        # Captured only when we are about to mutate (message is not None): a
        # None message means the caller pre-staged the wire and owns it.
        restore = None if message is None else self._snapshot_interface()
        try:
            if isinstance(message, str):
                self._interface.add_user_message(message)
            elif isinstance(message, list):
                self._interface.add_tool_results(message)
            # message is None -> caller pre-staged the interface; just generate.

            if self.pre_request_hook is not None:
                self.pre_request_hook(self._interface)

            def _do_call():
                self._interface.enforce_tool_pairing()
                prompt = self._render_prompt()
                return self._adapter._invoke(prompt, self._model)

            (action, usage, raw), total_dropped, rounds = self._run_with_overflow_recovery(_do_call)
            # On successful context-overflow recovery the kernel silently trimmed
            # oldest entries; inject a [kernel] notice (same idiom as the other
            # adapters) so the agent learns context was lost — before recording
            # the assistant turn so the notice precedes it in history.
            if rounds > 0:
                self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)
            return self._record_response(action, usage, raw)
        except Exception:
            if restore is not None:
                restore()
            raise

    def _snapshot_interface(self):
        """Capture the canonical history so a failed turn can be rolled back.

        Returns a zero-arg ``restore`` callable. Covers both ways ``send``
        mutates the interface: ``add_user_message``/``add_tool_results`` append
        a new entry, and ``add_tool_results`` may also overwrite a synthesized
        ToolResultBlock *in place* — so we snapshot the entry list, each entry's
        ``content`` list, and the id counter, and restore them on failure.
        """
        iface = self._interface
        entries = list(iface._entries)
        contents = [(e, list(e.content)) for e in entries]
        next_id = iface._next_id

        def restore() -> None:
            iface._entries[:] = entries
            for entry, content in contents:
                entry.content[:] = content
            iface._next_id = next_id

        return restore

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        self._tools = list(tools) if tools else []
        self._interface.add_system(
            self._system_prompt,
            tools=[t.to_dict() for t in self._tools] if self._tools else None,
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._interface.add_system(
            system_prompt,
            tools=[t.to_dict() for t in self._tools] if self._tools else None,
        )

    def commit_tool_results(self, tool_results: list) -> None:
        self._interface.add_tool_results(tool_results)

    def context_window(self) -> int:
        return self._context_window

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        return isinstance(exc, ClaudeCodeContextOverflow)

    # -- internals ------------------------------------------------------------

    def _record_response(self, action: dict, usage: UsageMetadata, raw: Any) -> LLMResponse:
        """Turn the parsed action into assistant blocks + an LLMResponse."""
        blocks: list = []
        tool_calls: list[ToolCall] = []
        text = ""

        kind = action.get("action")
        if kind == "tool_calls":
            for call in action.get("calls", []) or []:
                name = call.get("name")
                if not name:
                    continue
                args = call.get("input") or call.get("args") or {}
                cid = f"cc_{uuid.uuid4().hex[:24]}"
                blocks.append(ToolCallBlock(id=cid, name=name, args=args))
                tool_calls.append(ToolCall(name=name, args=args, id=cid))
        elif kind == "tool_call" or (kind is None and action.get("name")):
            name = action.get("name")
            args = action.get("input") or action.get("args") or {}
            if name:
                cid = f"cc_{uuid.uuid4().hex[:24]}"
                blocks.append(ToolCallBlock(id=cid, name=name, args=args))
                tool_calls.append(ToolCall(name=name, args=args, id=cid))

        if not tool_calls:
            # "final", or an unrecognised shape we fall back to as plain text.
            text = action.get("text")
            if text is None:
                text = action.get("_raw", "")
            text = str(text)
            blocks.append(TextBlock(text=text))

        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="claude-code",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
                "cached_tokens": usage.cached_tokens,
            },
        )
        return LLMResponse(text=text, tool_calls=tool_calls, usage=usage, raw=raw)

    def _render_prompt(self) -> str:
        """Serialise system prompt + tools + conversation into one CLI prompt."""
        parts: list[str] = [_PROTOCOL, ""]
        parts.append("# AGENT SYSTEM INSTRUCTIONS")
        parts.append(self._system_prompt or "(none)")
        parts.append("")

        parts.append("# AVAILABLE TOOLS")
        if self._tools:
            for t in self._tools:
                parts.append(f"## {t.name}")
                if t.description:
                    parts.append(t.description.strip())
                try:
                    schema = json.dumps(t.parameters, ensure_ascii=False)
                except (TypeError, ValueError):
                    schema = str(t.parameters)
                parts.append(f"input schema: {schema}")
                parts.append("")
        else:
            parts.append("(no tools available — respond with a final answer)")
            parts.append("")

        parts.append("# CONVERSATION")
        parts.append(self._render_conversation())
        parts.append("")
        parts.append("# NOW")
        parts.append("Decide your next action and output the single JSON object.")
        return "\n".join(parts)

    def _render_conversation(self) -> str:
        lines: list[str] = []
        for entry in self._interface._entries:
            role = entry.role
            if role == "system":
                continue  # system prompt + tools rendered separately
            for block in entry.content:
                if isinstance(block, ThinkingBlock):
                    continue
                if isinstance(block, TextBlock):
                    if role == "assistant":
                        lines.append(f"ASSISTANT: {block.text}")
                    else:
                        lines.append(f"USER: {block.text}")
                elif isinstance(block, ToolCallBlock):
                    try:
                        args = json.dumps(block.args, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args = str(block.args)
                    lines.append(
                        f"ASSISTANT_ACTION: called tool `{block.name}` with input {args}"
                    )
                elif isinstance(block, ToolResultBlock):
                    content = block.content
                    if not isinstance(content, str):
                        try:
                            content = json.dumps(content, ensure_ascii=False)
                        except (TypeError, ValueError):
                            content = str(content)
                    lines.append(f"TOOL_RESULT [{block.name}]: {content}")
        return "\n".join(lines) if lines else "(no messages yet)"


class ClaudeCodeAdapter(LLMAdapter):
    """LLMAdapter that drives the ``claude`` CLI as a stateless reasoning core."""

    def __init__(
        self,
        *,
        model: str | None = None,
        cli_path: str = "claude",
        disallowed_tools: tuple[str, ...] | list[str] | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        max_rpm: int = 0,
        strip_env: tuple[str, ...] | list[str] | None = None,
        extra_argv: list[str] | None = None,
        context_window: int = _DEFAULT_CONTEXT_WINDOW,
    ) -> None:
        self._model = model or "sonnet"
        self._cli_path = cli_path
        self._disallowed = (
            list(disallowed_tools)
            if disallowed_tools is not None
            else list(DEFAULT_DISALLOWED_TOOLS)
        )
        self._timeout_s = timeout_s
        self._strip_env = tuple(strip_env) if strip_env is not None else DEFAULT_STRIP_ENV
        self._extra_argv = list(extra_argv or [])
        self._context_window = context_window
        self._setup_gate(max_rpm)
        # Neutral, empty cwd so the CLI does not load a project's CLAUDE.md,
        # settings, or MCP servers (which could inject context or extra tools).
        self._cwd = Path(tempfile.gettempdir()) / "lingtai-claude-brain"
        try:
            self._cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._cwd = Path(tempfile.gettempdir())

    # -- LLMAdapter contract --------------------------------------------------

    def create_chat(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        *,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        interaction_id: str | None = None,
        context_window: int = 0,
    ) -> ChatSession:
        iface = interface or ChatInterface()
        tool_list = list(tools) if tools else []
        if interface is None:
            iface.add_system(
                system_prompt,
                tools=[t.to_dict() for t in tool_list] if tool_list else None,
            )
        session = ClaudeCodeChatSession(
            adapter=self,
            model=model or self._model,
            system_prompt=system_prompt,
            tools=tool_list,
            interface=iface,
            context_window=context_window or self._context_window,
        )
        return self._wrap_with_gate(session)

    def generate(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """One-shot generation (no action protocol — plain text out)."""
        if isinstance(contents, list):
            text_parts = []
            for c in contents:
                if isinstance(c, dict):
                    text_parts.append(c.get("text", ""))
                else:
                    text_parts.append(str(c))
            user_text = "\n".join(p for p in text_parts if p)
        else:
            user_text = str(contents)

        prompt_parts = []
        if system_prompt:
            prompt_parts.append(system_prompt)
            prompt_parts.append("")
        prompt_parts.append(user_text)
        if json_schema:
            prompt_parts.append("")
            prompt_parts.append(
                "Respond with ONLY a JSON object matching this schema, no prose: "
                + json.dumps(json_schema, ensure_ascii=False)
            )
        prompt = "\n".join(prompt_parts)

        result_str, usage, raw = self._invoke_raw(prompt, model or self._model)
        return LLMResponse(text=result_str, usage=usage, raw=raw)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        return ToolResultBlock(
            id=tool_call_id or f"cc_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return "rate limit" in msg or "429" in msg or "usage limit" in msg

    # -- CLI plumbing ---------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in self._strip_env:
            env.pop(key, None)
        return env

    def _invoke_raw(self, prompt: str, model: str) -> tuple[str, UsageMetadata, dict]:
        """Run ``claude -p`` once. Returns (result_text, usage, envelope)."""
        cmd = [self._cli_path, "-p", "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        if self._disallowed:
            cmd += ["--disallowedTools", *self._disallowed]
        cmd += self._extra_argv

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                env=self._build_env(),
                cwd=str(self._cwd),
                timeout=self._timeout_s,
            )
        except FileNotFoundError as e:
            raise ClaudeCodeAuthError(
                f"`{self._cli_path}` not found on PATH. Install Claude Code and run "
                f"`claude` (or `claude setup-token`) to log in with your subscription."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeCodeError(
                f"claude CLI timed out after {self._timeout_s}s"
            ) from e

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0 and not stdout:
            low = stderr.lower()
            if any(m in low for m in _OVERFLOW_MARKERS):
                raise ClaudeCodeContextOverflow(stderr[:500])
            if "login" in low or "not authenticated" in low or "setup-token" in low or "/login" in low:
                raise ClaudeCodeAuthError(
                    "claude CLI is not logged in. Run `claude` or `claude setup-token` "
                    f"to authenticate with your subscription. Detail: {stderr[:300]}"
                )
            raise ClaudeCodeError(
                f"claude CLI exited {proc.returncode}: {stderr[:500] or '(no stderr)'}"
            )

        if not stdout:
            raise ClaudeCodeError(
                f"claude CLI produced no output (exit {proc.returncode}). "
                f"stderr: {stderr[:300]}"
            )

        envelope = self._parse_envelope(stdout)

        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            msg = str(envelope.get("result") or envelope.get("error") or envelope.get("subtype") or "unknown error")
            low = msg.lower()
            if any(m in low for m in _OVERFLOW_MARKERS):
                raise ClaudeCodeContextOverflow(msg[:500])
            if "usage limit" in low or "rate limit" in low:
                raise ClaudeCodeError(f"claude usage/rate limit: {msg[:300]}")
            raise ClaudeCodeError(f"claude returned an error: {msg[:500]}")

        result_str = str(envelope.get("result") or "")
        usage = _map_usage(envelope.get("usage"))
        return result_str, usage, envelope

    @staticmethod
    def _parse_envelope(stdout: str) -> dict:
        """Parse the CLI's JSON envelope (tolerate a trailing-line stream)."""
        try:
            obj = json.loads(stdout)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # Fall back to the last JSON object on its own line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        raise ClaudeCodeError(f"could not parse claude CLI output as JSON: {stdout[:300]}")

    def _invoke(self, prompt: str, model: str) -> tuple[dict, UsageMetadata, dict]:
        """Run the CLI and parse one JSON *action* from its result."""
        result_str, usage, envelope = self._invoke_raw(prompt, model)
        action = _extract_json_object(result_str)
        if action is None:
            # Model ignored the protocol — treat the whole reply as a final answer
            # so the agent still makes progress rather than crashing.
            logger.warning("[claude-code] no JSON action parsed; treating as final text")
            action = {"action": "final", "_raw": result_str}
        return action, usage, envelope

    @property
    def context_window_size(self) -> int:
        return self._context_window
