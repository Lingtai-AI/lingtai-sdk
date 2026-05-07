"""
BaseAgent — generic agent kernel with intrinsic tools and capability dispatch.

Key concepts:
    - **5-state lifecycle**: ACTIVE, IDLE, STUCK, ASLEEP, SUSPENDED.
    - **Persistent LLM session**: each agent keeps its chat session across messages.
    - **2-layer tool dispatch**: intrinsics (built-in) + capability handlers.
    - **Opaque context**: the host app can pass any context object — the agent
      stores it but never introspects it.
    - **4 optional services**: LLM, FileIO, Mail, Logging —
      missing service auto-disables the intrinsics it backs.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import AgentConfig
from ..state import AgentState
from ..workdir import WorkingDir
from ..message import Message, _make_message, MSG_REQUEST, MSG_USER_INPUT, MSG_TC_WAKE
from ..intrinsics import ALL_INTRINSICS
from ..prompt import SystemPromptManager
from ..llm import (
    FunctionSchema,
    LLMService,
    ToolCall,
)
from ..i18n import t as _t
from ..logging import get_logger
from ..loop_guard import LoopGuard
from ..prompt import build_system_prompt, build_system_prompt_batches
from ..meta_block import build_meta, render_meta
from ..time_veil import now_iso, scrub_time_fields
from ..session import SessionManager
from ..tc_inbox import TCInbox
from ..tool_executor import ToolExecutor
from ..token_ledger import append_token_entry, sum_token_ledger
from ..types import UnknownToolError

logger = get_logger()


# ---------------------------------------------------------------------------
# Identity prompt section (curated prose)
# ---------------------------------------------------------------------------


def _format_stamina(seconds: float) -> str:
    """Render stamina capacity as a human-friendly duration string."""
    if seconds <= 0:
        return "no fixed limit"
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.1f}h" if hours != int(hours) else f"{int(hours)}h"
    minutes = seconds / 60
    return f"{int(minutes)}min"


def _build_identity_section(manifest_data: dict, mailbox_name: str | None = None) -> str:
    """Render the agent's identity as curated prose for the system prompt.

    Stable across turns (no transient runtime state) so it sits in the
    cacheable prefix without invalidating cache. The `state` field is
    explicitly omitted upstream — it changes every turn.

    Returns a markdown paragraph. Empty/missing fields are silently
    omitted so the prose stays clean for minimal manifests.
    """
    name = manifest_data.get("agent_name") or "(unnamed)"
    nickname = manifest_data.get("nickname") or ""
    agent_id = manifest_data.get("agent_id") or ""
    address = manifest_data.get("address") or ""
    created = manifest_data.get("created_at") or ""
    started = manifest_data.get("started_at") or ""
    admin = manifest_data.get("admin") or {}
    stamina = manifest_data.get("stamina") or 0
    soul_delay = manifest_data.get("soul_delay")
    molt_count = manifest_data.get("molt_count", 0)

    lines: list[str] = []

    # Lead — name, nickname, id, address.
    lead = f"You are **{name}**"
    if nickname:
        lead += f" — \"{nickname}\""
    if agent_id:
        lead += f" (id `{agent_id}`)"
    lead += "."
    lines.append(lead)
    if address:
        lines.append(f"Your address is `{address}`.")

    # Origins — birth, awakening, molts.
    origins: list[str] = []
    if created:
        origins.append(f"born {created}")
    if started:
        origins.append(f"woken {started} for this session")
    if origins:
        lines.append("You were " + ", ".join(origins) + ".")
    if molt_count > 0:
        lines.append(
            f"You have undergone {molt_count} molt"
            f"{'s' if molt_count != 1 else ''} since birth."
        )

    # Admin role.
    if admin:
        flags = [k for k, v in admin.items() if v]
        if flags:
            if "nirvana" in flags:
                lines.append(
                    "You hold both **karma** and **nirvana** privileges — "
                    "you can manage and destroy other agents in this network."
                )
            elif "karma" in flags:
                lines.append(
                    "You hold **karma** privilege — "
                    "you can lull / suspend / cpr / clear other agents."
                )
            else:
                lines.append(f"You hold admin flags: {', '.join(flags)}.")

    # Resources.
    if stamina:
        lines.append(
            f"Each session lasts up to {_format_stamina(stamina)} "
            "of work before rest. Your `stamina_left_seconds` "
            "appears on every tool result."
        )
    if soul_delay is not None:
        lines.append(f"Your soul flow fires {soul_delay}s after you go idle.")
    if mailbox_name:
        lines.append(f"You receive messages via {mailbox_name}.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class BaseAgent:
    """Generic research agent with intrinsic tools and MCP tool dispatch.

    Services (all optional):
        - ``service`` (LLMService): The brain — thinking, generating text.
        - ``file_io`` (FileIOService): File access — backs read/edit/write/glob/grep.
        - ``mail_service`` (MailService): Message transport — backs mail intrinsic.

    Missing service = intrinsics backed by it are auto-disabled.

    Subclasses customize behavior via:
        - ``_pre_request(msg)`` — transform message before LLM send
        - ``_post_request(msg, result)`` — side effects after LLM responds
        - ``_handle_message(msg)`` — message routing (must call super for processing)
        - ``_get_guard_limits()`` — per-agent loop guard limits
        - ``_PARALLEL_SAFE_TOOLS`` — set of tool names safe for concurrent execution
    """

    agent_type: str = ""

    # Tools safe for concurrent execution
    _PARALLEL_SAFE_TOOLS: set[str] = set()

    # Inbox polling interval (seconds)
    _inbox_timeout: float = 1.0

    def __init__(
        self,
        service: LLMService,
        *,
        agent_name: str | None = None,
        working_dir: str | Path,
        file_io: Any | None = None,
        mail_service: Any | None = None,
        config: AgentConfig | None = None,
        context: Any = None,
        admin: dict | None = None,
        streaming: bool = False,
        covenant: str = "",
        principle: str = "",
        substrate: str = "",
        procedures: str = "",
        brief: str = "",
        pad: str = "",
        comment: str = "",
    ):
        self.agent_name = agent_name  # true name (真名) — immutable once set
        self.nickname: str | None = None  # mutable alias (别名)
        self.service = service
        self._config = config or AgentConfig()
        self._context = context
        self._admin = admin or {}
        self._cancel_event = threading.Event()
        self._state = AgentState.IDLE
        self._started_at: str = ""
        self._last_usage = None  # UsageMetadata from last LLM call, for ledger
        self._created_at: str = ""
        self._uptime_anchor: float | None = None  # set in start(), None means not started

        # Working directory (caller-owned path)
        self._workdir = WorkingDir(working_dir)
        self._working_dir = self._workdir.path

        # LoggingService: always JSONL in working dir
        from ..services.logging import JSONLLoggingService
        log_dir = self._working_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        self._log_service = JSONLLoggingService(
            log_dir / "events.jsonl",
            ensure_ascii=self._config.ensure_ascii,
        )

        # Acquire working directory lock (10s grace for prior process cleanup)
        self._workdir.acquire_lock(timeout=10)

        # --- Wire services ---
        # FileIOService: optional, provided by Agent or host
        self._file_io = file_io

        # MailService: None means mail intrinsic disabled
        self._mail_service = mail_service

        # Covenant, principle, substrate, procedures, brief, and pad file paths
        system_dir = self._working_dir / "system"
        pad_file = system_dir / "pad.md"
        covenant_file = system_dir / "covenant.md"
        principle_file = system_dir / "principle.md"
        substrate_file = system_dir / "substrate.md"
        procedures_file = system_dir / "procedures.md"
        brief_file = system_dir / "brief.md"

        system_dir.mkdir(exist_ok=True)

        # Covenant: constructor value wins, then fall back to file on disk
        if covenant:
            covenant_file.write_text(covenant)
        elif covenant_file.is_file():
            covenant = covenant_file.read_text()

        # Principle: constructor value wins, then fall back to file on disk
        if principle:
            principle_file.write_text(principle)
        elif principle_file.is_file():
            principle = principle_file.read_text()

        # Substrate: same pattern as covenant/principle. Opt-in (issue
        # #39): kernel-owned, cross-app-stable system prompt section that
        # describes the agent's architecture to itself; rendered between
        # covenant and tools by SystemPromptManager.
        if substrate:
            substrate_file.write_text(substrate)
        elif substrate_file.is_file():
            substrate = substrate_file.read_text()

        # Procedures: same pattern as covenant/principle
        if procedures:
            procedures_file.write_text(procedures)
        elif procedures_file.is_file():
            procedures = procedures_file.read_text()

        # Brief: externally-maintained context (written by secretary agent).
        if brief and not brief_file.is_file():
            brief_file.write_text(brief)
        elif brief_file.is_file():
            brief = brief_file.read_text()

        # Pad: constructor value seeds the file if it doesn't exist
        if pad and not pad_file.is_file():
            pad_file.write_text(pad)

        # Auto-load pad from file into prompt manager
        loaded_pad = ""
        if pad_file.is_file():
            loaded_pad = pad_file.read_text()

        # System prompt manager
        self._prompt_manager = SystemPromptManager()
        if principle:
            self._prompt_manager.write_section("principle", principle, protected=True)
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)
        if substrate:
            self._prompt_manager.write_section("substrate", substrate, protected=True)
        if procedures:
            self._prompt_manager.write_section("procedures", procedures, protected=True)
        if brief:
            self._prompt_manager.write_section("brief", brief, protected=True)
        # Load existing rules from system/rules.md (survives molts, refreshes, and resumes)
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text().strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
            except OSError:
                pass
        if loaded_pad.strip():
            self._prompt_manager.write_section("pad", loaded_pad)
        if comment:
            self._prompt_manager.write_section("comment", comment)

        # Soul delay — needed before manifest build
        self._soul_delay = max(1.0, self._config.soul_delay)

        # Agent ID, created_at, and molt_count — persistent state restored
        from datetime import datetime, timezone
        import secrets
        existing = self._workdir.read_full_manifest()
        self._agent_id: str = existing.get("agent_id", "")
        self._created_at: str = existing.get("created_at", "")
        self._molt_count: int = existing.get("molt_count", 0)
        if not self._agent_id or not self._created_at:
            now = datetime.now(timezone.utc)
            if not self._agent_id:
                self._agent_id = now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            if not self._created_at:
                self._created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Write manifest — identity + construction recipe (no runtime state)
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        from .identity import _build_manifest
        manifest_data = _build_manifest(self)
        self._workdir.write_manifest(manifest_data)

        # Auto-inject identity into system prompt from manifest
        self._prompt_manager.write_section(
            "identity",
            _build_identity_section(
                manifest_data,
                mailbox_name=getattr(self, "_mailbox_name", None),
            ),
            protected=True,
        )

        self._nap_wake = threading.Event()  # signalled to wake nap early
        self._nap_wake_reason = ""  # why the nap was woken

        # Mailbox identity — capabilities override these to change notification text.
        self._mailbox_name = "email box"
        self._mailbox_tool = "email"

        # Non-intrinsic tool handlers (capabilities, MCP, add_tool)
        self._tool_handlers: dict[str, Callable[[dict], dict]] = {}
        self._tool_schemas: list[FunctionSchema] = []

        # --- Wire intrinsic tools ---
        self._intrinsics: dict[str, Callable[[dict], dict]] = {}
        self._wire_intrinsics()

        # Inbox — text-channel notifications (mail, daemon, user input)
        self.inbox: queue.Queue[Message] = queue.Queue()

        # Involuntary tool-call inbox
        self._tc_inbox: TCInbox = TCInbox()

        # Tracks the most recent in-history call_id for each "single-slot" source.
        self._appendix_ids_by_source: dict[str, str] = {}

        # _pending_mail_notifications removed — email arrivals now use
        # single-slot unread-digest (email.unread) instead of per-arrival
        # system.notification pairs. Bounce/MCP/soul notifications still
        # use system.notification but don't need per-ref tracking.

        # Notification sync state (filesystem-as-protocol redesign).
        # Tracks (a) the last-seen `.notification/` fingerprint so we can
        # detect changes between heartbeat ticks, and (b) the call_id of
        # the currently-injected wire pair (or None if no notification
        # block is currently in the wire).  See notifications.py and
        # discussions/notification-filesystem-redesign.md.
        self._notification_fp: tuple = ()
        self._notification_block_id: str | None = None
        # Monotonic counter ensuring every synthesized notification pair
        # carries unique tokens (timestamp + seq) even when the underlying
        # payload repeats — defeats DeepSeek's cache fast-path empty-completion
        # failure mode on byte-identical synthetic pairs.
        self._notification_inject_seq: int = 0
        # ACTIVE-state stash: the JSON body to prepend to the next
        # ToolResultBlock at request-send time.  Set by _sync_notifications
        # while the agent is mid-tool-chain; consumed (and reset to None)
        # by _inject_notification_meta inside SessionManager.send().
        self._pending_notification_meta: str | None = None

        # Lifecycle
        self._shutdown = threading.Event()
        self._asleep = threading.Event()   # set when entering ASLEEP; cleared on wake
        self._thread: threading.Thread | None = None
        self._idle = threading.Event()
        self._idle.set()
        self._state = AgentState.IDLE
        self._sealed = False

        # Soul — inner voice
        self._soul_prompt = ""       # non-empty during inquiry
        self._soul_oneshot = False    # True during pending inquiry
        self._soul_timer: threading.Timer | None = None
        # Held while a soul flow consultation fire is running. Voluntary
        # soul(action='flow') calls try-acquire non-blocking — if held,
        # the call is rejected with "soul flow ongoing".
        self._soul_fire_lock: threading.Lock = threading.Lock()
        self._insight_turn_counter: int = 0

        # Heartbeat — always-on health monitor
        self._heartbeat: float = 0.0
        self._heartbeat_thread: threading.Thread | None = None
        self._aed_start: float | None = None

        # Snapshot — periodic git commits (Time Machine)
        self._last_snapshot: float = 0.0
        self._last_gc: float = 0.0

        # Auto-fallback state
        self._preset_fallback_attempted = False

        # Session manager — LLM session, token tracking, compaction
        self._session = SessionManager(
            llm_service=service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            logger_fn=self._log,
            build_system_batches_fn=self._build_system_prompt_batches,
            notification_inject_fn=self._inject_notification_meta,
        )

        # Boot the psyche intrinsic
        from ..intrinsics import psyche as _psyche
        _psyche.boot(self)

        # Boot the email intrinsic
        from ..intrinsics import email as _email
        _email.boot(self)

    # ------------------------------------------------------------------
    # Intrinsic wiring
    # ------------------------------------------------------------------

    def _wire_intrinsics(self) -> None:
        """Wire kernel intrinsic tool handlers."""
        for name, info in ALL_INTRINSICS.items():
            handle_fn = info["module"].handle
            self._intrinsics[name] = lambda args, fn=handle_fn: fn(self, args)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        return self._idle.is_set()

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def agent_id(self) -> str:
        """Permanent birth certificate — never changes across restarts or moves."""
        return self._agent_id

    @property
    def working_dir(self) -> Path:
        """The agent's working directory."""
        return self._workdir.path

    @property
    def _chat(self) -> Any:
        """Proxy to SessionManager's chat session."""
        return self._session.chat

    @_chat.setter
    def _chat(self, value: Any) -> None:
        self._session.chat = value

    @property
    def _streaming(self) -> bool:
        """Proxy to SessionManager's streaming flag."""
        return self._session.streaming

    @property
    def _token_decomp_dirty(self) -> bool:
        """Proxy to SessionManager's token decomp dirty flag."""
        return self._session.token_decomp_dirty

    @_token_decomp_dirty.setter
    def _token_decomp_dirty(self, value: bool) -> None:
        self._session.token_decomp_dirty = value

    @property
    def _interaction_id(self) -> str | None:
        """Proxy to SessionManager's interaction ID."""
        return self._session.interaction_id

    @_interaction_id.setter
    def _interaction_id(self, value: str | None) -> None:
        self._session.interaction_id = value

    @property
    def _intermediate_text_streamed(self) -> bool:
        """Proxy to SessionManager's intermediate text streamed flag."""
        return self._session.intermediate_text_streamed

    @_intermediate_text_streamed.setter
    def _intermediate_text_streamed(self, value: bool) -> None:
        self._session.intermediate_text_streamed = value

    # ------------------------------------------------------------------
    # Naming (pass-throughs to identity.py)
    # ------------------------------------------------------------------

    def set_name(self, name: str) -> None:
        from .identity import _set_name
        _set_name(self, name)

    def set_nickname(self, nickname: str) -> None:
        from .identity import _set_nickname
        _set_nickname(self, nickname)

    def _update_identity(self) -> None:
        from .identity import _update_identity
        _update_identity(self)

    # ------------------------------------------------------------------
    # Lifecycle (pass-throughs to lifecycle.py + direct methods)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the agent's main loop thread."""
        from .lifecycle import _start
        _start(self)

    def _reset_uptime(self) -> None:
        """Reset the uptime anchor for stamina tracking."""
        from .lifecycle import _reset_uptime
        _reset_uptime(self)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and wait for the agent thread to exit."""
        from .lifecycle import _stop
        _stop(self, timeout)

    def _set_state(self, new_state: AgentState, reason: str = "") -> None:
        """Transition to a new state.

        Drives the soul cadence timer: the timer runs only when the agent
        is in a fire-eligible state (ACTIVE / IDLE). Entering STUCK,
        ASLEEP, or SUSPENDED cancels it outright; returning to ACTIVE or
        IDLE starts a fresh ``soul_delay``-second timer.
        """
        from ..intrinsics.soul.flow import _start_soul_timer, _cancel_soul_timer

        old = self._state
        if old == new_state:
            return
        self._state = new_state
        if new_state == AgentState.ACTIVE:
            self._idle.clear()
        else:
            self._idle.set()

        fire_eligible = {AgentState.ACTIVE, AgentState.IDLE}
        was_eligible = old in fire_eligible
        is_eligible = new_state in fire_eligible
        if was_eligible and not is_eligible:
            _cancel_soul_timer(self)
        elif is_eligible and not was_eligible:
            _start_soul_timer(self)

        self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
        self._workdir.write_manifest(self._build_manifest())

    def _wake_nap(self, reason: str) -> None:
        """Signal the nap to wake up with a given reason."""
        self._nap_wake_reason = reason
        self._nap_wake.set()

    def _log(self, event_type: str, **fields) -> None:
        """Write a structured event to the logging service, if configured."""
        if self._log_service:
            self._log_service.log({
                "type": event_type,
                "address": self._working_dir.name,
                "agent_name": self.agent_name,
                "ts": time.time(),
                **fields,
            })

    def wake(self, reason: str) -> None:
        """Wake the agent from nap. Call when external input arrives."""
        self._wake_nap(reason)

    def log(self, event_type: str, **fields) -> None:
        """Write a structured event to the agent's event log."""
        self._log(event_type, **fields)

    # ------------------------------------------------------------------
    # Public addon API (pass-throughs)
    # ------------------------------------------------------------------

    def _on_mail_received(self, payload: dict) -> None:
        from .messaging import _on_mail_received
        _on_mail_received(self, payload)

    def _on_normal_mail(self, payload: dict) -> None:
        from .messaging import _on_normal_mail
        _on_normal_mail(self, payload)

    def _enqueue_system_notification(self, *, source: str, ref_id: str, body: str) -> str:
        from .messaging import _enqueue_system_notification
        return _enqueue_system_notification(self, source=source, ref_id=ref_id, body=body)

    def notify(self, sender: str, text: str) -> None:
        from .messaging import _notify
        _notify(self, sender, text)

    # ------------------------------------------------------------------
    # Soul (pass-throughs to soul_flow.py)
    # ------------------------------------------------------------------

    def _start_soul_timer(self) -> None:
        from ..intrinsics.soul.flow import _start_soul_timer
        _start_soul_timer(self)

    def _cancel_soul_timer(self) -> None:
        from ..intrinsics.soul.flow import _cancel_soul_timer
        _cancel_soul_timer(self)

    def _soul_whisper(self) -> None:
        from ..intrinsics.soul.flow import _soul_whisper
        _soul_whisper(self)

    def _drain_tc_inbox(self) -> None:
        """Splice queued involuntary tool-call pairs at a safe boundary.

        Also (re)installs the pre-request drain hook on the active chat
        session — see :meth:`_install_drain_hook` for the rationale.
        Called from two paths today: the entry drain at request start
        (``base_agent/turn.py:_handle_request``) and the dedicated TC
        wake handler (``_handle_tc_wake``). The pre-request hook itself
        adds a third path: drain fires once per LLM round-trip inside
        the tool-call loop, so mail notifications and soul.flow voices
        splice into the wire mid-task instead of waiting for the outer
        turn to end.
        """
        if self._chat is None:
            try:
                self._session.ensure_session()
            except Exception:
                return
        # Idempotent — re-installing the same hook on the same session
        # is a no-op. Cheap to call on every drain so a session created
        # via _rebuild_session (AED recovery) gets the hook automatically
        # without the AED path needing to know about it.
        self._install_drain_hook()
        result = self._tc_inbox.drain_into(
            self._chat.interface,
            self._appendix_ids_by_source,
        )
        if result.count > 0:
            self._log("tc_inbox_drain", count=result.count, sources=result.sources)
            self._save_chat_history()

    def _install_drain_hook(self) -> None:
        """Install the mid-turn tc_inbox drain hook on the active chat session.

        The hook fires inside each adapter's ``send()`` after the message
        has been committed to the canonical ChatInterface but before the
        API call — at that moment the wire tail is ``user[tool_results]``
        or ``user[text]``, so ``has_pending_tool_calls()`` returns False
        and the splicer can safely append a new ``(call, result)`` pair.

        Wire-state semantic, in two regimes:

        * **Canonical-interface adapters** (anthropic, openai-CC,
          codex-Responses, deepseek): the hook splices into the same
          interface the adapter is about to serialize for the wire, so
          the spliced pair appears in the *current* API request.
          Mail notifications enqueued during a long bash chain reach
          the LLM within one tool round.

        * **Server-state adapters** (OpenAIResponsesSession, both
          GeminiChatSession and InteractionsChatSession): the hook
          splices into the canonical interface, but the wire payload
          for the current request is built from server-side state
          (``previous_response_id`` / ``previous_interaction_id``) or
          the genai SDK's own chat history. The spliced pair is only
          visible to the LLM on the *next* turn after the agent
          re-syncs. The agent-side persistence and inspection paths
          (chat_history.jsonl, .status.json, /codex view) update
          immediately either way.

        Subtle semantic for ``replace_in_history=True`` (soul.flow):
        when the hook fires mid-turn, splicing in a replacement pair
        removes the prior pair of the same source from the interface.
        This is *almost* identical to the turn-boundary behavior that
        already exists today, with one nuance: the LLM's reasoning in
        the *current* turn was conditioned on a wire that contained
        the prior pair, but its next API call (or its in-flight
        reasoning continuation) may serialize a wire that doesn't.
        For soul.flow's reflective voices this is harmless — they
        don't drive tool calls and the model isn't building a chain
        of reasoning that depends on the prior voice's exact text.
        For any future producer that uses ``replace_in_history=True``
        with content the agent might cite mid-turn, this is a
        consideration; flagged here rather than buried in commit
        history.

        Idempotent: re-assigning the same callable to the same session
        attribute is a no-op. Called from :meth:`_drain_tc_inbox` so
        sessions created via ``_rebuild_session`` (AED recovery) pick
        up the hook on the next drain without a separate code path.
        """
        if self._chat is None:
            return
        if not hasattr(self._chat, "pre_request_hook"):
            return
        # Bind via lambda so the hook captures self, not the chat session.
        # The drain method itself rebinds to self._chat.interface, so the
        # hook ignores the interface argument the adapter passes in.
        self._chat.pre_request_hook = lambda _iface: self._drain_tc_inbox_for_hook()

    def _drain_tc_inbox_for_hook(self) -> None:
        """Hook-callable variant of _drain_tc_inbox without re-installing.

        The pre-request hook is called from inside an adapter's send(),
        which means we're already inside a session.send() call. Calling
        the full _drain_tc_inbox would try to re-install the hook (cheap
        but pointless) and could in pathological cases recurse if a
        future producer enqueues during drain. This variant just splices
        and returns.
        """
        if self._chat is None:
            return
        result = self._tc_inbox.drain_into(
            self._chat.interface,
            self._appendix_ids_by_source,
        )
        if result.count > 0:
            self._log(
                "tc_inbox_drain",
                count=result.count,
                sources=result.sources,
                from_hook=True,
            )
            self._save_chat_history()

    # ------------------------------------------------------------------
    # Notification sync — filesystem-as-protocol replacement for tc_inbox.
    # See notifications.py and discussions/notification-filesystem-redesign.md.
    # ------------------------------------------------------------------

    _NOTIF_PREFIX_LEAD = "notifications:\n"

    @staticmethod
    def _strip_notification_prefix(content: str) -> str:
        """Remove a leading ``notifications:\\n…\\n\\n`` block if present.

        Idempotent.  Used both before reprepending fresh meta and on older
        result blocks to maintain the single-slot invariant.
        """
        lead = BaseAgent._NOTIF_PREFIX_LEAD
        if not content.startswith(lead):
            return content
        end = content.find("\n\n", len(lead))
        if end < 0:
            return content
        return content[end + 2:]

    def _sync_notifications(self) -> None:
        """Sync `.notification/` state into the wire.

        Computes the current fingerprint; if unchanged, no-op.  On change:
        1. Strip the prior wire pair (if any).
        2. If the new collection is empty, commit the empty fingerprint
           and return — wire now has zero notification blocks.
        3. Otherwise, inject a new block appropriate for current state:

           * IDLE → splice ``(call, result)`` pair (impersonates a
             voluntary ``system(action="notification")`` call from the
             agent's perspective), post ``MSG_TC_WAKE`` so the run loop
             unblocks and ``_handle_tc_wake`` drives the next inference
             round off the existing wire — no fake user input, no meta
             prefix.
           * ACTIVE → stash JSON body in ``_pending_notification_meta``
             for ``SessionManager.send()`` to pick up.
           * ASLEEP → wake to IDLE, splice the pair, post
             ``MSG_TC_WAKE``.

        From the LLM's viewpoint the wake path is indistinguishable
        from a voluntary tool call: the agent appears to have called
        ``system(action="notification")``, gotten the digest back, and
        is now responding to it.  The ``_synthesized: true`` field in
        the JSON body is the only tell, and only if the agent
        introspects it.

        The fingerprint is committed only when injection succeeds (or
        when in a state that cannot inject — STUCK/SUSPENDED/empty).
        If injection is blocked (e.g. ``has_pending_tool_calls()``),
        the fingerprint stays at its prior value and the next heartbeat
        tick retries.
        """
        from ..notifications import notification_fingerprint, collect_notifications

        fp = notification_fingerprint(self._working_dir)
        if fp == self._notification_fp:
            return

        notifications = collect_notifications(self._working_dir)
        prior_block_id = self._notification_block_id

        # --- Strip prior block ---
        if prior_block_id is not None and self._chat is not None:
            try:
                self._chat.interface.remove_pair_by_call_id(prior_block_id)
            except Exception:
                pass
            self._notification_block_id = None

        if not notifications:
            # All cleared — wire now has zero notification blocks.
            self._notification_fp = fp
            return

        # --- Inject new block based on current state ---
        from ..state import AgentState

        inject_ok = False

        if self._state == AgentState.ASLEEP:
            # Notification arrival wakes the agent, then inject as IDLE.
            # The synthesized (call, result) pair impersonates a
            # voluntary system(action="notification") call; MSG_TC_WAKE
            # unblocks the run loop so _handle_tc_wake drives one
            # inference round off the existing wire (no fake user
            # input, no meta prefix).
            #
            # If the wire has pending tool_calls left over from an
            # earlier turn that exited mid-sequence (e.g. AED-exhausted
            # ASLEEP after a stuck LLM call), `_inject_notification_pair`
            # would refuse the append to preserve alternation. Heal the
            # wire first by closing those pending calls with synthetic
            # error results, then retry. If injection STILL fails after
            # healing, revert state to ASLEEP so the inbox doesn't
            # deadlock in IDLE with no MSG_TC_WAKE pending — the next
            # heartbeat tick will see the same fingerprint and retry.
            self._asleep.clear()
            self._cancel_event.clear()
            self._set_state(AgentState.IDLE, reason="notification_arrival")
            self._reset_uptime()
            inject_ok = self._inject_notification_pair(notifications)
            if not inject_ok:
                self._heal_pending_tool_calls(reason="wake_inject_blocked")
                inject_ok = self._inject_notification_pair(notifications)
            if inject_ok:
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_arrival")
                except Exception:
                    pass
            else:
                # Could not inject even after healing — revert to ASLEEP
                # so state reflects reality. Without this, the agent
                # would sit in IDLE with no wake message and the run
                # loop would block on inbox.get() indefinitely.
                self._asleep.set()
                self._set_state(
                    AgentState.ASLEEP,
                    reason="wake_aborted_inject_failed",
                )

        elif self._state == AgentState.IDLE:
            # Strip + reinject AND post MSG_TC_WAKE.  IDLE is "between
            # turns, run loop blocked on inbox.get()" — without a wake
            # message the loop sits forever, the wire pair never goes
            # to the LLM, and the agent appears unresponsive even
            # though the notification arrived.
            #
            # _handle_tc_wake (post-rewrite) drives the wire forward
            # without appending anything: the (call, result) pair we
            # just spliced IS the new turn from the agent's
            # perspective.  No fake user input, no meta prefix.
            #
            # Same heal-and-retry as the ASLEEP branch: if the wire
            # has dangling tool_calls, close them synthetically and
            # retry, otherwise the IDLE inbox stays dead.
            inject_ok = self._inject_notification_pair(notifications)
            if not inject_ok:
                self._heal_pending_tool_calls(reason="idle_inject_blocked")
                inject_ok = self._inject_notification_pair(notifications)
            if inject_ok:
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_sync")
                except Exception:
                    pass

        elif self._state == AgentState.ACTIVE:
            # Stash for injection at request-send time (meta on latest
            # ToolResultBlock).  Same envelope shape as IDLE pair so the
            # agent sees one signal regardless of delivery path.
            body = {"_synthesized": True, "notifications": notifications}
            self._pending_notification_meta = json.dumps(
                body, indent=2, ensure_ascii=False
            )
            inject_ok = True
            self._log(
                "notification_stashed_active",
                sources=list(notifications.keys()),
            )

        # STUCK / SUSPENDED — no injection.  The on-disk state is
        # observed; we just can't act on it until state recovers.

        # --- Commit fingerprint only if injection succeeded ---
        if inject_ok or self._state not in (
            AgentState.IDLE, AgentState.ASLEEP
        ):
            self._notification_fp = fp

    def _heal_pending_tool_calls(self, *, reason: str) -> bool:
        """Close any unanswered tool_calls on the wire with synthetic
        error results so subsequent appends respect the alternation
        invariant.

        Used by the notification-sync wake path: if a previous turn
        exited mid-tool-sequence (AED-exhausted, kernel exception, etc.)
        and left dangling tool_calls, ``_inject_notification_pair``
        refuses to append. Without healing, the agent is stuck —
        notifications keep arriving, the inject keeps failing, and the
        run loop never gets a MSG_TC_WAKE. Heal once on wake so the
        retry can succeed.

        Returns True if anything was closed, False if the wire was
        already clean (or the session isn't ready, in which case there's
        nothing we can do here).
        """
        if self._chat is None:
            return False
        iface = self._chat.interface
        if not iface.has_pending_tool_calls():
            return False
        try:
            iface.close_pending_tool_calls(reason=f"heal:{reason}")
        except Exception as e:
            self._log("heal_pending_tool_calls_failed", reason=reason, error=str(e)[:200])
            return False
        self._log("heal_pending_tool_calls", reason=reason)
        try:
            self._save_chat_history(ledger_source="heal")
        except Exception:
            pass
        return True

    def _inject_notification_pair(self, notifications: dict) -> bool:
        """Inject a synthetic (call, result) pair for IDLE / ASLEEP states.

        Builds ``system(action="notification")`` / ``<JSON dict>`` and
        appends to the wire interface.  Records the call_id for later
        stripping.

        The assistant turn carries a ``TextBlock`` summary alongside the
        synthetic ``ToolCallBlock`` — e.g. ``"通知至：3 email, 1 soul"`` —
        which (a) is honest about what's on the wire from the agent's
        introspective POV and (b) provides substantive prefix novelty
        that defeats DeepSeek's cache fast-path empty-response failure
        mode (see deepseek/adapter.py for the protocol-level fix; this
        is the semantic-layer companion).

        The ``ToolResultBlock`` is created with ``synthesized=True``
        (the existing flag the kernel already uses for heal-path
        placeholders).  The result content also carries a top-level
        ``_synthesized: true`` field in its JSON body so the agent can
        distinguish kernel-injected reads from voluntary calls when
        reading conversation history.

        Both call.args and result.content carry meta blocks matching what
        real tool calls produce (build_meta → current_time, context,
        stamina_left_seconds, plus a monotonic injection_seq). This makes
        every synthesized pair tokenize uniquely even when the underlying
        notification payload repeats — a second protection layer against
        the DeepSeek cache fast-path empty-response failure beyond the
        TextBlock prefix novelty.

        Returns True if injection succeeded, False if it had to abort
        (e.g. pending tool_calls block append).  When False is returned,
        the caller MUST NOT update ``_notification_fp`` — otherwise the
        change would be silently dropped instead of retried.
        """
        import secrets
        from ..llm.interface import TextBlock, ToolCallBlock, ToolResultBlock

        if self._chat is None:
            try:
                self._session.ensure_session()
            except Exception as e:
                self._log("notification_inject_aborted",
                          reason="ensure_session_failed", error=str(e)[:200])
                return False
            if self._chat is None:
                self._log("notification_inject_aborted",
                          reason="chat_still_none_after_ensure")
                return False

        iface = self._chat.interface
        # If the wire has unanswered tool_calls, appending a user-role
        # result entry would violate the alternation invariant.  Defer.
        if iface.has_pending_tool_calls():
            self._log("notification_inject_aborted",
                      reason="pending_tool_calls",
                      sources=list(notifications.keys()))
            return False

        call_id = f"notif_{int(time.time()*1000):x}_{secrets.token_hex(2)}"

        # Meta block — same shape real tool results carry (current_time +
        # context + stamina_left_seconds, via build_meta), embedded in BOTH
        # call.args and result.content so every synthesized pair tokenizes
        # uniquely even when the notification payload repeats. The monotonic
        # injection_seq is added on top to guarantee novelty within the same
        # second (heal+retry tight loops, time-blind agents).
        # Defensive try/except + getattr cover test doubles that bypass
        # __init__ and don't carry the full agent attribute surface.
        self._notification_inject_seq = getattr(self, "_notification_inject_seq", 0) + 1
        try:
            meta = build_meta(self)
        except (AttributeError, TypeError):
            meta = {}
        meta["injection_seq"] = self._notification_inject_seq

        body = {
            "_synthesized": True,
            "notifications": notifications,
        }
        # Flatten meta into body top-level — matches real tool results
        # (status/result fields then current_time/context/stamina_left_seconds
        # at the same level), so the model sees the same shape it's used to.
        body.update(meta)
        content_json = json.dumps(body, indent=2, ensure_ascii=False)

        # Build a per-source summary: "3 email, 1 soul, 0 system".
        # Counts come from data.count / len(data.events) / len(data.voices)
        # depending on the producer; fall back to "?" if unparseable.
        summary_parts = []
        for source, payload in notifications.items():
            count = None
            if isinstance(payload, dict):
                data = payload.get("data") or {}
                if isinstance(data, dict):
                    count = data.get("count")
                    if count is None and isinstance(data.get("events"), list):
                        count = len(data["events"])
                    if count is None and isinstance(data.get("voices"), list):
                        count = len(data["voices"])
            summary_parts.append(f"{count if count is not None else '?'} {source}")
        summary_text = (
            f"[synthesized — kernel notification sync] "
            f"通知至：{'，'.join(summary_parts)}。"
            if summary_parts
            else "[synthesized — kernel notification sync] 通知至。"
        )

        text_block = TextBlock(text=summary_text)
        # call.args carries injection_seq only — real tool calls don't have
        # current_time/context/stamina in their args (those live in results).
        # The seq is enough to defeat byte-equality on the assistant turn.
        call_block = ToolCallBlock(
            id=call_id,
            name="system",
            args={
                "action": "notification",
                "injection_seq": self._notification_inject_seq,
            },
        )
        result_block = ToolResultBlock(
            id=call_id,
            name="system",
            content=content_json,
            synthesized=True,
        )

        iface.add_assistant_message(content=[text_block, call_block])
        iface.add_tool_results([result_block])
        self._notification_block_id = call_id
        self._save_chat_history(ledger_source="notification_sync")
        self._log(
            "notification_pair_injected",
            call_id=call_id,
            sources=list(notifications.keys()),
            summary=summary_text,
            meta=meta,
        )
        return True

    def _inject_notification_meta(self, message):
        """ACTIVE-state: prepend notification JSON to a recent ToolResultBlock.

        Called from ``SessionManager.send()`` before the API call.  Walks
        the wire backwards looking for the most recent ``ToolResultBlock``.
        Dict-content blocks are serialized to JSON before prepending.
        Prepends the ``notifications:\\n<json>\\n\\n`` prefix to the
        target result, stripping any stale prefix from older results.

        If no ``ToolResultBlock`` exists at all, leaves
        ``_pending_notification_meta`` set and the next ``send()``
        retries.

        Returns the (possibly unchanged) message.
        """
        from ..llm.interface import ToolResultBlock

        if self._pending_notification_meta is None:
            return message
        if self._chat is None:
            return message

        iface = self._chat.interface
        notif_prefix = (
            f"{self._NOTIF_PREFIX_LEAD}{self._pending_notification_meta}\n\n"
        )

        # Walk backwards to find the most recent user entry whose content
        # contains a ToolResultBlock (str or dict content).
        target_entry = None
        target_block = None
        for entry in reversed(iface.entries):
            if entry.role != "user":
                continue
            for block in entry.content:
                if isinstance(block, ToolResultBlock):
                    target_entry = entry
                    target_block = block
                    break
            if target_block is not None:
                break

        if target_block is None:
            # No ToolResultBlocks at all.
            self._log(
                "notification_meta_deferred",
                reason="no_tool_result",
            )
            return message

        # If the target block has dict content, serialize to JSON string.
        if isinstance(target_block.content, dict):
            target_block.content = json.dumps(
                target_block.content, ensure_ascii=False
            )

        # Strip notification prefix from ALL OTHER user ToolResultBlocks.
        for entry in iface.entries:
            if entry.role != "user":
                continue
            for block in entry.content:
                if block is target_block:
                    continue
                if isinstance(block, ToolResultBlock) and isinstance(
                    block.content, str
                ):
                    block.content = self._strip_notification_prefix(block.content)

        # Strip-and-reinject on the target.
        cleaned = self._strip_notification_prefix(target_block.content)
        target_block.content = notif_prefix + cleaned

        self._pending_notification_meta = None
        self._log(
            "notification_meta_injected",
            entry_id=target_entry.id,
        )
        return message

    def _persist_soul_entry(self, result: dict, mode: str = "flow", source: str = "agent") -> None:
        from ..intrinsics.soul.flow import _persist_soul_entry
        _persist_soul_entry(self, result, mode=mode, source=source)

    def _append_soul_flow_record(self, record: dict) -> None:
        from ..intrinsics.soul.flow import _append_soul_flow_record
        _append_soul_flow_record(self, record)

    def _run_inquiry(self, question: str, source: str = "agent") -> None:
        from ..intrinsics.soul.inquiry import _run_inquiry
        _run_inquiry(self, question, source=source)

    def _flatten_v3_for_pair(self, voice: dict) -> dict:
        from ..intrinsics.soul.flow import _flatten_v3_for_pair
        return _flatten_v3_for_pair(self, voice)

    def _run_consultation_fire(self) -> None:
        from ..intrinsics.soul.flow import _run_consultation_fire
        _run_consultation_fire(self)

    def _rehydrate_appendix_tracking(self) -> None:
        from ..intrinsics.soul.flow import _rehydrate_appendix_tracking
        _rehydrate_appendix_tracking(self)

    # ------------------------------------------------------------------
    # Heartbeat (pass-throughs to lifecycle.py)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        from .lifecycle import _start_heartbeat
        _start_heartbeat(self)

    def _stop_heartbeat(self) -> None:
        from .lifecycle import _stop_heartbeat
        _stop_heartbeat(self)

    def _heartbeat_loop(self) -> None:
        from .lifecycle import _heartbeat_loop
        _heartbeat_loop(self)

    # ------------------------------------------------------------------
    # Main loop (pass-throughs to turn.py)
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        from .turn import _run_loop
        _run_loop(self)

    def _concat_queued_messages(self, msg: Message) -> Message:
        from .turn import _concat_queued_messages
        return _concat_queued_messages(self, msg)

    def _handle_message(self, msg: Message) -> None:
        from .turn import _handle_message
        _handle_message(self, msg)

    def _handle_request(self, msg: Message) -> None:
        from .turn import _handle_request
        _handle_request(self, msg)

    def _handle_tc_wake(self, msg: Message) -> None:
        from .turn import _handle_tc_wake
        _handle_tc_wake(self, msg)

    def _get_guard_limits(self) -> tuple[int, int, int]:
        from .turn import _get_guard_limits
        return _get_guard_limits(self)

    def _process_response(self, response, *, ledger_source: str = "main") -> dict:
        from .turn import _process_response
        return _process_response(self, response, ledger_source=ledger_source)

    # ------------------------------------------------------------------
    # Refresh / preset (pass-throughs to lifecycle.py)
    # ------------------------------------------------------------------

    def _perform_refresh(self) -> None:
        from .lifecycle import _perform_refresh
        _perform_refresh(self)

    def _activate_preset(self, name: str) -> None:
        """Swap to a named preset — override in subclasses that support presets.

        BaseAgent raises NotImplementedError; Agent (lingtai.agent) overrides
        this with the real implementation.
        """
        raise NotImplementedError(
            f"_activate_preset not supported on {type(self).__name__}"
        )

    def _can_fallback_preset(self) -> bool:
        from .lifecycle import _can_fallback_preset
        return _can_fallback_preset(self)

    def _activate_default_preset(self) -> None:
        """Override hook — Agent subclass implements via _activate_preset(default).
        BaseAgent stub raises NotImplementedError."""
        raise NotImplementedError(
            "_activate_default_preset must be implemented by Agent subclass"
        )

    def _build_launch_cmd(self) -> list[str] | None:
        """Return the command to relaunch this agent. Override in subclasses."""
        return None

    # ------------------------------------------------------------------
    # Tool dispatch (pass-throughs to tools.py)
    # ------------------------------------------------------------------

    def _dispatch_tool(self, tc: ToolCall) -> dict:
        from .tools import _dispatch_tool
        return _dispatch_tool(self, tc)

    def _refresh_tool_inventory_section(self) -> None:
        from .tools import _refresh_tool_inventory_section
        _refresh_tool_inventory_section(self)

    def _build_tool_schemas(self) -> list[FunctionSchema]:
        from .tools import _build_tool_schemas
        return _build_tool_schemas(self)

    def has_capability(self, name: str) -> bool:
        from .tools import _has_capability
        return _has_capability(self, name)

    def add_tool(
        self,
        name: str,
        *,
        schema: dict | None = None,
        handler: Callable[[dict], dict] | None = None,
        description: str = "",
        system_prompt: str = "",
    ) -> None:
        from .tools import _add_tool
        _add_tool(self, name, schema=schema, handler=handler, description=description, system_prompt=system_prompt)

    def remove_tool(self, name: str) -> None:
        from .tools import _remove_tool
        _remove_tool(self, name)

    def override_intrinsic(self, name: str) -> Callable[[dict], dict]:
        from .tools import _override_intrinsic
        return _override_intrinsic(self, name)

    # ------------------------------------------------------------------
    # Prompt (pass-throughs to prompt.py)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        from .prompt import _build_system_prompt
        return _build_system_prompt(self)

    def _build_system_prompt_batches(self) -> list[str]:
        from .prompt import _build_system_prompt_batches
        return _build_system_prompt_batches(self)

    def _flush_system_prompt(self) -> None:
        from .prompt import _flush_system_prompt
        _flush_system_prompt(self)

    def update_system_prompt(
        self, section: str, content: str, *, protected: bool = False
    ) -> None:
        from .prompt import _update_system_prompt
        _update_system_prompt(self, section, content, protected=protected)

    def _check_rules_file(self) -> None:
        from .lifecycle import _check_rules_file
        _check_rules_file(self)

    # ------------------------------------------------------------------
    # Identity / status (pass-throughs to identity.py)
    # ------------------------------------------------------------------

    def _build_manifest(self) -> dict:
        from .identity import _build_manifest
        return _build_manifest(self)

    def status(self) -> dict:
        from .identity import _status
        return _status(self)

    # ------------------------------------------------------------------
    # Messaging (pass-throughs)
    # ------------------------------------------------------------------

    def mail(self, address: str, message: str, subject: str = "") -> dict:
        from .messaging import _mail
        return _mail(self, address, message, subject)

    def send(self, content: str | dict, sender: str = "user") -> None:
        from .messaging import _send
        _send(self, content, sender)

    # ------------------------------------------------------------------
    # Session persistence (delegates to SessionManager)
    # ------------------------------------------------------------------

    def get_token_usage(self) -> dict:
        """Return token usage summary (delegates to SessionManager)."""
        if not hasattr(self, "_session"):
            return {
                "input_tokens": 0, "output_tokens": 0,
                "thinking_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "api_calls": 0,
                "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
                "ctx_history_tokens": 0, "ctx_total_tokens": 0,
            }
        return self._session.get_token_usage()

    def get_chat_state(self) -> dict:
        """Serialize current chat session for persistence."""
        return self._session.get_chat_state()

    def restore_chat(self, state: dict) -> None:
        """Restore or create a chat session from saved state."""
        self._session.restore_chat(state)

    def restore_token_state(self, state: dict) -> None:
        """Restore cumulative token counters from a saved session."""
        self._session.restore_token_state(state)

    def _save_chat_history(self, *, ledger_source: str = "main") -> None:
        """Write chat history and token usage to disk (no git commit).

        Called after every completed interaction for crash resilience.
        Git commits are handled by the periodic snapshot system.

        ``ledger_source`` tags any token-ledger entry written for the
        most recent LLM round-trip. Default ``"main"`` covers the bulk
        of callers. Set to ``"tc_wake"`` from involuntary splice paths
        so consultation cadence does not double-count splices as main turns.
        """
        history_dir = self._working_dir / "history"
        history_dir.mkdir(exist_ok=True)
        try:
            state = self.get_chat_state()
            if state and state.get("messages"):
                lines = [json.dumps(entry, ensure_ascii=False) for entry in state["messages"]]
                (history_dir / "chat_history.jsonl").write_text("\n".join(lines) + "\n")
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to save chat history: {e}")
        # Update .agent.json with current state
        try:
            self._workdir.write_manifest(self._build_manifest())
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to update manifest: {e}")
        # Write .status.json — live runtime snapshot consumed by TUI/portal
        try:
            (self._working_dir / ".status.json").write_text(
                json.dumps(self.status(), ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write .status.json: {e}")
        # Append per-call token usage to ledger
        usage, self._last_usage = self._last_usage, None
        if usage is not None:
            try:
                ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
                model = getattr(self._session, "_model", None) or getattr(self.service, "model", None)
                endpoint = getattr(self.service, "_base_url", None)
                append_token_entry(
                    ledger_path,
                    input=usage.input_tokens,
                    output=usage.output_tokens,
                    thinking=usage.thinking_tokens,
                    cached=usage.cached_tokens,
                    model=model,
                    endpoint=endpoint,
                    extra={"source": ledger_source},
                )
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to append token ledger: {e}")

    # ------------------------------------------------------------------
    # Hooks (overridable by subclasses)
    # ------------------------------------------------------------------

    def _cpr_agent(self, address: str) -> "BaseAgent | None":
        """Resuscitate a suspended agent at *address*.

        Returns the resuscitated agent, or None if not supported.
        Override in subclasses (e.g. lingtai's Agent) to provide
        full reconstruction from persisted working dir state.
        """
        return None

    def _pre_request(self, msg: Message) -> str:
        """Transform message content before sending to LLM.

        Returns the content string to send.
        """
        return msg.content if isinstance(msg.content, str) else json.dumps(msg.content)

    def _post_request(self, msg: Message, result: dict) -> None:
        """Called after _process_response.

        Override in subclasses for post-processing.
        """

    def _on_tool_result_hook(
        self, tool_name: str, tool_args: dict, result: dict
    ) -> str | None:
        """Hook called after each tool execution.

        If this returns a non-None string, the current request processing
        returns immediately with that string as the result text.
        """
        return None
