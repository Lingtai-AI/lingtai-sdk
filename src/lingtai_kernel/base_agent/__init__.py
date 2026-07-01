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

import copy
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import AgentConfig
from ..state import AgentState
from ..workdir import WorkingDir
from ..message import Message
from ..intrinsics import ALL_INTRINSICS
from ..prompt import SystemPromptManager
from ..llm import (
    FunctionSchema,
    LLMService,
    ToolCall,
)
from ..logging import get_logger
from ..meta_block import (
    TOOL_META_CONTEXT_EVENT_PENDING_KEY,
    TOOL_META_CONTEXT_PENDING_KEY,
    build_meta,
    build_notification_payload,
    formal_tool_result_preview,
    formal_tool_result_visible_len,
)
from ..session import SessionManager
from ..tc_inbox import TCInbox
from ..token_ledger import append_token_entry
from ..trace_redaction import redact_for_trajectory
from ..runtime_identity import runtime_identity_event_fields

logger = get_logger()


def _block_type_name(block: object) -> str:
    """Return a compact, safe block type label for diagnostics."""
    try:
        data = block.to_dict()  # type: ignore[attr-defined]
        btype = data.get("type") if isinstance(data, dict) else None
        if isinstance(btype, str) and btype:
            return btype
    except Exception:
        pass
    name = type(block).__name__
    if name.endswith("Block"):
        name = name[:-5]
    return name[:80]


def _pending_tool_call_diagnostics(iface, *, tail_limit: int = 3) -> dict:
    """Bounded, argument-free diagnostics for a pending tool-call tail."""
    entries = list(getattr(iface, "entries", None) or [])
    tail_entries = entries[-tail_limit:]
    tail = entries[-1] if entries else None
    pending_calls = []
    if getattr(tail, "role", None) == "assistant":
        pending_calls = [
            block
            for block in getattr(tail, "content", []) or []
            if hasattr(block, "id") and hasattr(block, "name") and hasattr(block, "args")
        ]

    return {
        "pending_tool_call_count": len(pending_calls),
        "pending_tool_call_ids": [getattr(call, "id", None) for call in pending_calls],
        "pending_tool_names": [getattr(call, "name", None) for call in pending_calls],
        "pending_tail_roles": [getattr(entry, "role", None) for entry in tail_entries],
        "pending_tail_block_types": [
            [_block_type_name(block) for block in (getattr(entry, "content", []) or [])]
            for entry in tail_entries
        ],
    }


# Issue #164 — event types that count as "the agent made forward
# progress." Bumping ``_last_progress_at`` on these gives the ACTIVE-
# without-progress watchdog a single, robust signal that survives
# refactors of individual call sites: every progress event already calls
# ``_log()``. Each entry's value is the active-turn ``kind`` to record
# (``None`` means "leave kind alone").
_PROGRESS_EVENTS: dict[str, str | None] = {
    "wake": "wake",
    "tc_wake_continue": "wake",
    "llm_call": "llm_call",
    "llm_response": None,  # progress, but turn kind stays "llm_call"
    "tool_call": "tool_call",
    "tool_result": None,
    "notification_pair_injected": "notification_injection",
    "turn_cancelled_post_tool": None,
}


# ---------------------------------------------------------------------------
# Identity prompt section (curated prose)
# ---------------------------------------------------------------------------



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
    if soul_delay is not None:
        lines.append(f"Your soul flow fires {soul_delay}s after you go idle.")
    if mailbox_name:
        lines.append(f"You receive messages via {mailbox_name}.")

    # Runtime LLM identity — provider/model/endpoint as the agent runs.
    # Sourced from `manifest_data["llm"]` (sanitized at build time —
    # see identity.py `_safe_llm_from_service` and wrapper `Agent._build_manifest`).
    # Rendered as a single line so it sits in the cacheable prefix without
    # adding much weight; missing fields are silently skipped.
    llm = manifest_data.get("llm") or {}
    if isinstance(llm, dict):
        model = _identity_scalar(llm.get("model"))
        provider = _identity_scalar(llm.get("provider"))
        base_url = _identity_scalar(llm.get("base_url"))
        if provider or model:
            bits = []
            if model:
                bits.append(f"model `{model}`")
            if provider:
                bits.append(f"provider `{provider}`")
            if base_url:
                bits.append(f"endpoint `{base_url}`")
            if bits:
                lines.append("You are running on " + ", ".join(bits) + ".")

    # Active preset — only the wrapper agent has a preset surface, so this
    # block is silent for bare BaseAgent instances. Reports the active path
    # plus the default if the two differ (lets the agent see when it's on a
    # non-default preset). Allowed list is intentionally omitted from the
    # prompt — it's structural metadata, not identity prose.
    preset = manifest_data.get("preset") or {}
    if isinstance(preset, dict):
        active = _identity_scalar(preset.get("active"))
        default = _identity_scalar(preset.get("default"))
        if active:
            if default and default != active:
                lines.append(
                    f"Your active preset is `{active}` "
                    f"(default `{default}`)."
                )
            else:
                lines.append(f"Your active preset is `{active}`.")

    return "\n".join(lines)


def _identity_scalar(value) -> str:
    """Return prompt-safe scalar text for identity metadata, else empty string."""
    if isinstance(value, str):
        return value if value else ""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


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
        self._idle_since_monotonic: float | None = time.monotonic()
        self._started_at: str = ""
        self._last_usage = None  # UsageMetadata from last LLM call, for ledger
        self._created_at: str = ""
        self._uptime_anchor: float | None = None  # set in start(), None means not started
        self._runtime_identity_event_fields = runtime_identity_event_fields()

        # Working directory (caller-owned path)
        self._workdir = WorkingDir(working_dir)
        self._working_dir = self._workdir.path

        # LoggingService: JSONL is the source of truth; SQLite is an additive,
        # fail-open sidecar index for queryable history.
        from ..services.logging import CompositeLoggingService, JSONLLoggingService, SQLiteEventIndex
        log_dir = self._working_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        jsonl_log_service = JSONLLoggingService(
            log_dir / "events.jsonl",
            ensure_ascii=self._config.ensure_ascii,
        )
        self._log_service = CompositeLoggingService(
            jsonl_log_service,
            sqlite_index=SQLiteEventIndex(log_dir / "log.sqlite", ensure=False, keep_open=False),
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

        # The kernel-owned section mirrors (principle/substrate/procedures) may
        # carry skill-style YAML frontmatter on disk — developer-facing metadata
        # that must never reach the LLM prompt. Strip it on read so the section
        # the prompt manager renders is body-only. Covenant mirrors are operator
        # content with no frontmatter, but stripping is a no-op there too.
        from .._frontmatter import strip_frontmatter as _strip_frontmatter

        # Covenant: constructor value wins, then fall back to file on disk
        if covenant:
            covenant_file.write_text(covenant)
        elif covenant_file.is_file():
            covenant = _strip_frontmatter(covenant_file.read_text(encoding="utf-8"))

        # Principle: constructor value wins, then fall back to file on disk
        if principle:
            principle_file.write_text(principle)
        elif principle_file.is_file():
            principle = _strip_frontmatter(principle_file.read_text(encoding="utf-8"))

        # Substrate: lower-level BaseAgent seed/fallback. The init.json
        # contract is enforced by lingtai.agent.Agent, where substrate is
        # kernel-owned and not an external override.
        if substrate:
            substrate_file.write_text(substrate)
        elif substrate_file.is_file():
            substrate = _strip_frontmatter(substrate_file.read_text(encoding="utf-8"))

        # Procedures: same pattern as covenant/principle
        if procedures:
            procedures_file.write_text(procedures)
        elif procedures_file.is_file():
            procedures = _strip_frontmatter(procedures_file.read_text(encoding="utf-8"))

        # Brief: disk-owned context (normally written by secretary/briefing
        # flows). Init.json brief overrides are retired at the Agent wrapper.
        if brief and not brief_file.is_file():
            brief_file.write_text(brief)
        elif brief_file.is_file():
            brief = brief_file.read_text(encoding="utf-8")

        # Pad: constructor value seeds the file if it doesn't exist
        if pad and not pad_file.is_file():
            pad_file.write_text(pad)

        # Auto-load pad from file into prompt manager
        loaded_pad = ""
        if pad_file.is_file():
            loaded_pad = pad_file.read_text(encoding="utf-8")

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
                rules_content = rules_md.read_text(encoding="utf-8").strip()
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
        # notification pairs. Bounce/MCP/soul events publish their own
        # `.notification/*.json` files and don't need per-ref tracking.

        # LLM worker poison state. Set when WorkerStillRunningError means the
        # current in-memory ChatInterface may still be mutated by a worker
        # thread. Process-local only; refresh/relaunch restores from disk.
        self._llm_worker_interface_poisoned: bool = False
        self._llm_worker_poison_reason: str | None = None
        self._llm_worker_poison_artifact: str | None = None
        self._llm_worker_poisoned_at: str | None = None
        self._llm_worker_poison_turn_entry: str | None = None
        self._llm_worker_refresh_requested: bool = False
        self._llm_worker_refresh_source: str | None = None

        # Notification sync state (filesystem-as-protocol redesign).
        # _notification_fp: last-seen `.notification/` fingerprint for
        #   change-detection between heartbeat ticks.
        # _notification_block_id: call_id of the most recently injected
        #   synthesized pair — kept for informational/molt-reset purposes;
        #   no longer used for remove_pair_by_call_id (pairs are now
        #   skeletonized in-place, not deleted).
        # See notifications.py and notification-filesystem-redesign.md.
        self._notification_fp: tuple = ()
        # Protects read-modify-write updates and guarded clears for the
        # shared `.notification/system.json` channel.
        self._system_notification_lock: threading.Lock = threading.Lock()
        # Last ACTIVE-state notification fingerprint that has already emitted
        # ``notification_deferred_active``.  This is intentionally separate
        # from ``_notification_fp``: ACTIVE must keep the delivery fingerprint
        # uncommitted so the next IDLE boundary retries, but the log should not
        # repeat the same status echo on every heartbeat.
        self._notification_deferred_log_fp: tuple = ()
        self._notification_block_id: str | None = None
        # Monotonic counter ensuring every synthesized notification pair
        # carries unique tokens (timestamp + seq) even when the underlying
        # payload repeats — defeats DeepSeek's cache fast-path empty-completion
        # failure mode on byte-identical synthetic pairs.
        self._notification_inject_seq: int = 0
        # Unified live notification holder — points to whichever dict
        # currently carries the live notification payload.  May be:
        #   * a normal tool-result content dict (ACTIVE path), or
        #   * a synthesized pair's result content dict (IDLE path).
        # Only ONE holder exists at a time.  When a new holder is
        # registered, the old one is skeletonized in-place so history
        # never accumulates stale notification data across results.
        # See `meta_block.skeletonize_notification_holder` and
        # `meta_block.attach_active_notifications`.
        #
        # The notification payload is SPARSE / update-driven (mirrors the #618
        # `agent_meta` cadence), not latest-result-only: while notifications stay
        # active but their material content is unchanged, the payload is NOT
        # chased onto every newest tool result — the prior holder keeps it.  It
        # only moves/re-stamps when the notification payload materially changes,
        # or when the target is a deliberate `notification(action="check")` read.
        self._notification_live_holder: dict | None = None
        # Material signature of the last emitted `_meta.notifications` payload;
        # the change gate for the sparse notification attach above.  ``None``
        # until the first active payload, and reset to ``None`` whenever
        # notifications go empty so a later reappearance attaches afresh.
        self._notification_payload_signature: str | None = None

        # Provider-visible tool result currently carrying the live `_meta.agent_meta`
        # / `_meta.guidance` blocks (kernel runtime state + guidance ref).
        # `agent_meta` is SPARSE / update-driven, not latest-result-only: it is
        # (re)attached only when the material agent snapshot changes since the
        # last emitted `agent_meta` (tracked by `_agent_meta_signature`).  When
        # the snapshot is unchanged it is NOT chased onto the newest result; the
        # prior holder keeps it as a historical update point.  When it changes,
        # the prior *live* holder sheds its blocks and the newer result takes
        # over.  See `meta_block.attach_active_runtime` / `agent_meta_signature`.
        self._runtime_live_holder: dict | None = None
        # Material signature of the last emitted `_meta.agent_meta`; the change
        # gate for the sparse attach above.  ``None`` until the first snapshot.
        self._agent_meta_signature: str | None = None

        # Large-result hint threshold (chars).  When a main-agent tool result's
        # serialized length exceeds this value it is treated as "large": the
        # ToolExecutor stamps a tool_meta.comment.overflow hint, and the result
        # is surfaced for summarization through
        # _meta.agent_meta.current_tool_result_chars.top_results.  Large results
        # no longer raise a `large_tool_result` system notification — see
        # meta_block.current_tool_result_chars and _maybe_notify_large_tool_result.
        # Default: 3000 chars.  Configurable only via manifest.summarize_notification_threshold
        # in init.json + refresh — runtime mutation is not supported.
        self._summarize_notification_threshold: int = 3000

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

        # Issue #164 — ACTIVE-without-progress watchdog.
        #
        # ``_state_changed_at`` records when the agent last transitioned
        # state (wall-clock seconds, ``time.time()``). ``_last_progress_at``
        # is bumped by any of the kernel's progress events — ``wake``,
        # ``tc_wake_continue``, ``llm_call``, ``llm_response``, ``tool_call``,
        # ``tool_result``, ``notification_pair_injected``, and state
        # transitions themselves. The heartbeat tick reads both: when
        # ``state == ACTIVE`` and no progress event has fired for longer
        # than ``LINGTAI_ACTIVE_STUCK_THRESHOLD_S`` (default 600s, ~10min),
        # we log ``active_without_progress`` once per condition so the
        # symptom Jason reported (ACTIVE wedged + notification_deferred
        # storm with no turn ever starting) is diagnosable from the event
        # log instead of requiring forensic cross-referencing.
        #
        # The watchdog deliberately does NOT auto-restart the agent — the
        # safest action across the failure modes we've seen is "make it
        # visible and let admin or .clear handle recovery." Auto-restart
        # without understanding the underlying race could mask real bugs
        # behind retries.
        now_wall = time.time()
        self._state_changed_at: float = now_wall
        self._last_progress_at: float = now_wall
        self._active_turn_kind: str | None = None
        self._active_turn_started_at: float | None = None
        self._active_turn_id: str | None = None
        #: Counts repeated ``notification_deferred_active`` events since
        #: the last successful injection. Reset on
        #: ``notification_pair_injected``. Surfaced in ``.status.json`` so
        #: the deferral storm in #164 shows up before the user notices.
        self._deferred_notifications_count: int = 0
        self._deferred_notifications_oldest_at: float | None = None
        #: One-shot latch so the watchdog logs exactly once per stuck
        #: episode. Cleared on any state transition out of ACTIVE.
        self._active_stuck_logged: bool = False

        # Snapshot — periodic git commits (Time Machine)
        self._last_snapshot: float = 0.0
        self._last_gc: float = 0.0

        # Auto-fallback state
        self._preset_fallback_attempted = False

        # Sent message tracker — dedup + idle-after-send for external channels
        from ..sent_message_tracker import SentMessageTracker
        self._sent_tracker = SentMessageTracker()

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
            tool_result_recovery_lookup_fn=self._recover_pending_tool_result,
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
        """Reset the uptime anchor for runtime uptime tracking."""
        from .lifecycle import _reset_uptime
        _reset_uptime(self)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and wait for the agent thread to exit."""
        from .lifecycle import _stop
        _stop(self, timeout)

    def _set_state(self, new_state: AgentState, reason: str = "") -> None:
        """Transition to a new state.

        Drives the soul cadence timer: the timer runs only while the
        agent is IDLE.  Entering IDLE starts a fresh ``soul_delay``-second
        timer; leaving IDLE (to ACTIVE, STUCK, ASLEEP, or SUSPENDED)
        cancels it.  The timer does NOT reschedule itself after firing —
        the next IDLE transition starts a fresh countdown.
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

        # Soul timer + hidden idle-timeout bookkeeping: IDLE-only.  Start on
        # entering IDLE, cancel/clear on leaving.
        if new_state == AgentState.IDLE:
            self._idle_since_monotonic = time.monotonic()
            _start_soul_timer(self)
        elif old == AgentState.IDLE:
            self._idle_since_monotonic = None
            _cancel_soul_timer(self)

        # Issue #164 — watchdog bookkeeping. A state transition is itself
        # forward progress, so reset the no-progress clock. The
        # one-shot stuck-logged latch is cleared whenever we leave ACTIVE
        # so the next stuck episode can be reported.
        now_wall = time.time()
        self._state_changed_at = now_wall
        self._last_progress_at = now_wall
        if new_state == AgentState.ACTIVE:
            # The kernel doesn't know yet what kind of turn this will be —
            # the next progress event (``wake``, ``tc_wake_continue``,
            # ``llm_call``, ``tool_call``) refines this. We seed with a
            # "pending" marker so .status.json never claims a turn is
            # already in flight when only the state flipped.
            self._active_turn_kind = "pending"
            self._active_turn_started_at = now_wall
            self._active_turn_id = None
        else:
            self._active_turn_kind = None
            self._active_turn_started_at = None
            self._active_turn_id = None
            self._active_stuck_logged = False

        self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
        self._workdir.write_manifest(self._build_manifest())

    def _wake_nap(self, reason: str) -> None:
        """Signal the nap to wake up with a given reason."""
        self._nap_wake_reason = reason
        self._nap_wake.set()

    def _note_notification_deferred_active(self, fp: tuple, *, sources: list[str]) -> None:
        """Record ACTIVE notification deferral without per-heartbeat log spam.

        ACTIVE deliberately leaves ``_notification_fp`` uncommitted so delivery
        retries at the next IDLE boundary.  Heartbeat ticks therefore rediscover
        the same filesystem fingerprint.  Keep watchdog counters accurate for
        every tick, but emit ``notification_deferred_active`` only once per
        distinct notification fingerprint.
        """
        self._deferred_notifications_count += 1
        if self._deferred_notifications_oldest_at is None:
            self._deferred_notifications_oldest_at = time.time()

        if fp == getattr(self, "_notification_deferred_log_fp", ()):
            return

        self._log(
            "notification_deferred_active",
            sources=sources,
            _deferred_counter_already_updated=True,
        )
        self._notification_deferred_log_fp = fp

    def _log(self, event_type: str, **fields) -> None:
        """Write a structured event to the logging service, if configured.

        Also updates issue #164 watchdog bookkeeping: known progress
        events bump ``_last_progress_at`` and may refine the active-turn
        kind/id, and ``notification_deferred_active`` events update the
        deferred-notification counters.
        """
        deferred_counter_already_updated = bool(
            fields.pop("_deferred_counter_already_updated", False)
        )

        # Watchdog bookkeeping — done before the actual log write so the
        # bookkeeping is in place even if the log service raises.
        if event_type in _PROGRESS_EVENTS:
            self._last_progress_at = time.time()
            kind = _PROGRESS_EVENTS[event_type]
            if kind is not None:
                self._active_turn_kind = kind
                self._active_turn_started_at = self._last_progress_at
            # ToolExecutor emits provider IDs as tool_call_id; older/manual
            # event producers may still use call_id. Surface either one so
            # status snapshots can tie back to events.jsonl.
            call_id = fields.get("tool_call_id") or fields.get("call_id")
            if isinstance(call_id, str):
                self._active_turn_id = call_id
        elif event_type == "notification_deferred_active":
            if not deferred_counter_already_updated:
                self._deferred_notifications_count += 1
                if self._deferred_notifications_oldest_at is None:
                    self._deferred_notifications_oldest_at = time.time()
        elif event_type == "agent_state":
            # Successful injection / state transitions reset the deferral
            # storm counter — the very next state change after a deferral
            # storm is exactly the recovery signal we want to note.
            if self._deferred_notifications_count:
                self._deferred_notifications_count = 0
                self._deferred_notifications_oldest_at = None

        if self._log_service:
            self._log_service.log({
                "type": event_type,
                "address": self._working_dir.name,
                "agent_name": self.agent_name,
                "ts": time.time(),
                **self._runtime_identity_event_fields,
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

    def _enqueue_system_notification(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        skip_if_ref_id_exists: bool = False,
        priority: str = "normal",
        extra: dict | None = None,
    ) -> str:
        from .messaging import _enqueue_system_notification
        return _enqueue_system_notification(
            self,
            source=source,
            ref_id=ref_id,
            body=body,
            skip_if_ref_id_exists=skip_if_ref_id_exists,
            priority=priority,
            extra=extra,
        )

    def notify(self, sender: str, text: str) -> None:
        from .messaging import _notify
        _notify(self, sender, text)

    def _rescan_large_tool_results(self) -> int:
        from .messaging import _rescan_large_tool_results
        return _rescan_large_tool_results(self)

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
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "tc_inbox_drain_skipped_poisoned_interface",
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return
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
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "tc_inbox_drain_skipped_poisoned_interface",
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
                from_hook=True,
            )
            return
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
    # See notifications.py for the notification filesystem design rationale.
    # ------------------------------------------------------------------

    def _sync_notifications(self) -> None:
        """Sync `.notification/` state into the wire.

        Computes the current fingerprint; if unchanged, no-op.  On change:
        1. Skeletonize the current live holder (if any) in-place — does NOT
           remove synthesized pairs from history.  Synthesized pairs are kept
           as placeholder skeletons; only normal tool-result dicts have their
           notification keys stripped.
        2. If the new collection is empty, commit the empty fingerprint and
           return.
        3. Otherwise, inject a new block appropriate for current state:

           * IDLE → splice ``(call, result)`` pair (impersonates a
             voluntary ``notification(action="check")`` call from the
             agent's perspective), post ``MSG_TC_WAKE`` so the run loop
             unblocks and ``_handle_tc_wake`` drives the next inference
             round off the existing wire — no fake user input, no meta
             prefix.
           * ACTIVE → defer without touching the wire or committing the
             fingerprint; the next IDLE boundary retries delivery via
             the ordinary synthetic pair path.
           * ASLEEP → wake to IDLE, splice the pair, post
             ``MSG_TC_WAKE``.

        Invariant: at most one result block in history carries live
        notification payload at any time.  Old synthesized pairs become
        skeleton placeholders but are never deleted — the conversation
        structure is preserved.

        The fingerprint is committed only when injection succeeds (or
        when in a state that cannot inject — STUCK/SUSPENDED/empty).
        If injection is blocked (e.g. ``has_pending_tool_calls()``),
        the fingerprint stays at its prior value and the next heartbeat
        tick retries.
        """
        from ..notifications import notification_fingerprint, collect_notifications
        from ..meta_block import skeletonize_notification_holder
        from .worker_recovery import (
            is_worker_interface_poisoned,
            request_worker_hang_refresh,
        )

        def _skip_poisoned_sync(*, phase: str) -> bool:
            """Fail closed: never touch a poisoned interface; request refresh."""
            if not is_worker_interface_poisoned(self):
                return False
            artifact = getattr(self, "_llm_worker_poison_artifact", None)
            self._log(
                "notification_sync_skipped_poisoned_interface",
                phase=phase,
                artifact=artifact,
                action="refresh_requested",
            )
            request_worker_hang_refresh(
                self,
                artifact_relpath=artifact,
                source="notification_sync",
            )
            return True

        fp = notification_fingerprint(self._working_dir)
        if fp == self._notification_fp:
            return

        if _skip_poisoned_sync(phase="before_collect"):
            return

        notifications = collect_notifications(self._working_dir)

        if not notifications:
            if _skip_poisoned_sync(phase="before_empty_skeletonize"):
                return
            # All channels cleared.  Skeletonize the current live holder
            # (whether it is a normal tool-result dict or a synthesized
            # pair content dict) so no history block keeps advertising
            # stale notification state.  Synthesized pairs remain in
            # history as placeholders; they are never deleted.
            skeletonize_notification_holder(self)
            self._notification_fp = fp
            self._notification_deferred_log_fp = ()
            return

        # --- Inject new block based on current state ---
        from ..state import AgentState

        inject_ok = False

        if self._state == AgentState.ASLEEP:
            if _skip_poisoned_sync(phase="asleep_before_wake"):
                return
            # Notification arrival wakes the agent, then inject as IDLE.
            # The synthesized (call, result) pair impersonates a
            # voluntary notification(action="check") call; MSG_TC_WAKE
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
            # healing, fall through to the degraded path below: stay
            # IDLE, deliver a degraded `MSG_REQUEST` that points the
            # agent at the recovery handles, and commit the fingerprint
            # so the same failure does not replay until on-disk state
            # changes.
            self._asleep.clear()
            self._cancel_event.clear()
            self._set_state(AgentState.IDLE, reason="notification_arrival")
            self._reset_uptime()
            # Old synthesized pairs are kept in history as placeholder
            # skeletons, not deleted.  Do not skeletonize the current holder
            # until this new injection succeeds; otherwise a blocked append
            # would discard the only live payload even though _notification_fp
            # remains uncommitted for retry.
            if _skip_poisoned_sync(phase="asleep_before_inject"):
                return
            inject_ok = self._inject_notification_pair(notifications)
            if not inject_ok:
                if _skip_poisoned_sync(phase="asleep_before_heal"):
                    return
                self._heal_pending_tool_calls(reason="wake_inject_blocked")
                if _skip_poisoned_sync(phase="asleep_before_reinject"):
                    return
                inject_ok = self._inject_notification_pair(notifications)
            if inject_ok:
                if _skip_poisoned_sync(phase="asleep_before_wake_enqueue"):
                    return
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_arrival")
                except Exception:
                    pass
            else:
                # Could not inject even after healing. Reverting to ASLEEP
                # without committing the fingerprint produced a livelock:
                # the next heartbeat tick saw the same .notification/
                # state, woke us again, failed inject again, reverted
                # again — forever (Jason's MCP/WeChat wake report).
                # Instead, stay IDLE and deliver a degraded MSG_REQUEST
                # that explains the situation and tells the agent how to
                # read the notification state directly. Commit the
                # fingerprint so the same failure does not replay.
                sources = sorted(notifications.keys())
                from ..message import _make_message, MSG_REQUEST
                degraded_text = (
                    "[system] Notification delivery could not be injected onto "
                    f"the wire after a heal attempt. Affected source(s): "
                    f"{', '.join(sources)}. Please query the current state by "
                    "calling notification(action=\"check\") or read the "
                    "producer files under .notification/ directly, then decide "
                    "whether to act. The kernel will not retry this delivery "
                    "until the on-disk state changes."
                )
                try:
                    self.inbox.put(_make_message(MSG_REQUEST, "system", degraded_text))
                    self._wake_nap("notification_arrival_degraded")
                except Exception:
                    pass
                self._log(
                    "notification_wake_degraded",
                    reason="inject_failed_after_heal",
                    sources=sources,
                )
                self._notification_fp = fp

        elif self._state == AgentState.IDLE:
            if _skip_poisoned_sync(phase="idle_before_inject"):
                return
            # Skeletonize + reinject AND post MSG_TC_WAKE.  IDLE is
            # "between turns, run loop blocked on inbox.get()" — without
            # a wake message the loop sits forever, the wire pair never
            # goes to the LLM, and the agent appears unresponsive even
            # though the notification arrived.
            #
            # _handle_tc_wake (post-rewrite) drives the wire forward
            # without appending anything: the (call, result) pair we
            # just spliced IS the new turn from the agent's perspective.
            # No fake user input, no meta prefix.
            #
            # Same heal-and-retry as the ASLEEP branch: if the wire has
            # dangling tool_calls, close them synthetically and retry,
            # otherwise the IDLE inbox stays dead.
            # Old synthesized pairs are kept in history as placeholder
            # skeletons, not deleted.  Do not skeletonize the current holder
            # until this new injection succeeds; otherwise a blocked append
            # would discard the only live payload even though _notification_fp
            # remains uncommitted for retry.
            inject_ok = self._inject_notification_pair(notifications)
            if not inject_ok:
                if _skip_poisoned_sync(phase="idle_before_heal"):
                    return
                self._heal_pending_tool_calls(reason="idle_inject_blocked")
                if _skip_poisoned_sync(phase="idle_before_reinject"):
                    return
                inject_ok = self._inject_notification_pair(notifications)
            if inject_ok:
                if _skip_poisoned_sync(phase="idle_before_wake_enqueue"):
                    return
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_sync")
                except Exception:
                    pass

        elif self._state == AgentState.ACTIVE:
            # Do not mutate unrelated tool results while a turn is active.
            # Leave the fingerprint uncommitted so the same on-disk
            # notification state is retried once the run loop transitions
            # to IDLE at the post-turn boundary.
            self._note_notification_deferred_active(
                fp,
                sources=list(notifications.keys()),
            )

        # STUCK / SUSPENDED — no injection.  The on-disk state is
        # observed; we just can't act on it until state recovers.

        # --- Commit fingerprint only if injection succeeded ---
        # ACTIVE deliberately defers without committing; only
        # STUCK/SUSPENDED commit here (they can't inject at all).
        if _skip_poisoned_sync(phase="before_fingerprint_commit"):
            return
        if inject_ok:
            self._notification_fp = fp
            self._notification_deferred_log_fp = ()
        elif self._state in (AgentState.STUCK, AgentState.SUSPENDED):
            self._notification_fp = fp
            self._notification_deferred_log_fp = ()

    def _heal_pending_tool_calls(self, *, reason: str) -> bool:
        """Close unanswered tool_calls so subsequent appends respect pairing.

        The close path first replays any matching durable real tool results
        from ``logs/events.jsonl``; calls without recorded results still get the
        existing synthetic error results.

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
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "heal_pending_tool_calls_skipped_poisoned_interface",
                reason=reason,
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return False
        if self._chat is None:
            return False
        iface = self._chat.interface
        try:
            iface.tool_result_recovery_lookup = self._recover_pending_tool_result
        except Exception:
            pass
        if not iface.has_pending_tool_calls():
            return False
        diagnostics = _pending_tool_call_diagnostics(iface)
        try:
            iface.close_pending_tool_calls(reason=f"heal:{reason}")
        except Exception as e:
            self._log(
                "heal_pending_tool_calls_failed",
                reason=reason,
                error=str(e)[:200],
                **diagnostics,
            )
            return False
        self._log("heal_pending_tool_calls", reason=reason, **diagnostics)
        try:
            self._save_chat_history(ledger_source="heal")
        except Exception:
            pass
        return True

    def _recover_pending_tool_result(self, tool_call):
        from ..tool_result_recovery import recover_tool_result_block_from_events

        return recover_tool_result_block_from_events(
            self._working_dir,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            logger_fn=self._log,
        )

    def _inject_notification_pair(self, notifications: dict) -> bool:
        """Inject a synthetic (call, result) pair for IDLE / ASLEEP states.

        Builds ``notification(action="check")`` / ``<JSON dict>`` and
        appends to the wire interface.  Records the call_id for later
        stripping.

        The synthesized pair is byte-shape-identical to a voluntary
        ``notification(action="check")`` read so the LLM cannot distinguish
        a kernel-injected delivery from one it issued itself; the
        ``_synthesized: true`` body flag remains the only marker.

        The assistant turn carries only the synthetic ``ToolCallBlock``.
        The model-visible notification details and guidance live in the
        matching ``ToolResultBlock`` body, so notification wakes do not
        surface as transcript text / diary-like synthesized summaries.

        The ``ToolResultBlock`` is created with ``synthesized=True``
        (the existing flag the kernel already uses for heal-path
        placeholders) and its ``content`` is a mutable dict (not a JSON
        string).  All adapters serialize dict content correctly via
        ``json.dumps``.  Storing a dict enables in-place skeletonization
        later: when the live payload moves to a newer result, the dict
        is mutated to the skeleton placeholder shape — the pair stays in
        history but carries no live data.  The ``_synthesized: true``
        field in the body lets the agent distinguish kernel-injected
        reads from voluntary calls when reading conversation history.

        Both call.args and result.content carry safe notification freshness
        fields from build_meta plus a monotonic injection_seq. Internal tool-meta
        transit keys are stripped below before the synthesized pair reaches the wire.
        This makes
        every synthesized pair tokenize uniquely even when the underlying
        notification payload repeats — a protection layer against the
        DeepSeek cache fast-path empty-response failure without needing a
        visible assistant text prefix.

        Returns True if injection succeeded, False if it had to abort
        (e.g. pending tool_calls block append).  When False is returned,
        the caller MUST NOT update ``_notification_fp`` — otherwise the
        change would be silently dropped instead of retried.
        """
        import secrets
        from ..llm.interface import ToolCallBlock, ToolResultBlock
        from .worker_recovery import is_worker_interface_poisoned

        if is_worker_interface_poisoned(self):
            self._log(
                "notification_inject_skipped_poisoned_interface",
                sources=list(notifications.keys()),
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return False

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
            # Issue #126 diagnostic: log the tail shape so we can trace
            # why tool results were not detected as committed.
            tail_info = ""
            if iface._entries:
                last = iface._entries[-1]
                tail_info = f" tail_role={last.role} tail_blocks={len(last.content)}"
                if last.role == "assistant":
                    tc_ids = [b.id[:20] for b in last.content
                              if hasattr(b, 'id') and hasattr(b, 'name')]
                    tail_info += f" tc_ids={tc_ids}"
            self._log("notification_inject_aborted",
                      reason="pending_tool_calls",
                      sources=list(notifications.keys()),
                      _tail=tail_info)
            return False

        call_id = f"notif_{int(time.time()*1000):x}_{secrets.token_hex(2)}"

        # Meta freshness fields — same build_meta current-state hints real tool
        # results use for runtime state snapshots, embedded in BOTH call.args
        # and result.content so every synthesized pair tokenizes
        # uniquely even when the notification payload repeats. The monotonic
        # injection_seq is added on top to guarantee novelty within the same
        # second (heal+retry tight loops, time-blind agents).
        # Defensive getattr covers test doubles that bypass __init__ and
        # don't carry the full agent attribute surface.
        self._notification_inject_seq = getattr(self, "_notification_inject_seq", 0) + 1
        try:
            meta = build_meta(self)
        except (AttributeError, TypeError):
            meta = {}
        # ``current_time`` and the ``_tool_meta_*`` transit keys are
        # permanent per-tool-result fields consumed by ToolExecutor.
        # Notification injections are synthesized pairs, not real tool results,
        # and already have injection_seq for freshness/novelty; never flatten
        # internal tool-meta transit payloads onto the model-visible wire.
        meta.pop("current_time", None)
        meta.pop(TOOL_META_CONTEXT_PENDING_KEY, None)
        meta.pop(TOOL_META_CONTEXT_EVENT_PENDING_KEY, None)
        meta["injection_seq"] = self._notification_inject_seq

        notifications_with_guidance = build_notification_payload(notifications)

        # Nest the canonical notification payload under the unified ``_meta``
        # envelope so the synthesized pair presents notifications the same way
        # an ACTIVE tool result does (``_meta.notifications`` +
        # ``_meta.notification_guidance``).
        body = {
            "_synthesized": True,
            "_meta": dict(notifications_with_guidance),
        }
        # Flatten the remaining safe build_meta fields into body top-level —
        # these are the synthesized pair's own freshness/uniqueness fields
        # (for example injection_seq), distinct from the tool-result metadata
        # blocks under ``_meta``.
        body.update(meta)
        # Store body as a dict (not a JSON string) so it can be mutated
        # in-place when this pair is skeletonized later.  All adapters
        # already handle dict content via isinstance checks — see
        # interface_converters.py and anthropic/adapter.py.
        content_dict = body

        # Build a per-source summary: "3 email, 1 soul, 0 system".
        # Counts come from data.count / len(data.events) / len(data.voices)
        # depending on the producer; fall back to "?" if unparseable.
        summary_parts = []
        for source, payload in notifications_with_guidance["notifications"].items():
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
        guidance_text = (
            "Notice: this is kernel-synchronized state from notification channels, "
            "not necessarily a human instruction. Identify the source, interpret "
            "the relevant channel payload, and verify intent before deciding "
            "whether to act. If it contains an identifiable human message whose "
            "preview is truncated, ambiguous, includes media, or needs exact "
            "anchoring, first use the producer channel's normal read action; if "
            "a human is waiting, acknowledge directly before long work."
        )
        summary_text = (
            f"[synthesized — kernel notification sync] "
            f"Notification received: {', '.join(summary_parts)}. {guidance_text}"
            if summary_parts
            else f"[synthesized — kernel notification sync] Notification received. {guidance_text}"
        )

        # ``summary_text`` is log-only.  Do not place it in a TextBlock on the
        # wire: successful notification sync should be a structured
        # notification(action="check") call/result pair, not a visible
        # synthesized diary/text-input row.
        # call.args carries injection_seq only — real tool calls don't have
        # runtime freshness fields in their args (those live in results).
        # The seq is enough to defeat byte-equality on the assistant turn.
        call_block = ToolCallBlock(
            id=call_id,
            name="notification",
            args={
                "action": "check",
                "injection_seq": self._notification_inject_seq,
            },
        )
        result_block = ToolResultBlock(
            id=call_id,
            name="notification",
            content=content_dict,  # dict, not JSON string — mutable for skeletonization
            synthesized=True,
        )

        iface.add_assistant_message(content=[call_block])
        iface.add_tool_results([result_block])

        # The append succeeded.  Now skeletonize the previous live holder
        # (if any) before registering this synthesized pair as the new live
        # holder.  Doing it after append preserves the old live payload if
        # injection had to abort because of pending tool calls.
        prior_holder = getattr(self, "_notification_live_holder", None)
        if prior_holder is not None and prior_holder is not content_dict:
            try:
                from ..meta_block import skeletonize_notification_holder
                self._notification_live_holder = prior_holder
                skeletonize_notification_holder(self)
            except Exception:
                pass

        # Register content_dict as the live holder so future
        # skeletonize_notification_holder / attach_active_notifications calls
        # can mutate it in-place without touching conversation history.
        # _notification_block_id is retained for informational / molt-reset
        # purposes; it is no longer used for remove_pair_by_call_id.
        self._notification_live_holder = content_dict
        self._notification_block_id = call_id

        self._save_chat_history(ledger_source="notification_sync")
        self._log(
            "notification_pair_injected",
            call_id=call_id,
            sources=list(notifications.keys()),
            summary=summary_text,
            meta=meta,
        )
        # Reconstruct the full four-block ``_meta`` envelope for the durable
        # snapshot so the TUI /notification view shows the same ``_meta.*``
        # blocks (tool_meta/agent_meta/guidance/notifications/
        # notification_guidance) a synthesized pair would carry.  The live wire
        # body keeps its notification-only ``_meta``; this is logging-side only.
        from ..meta_block import build_synthetic_meta_envelope
        synthetic_envelope = build_synthetic_meta_envelope(
            self,
            notifications_with_guidance,
            call_id=call_id,
        )
        self._log_notification_block_injected(
            synthetic_envelope,
            mode="synthetic_notification_pair",
            call_id=call_id,
        )
        return True

    def _log_notification_block_injected(
        self,
        meta_envelope: dict,
        *,
        mode: str,
        call_id: str | None = None,
    ) -> None:
        """Persist a durable notification_block_injected event capturing the
        full ``_meta`` envelope the model saw.

        Best-effort: any exception is swallowed so callers are never broken by a
        logging failure.  ``meta_envelope`` is the complete four-block envelope
        — ``tool_meta``, ``agent_meta``, ``guidance``, plus ``notifications`` and
        ``notification_guidance`` — exactly as it appears under the tool result's
        ``_meta`` key (ACTIVE) or as reconstructed for the synthesized pair
        (IDLE/ASLEEP, via ``build_synthetic_meta_envelope``).

        The envelope is persisted under a top-level ``_meta`` field on the event
        so the TUI ``/notification`` view renders ``_meta.tool_meta`` /
        ``_meta.agent_meta`` / ``_meta.guidance`` / ``_meta.notification_guidance``
        / ``_meta.notifications`` directly.  A deep copy is stored so later
        in-place skeletonization or nested mutation of the live holder does not
        corrupt the logged snapshot.
        """
        try:
            notifications = meta_envelope.get("notifications", {})
            sources = sorted(notifications.keys()) if isinstance(notifications, dict) else []
            self._log(
                "notification_block_injected",
                mode=mode,
                call_id=call_id or "",
                sources=sources,
                _meta=copy.deepcopy(meta_envelope),
            )
        except Exception:
            pass

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

    def _perform_refresh(
        self,
        *,
        skip_chat_history_save: bool = False,
        skip_save_reason: str | None = None,
    ) -> None:
        from .lifecycle import _perform_refresh
        _perform_refresh(
            self,
            skip_chat_history_save=skip_chat_history_save,
            skip_save_reason=skip_save_reason,
        )

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

    def _write_status_snapshot(self) -> None:
        """Write .status.json — live runtime snapshot consumed by TUI/portal."""
        try:
            (self._working_dir / ".status.json").write_text(
                json.dumps(self.status(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write .status.json: {e}")

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

    def get_current_session_token_usage(self) -> dict:
        """Return current-runtime-session token usage DELTAS (not lifetime totals).

        Delegates to :meth:`SessionManager.get_current_session_token_usage`. Used
        by the injected ``_meta.tool_meta.token_usage`` session-stat half so that
        restored cumulative/persisted totals never leak in as current-session
        activity. Distinct from :meth:`get_token_usage`, which reports lifetime
        totals for status/identity surfaces.
        """
        if not hasattr(self, "_session"):
            return {
                "api_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "session_cache_rate": 0.0,
                "avg_input_tokens_per_api_call": 0,
            }
        return self._session.get_current_session_token_usage()

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
        Git commits are handled by the periodic snapshot system. The persisted
        chat history is intentionally redacted; after process restart, restored
        history likewise contains redacted placeholders rather than raw secrets.

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
                redacted_messages = redact_for_trajectory(state["messages"])
                lines = [json.dumps(entry, ensure_ascii=False) for entry in redacted_messages]
                (history_dir / "chat_history.jsonl").write_text("\n".join(lines) + "\n")
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to save chat history: {e}")
        # Update .agent.json with current state
        try:
            self._workdir.write_manifest(self._build_manifest())
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to update manifest: {e}")
        self._write_status_snapshot()
        # Append per-call token usage to ledger
        usage, self._last_usage = self._last_usage, None
        if usage is not None:
            try:
                ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
                model = getattr(self._session, "_model", None) or getattr(self.service, "model", None)
                endpoint = getattr(self.service, "_base_url", None)
                ledger_extra = {"source": ledger_source}
                usage_extra = getattr(usage, "extra", None)
                if isinstance(usage_extra, dict):
                    ledger_extra.update(
                        {k: v for k, v in usage_extra.items() if v is not None}
                    )
                append_token_entry(
                    ledger_path,
                    input=usage.input_tokens,
                    output=usage.output_tokens,
                    thinking=usage.thinking_tokens,
                    cached=usage.cached_tokens,
                    model=model,
                    endpoint=endpoint,
                    extra=ledger_extra,
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
        self,
        tool_name: str,
        tool_args: dict,
        result: dict,
        *,
        tool_call_id: str | None = None,
    ) -> str | None:
        """Hook called after each tool execution.

        If this returns a non-None string, the current request processing
        returns immediately with that string as the result text.

        ``tool_call_id`` is the provider-assigned id for this tool call,
        passed directly by ToolExecutor so no heuristic scan is needed.

        Large tool results no longer raise a ``large_tool_result`` system
        notification here.  They are ranked instead through
        ``_meta.agent_meta.current_tool_result_chars.top_results`` and digested
        via ``system(action="summarize")`` (see meta_block.current_tool_result_chars).
        This hook is retained as a stable extension point; the large-result
        reminder it once published has been removed.
        """
        return None

    def _maybe_notify_large_tool_result(
        self,
        tool_name: str,
        result: object,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Retained no-op: large tool results no longer raise a notification.

        Large results used to publish a ``source="large_tool_result"`` system
        notification (gated by a total-length threshold) so the agent would be
        reminded to summarize them.  That reminder has been removed: large
        results are surfaced as a ranked list under
        ``_meta.agent_meta.current_tool_result_chars.top_results`` (see
        :func:`meta_block.current_tool_result_chars`) and digested via
        ``system(action="summarize")``.  The result still flows into normal
        tool-result history and the char-ranking; it simply creates no
        ``.notification/system.json`` event.

        This method is kept as a stable, overridable seam (subclasses/tests
        may still reference it) but is intentionally inert.
        """
        return None
