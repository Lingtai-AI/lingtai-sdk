"""OpenAI adapter — wraps the ``openai`` SDK for OpenAI and compatible APIs.

Covers: OpenAI, DeepSeek, Together AI, Groq, Fireworks, Ollama, vLLM,
and any other provider exposing an OpenAI-compatible ``/chat/completions``
endpoint.

This is the **only** module that imports the ``openai`` package.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import openai

from lingtai_kernel.logging import get_logger
from lingtai_kernel.config import THINKING_LEVELS

from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
    UsageMetadata,
)
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai.llm.base import LLMAdapter
from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ThinkingBlock, ToolCallBlock
from ..interface_converters import to_openai, to_responses_input
from lingtai_kernel.llm.streaming import StreamingAccumulator

logger = get_logger()


_CODEX_RESPONSES_TRACE_ENV = "LINGTAI_CODEX_RESPONSES_TRACE"
_CODEX_RESPONSES_TRACE_PATH_ENV = "LINGTAI_CODEX_RESPONSES_TRACE_PATH"
_CODEX_RESPONSES_TRACE_FILE = "codex_responses_trace.jsonl"


# Sentinel for "auto-derive a default prompt_cache_key". The adapter accepts
# ``prompt_cache_key=None`` to mean "compute the stable default", an explicit
# string to override it, and ``False`` to disable cache-key emission entirely.
_AUTO_PROMPT_CACHE_KEY = object()


# Codex REST cache-affinity headers (issue #378). The official Codex client
# sends ``session_id`` / ``thread_id`` headers on its
# ``/backend-api/codex/responses`` calls; a probe showed they materially
# improve prompt-cache affinity for repeated full-history replays. The REST
# endpoint does NOT accept ``previous_response_id`` (``Unsupported
# parameter``), so stable headers — not delta chaining — are the near-term
# cache-affinity lever.
#
# The header keys MUST be the underscore names ``session_id`` / ``thread_id``
# exactly as the Codex backend/CLI sends them. Do NOT "normalise" them into
# hyphenated, HTTP-looking ``session-id`` / ``thread-id``: the Codex backend
# matches the literal underscore key, so a hyphenated spelling silently loses
# cache affinity — every request fragments to a cold slot, exploding cache
# misses and token cost. (This comment block uses the spelling the code must
# emit; keep prose and code in sync.)
#
# For the normal/root main Codex session, the three cache-affinity values are
# byte-identical and stable for the lifetime of the agent's durable identity:
#
#     session_id == thread_id == prompt_cache_key == <8-char agent-path hash>
#
# The shared value is a deterministic 8-character lowercase-hex digest of the
# agent's durable identity anchor (the resolved ``init.json`` / agent path).
# Ordinary LLM calls, ``api_call_id`` rotation, refresh/rebuild, molt, and
# clear (same agent path) all leave it unchanged — only a different agent path
# changes it. We do NOT use the latest ``api_call_id``, molt time, or generated
# UUIDs for these defaults.
#
# IMPORTANT — these identifiers MUST be per-agent. The value anchors on the
# agent's durable identity (the resolved ``init.json`` path). It must NOT be
# derived from a global, model-only anchor (e.g.
# ``prompt_cache_key=lingtai-codex:{model}:v1``): every agent on the same model
# shares that string, which would collapse all of them onto one
# session/thread and is exactly the wrong behavior.
#
# The adapter layer has no per-agent identity of its own, so the host wiring
# (``lingtai/llm/service.py:build_provider_defaults_from_manifest_llm``) passes
# the agent path down by default as ``codex_session_anchor``: a normal Codex
# agent gets a stable per-agent hash used identically for all three values. The
# constructor kwargs below are the seam those defaults flow through (and an
# internal override / testing escape hatch).


def _codex_session_id(anchor: str) -> str:
    """Derive the stable 8-char Codex cache-affinity id from ``anchor``.

    ``anchor`` MUST be a per-agent identity string (e.g. the agent's resolved
    ``init.json`` / agent-dir path), NOT a global model-only key. The result is
    a deterministic 8-character lowercase-hex sha256 prefix: the same anchor
    always yields the same id; distinct anchors differ. The same value is used
    byte-identically for ``session_id``, ``thread_id``, and the default
    ``prompt_cache_key`` on the normal/root path.
    """
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:8]

# Codex cache-affinity identity is a SINGLE STABLE per-agent value: a pure
# deterministic hash of the agent's durable identity anchor (the resolved
# ``init.json`` / agent-dir path), used byte-identically for ``session_id`` /
# ``thread_id`` / ``prompt_cache_key`` and NEVER changed for the life of the
# agent's identity. See :func:`_codex_session_id`.
#
# Historically there were two churn mechanisms here, both REMOVED because they
# were empirically counterproductive (the backend routes the prompt cache to a
# sticky-warm replica off a STABLE session id; changing the id re-rolls the
# routing and discards the warm slot):
#   - epoch-stamping the id on every adapter (re)build, and
#   - a "stalled-cache" rotation that changed the id when the cache rate dipped.
# Both are gone. The id depends only on the agent path — no time, no epoch, no
# rotation. The ``codex-cache-key`` request header (first chars of the prompt
# key) was part of the same churn apparatus and is no longer sent; Codex CLI
# never sends it either.


# Client-identity headers for the Codex ``/backend-api/codex/responses`` path.
#
# Default policy: identify LingTai honestly to ChatGPT Codex:
#   originator: lingtai
#   User-Agent: LingTai/<installed-version>
#
# During the #471 websocket/cache investigation we used an official Codex
# CLI-shaped identity as a local diagnostic. Keep that path as an explicit
# opt-in comparison switch only; do not ship impersonation as the default.
# Caller-supplied ``extra_headers`` still win over this base layer.
#
# Do not log bearer tokens or full request headers while changing this area.
_CODEX_IMPERSONATE_OFFICIAL_CLI = False

# Official Codex CLI app-name identity (version pinned to the installed
# ``codex-cli`` build we inspected). Kept as data so the official-shaped UA is
# a deliberate code switch rather than hidden string literals.
_CODEX_CLI_ORIGINATOR = "codex_cli_rs"
_CODEX_CLI_VERSION = "0.130.0"

# Honest LingTai identity (the shipped default).
_LINGTAI_ORIGINATOR = "lingtai"

# Effective originator for this build. Flipping the switch above swaps both the
# originator and the User-Agent together so they never disagree.
_CODEX_ORIGINATOR = (
    _CODEX_CLI_ORIGINATOR if _CODEX_IMPERSONATE_OFFICIAL_CLI else _LINGTAI_ORIGINATOR
)


def _codex_cli_user_agent() -> str:
    """Return an official-CLI-shaped ``User-Agent``, e.g.
    ``codex_cli_rs/0.130.0 (Darwin 23.4.0; arm64)``.

    Mirrors the official Codex CLI UA shape: ``{originator}/{version} ({os}
    {os_version}; {arch})``. The OS/arch suffix is best-effort — on failure we
    fall back to the bare ``{originator}/{version}`` token rather than raising.
    """
    base = f"{_CODEX_CLI_ORIGINATOR}/{_CODEX_CLI_VERSION}"
    try:
        import platform

        system = platform.system() or "unknown"
        release = platform.release() or ""
        machine = platform.machine() or ""
        return f"{base} ({system} {release}; {machine})".replace("  ", " ").strip()
    except Exception:
        return base


def _lingtai_user_agent() -> str:
    """Return the effective Codex ``User-Agent`` string.

    When ``_CODEX_IMPERSONATE_OFFICIAL_CLI`` is set, this
    returns the official-Codex-CLI-shaped UA so the app name matches what the
    ChatGPT backend recognizes (see the identity-policy note above). When the
    switch is off it returns the honest ``LingTai/<version>`` UA, falling back
    to an unversioned ``LingTai`` token if the package version can't be
    resolved.

    (The function name is retained for back-compat with existing imports/tests;
    it is the single resolver for the Codex identity UA regardless of policy.)
    """
    if _CODEX_IMPERSONATE_OFFICIAL_CLI:
        return _codex_cli_user_agent()
    try:
        from importlib.metadata import version

        return f"LingTai/{version('lingtai')}"
    except Exception:
        return "LingTai"


def _codex_installation_id(anchor: str | None) -> str | None:
    """Return a stable, honest LingTai installation id for Codex metadata.

    Codex CLI sends an opaque UUID-shaped ``x-codex-installation-id``. LingTai
    must not borrow ``~/.codex/installation_id`` or impersonate the CLI, so we
    derive our own UUID-shaped identifier from the same non-secret local anchor
    used for Codex cache affinity. The raw path/anchor is never sent.
    """

    if not anchor:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lingtai-codex-installation:{anchor}"))

def _codex_identity_headers() -> dict[str, str]:
    """Return Codex client identity headers.

    The shipped default is explicit LingTai identity. The official-Codex-CLI
    app-name identity is available only when ``_CODEX_IMPERSONATE_OFFICIAL_CLI``
    is explicitly enabled for local protocol comparisons. The originator and
    User-Agent are always resolved together so they agree. See
    ``tests/test_codex_prompt_cache_key.py`` for the request-level guardrails.
    """
    return {"originator": _CODEX_ORIGINATOR, "User-Agent": _lingtai_user_agent()}


# ---------------------------------------------------------------------------
# Codex Responses-over-WebSocket incremental turn state (EXPERIMENTAL, #471).
#
# This mirrors the official Codex CLI source path (repo openai/codex, tag
# ``rust-v0.130.0``, commit 58573da). The high-cache/stateful behavior on the
# ChatGPT Codex backend is NOT server-side Responses storage (``store`` is
# ``false`` there by construction — ``codex-rs/core/src/client.rs:722``); it is
# Responses-over-WebSocket incremental ``response.create`` frames that carry
# ``previous_response_id`` plus only the delta input when the new full request is
# a strict extension of (previous request input + previous response output
# items). See ``get_incremental_items`` (``client.rs:949-985``) and
# ``prepare_websocket_request`` (``client.rs:998-1024``).
#
# These objects are pure Python data + a pure algorithm so the request-shape
# logic is unit-testable without any network. The actual websocket wire goes
# through an injectable transport (``_CodexWebsocketTransport``) so tests can
# substitute a fake.
# ---------------------------------------------------------------------------

# Official websocket beta header value (``client.rs:142``) and the per-turn
# sticky-routing state header (``client.rs:134`` /
# ``responses_websocket.rs:155``). Kept as data so the wire stays auditable.
_CODEX_WS_BETA_HEADER = "OpenAI-Beta"
_CODEX_WS_BETA_VALUE = "responses_websockets=2026-02-06"
_CODEX_TURN_STATE_HEADER = "x-codex-turn-state"

# Env gate for the websocket transport. On by default: when unset the session
# uses the websocket path (which still falls back to HTTP full-replay on any
# handshake/connection/auth error, unsupported runtime, or delta mismatch).
# Set ``LINGTAI_CODEX_WS=0`` (or ``false``/``no``/``off``) to force the existing,
# proven HTTP full-replay path.
_CODEX_WS_ENABLED_ENV = "LINGTAI_CODEX_WS"
_CODEX_WS_EPOCH_RESET_TURNS_ENV = "LINGTAI_CODEX_WS_EPOCH_RESET_TURNS"
_CODEX_WS_EPOCH_RESET_TURNS_DEFAULT = 20

# Non-input request fields that must match between two requests for an
# incremental delta to be valid. Mirrors the official ``get_incremental_items``
# which clones both requests, clears ``.input`` on each, and compares the rest
# for strict equality (``client.rs:960-970``).


def _codex_ws_enabled() -> bool:
    """Return True when the websocket transport is enabled (default on).

    Unset env means enabled. An explicit falsy value (``0``/``false``/``no``/
    ``off``, case-insensitive) disables it and forces the HTTP full-replay path.
    Any other value leaves the default (enabled) in place.
    """
    raw = os.environ.get(_CODEX_WS_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _codex_ws_epoch_reset_turns() -> int:
    """Return the configurable WS response-chain reset interval."""

    raw = os.getenv(_CODEX_WS_EPOCH_RESET_TURNS_ENV, "").strip()
    if not raw:
        return _CODEX_WS_EPOCH_RESET_TURNS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _CODEX_WS_EPOCH_RESET_TURNS_DEFAULT
    return max(0, value)


@dataclass
class _CodexLastResponse:
    """The previous completed websocket response, for delta computation.

    Mirrors the official ``LastResponse`` (``client.rs:1748-1774``): the
    ``response_id`` becomes the next request's ``previous_response_id``, and
    ``items_added`` are the server-added output items that form part of the
    delta baseline so they are never resent.
    """

    response_id: str
    items_added: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _CodexWebsocketSession:
    """Per-turn websocket state: last full request + last completed response.

    Mirrors the fields the official ``ModelClientSession`` caches for the turn
    (``client.rs:214-226``): ``last_request`` (the full request) and the last
    response. The captured ``turn_state`` token is sticky-routing state replayed
    within the same turn and reset across turns (``client.rs:227-240``).
    """

    last_request: dict[str, Any] | None = None
    last_response: _CodexLastResponse | None = None
    turn_state: str | None = None


def _codex_incremental_items(
    previous_request: dict[str, Any],
    previous_items_added: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    allow_empty_delta: bool,
) -> list[dict[str, Any]] | None:
    """Compute the incremental input delta, or ``None`` to send full input.

    Faithful port of the official ``get_incremental_items``
    (``codex-rs/core/src/client.rs:949-985``):

      1. All non-input request fields must be identical between the previous and
         current request (compare both with ``input`` cleared).
      2. The baseline is ``previous_request.input + previous_items_added``.
      3. The current ``input`` must start with that baseline; the suffix after
         the baseline is the delta. An empty delta is only returned when
         ``allow_empty_delta`` is true (the websocket prewarm/no-op case).

    Returns ``None`` whenever a strict extension cannot be proven, so the caller
    falls back to sending the full input rather than a bad delta.
    """
    delta, _reason = _codex_incremental_diagnose(
        previous_request,
        previous_items_added,
        request,
        allow_empty_delta=allow_empty_delta,
    )
    return delta


def _codex_diff_keys(prev_no_input: dict[str, Any], cur_no_input: dict[str, Any]) -> list[str]:
    """Return the sorted set of non-input request keys that changed.

    Used only for safe diagnostics: it records WHICH non-input field names
    diverged (e.g. ``tools``, ``include``), never their values, so the reason
    string carries no prompt/tool/secret content.
    """
    keys = set(prev_no_input) | set(cur_no_input)
    return sorted(k for k in keys if prev_no_input.get(k) != cur_no_input.get(k))


def _codex_item_safe_diag(item: Any) -> dict[str, str]:
    """Return safe, content-free diagnostics for one Responses input item."""
    if not isinstance(item, dict):
        return {"type": type(item).__name__, "role": "", "keys": "", "hash": ""}
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return {
        "type": str(item.get("type") or "")[:80],
        "role": str(item.get("role") or "")[:80],
        "keys": ",".join(sorted(str(k) for k in item.keys()))[:160],
        # Short hash only: enough to tell whether two opaque items differ,
        # without leaking prompt/tool/result content into provider metadata.
        "hash": hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:12],
    }


def _codex_incremental_diagnose(
    previous_request: dict[str, Any],
    previous_items_added: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    allow_empty_delta: bool,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Like :func:`_codex_incremental_items` but also return a safe diagnostic.

    The second element is a small metadata dict explaining the decision. It
    records ONLY classes/counts/lengths/short-hash/booleans — never prompt,
    tool-result, reasoning, token, header, or secret content — so it is safe to
    surface in provider metadata / the token ledger:

      * ``reason``: ``ok`` | ``non_input_fields_changed`` | ``prefix_mismatch``
        | ``empty_delta_rejected``
      * ``changed_fields``: list of non-input KEY NAMES that diverged (no values)
      * ``baseline_len`` / ``cur_input_len`` / ``delta_len``: item counts
      * ``mismatch_index``: first baseline index where the prefix diverged (or -1)
    """
    prev_no_input = {k: v for k, v in previous_request.items() if k != "input"}
    cur_no_input = {k: v for k, v in request.items() if k != "input"}

    baseline = list(previous_request.get("input") or [])
    baseline.extend(previous_items_added or [])
    baseline_len = len(baseline)
    cur_input = list(request.get("input") or [])
    cur_len = len(cur_input)

    diag: dict[str, Any] = {
        "reason": "ok",
        "changed_fields": [],
        "baseline_len": baseline_len,
        "cur_input_len": cur_len,
        "delta_len": 0,
        "mismatch_index": -1,
    }

    if prev_no_input != cur_no_input:
        diag["reason"] = "non_input_fields_changed"
        diag["changed_fields"] = _codex_diff_keys(prev_no_input, cur_no_input)
        return None, diag

    # Find the first position where the current input diverges from the baseline.
    prefix = cur_input[:baseline_len]
    if prefix != baseline:
        mismatch = baseline_len  # default: current input is shorter than baseline
        for idx in range(min(len(prefix), baseline_len)):
            if prefix[idx] != baseline[idx]:
                mismatch = idx
                break
        diag["reason"] = "prefix_mismatch"
        diag["mismatch_index"] = mismatch
        if mismatch < baseline_len:
            prev_diag = _codex_item_safe_diag(baseline[mismatch])
            diag["mismatch_prev_type"] = prev_diag.get("type")
            diag["mismatch_prev_role"] = prev_diag.get("role")
            diag["mismatch_prev_keys"] = prev_diag.get("keys")
            diag["mismatch_prev_hash"] = prev_diag.get("hash")
        if mismatch < cur_len:
            cur_diag = _codex_item_safe_diag(cur_input[mismatch])
            diag["mismatch_cur_type"] = cur_diag.get("type")
            diag["mismatch_cur_role"] = cur_diag.get("role")
            diag["mismatch_cur_keys"] = cur_diag.get("keys")
            diag["mismatch_cur_hash"] = cur_diag.get("hash")
        return None, diag

    if not (allow_empty_delta or baseline_len < cur_len):
        diag["reason"] = "empty_delta_rejected"
        return None, diag

    delta = cur_input[baseline_len:]
    diag["delta_len"] = len(delta)
    return delta, diag


def _ws_is_synthesized_orphan_output(item: Any) -> bool:
    """True if ``item`` is the synthesized orphan ``function_call_output`` guard.

    ``to_responses_input`` injects a placeholder ``function_call_output`` for any
    unanswered ``function_call`` (issue #170). That placeholder must not enter the
    websocket delta baseline: the real tool-result continuation replaces it next
    turn, so a baseline containing it can never strict-prefix-match. Detect it by
    the sentinel output string so the baseline builder can trim it.
    """
    from ..interface_converters import _RESPONSES_ORPHAN_OUTPUT_PLACEHOLDER

    return (
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("output") == _RESPONSES_ORPHAN_OUTPUT_PLACEHOLDER
    )


def _freeze_responses_outputs(
    items: list[dict[str, Any]],
    frozen: dict[str, str],
) -> list[dict[str, Any]]:
    """Stabilize ``function_call_output.output`` strings across WS replay turns.

    The kernel carries *latest-only* resident-meta blocks (``_meta.agent_meta`` /
    ``_meta.guidance`` / ``_meta.notifications``) and MOVES them off an older tool
    result onto the freshest one each turn (``meta_block.attach_active_runtime`` /
    ``attach_active_notifications``). That rewrites an older
    ``ToolResultBlock.content`` *in place*, so the same ``call_id``'s
    ``function_call_output.output`` serializes differently on a later turn even
    though, semantically, the model already saw that result.

    For Codex's stateful WS delta path the next request's converted input must
    strict-prefix-match the prior baseline. A changed older
    ``function_call_output`` (same ``call_id`` and keys, different ``output``
    hash) breaks the prefix and forces ``ws_full`` every turn (the observed
    ``prefix_mismatch``).

    This freezes each output by ``call_id`` at first send for the life of the
    session: the first time a ``call_id`` is converted, its ``output`` is
    recorded; every later conversion replays the recorded string. Replay is
    therefore byte-identical regardless of in-place resident-meta movement.

    Fidelity is preserved, not lost: the model already saw the frozen version
    when it was first sent, and the freshest result is *first-seen on its own
    turn*, so it freezes WITH its live meta — live guidance / notifications still
    reach the model on the result that is supposed to carry them.

    Pure and content-free: returns a new list (shallow-copying only the rewritten
    items), never mutates the caller's items, and records nothing to diagnostics.
    Non-``function_call_output`` items and outputs missing a ``call_id`` pass
    through untouched.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and isinstance(item.get("call_id"), str)
            # The synthesized orphan placeholder (issue #170 wire guard) is a
            # transient stand-in, NOT the real tool result. Never freeze it:
            # doing so would replay the placeholder once the real continuation
            # arrives, hiding the actual result from the model. Let it pass
            # through so the real output freezes when it first appears.
            and not _ws_is_synthesized_orphan_output(item)
        ):
            call_id = item["call_id"]
            cached = frozen.get(call_id)
            if cached is None:
                frozen[call_id] = item.get("output")
                out.append(item)
            else:
                replayed = dict(item)
                replayed["output"] = cached
                out.append(replayed)
        else:
            out.append(item)
    return out


def _ws_dump_item(item: Any) -> dict[str, Any] | None:
    """Normalize a streamed output item to a plain dict.

    Retained as a small, well-tested normalizer for SDK event items (pydantic
    models -> dict; dicts pass through; anything else -> ``None``). It is NO
    LONGER the delta-baseline source: the server's streamed output items are in
    the Responses *output* schema and never strict-prefix-match the *input*
    schema this session re-derives next turn, which forced ``ws_full`` every
    turn. The baseline is now built from the converter via
    ``CodexResponsesSession._ws_record_baseline_from_interface``. This helper is
    kept for diagnostics / potential reuse and to preserve its unit contract.
    """
    if item is None:
        return None
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump(exclude_none=True)
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(item, dict):
        return item
    return None


class _CodexWsFallback(Exception):
    """Raised by a websocket transport to request a fall back to HTTP.

    Mirrors the official ``WebsocketStreamOutcome::FallbackToHttp`` decision
    (``client.rs:1361-1364``): a handshake ``426 UPGRADE_REQUIRED``, a
    connection/handshake failure, an unsupported runtime (e.g. the optional
    ``websockets`` dependency is absent), or any condition under which we cannot
    safely use the websocket path. The caller catches it and replays the full
    input over HTTP with ``store=false``.
    """


def _codex_ws_url(base_url: str | None) -> str:
    """Convert the Codex HTTP base URL to the websocket ``responses`` URL.

    Mirrors ``Provider::websocket_url_for_path`` (``provider.rs:92-103``):
    ``https://chatgpt.com/backend-api/codex`` -> ``wss://.../responses``.
    """
    base = (base_url or "https://chatgpt.com/backend-api/codex").rstrip("/")
    path = base + "/responses"
    if path.startswith("https://"):
        return "wss://" + path[len("https://"):]
    if path.startswith("http://"):
        return "ws://" + path[len("http://"):]
    return path


def _default_codex_ws_transport_factory(url: str, headers: dict[str, str]):
    """Build the real websocket transport, or raise ``_CodexWsFallback``.

    Lazily imports the optional ``websockets`` package; if it is not installed
    (the kernel does not hard-depend on it), the websocket path is treated as an
    unsupported runtime and the caller falls back to HTTP. The real transport is
    intentionally NOT exercised by the unit tests (which inject a fake) — a live
    smoke test is gated behind ``LINGTAI_CODEX_WS`` + parent approval.
    """
    try:  # pragma: no cover - import guard, exercised only with the dep present
        import websockets  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise _CodexWsFallback(f"websockets unavailable: {exc}") from exc
    # The synchronous wire driver lives in a companion module to keep the import
    # cost and event-loop plumbing out of the hot adapter import path. It is only
    # reached on a live run, never in the mock tests.
    from .codex_ws import SyncCodexWebsocketTransport  # pragma: no cover

    return SyncCodexWebsocketTransport(url=url, headers=headers)  # pragma: no cover


def _base_url_namespace(base_url: str | None) -> str:
    """Return a stable namespace token for an OpenAI-compatible ``base_url``.

    Prefers the URL host (e.g. ``api.vendor.example``) so distinct endpoints
    never share a prompt-cache namespace. Falls back to a short hash of the
    full URL when no host can be parsed, so the result is always deterministic
    and non-empty.
    """
    if not base_url:
        return ""
    host = urlsplit(base_url).hostname or ""
    if host:
        return host
    return "h" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:12]


def _parse_codex_base_urls(value: object) -> tuple[str, ...]:
    """Normalize a ``codex_base_urls`` config value into a clean tuple.

    Accepts a list/tuple of strings, or a single comma/newline-separated string
    (whichever the host config layer happens to deliver). Each entry is
    stripped; blank/whitespace-only entries are dropped. Order is preserved and
    duplicates are kept (the caller indexes by molt_count, not by uniqueness).
    Returns ``()`` when nothing valid remains — the caller then falls back to
    the single ``base_url`` behavior (PR #495). NEVER raises and NEVER touches
    the network.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw: list = []
        for item in value:
            # Tolerate a nested comma/newline string inside a list entry.
            if isinstance(item, str) and ("," in item or "\n" in item):
                raw.extend(item.replace("\n", ",").split(","))
            else:
                raw.append(item)
    else:
        return ()
    return tuple(
        s.strip() for s in raw if isinstance(s, str) and s.strip()
    )


def _read_molt_count(agent_json_path: Path) -> int:
    """Read ``molt_count`` from ``<working_dir>/.agent.json``; 0 on any failure.

    The molt path does NOT rebuild the Codex adapter, so the running adapter
    reads this file at request time to observe molt-boundary changes. A missing
    or malformed file, or a non-int ``molt_count``, yields 0 — no exception is
    raised and no network call is made.
    """
    try:
        data = json.loads(agent_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    raw = data.get("molt_count", 0) if isinstance(data, dict) else 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _validate_compact_threshold(value: int | None) -> int | None:
    """Normalize the OpenAI Responses auto-compaction threshold.

    ``None`` intentionally disables Responses ``context_management``.  Any
    concrete value must be a positive integer; reject bool explicitly because
    it is an ``int`` subclass in Python but not a valid token threshold.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("compact_threshold must be a positive int or None")
    if value <= 0:
        raise ValueError("compact_threshold must be > 0 or None")
    return value


def _responses_reasoning_kwargs(thinking: str | None) -> dict[str, dict[str, str]]:
    """Return OpenAI Responses reasoning kwargs for a configured thinking level."""
    if thinking in (None, "default"):
        return {}
    if thinking not in THINKING_LEVELS:
        raise ValueError(
            "OpenAI Responses thinking must be one of "
            f"{', '.join(THINKING_LEVELS)}, or default"
        )
    return {"reasoning": {"effort": thinking}}


def _codex_responses_trace_path() -> Path | None:
    """Return the opt-in Codex Responses stream trace path, if enabled."""
    enabled = os.environ.get(_CODEX_RESPONSES_TRACE_ENV, "")
    if enabled.lower() not in {"1", "true", "yes", "on"}:
        return None

    explicit_path = os.environ.get(_CODEX_RESPONSES_TRACE_PATH_ENV)
    if explicit_path:
        return Path(explicit_path).expanduser()

    base_dir = Path(os.environ.get("LINGTAI_AGENT_DIR", ".")).expanduser()
    return base_dir / "logs" / _CODEX_RESPONSES_TRACE_FILE


def _text_fingerprint(text: str | None) -> dict[str, Any]:
    """Return safe metadata for text-like stream deltas without storing content."""
    if text is None:
        return {"present": False, "length": 0}
    encoded = text.encode("utf-8", errors="replace")
    return {
        "present": True,
        "length": len(text),
        "sha256_12": hashlib.sha256(encoded).hexdigest()[:12],
    }


def _codex_responses_trace_record(
    *,
    event: Any,
    accepted_reasoning: bool,
    thoughts_before: list[str],
    thoughts_after: list[str],
    pending_thought_chars_before: int,
    pending_thought_chars_after: int,
    trace_path: Path | None,
) -> None:
    """Append safe diagnostic metadata for one Codex Responses stream event.

    This intentionally records event/item shapes, text lengths, and hashes only;
    it must not store prompt text, raw response text, raw reasoning text, tool
    result content, or API credentials.
    """
    if trace_path is None:
        return

    item = getattr(event, "item", None)
    response = getattr(event, "response", None)
    usage = getattr(response, "usage", None) if response is not None else None
    input_details = getattr(usage, "input_tokens_details", None) if usage is not None else None
    output_details = getattr(usage, "output_tokens_details", None) if usage is not None else None

    summaries = []
    for summary in getattr(item, "summary", None) or []:
        summaries.append({
            "type": getattr(summary, "type", None),
            "text": _text_fingerprint(getattr(summary, "text", None)),
        })

    record = {
        "ts": time.time(),
        "event_type": getattr(event, "type", None),
        "accepted_reasoning": accepted_reasoning,
        "item": None if item is None else {
            "type": getattr(item, "type", None),
            "id": getattr(item, "id", None),
            "call_id": getattr(item, "call_id", None),
            "name": getattr(item, "name", None),
            "summary": summaries,
        },
        "item_id": getattr(event, "item_id", None),
        "delta": _text_fingerprint(getattr(event, "delta", None)),
        "summary_text": _text_fingerprint(getattr(event, "text", None)),
        "thoughts": {
            "before_count": len(thoughts_before),
            "after_count": len(thoughts_after),
            "before_lengths": [len(t) for t in thoughts_before],
            "after_lengths": [len(t) for t in thoughts_after],
            "pending_chars_before": pending_thought_chars_before,
            "pending_chars_after": pending_thought_chars_after,
        },
    }
    if usage is not None:
        record["usage"] = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cached_tokens": getattr(input_details, "cached_tokens", None),
            "reasoning_tokens": getattr(output_details, "reasoning_tokens", None),
        }

    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - diagnostics must not break send
        logger.warning("Codex Responses trace write failed: %s", exc)


def _build_http_timeout(request_timeout: float | None):
    """Build explicit per-phase HTTP timeout for SDK calls.

    The main-thread watchdog controls total wall-clock time. SDK/httpx
    timeout values are per phase, so cap read waits to keep wedged sockets
    from occupying the worker indefinitely.
    """
    if request_timeout is None:
        return None
    return httpx.Timeout(
        connect=min(float(request_timeout), 30.0),
        read=min(float(request_timeout), 60.0),
        write=min(float(request_timeout), 30.0),
        pool=10.0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to OpenAI tool format."""
    if not schemas:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in schemas
    ]


# Top-level JSON-Schema combinators the Responses API rejects on a
# function-tool `parameters` root. `enum` is only disallowed at the root —
# it is valid (and common) inside individual properties.
_RESPONSES_DISALLOWED_TOP_LEVEL = ("allOf", "oneOf", "anyOf", "not", "enum")


# Keys that, if present on a schema node, already establish its kind so it
# does not need a synthesized `type`. The Codex backend rejects a property
# schema that carries none of these (a "typeless" property, e.g. one with
# only a `description`), so such nodes are coerced to `{"type": "string"}`.
_SCHEMA_KIND_KEYS = (
    "type", "enum", "const", "$ref",
    "anyOf", "allOf", "oneOf", "not",
    "properties", "items",
)


def _scrub_responses_schema(node: Any) -> Any:
    """Recursively normalize a JSON schema for the Codex Responses backend.

    Empirically, `/backend-api/codex/responses` rejects three constructs even
    when nested inside a property, returning an opaque `server_error`:

      1. `oneOf` / `not` combinators  -> `oneOf` rewritten to the accepted
         `anyOf`; `not` dropped (no accepted equivalent).
      2. Typeless property schemas    -> a node with a `description` but no
         type-establishing key (see `_SCHEMA_KIND_KEYS`) gets `type:
         "string"`. This covers the nested `secondary.args.*` fields LingTai
         emits with only a description.
      3. `{"type": "object"}` with no `properties` key (a free-form object,
         e.g. `daemon`'s `tasks[].backend_options`) -> an empty
         `properties: {}` is added. An empty `properties` map is accepted.

    `enum`/`anyOf`/`allOf` are left untouched (the backend accepts them
    nested). Walks dicts and lists so all fixes apply at any depth.
    """
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            if key == "oneOf":
                out["anyOf"] = [_scrub_responses_schema(v) for v in value]
            elif key == "not":
                continue  # no accepted equivalent — drop it
            else:
                out[key] = _scrub_responses_schema(value)
        # Coerce typeless property schemas: a node describing a value (has a
        # description) but lacking any kind key. Skip bare containers like an
        # empty {} or {"required": [...]} that aren't value descriptors.
        if "description" in out and not any(k in out for k in _SCHEMA_KIND_KEYS):
            out["type"] = "string"
        # A typed object with no `properties` is rejected; give it an empty map.
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out
    if isinstance(node, list):
        return [_scrub_responses_schema(v) for v in node]
    return node


def _build_responses_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to Responses API tool format.

    Responses uses a flat shape (`type: function`, fields hoisted) instead
    of Chat Completions' nested `{type: function, function: {...}}`. Scrubs
    top-level combinators the Responses API rejects at the parameters root,
    then runs `_scrub_responses_schema` to fix the constructs the Codex
    backend rejects even nested in a property (`oneOf`/`not` combinators and
    typeless property schemas).
    """
    if not schemas:
        return None
    tools = []
    for s in schemas:
        params = dict(s.parameters or {})
        for key in _RESPONSES_DISALLOWED_TOP_LEVEL:
            params.pop(key, None)
        params = _scrub_responses_schema(params)
        tools.append(
            {
                "type": "function",
                "name": s.name,
                "description": s.description,
                "parameters": params,
            }
        )
    return tools


def _parse_tool_calls(raw_tool_calls) -> list[ToolCall]:
    """Parse OpenAI tool calls into our ToolCall dataclass."""
    if not raw_tool_calls:
        return []
    result = []
    for tc in raw_tool_calls:
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        result.append(
            ToolCall(
                name=tc.function.name,
                args=args,
                id=tc.id,
            )
        )
    return result


def _parse_response(raw) -> LLMResponse:
    """Parse a raw OpenAI ChatCompletion into a provider-agnostic LLMResponse."""
    if not raw.choices:
        return LLMResponse(raw=raw)

    choice = raw.choices[0]
    message = choice.message

    text = message.content or ""
    tool_calls = _parse_tool_calls(message.tool_calls)

    # Extract thinking/reasoning. Field name varies by provider:
    #   OpenAI o-series native        -> message.reasoning_content
    #   OpenRouter (any reasoning mdl) -> message.reasoning
    # We check both so the same parser works across providers. Native
    # providers that don't set either field just produce no thoughts.
    thoughts: list[str] = []
    reasoning = (
        getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
    )
    if reasoning:
        thoughts.append(reasoning)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "prompt_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=raw.usage.prompt_tokens or 0,
            output_tokens=raw.usage.completion_tokens or 0,
            thinking_tokens=getattr(raw.usage, "completion_tokens_details", None)
            and getattr(raw.usage.completion_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


def _add_responses_reasoning_done_text(
    acc: StreamingAccumulator,
    reasoning_item_id: str | None,
    seen_reasoning_summary_items: set[str],
    text: str | None,
) -> None:
    """Add final reasoning-summary text only when no delta was seen.

    Responses ``*.done`` events carry the complete text.  When the stream
    already accepted summary text for the same reasoning item from deltas or a
    done fallback, adding ``done.text`` would duplicate the thought.  If a
    provider emits only the done event, use it as a lossless fallback.
    """
    if text and (not reasoning_item_id or reasoning_item_id not in seen_reasoning_summary_items):
        acc.add_thought(text)
        if reasoning_item_id:
            seen_reasoning_summary_items.add(reasoning_item_id)
    acc.finish_thought()


def _handle_responses_reasoning_event(
    event: Any,
    acc: StreamingAccumulator,
    seen_reasoning_summary_items: set[str],
) -> bool:
    """Feed safe Responses reasoning-summary events into ``acc``.

    We persist summary text, not raw ``response.reasoning_text.*`` events, so
    stateless Codex replay can include documented ``summary_text`` reasoning
    items without storing hidden chain-of-thought.
    """
    event_type = getattr(event, "type", None)
    if event_type == "response.reasoning_summary_text.delta":
        delta = getattr(event, "delta", None)
        if delta:
            acc.add_thought(delta)
            item_id = getattr(event, "item_id", None)
            if item_id:
                seen_reasoning_summary_items.add(item_id)
        return True
    if event_type == "response.reasoning_summary_text.done":
        _add_responses_reasoning_done_text(
            acc,
            getattr(event, "item_id", None),
            seen_reasoning_summary_items,
            getattr(event, "text", None),
        )
        return True
    if event_type == "response.output_item.done" and getattr(event.item, "type", None) == "reasoning":
        summaries = getattr(event.item, "summary", None) or []
        added_fallback = False
        for summary in summaries:
            if getattr(summary, "type", None) == "summary_text":
                item_id = getattr(event.item, "id", None)
                text = getattr(summary, "text", None)
                if text and (not item_id or item_id not in seen_reasoning_summary_items):
                    acc.add_thought(text)
                    if item_id:
                        seen_reasoning_summary_items.add(item_id)
                    added_fallback = True
        if added_fallback or acc.thoughts:
            acc.finish_thought()
        return True
    return False


def _parse_responses_api_response(raw) -> LLMResponse:
    """Parse a raw OpenAI Responses API response into a provider-agnostic LLMResponse."""
    text_parts = []
    tool_calls = []
    thoughts = []

    for item in raw.output or []:
        if item.type == "message":
            for block in item.content or []:
                if block.type == "output_text":
                    text_parts.append(block.text)
        elif item.type == "function_call":
            try:
                args = json.loads(item.arguments) if item.arguments else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=item.name, args=args, id=item.call_id))
        elif item.type == "reasoning":
            for summary in getattr(item, "summary", None) or []:
                if getattr(summary, "type", None) == "summary_text":
                    thoughts.append(summary.text)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "input_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=getattr(raw.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
            thinking_tokens=getattr(raw.usage, "output_tokens_details", None)
            and getattr(raw.usage.output_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# OpenAIChatSession
# ---------------------------------------------------------------------------


class OpenAIChatSession(ChatSession):
    """Client-managed chat session for OpenAI-compatible APIs.

    Uses ChatInterface as the single source of truth.
    """

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        interface: ChatInterface,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        client_kwargs: dict | None = None,
        context_window: int = 0,
        prompt_cache_key: str | None = None,
    ):
        self._client = client
        self._model = model
        self._interface = interface
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._client_kwargs = client_kwargs or {}
        self._context_window = context_window
        # Stable ``prompt_cache_key`` for OpenAI-compatible Chat Completions
        # cross-request prompt caching. Sent only when set; ``None`` leaves it
        # off (so a directly-constructed session is opt-in). The adapter
        # supplies the namespaced default — see ``_default_prompt_cache_key``.
        # ``prompt_cache_retention`` is deliberately never sent (Codex rejects
        # it; we keep the whole OpenAI-compatible surface uniform).
        self._prompt_cache_key = prompt_cache_key
        # Per-request HTTP timeout (seconds). Set by send_with_timeout before
        # dispatching the worker so the HTTP client aborts at the same moment
        # the main-thread watchdog gives up. Prevents a race where the worker
        # keeps mutating the shared ChatInterface after AED has already
        # declared a timeout and started recovering.
        self._request_timeout: float | None = None

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def _build_messages(self) -> list[dict]:
        """Return the message list to send to the API.

        Default: the canonical OpenAI serialization of the current
        interface. Subclasses override to mutate or wrap — e.g. the
        DeepSeek session injects ``reasoning_content`` onto assistant
        turns that carry tool calls, which DeepSeek V4 thinking mode
        requires for the round-trip.
        """
        return to_openai(self._interface)

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Detect provider 400-class context-length-exceeded errors.

        Covers OpenAI's canonical ``context_length_exceeded`` code plus the
        loose string-match heuristics used by compatible vendors (DeepSeek,
        Together, Groq, etc.) that often only signal via the message body.
        """
        if not isinstance(exc, openai.BadRequestError):
            return False
        # Canonical OpenAI code on the body's error object.
        code = None
        try:
            body = getattr(exc, "body", None) or {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
        except Exception:
            pass
        if code == "context_length_exceeded":
            return True
        msg = (str(exc) or "").lower()
        return any(
            needle in msg
            for needle in (
                "context length",
                "context_length_exceeded",
                "maximum context",
                "context window",
                "too many tokens",
                "input is too long",
                "prompt is too long",
            )
        )

    # _trim_context_one_round, _run_with_overflow_recovery, and
    # _inject_overflow_notice are inherited from ChatSession (base class).
    # Only _is_context_overflow_error needs to be provider-specific.

    def _pair_orphan_tool_calls(self, messages: list[dict]) -> list[dict]:
        """Final wire-layer guard: synthesize placeholder tool messages for
        any assistant[tool_calls] that are not immediately followed by
        matching role=tool messages. Does NOT mutate the canonical interface
        — synthesis is local to this serialization pass, re-runs from scratch
        next send.

        This catches several known pathologies:
        - An interleaved entry (e.g. a new system prompt appended because
          identity changed) slipping between an assistant[tool_calls] and
          its tool_results in the canonical interface.
        - A cancelled / partial tool batch where some tool_results never
          made it into the interface.
        - Any future drift we haven't anticipated.

        Once the real tool_result arrives in the interface later, the next
        serialization sees it naturally and no synthesis fires — implicit
        dedup without any stateful replace step.

        Each synthesis logs a warning with the tool_call_id and tool name
        so we can track how often this fires and fix the root cause if it
        becomes common.
        """
        patched: list[dict] = []
        for i, msg in enumerate(messages):
            patched.append(msg)
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                continue
            # Look ahead in the ORIGINAL input list for role=tool entries
            # immediately following this assistant turn. Synthesize
            # placeholders for any tool_call_id not covered.
            seen_ids: set[str] = set()
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tcid = messages[j].get("tool_call_id")
                if tcid:
                    seen_ids.add(tcid)
                j += 1
            # For each tool_call without a matching tool message, emit a
            # synthesized placeholder immediately after the assistant turn.
            for tc in tool_calls:
                tcid = tc.get("id")
                name = (tc.get("function") or {}).get("name", "?")
                if not tcid or tcid in seen_ids:
                    continue
                logger.warning(
                    "[wire-guard] synthesizing placeholder tool_result for "
                    "orphan tool_call id=%s name=%s — real result was not "
                    "in context at send time. Investigate if this recurs.",
                    tcid, name,
                )
                patched.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": "[synthesized placeholder — real result was not in context at send time]",
                })
                seen_ids.add(tcid)
        return patched

    def send(self, message) -> LLMResponse:
        """Send a user message (str), tool results (list of dicts), or
        drive the existing wire forward (``None``).

        For tool results, ``message`` is a list of ToolResultBlock instances
        built by :meth:`OpenAIAdapter.make_tool_result_message`.

        ``None`` is the "continue from wire" signal — the caller has
        already appended whatever needs to land (see
        ``base_agent/turn.py:_handle_tc_wake`` for the notification
        path).  No input append happens here; the existing wire is
        sent as-is.

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            # Tool results — list of ToolResultBlock instances
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — kernel-side splice point for mid-turn
        # tc_inbox drains. Wire tail is user[tool_results] or user[text],
        # so any (call, result) pair the hook splices is appended at a
        # safe boundary and rides along on this same API request.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard: synthesize placeholder tool messages for
            # any orphan assistant[tool_calls] that aren't immediately followed
            # by matching role=tool entries. Canonical interface untouched.
            candidate = self._pair_orphan_tool_calls(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        # 3. Make the API call (with auto-recovery on context overflow);
        #    revert interface on any other error.
        def _do_call():
            return self._client.chat.completions.create(**_build_kwargs())

        try:
            raw, total_dropped, rounds = self._run_with_overflow_recovery(_do_call)
        except Exception:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # 3b. If recovery fired (entries were dropped), inject the molt notice.
        if rounds > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

        # 4. Record assistant response into interface
        self._record_assistant_response(raw)

        return _parse_response(raw)

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to interface without an API call."""
        if tool_results:
            self._interface.add_tool_results(tool_results)

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session."""
        self._tools = _build_tools(tools) if tools else None
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        self._interface.add_system(
            self._interface.current_system_prompt or "", tools=tool_dicts,
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session."""
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)

    def reset(self) -> None:
        """Create a truly fresh session instance while preserving state.

        Reconstructs a new OpenAIChatSession with a fresh HTTP client
        and copies all attributes onto self, giving a clean connection and
        fresh internal state.
        """
        if self._client_kwargs:
            new_client = openai.OpenAI(**self._client_kwargs)
            new_session = OpenAIChatSession(
                client=new_client,
                model=self._model,
                interface=self._interface,
                tools=self._tools,
                tool_choice=self._tool_choice,
                extra_kwargs=self._extra_kwargs,
                client_kwargs=self._client_kwargs,
                context_window=self._context_window,
                prompt_cache_key=self._prompt_cache_key,
            )
            self.__dict__.update(new_session.__dict__)

    def _record_assistant_response(self, raw) -> None:
        """Parse a raw ChatCompletion and record the assistant response into the interface."""
        choice = raw.choices[0] if raw.choices else None
        blocks: list = []
        if choice and choice.message:
            msg = choice.message
            # Capture reasoning_content (DeepSeek/o-series) or reasoning
            # (OpenRouter) into a ThinkingBlock. Persisting it makes the
            # next request carry real reasoning back to the provider on
            # replay, instead of a constant placeholder. See issue #9.
            reasoning = (
                getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None)
            )
            if reasoning:
                blocks.append(ThinkingBlock(text=reasoning))
            if msg.content:
                blocks.append(TextBlock(text=msg.content))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append(ToolCallBlock(id=tc.id, name=tc.function.name, args=args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        usage_dict = {}
        if raw.usage:
            details = getattr(raw.usage, "completion_tokens_details", None)
            usage_dict = {
                "input_tokens": raw.usage.prompt_tokens or 0,
                "output_tokens": raw.usage.completion_tokens or 0,
                "thinking_tokens": getattr(details, "reasoning_tokens", 0) or 0 if details else 0,
            }
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage=usage_dict,
        )

    @staticmethod
    def _response_to_message(raw) -> dict:
        """Convert an OpenAI ChatCompletion response to a message dict for history."""
        choice = raw.choices[0] if raw.choices else None
        if not choice:
            return {"role": "assistant", "content": ""}
        msg = choice.message
        result: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            result["content"] = msg.content
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        if not msg.content and not msg.tool_calls:
            result["content"] = ""
        return result

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        """Send a streaming request.  Same shape as :meth:`send` —
        ``str`` / ``list`` / ``None`` (continue from wire).

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — see send() above for contract.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard — same as non-streaming send().
            candidate = self._pair_orphan_tool_calls(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                "stream": True,
                "stream_options": {"include_usage": True},
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        acc = StreamingAccumulator()
        usage = UsageMetadata()

        # Streaming overflow-recovery: most providers raise the 400 either
        # when ``create()`` returns or on the first iteration of the stream
        # — before any content has been emitted to ``on_chunk``. We open the
        # stream and pull the first chunk inside the recovery wrapper; once
        # that succeeds, we hand off to the regular streaming loop.
        def _open_and_first_chunk():
            stream = self._client.chat.completions.create(**_build_kwargs())
            it = iter(stream)
            try:
                first = next(it)
            except StopIteration:
                first = None
            return stream, it, first

        # 3. Stream; revert interface on error
        try:
            (stream, it, first_chunk), total_dropped, rounds = (
                self._run_with_overflow_recovery(_open_and_first_chunk)
            )
            if rounds > 0:
                self._inject_overflow_notice(
                    total_dropped=total_dropped, rounds=rounds,
                )
            # Re-stitch: first chunk + remaining iterator.
            def _chunks():
                if first_chunk is not None:
                    yield first_chunk
                for c in it:
                    yield c
            for chunk in _chunks():
                if not chunk.choices:
                    if chunk.usage:
                        cached = getattr(chunk.usage, "prompt_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        usage = UsageMetadata(
                            input_tokens=chunk.usage.prompt_tokens or 0,
                            output_tokens=chunk.usage.completion_tokens or 0,
                            thinking_tokens=(
                                getattr(
                                    getattr(chunk.usage, "completion_tokens_details", None),
                                    "reasoning_tokens",
                                    0,
                                )
                                or 0
                            ),
                            cached_tokens=cached_tokens,
                        )
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    acc.add_text(delta.content)
                    if on_chunk:
                        on_chunk(delta.content)
                # OpenRouter (and OpenAI o-series under some SDKs) streams
                # reasoning text deltas under `reasoning` / `reasoning_content`.
                # Capture into the thoughts channel, never into visible text.
                reasoning_delta = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "reasoning_content", None)
                )
                if reasoning_delta:
                    acc.add_thought(reasoning_delta)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc.add_tool_delta(
                            tc.index,
                            id=tc.id,
                            name=(tc.function.name if tc.function else None),
                            args_delta=(tc.function.arguments if tc.function else None),
                        )
        except Exception:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        # 4. Finalize
        acc.finish_all_tools()
        result = acc.finalize(usage=usage)


        # 5. Record assistant response into interface
        blocks: list = []
        # Persist captured reasoning as a ThinkingBlock so the next request
        # can replay it via reasoning_content (see issue #9).
        if result.thoughts:
            joined = "\n".join(t for t in result.thoughts if t)
            if joined:
                blocks.append(ThinkingBlock(text=joined))
        if result.text:
            blocks.append(TextBlock(text=result.text))
        for tc in result.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
            },
        )

        return result

    # -- Context compaction ---------------------------------------------------

    def context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# OpenAIResponsesSession
# ---------------------------------------------------------------------------


class OpenAIResponsesSession(ChatSession):
    """Session backed by OpenAI's Responses API with server-side state."""

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        instructions: str,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        previous_response_id: str | None = None,
        compact_threshold: int | None = None,
        interface: ChatInterface | None = None,
        prompt_cache_key: str | None = None,
    ):
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._response_id: str | None = previous_response_id
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        self._interface = interface or ChatInterface()
        # Optional OpenAI Responses ``prompt_cache_key`` — opts the request
        # into cross-request prompt caching keyed by a stable string. Sent
        # only when set; ``None`` leaves it off (default OpenAI behavior).
        # Note: ``prompt_cache_retention`` is deliberately never sent — the
        # Codex backend rejects it (``Unsupported parameter``).
        self._prompt_cache_key = prompt_cache_key

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def _convert_input(self, message) -> list[dict]:
        """Convert messages to Responses API input format.

        ``None`` yields ``[]`` — caller wants the existing
        ``previous_response_id`` chain to continue with no new input.
        """
        if message is None:
            return []
        if isinstance(message, str):
            return [{"role": "user", "content": message}]
        elif isinstance(message, dict):
            return [message]
        elif isinstance(message, list):
            items = []
            for item in message:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                ):
                    items.append(item)
                elif isinstance(item, dict) and item.get("role") == "tool":
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item["tool_call_id"],
                            "output": item["content"],
                        }
                    )
                else:
                    items.append(item)
            return items
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

    def send(self, message) -> LLMResponse:
        """Send a user message (str) or tool results (list of dicts)."""
        # Pre-request hook — fired for the kernel-side drain. NOTE: this
        # session uses server-side state (previous_response_id) and does
        # NOT commit message content to the canonical ChatInterface, so a
        # pair the hook splices is only visible to the LLM on the *next*
        # turn (when the interface re-syncs). This is acceptable because
        # the agent's local view is updated immediately for persistence
        # and inspection. Most agents using this session use it via Codex
        # OAuth (CodexResponsesSession), which DOES replay the full
        # interface and gets same-turn delivery.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        input_items = self._convert_input(message)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            **self._extra_kwargs,
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        if self._response_id:
            kwargs["previous_response_id"] = self._response_id
        if self._compact_threshold:
            kwargs["context_management"] = [
                {"type": "compaction", "compact_threshold": self._compact_threshold}
            ]
        if self._prompt_cache_key:
            kwargs["prompt_cache_key"] = self._prompt_cache_key

        raw = self._client.responses.create(**kwargs)
        self._response_id = raw.id
        return _parse_responses_api_response(raw)

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        """Send a streaming request."""
        # Pre-request hook — see send() above for contract + caveat.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        input_items = self._convert_input(message)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
            **self._extra_kwargs,
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        if self._response_id:
            kwargs["previous_response_id"] = self._response_id
        if self._compact_threshold:
            kwargs["context_management"] = [
                {"type": "compaction", "compact_threshold": self._compact_threshold}
            ]
        if self._prompt_cache_key:
            kwargs["prompt_cache_key"] = self._prompt_cache_key

        acc = StreamingAccumulator()
        response_id = None
        usage = UsageMetadata()
        seen_reasoning_summary_items: set[str] = set()

        stream = self._client.responses.create(**kwargs)
        for event in stream:
            if _handle_responses_reasoning_event(event, acc, seen_reasoning_summary_items):
                continue
            if event.type == "response.output_text.delta":
                acc.add_text(event.delta)
                if on_chunk:
                    on_chunk(event.delta)
            elif event.type == "response.function_call_arguments.delta":
                acc.add_tool_args(event.delta)
            elif event.type == "response.output_item.added":
                if getattr(event.item, "type", None) == "function_call":
                    acc.start_tool(id=event.item.call_id, name=event.item.name)
            elif event.type == "response.output_item.done":
                if getattr(event.item, "type", None) == "function_call":
                    acc.finish_tool()
            elif event.type == "response.completed":
                response_id = event.response.id
                if event.response.usage:
                    cached = getattr(event.response.usage, "input_tokens_details", None)
                    cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                    usage = UsageMetadata(
                        input_tokens=getattr(event.response.usage, "input_tokens", 0)
                        or 0,
                        output_tokens=getattr(event.response.usage, "output_tokens", 0)
                        or 0,
                        thinking_tokens=getattr(
                            event.response.usage, "output_tokens_details", None
                        )
                        and getattr(
                            event.response.usage.output_tokens_details,
                            "reasoning_tokens",
                            0,
                        )
                        or 0,
                        cached_tokens=cached_tokens,
                    )

        self._response_id = response_id
        return acc.finalize(usage=usage)

    def get_history(self) -> list[dict]:
        """Return minimal state for session persistence (server-side)."""
        return [{"_response_id": self._response_id}]

    @property
    def session_resume_id(self) -> str | None:
        """Return the response ID for session resumption."""
        return self._response_id


# ---------------------------------------------------------------------------
# OpenAIAdapter
# ---------------------------------------------------------------------------


class OpenAIAdapter(LLMAdapter):
    """Adapter that wraps the ``openai`` SDK for OpenAI and compatible APIs."""

    # Session class for the Chat Completions path. Subclasses override
    # this to inject provider-specific behavior (e.g. DeepSeek preserves
    # ``reasoning_content`` on tool-call turns for thinking-mode replay).
    # Responses-API sessions use OpenAIResponsesSession unconditionally
    # since that path is OpenAI-only.
    _session_class: type = OpenAIChatSession

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        use_responses: bool = False,
        force_responses: bool = False,
        max_rpm: int = 0,
        default_headers: dict | None = None,
        compact_threshold: int | None = 100_000,
        prompt_cache_key: str | bool | None = None,
    ):
        self.base_url = base_url
        self._use_responses = use_responses
        self._force_responses = force_responses
        # Prompt-cache-key policy for this adapter's OpenAI-compatible sessions:
        #   None  -> auto-derive a stable, namespaced default per model
        #   str   -> use this exact key for every session (override)
        #   False -> disable; never send prompt_cache_key
        # Default-on is intentional: every OpenAI-compatible endpoint LingTai
        # talks to accepted the field in the compat probe, and a stable key is
        # what lets successive agent turns hit the cross-request prompt cache.
        if prompt_cache_key is False:
            self._prompt_cache_key_policy: object = False
        elif prompt_cache_key is None:
            self._prompt_cache_key_policy = _AUTO_PROMPT_CACHE_KEY
        else:
            self._prompt_cache_key_policy = prompt_cache_key
        # Responses-API auto-compaction threshold (input tokens). The host
        # injects its resolved config value via the adapter factory
        # (lingtai/llm/_register.py:_openai reads provider defaults); when
        # unset we fall back to the intended 100k default. ``None`` disables
        # compaction entirely. Config is injected at construction here, never
        # read from a global module — see lingtai_kernel.config's contract.
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        kwargs["timeout"] = timeout_ms / 1000.0  # openai SDK uses seconds
        if default_headers:
            kwargs["default_headers"] = dict(default_headers)
        self._client_kwargs = dict(kwargs)  # store for session reset
        self._client = openai.OpenAI(**kwargs)
        self._setup_gate(max_rpm)

    # -- Prompt cache key ------------------------------------------------------

    def _default_prompt_cache_key(self, model: str) -> str:
        """Return the auto-derived, namespaced default prompt_cache_key.

        Namespacing keeps incompatible endpoints from sharing a cache slot:
          * official OpenAI (no base_url) -> ``lingtai-openai:{model}:v1``
          * custom/compatible base_url    -> ``lingtai-openai-compat:{host}:{model}:v1``

        Subclasses with a fixed provider identity (DeepSeek, Zhipu, MiMo,
        Codex) override this to use a clean provider namespace instead of the
        base_url host.
        """
        if not self.base_url:
            return f"lingtai-openai:{model}:v1"
        return f"lingtai-openai-compat:{_base_url_namespace(self.base_url)}:{model}:v1"

    def _resolve_prompt_cache_key(self, model: str) -> str | None:
        """Resolve the effective prompt_cache_key for a session on ``model``.

        Honors the adapter's policy: ``False`` disables (returns ``None``), an
        explicit string overrides, and the auto sentinel derives the default.
        """
        policy = self._prompt_cache_key_policy
        if policy is False:
            return None
        if policy is _AUTO_PROMPT_CACHE_KEY:
            return self._default_prompt_cache_key(model)
        return policy  # explicit override string

    # -- LLMAdapter interface --------------------------------------------------

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
        interaction_id: str | None = None,  # ignored — Gemini-specific
        context_window: int = 0,
    ) -> ChatSession:
        # Create interface if not provided
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=tool_dicts)

        use_responses = self._use_responses

        # Only use Responses API for actual OpenAI (not compatible providers)
        if use_responses and (not self.base_url or self._force_responses):
            session = self._create_responses_session(
                model,
                system_prompt,
                tools,
                json_schema,
                force_tool_call,
                interface,
                thinking,
            )
        else:
            # Fallback: Chat Completions for compatible providers
            session = self._create_completions_session(
                model, system_prompt, tools, json_schema, force_tool_call, interface, thinking,
                context_window=context_window,
            )
        return self._wrap_with_gate(session)

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
    ) -> OpenAIResponsesSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Responses API takes `reasoning: { effort: ... }`, not the Chat
        # Completions SDK's flat `reasoning_effort`. Sending the wrong shape
        # silently drops the field on the OpenAI Responses endpoint and 400s
        # on Codex's `/backend-api/codex/responses`.
        extra_kwargs.update(_responses_reasoning_kwargs(thinking))

        return OpenAIResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=self._compact_threshold,
            interface=interface,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
        )

    def _create_completions_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> OpenAIChatSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        # Extra kwargs for the completions call
        extra_kwargs: dict[str, Any] = {}

        # JSON schema enforcement (OpenAI Structured Outputs)
        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Reasoning effort for o-series models
        if thinking != "default":
            extra_kwargs["reasoning_effort"] = "high" if thinking == "high" else "low"

        # Subclass-provided extra_body (e.g. OpenRouter's reasoning include).
        # Merge rather than overwrite so callers adding their own extra_body
        # via extra_kwargs aren't clobbered.
        sub_extra_body = self._adapter_extra_body()
        if sub_extra_body:
            existing = extra_kwargs.get("extra_body") or {}
            extra_kwargs["extra_body"] = {**sub_extra_body, **existing}

        return self._session_class(
            client=self._client,
            model=model,
            interface=interface,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            client_kwargs=self._client_kwargs,
            context_window=context_window,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
        )

    def _adapter_extra_body(self) -> dict:
        """Return extra_body JSON fields to include on every request.

        Default is empty. Subclasses override to inject provider-specific
        kwargs (e.g. OpenRouter needs `reasoning: {include: true}` to
        surface reasoning text on reasoning-capable models).
        """
        return {}

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
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # contents can be a string or a list of content blocks
        if isinstance(contents, str):
            messages.append({"role": "user", "content": contents})
        elif isinstance(contents, list):
            messages.append({"role": "user", "content": contents})
        else:
            messages.append({"role": "user", "content": str(contents)})

        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens

        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        raw = self._gated_call(lambda: self._client.chat.completions.create(**kwargs))
        return _parse_response(raw)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock."""
        return ToolResultBlock(
            id=tool_call_id or f"call_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        """Check if the exception is an OpenAI rate-limit error."""
        return isinstance(exc, openai.RateLimitError)

    # -- Convenience properties ------------------------------------------------

    @property
    def client(self):
        """Escape hatch — the underlying ``openai.OpenAI`` client."""
        return self._client


# ---------------------------------------------------------------------------
# CodexResponsesSession — stateless variant for ChatGPT-OAuth backend
# ---------------------------------------------------------------------------


class CodexResponsesSession(OpenAIResponsesSession):
    """Stateless Responses session for Codex's `/backend-api/codex/responses`.

    Differences from the parent:
      * `previous_response_id` is never sent — Codex's backend doesn't
        persist turns server-side. The full input must be carried each
        request by the caller (interface layer accumulates messages).
      * `store=False` is forced — same reason.
      * Streaming is forced (`stream=True` on send/send_stream alike) —
        non-streaming Codex requests return data the SDK can't unmarshal.
      * Optional stable ``session_id`` / ``thread_id`` request headers are
        sent for REST prompt-cache affinity (issue #378). They are HTTP
        headers (``extra_headers``), not request-body fields, and are
        independent of ``prompt_cache_key`` (both may be sent together). The
        keys are underscored (``session_id`` / ``thread_id``) to match the
        Codex backend literally — a hyphenated spelling loses cache affinity.
    """

    def __init__(
        self,
        *args,
        session_id: str | None = None,
        thread_id: str | None = None,
        account_id: str | None = None,
        installation_id: str | None = None,
        metadata_sandbox: str = "lingtai",
        ws_enabled: bool | None = None,
        ws_epoch_reset_turns: int | None = None,
        ws_transport_factory: "Callable[[str, dict[str, str]], Any] | None" = None,
        base_url: str | None = None,
        api_key: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # EXPERIMENTAL Codex Responses-over-WebSocket transport (#471). Gated:
        # ``ws_enabled`` defaults to the ``LINGTAI_CODEX_WS`` env switch (on
        # unless explicitly disabled with ``0``/``false``/``no``/``off``).
        # ``ws_transport_factory`` is an injection
        # seam used by the mock tests; when ``None`` and the gate is on, the real
        # factory is used (and itself falls back to HTTP if ``websockets`` is
        # missing). ``_ws_session`` holds the per-turn last_request/last_response
        # + captured ``x-codex-turn-state`` used to compute incremental deltas
        # exactly like the official ``ModelClientSession`` (``client.rs:214-240``).
        self._ws_enabled = _codex_ws_enabled() if ws_enabled is None else bool(ws_enabled)
        self._ws_transport_factory = ws_transport_factory
        self._ws_base_url = base_url
        self._ws_api_key = api_key if isinstance(api_key, str) and api_key else None
        self._ws_session = _CodexWebsocketSession()
        # Set while a websocket request is in flight: the full converted input we
        # sent this turn, used by ``_ws_record_baseline_from_interface`` to derive
        # a converter-stable delta baseline once the assistant turn is recorded.
        self._ws_pending_baseline_input: list[dict[str, Any]] | None = None
        # Per-session freeze of model-facing ``function_call_output.output`` strings
        # keyed by ``call_id``. The kernel moves latest-only resident-meta blocks
        # off older tool results onto the freshest one each turn, which rewrites an
        # older ``ToolResultBlock.content`` in place and would change that result's
        # converted ``output`` on replay — breaking the strict-prefix WS delta
        # baseline. Freezing the first-seen output per call_id keeps replay
        # byte-identical while the freshest result still carries live meta (it is
        # first-seen on its own turn). See ``_freeze_responses_outputs``.
        self._ws_frozen_outputs: dict[str, str] = {}
        # Last websocket delta decision diagnostic (safe metadata only): why the
        # request went ``ws_incremental`` vs ``ws_full``. Surfaced in usage.extra.
        self._ws_last_diag: dict[str, Any] = {}
        self._ws_cache_ledger: deque[dict[str, Any]] = deque(maxlen=20)
        self._ws_cache_call_seq = 0
        # One Codex websocket connection may carry multiple ``response.create``
        # frames. Live smoke showed ChatGPT Codex resolves
        # ``previous_response_id`` only when the follow-up request stays on the
        # same websocket session. Keep the transport alive across sequential
        # sends in this ChatSession, and close/reset it on transport failures.
        self._ws_transport = None
        self._ws_epoch_reset_turns_explicit = ws_epoch_reset_turns is not None
        if ws_epoch_reset_turns is None:
            self._ws_epoch_reset_turn_limit = _codex_ws_epoch_reset_turns()
        else:
            self._ws_epoch_reset_turn_limit = max(0, int(ws_epoch_reset_turns))
        self._ws_turns_since_epoch_reset = 0
        self._ws_epoch_reset_reason_pending: str | None = None
        # The user's own ChatGPT account id (decoded upstream from their OAuth
        # auth data). When present it is sent as the ``ChatGPT-Account-ID`` HTTP
        # Account routing is a ChatGPT-account concern and intentionally
        # orthogonal to the app-name identity (``originator``/``User-Agent``),
        # which remains honest LingTai by default (see
        # ``_codex_identity_headers`` / ``_CODEX_IMPERSONATE_OFFICIAL_CLI``).
        # It is a non-secret account identifier and is never copied into usage
        # metadata or logs.
        self._account_id = account_id if isinstance(account_id, str) and account_id else None
        self._installation_id = installation_id
        self._metadata_sandbox = metadata_sandbox or "lingtai"
        # Codex REST cache-affinity identity: ONE stable per-agent value used
        # byte-identically for ``prompt_cache_key`` / ``session_id`` /
        # ``thread_id`` on EVERY request, and NEVER changed for the life of the
        # session. There is no epoch-stamping and no rotation — the id is a pure
        # deterministic hash of the agent path (resolved upstream by the adapter
        # and passed in here). Priority for the single value: ``prompt_cache_key``
        # (explicit request-body cache key) > ``session_id`` > ``thread_id``.
        #
        # The header carve-out (issue #378): ``session_id`` / ``thread_id``
        # headers route the backend cache slot and MUST be per-agent. The
        # model-only fallback ``prompt_cache_key`` form
        # (``lingtai-codex:{model}:v1``) is shared by every agent on a model, so
        # it is NEVER promoted to headers (that would collapse all agents onto one
        # slot). Headers are emitted only when an explicit ``session_id`` /
        # ``thread_id`` was supplied (the per-agent path or a direct-construction
        # test); a cache-key-only construction (bare/no-anchor) keeps its body
        # ``prompt_cache_key`` and sends NO headers.
        self._current_id = self._prompt_cache_key or session_id or thread_id
        self._prompt_cache_key = self._current_id
        self._has_header_identity = bool(session_id or thread_id)
        if self._has_header_identity:
            self._session_id = self._current_id
            self._thread_id = self._current_id
        else:
            self._session_id = None
            self._thread_id = None

    def _cache_affinity_headers(self) -> dict[str, str]:
        """Return the stable ``session_id`` / ``thread_id`` headers, if any.

        The header names use UNDERSCORES (``session_id`` / ``thread_id``) to match
        what the official Codex CLI sends on its
        ``/backend-api/codex/responses`` calls (verified by capturing real Codex
        CLI traffic, 2026-06). This spelling is load-bearing: the Codex backend
        matches the literal underscore key, so emitting hyphenated
        ``session-id`` / ``thread-id`` would silently lose cache affinity and
        fragment every request onto a cold slot (cache/cost explosion). Do NOT
        rename these to HTTP-looking hyphenated forms. The backend routes the
        prompt-cache slot to a sticky-warm replica off a STABLE session id; we
        send one fixed per-agent value for the life of the session and never
        change it.
        """
        headers: dict[str, str] = {}
        if self._session_id:
            headers["session_id"] = self._session_id
        if self._thread_id:
            headers["thread_id"] = self._thread_id
        return headers

    def _codex_metadata_headers(self) -> dict[str, str]:
        """Return honest LingTai Codex metadata headers for this request."""

        if not (self._session_id and self._thread_id):
            return {}
        turn_metadata = {
            "session_id": self._session_id,
            "thread_id": self._thread_id,
            "turn_id": str(uuid.uuid4()),
            "sandbox": self._metadata_sandbox,
            "turn_started_at_unix_ms": int(time.time() * 1000),
        }
        return {
            "x-client-request-id": str(uuid.uuid4()),
            "x-codex-window-id": f"{self._session_id}:0",
            "x-codex-turn-metadata": json.dumps(turn_metadata, separators=(",", ":"), sort_keys=True),
        }

    def _codex_client_metadata(self) -> dict[str, str]:
        if not self._installation_id:
            return {}
        return {"x-codex-installation-id": self._installation_id}

    def _effective_affinity(self) -> tuple[str | None, dict[str, str]]:
        """Resolve this request's (prompt_cache_key, headers) pair.

        Always the single stable per-agent id — fixed for the life of the
        session, used byte-identically for ``prompt_cache_key`` / ``session_id``
        / ``thread_id`` on every request. No rotation, no epoch, no time
        dependence.
        """
        return self._prompt_cache_key, self._cache_affinity_headers()

    @staticmethod
    def _usage_extra(
        affinity_headers: dict[str, str],
        cache_key: str | None,
        *,
        request_mode: str | None = None,
        previous_response_id: str | None = None,
        store: bool | None = None,
        fallback_error_type: str | None = None,
        fallback_error_message: str | None = None,
        ws_diag: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Build the token-ledger ``UsageMetadata.extra`` for this request.

        Surfaces the ACTUAL current ids used so a stalled-cache rotation is
        visible in ``token_ledger.jsonl`` alongside the pre-rotation requests.
        Only the short non-secret affinity ids ride here — no prompt body, no
        tokens, no OAuth secret.

        ``ws_diag`` carries the websocket delta decision — ONLY safe metadata
        (reason class, item counts, changed non-input KEY names, a mismatch
        index). It explains why a turn went ``ws_full`` vs ``ws_incremental``
        (``no_baseline`` / ``missing_response_id`` / ``missing_output_items`` /
        ``prefix_mismatch`` / ``non_input_fields_changed`` / ``ok`` …). No prompt,
        tool-result, reasoning, token, header, or secret content is included.
        """
        extra: dict[str, str] = {}
        if affinity_headers.get("session_id"):
            extra["codex_session_id"] = affinity_headers["session_id"]
        if affinity_headers.get("thread_id"):
            extra["codex_thread_id"] = affinity_headers["thread_id"]
        if cache_key:
            extra["codex_prompt_cache_key"] = cache_key
        if request_mode:
            extra["codex_request_mode"] = request_mode
        if previous_response_id:
            extra["codex_previous_response_id"] = previous_response_id
        if store is not None:
            extra["codex_store"] = str(bool(store)).lower()
        if fallback_error_type:
            extra["codex_fallback_error_type"] = fallback_error_type
        if fallback_error_message:
            extra["codex_fallback_error_message"] = fallback_error_message[:240]
        if ws_diag:
            reason = ws_diag.get("reason")
            if reason:
                extra["codex_ws_delta_reason"] = str(reason)
            changed = ws_diag.get("changed_fields")
            if changed:
                # Key NAMES only (e.g. "tools,include"), never their values.
                extra["codex_ws_changed_fields"] = ",".join(str(k) for k in changed)[:240]
            for key in (
                "baseline_len",
                "cur_input_len",
                "delta_len",
                "mismatch_index",
                "mismatch_prev_type",
                "mismatch_prev_role",
                "mismatch_prev_keys",
                "mismatch_prev_hash",
                "mismatch_cur_type",
                "mismatch_cur_role",
                "mismatch_cur_keys",
                "mismatch_cur_hash",
                "epoch_reset_reason",
                "epoch_reset_turns",
                "turns_since_epoch_reset",
            ):
                if ws_diag.get(key) is not None:
                    extra[f"codex_ws_{key}"] = str(ws_diag[key])[:240]
        return extra

    def send(self, message) -> LLMResponse:
        # Force the streaming path — Codex doesn't serve non-streaming JSON.
        return self.send_stream(message, on_chunk=None)

    # -- Experimental Responses-over-WebSocket transport (#471) ---------------

    # Request-body keys that are NOT part of the websocket ``response.create``
    # frame's comparable shape — SDK transport-only kwargs. They are excluded
    # when building the request dict used for the delta-extension check (the
    # official algorithm compares the request struct, which never contains
    # transport headers). ``input`` is compared separately by the algorithm.
    _WS_NON_FRAME_KEYS = ("extra_headers", "extra_body", "timeout")

    def _ws_frame_request(self, kwargs: dict[str, Any], input_items: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the comparable ``response.create`` request dict from ``kwargs``.

        Drops SDK transport-only keys (headers/body/timeout) and forces
        ``store=false`` (the ChatGPT Codex backend rejects ``store=true`` —
        ``client.rs:722``). ``client_metadata`` from ``extra_body`` is folded in
        as a body field so it participates in the non-input equality check, like
        the official request struct's ``client_metadata`` (``common.rs:189``).
        """
        frame: dict[str, Any] = {
            k: v for k, v in kwargs.items() if k not in self._WS_NON_FRAME_KEYS
        }
        frame["type"] = "response.create"
        frame["store"] = False
        frame["stream"] = True
        frame["input"] = list(input_items)
        extra_body = kwargs.get("extra_body") or {}
        if isinstance(extra_body, dict) and extra_body.get("client_metadata"):
            frame["client_metadata"] = extra_body["client_metadata"]
        return frame

    def _codex_ws_open(
        self,
        kwargs: dict[str, Any],
        *,
        full_replay_input_items: list[dict[str, Any]],
    ) -> tuple[Any, str, str | None]:
        """Open a websocket ``response.create`` stream, or raise ``_CodexWsFallback``.

        Returns ``(event_iterable, request_mode, previous_response_id_or_None)``.

        Mirrors the official path: build the full request, then via
        ``prepare_websocket_request`` (``client.rs:998-1024``) decide whether to
        send only the delta input with ``previous_response_id`` (when the new
        full request is a strict extension of the previous request + its output
        items) or the full input with no previous id (first request / mismatch).
        The connection captures ``x-codex-turn-state`` from the handshake and
        replays it within the turn; ``response.processed`` is sent after the
        completed response (``responses_websocket.rs:208-240``).
        """
        # The full request shape (full input) is what we record as
        # ``last_request`` for the NEXT turn's delta baseline, regardless of what
        # we actually transmit this turn.
        full_request = self._ws_frame_request(kwargs, full_replay_input_items)

        previous_response_id: str | None = None
        frame_input = full_replay_input_items
        request_mode = "ws_full"
        # Safe fallback diagnostics (no prompt/tool/secret content): explains why
        # we are sending full input instead of a delta. Surfaced via
        # ``self._ws_last_diag`` so ``send_stream`` can ride it into ``usage.extra``
        # / the token ledger. Only classes / counts / key-names / a short hash.
        diag: dict[str, Any]
        epoch_reset_reason = self._ws_epoch_reset_reason_pending
        self._ws_epoch_reset_reason_pending = None
        last = self._ws_session.last_response
        if last is None:
            diag = {"reason": "no_baseline"}
        elif not last.response_id:
            diag = {"reason": "missing_response_id"}
        elif self._ws_session.last_request is None:
            diag = {"reason": "missing_baseline_request"}
        else:
            delta, diag = _codex_incremental_diagnose(
                self._ws_session.last_request,
                last.items_added,
                full_request,
                allow_empty_delta=True,
            )
            if delta is None and not last.items_added:
                # Baseline lacks the server's output items (e.g. the previous turn
                # completed but produced no recordable output item). Make the
                # reason explicit rather than a generic prefix mismatch.
                if diag.get("reason") == "prefix_mismatch" and diag.get("baseline_len") == len(
                    self._ws_session.last_request.get("input") or []
                ):
                    diag = {**diag, "reason": "missing_output_items"}
            if delta is not None:
                previous_response_id = last.response_id
                frame_input = delta
                request_mode = "ws_incremental"
        if epoch_reset_reason:
            diag = {
                "reason": "epoch_reset",
                "epoch_reset_reason": epoch_reset_reason,
                "epoch_reset_turns": self._ws_epoch_reset_turn_limit,
                "turns_since_epoch_reset": self._ws_turns_since_epoch_reset,
                "cur_input_len": len(full_replay_input_items),
            }
        self._ws_last_diag = dict(diag)

        # Build the transmitted frame (delta or full).
        frame = dict(full_request)
        frame["input"] = list(frame_input)
        if previous_response_id:
            frame["previous_response_id"] = previous_response_id

        # Handshake headers: the per-request Codex headers plus the websocket
        # beta header, plus the captured per-turn ``x-codex-turn-state`` (only
        # after it has been captured earlier in the same turn).
        headers = dict(kwargs.get("extra_headers") or {})
        if self._ws_api_key and not any(k.lower() == "authorization" for k in headers):
            bearer = self._ws_api_key.strip()
            if not bearer.lower().startswith("bearer "):
                bearer = f"Bearer {bearer}"
            headers["Authorization"] = bearer
        headers[_CODEX_WS_BETA_HEADER] = _CODEX_WS_BETA_VALUE
        if self._ws_session.turn_state:
            headers[_CODEX_TURN_STATE_HEADER] = self._ws_session.turn_state

        transport = self._ws_transport
        if transport is None:
            factory = self._ws_transport_factory or _default_codex_ws_transport_factory
            url = _codex_ws_url(self._ws_base_url)
            transport = factory(url, headers)

            # connect() may raise _CodexWsFallback (426/connect/auth); let it
            # propagate to the caller which falls back to HTTP.
            captured_turn_state = transport.connect(headers=headers)
            if captured_turn_state and not self._ws_session.turn_state:
                # Capture once per turn. With a persistent websocket connection
                # there may be no second handshake to replay this header on, but
                # keep it for reconnects within the same turn.
                self._ws_session.turn_state = captured_turn_state
            self._ws_transport = transport

        # Record the FULL request as the next delta baseline before streaming.
        # If streaming fails before a completed response, restore the previous
        # baseline so a later retry does not compare against an unaccepted turn.
        previous_last_request = self._ws_session.last_request
        previous_last_response = self._ws_session.last_response
        self._ws_session.last_request = full_request
        # Park the baseline for THIS request's input length so the post-stream
        # baseline (recomputed from the canonical interface in ``send_stream``)
        # knows where the server-added output items begin. The previous-baseline
        # values above are restored on any stream failure.
        self._ws_pending_baseline_input = list(full_replay_input_items)

        def _events():
            response_id_local: str | None = None
            try:
                for event in transport.stream(frame):
                    etype = getattr(event, "type", None)
                    if etype == "response.completed":
                        response_id_local = getattr(getattr(event, "response", None), "id", None)
                    yield event
            except _CodexWsFallback:
                self._ws_session.last_request = previous_last_request
                self._ws_session.last_response = previous_last_response
                self._ws_pending_baseline_input = None
                self._close_ws_transport(transport)
                raise
            except Exception:
                self._ws_session.last_request = previous_last_request
                self._ws_session.last_response = previous_last_response
                self._ws_pending_baseline_input = None
                self._close_ws_transport(transport)
                raise
            # Record the completed response so the next request can delta off it,
            # then notify the server it was processed (official posts
            # ``response.processed`` after handling a completed response).
            #
            # NOTE: ``items_added`` is intentionally left EMPTY here. The server's
            # streamed ``response.output_item.done`` items are in the Responses
            # *output* schema (``{"type":"message","id":...,"status":...,
            # "content":[{"type":"output_text",...}]}``), which does NOT compare
            # equal to the *input* schema this session re-derives next turn via
            # ``to_responses_input`` (``{"role":"assistant","content":<str>}`` +
            # ``{"type":"reasoning","summary":[...]}``). Using the raw output items
            # as the delta baseline therefore failed the strict-prefix check on
            # EVERY follow-up turn, collapsing every real agent turn to ``ws_full``.
            # The correct, converter-stable baseline is filled in by
            # ``_ws_record_baseline_from_interface`` after ``send_stream`` records
            # the assistant turn into the canonical interface (#471 delta fix).
            if response_id_local:
                self._ws_session.last_response = _CodexLastResponse(
                    response_id=response_id_local,
                    items_added=[],
                )
                try:
                    transport.send_response_processed(response_id_local)
                except Exception as exc:  # pragma: no cover - best-effort ack
                    logger.debug("codex ws response.processed failed: %s", exc)

        return _events(), request_mode, previous_response_id

    def _ws_record_baseline_from_interface(self) -> None:
        """Fill the WS delta baseline from the converter, not raw server output.

        Called by ``send_stream`` AFTER the just-completed assistant turn has been
        recorded into the canonical interface. The next turn's full input is
        ``to_responses_input(interface)`` (plus that turn's new user/tool items),
        so the only baseline that can ever strict-prefix-match it is one expressed
        in the SAME converter schema. We therefore derive ``items_added`` as the
        suffix of the current full converted input beyond the input we actually
        sent this turn — i.e. exactly the assistant turn the server added,
        rendered in input schema. This is the conservative fix for the
        ``ws_full``-every-turn root cause: it makes the baseline and the next full
        request byte-comparable by construction. No prompt/secret content leaves
        the process; this only rearranges in-memory request dicts.
        """
        pending = getattr(self, "_ws_pending_baseline_input", None)
        last = self._ws_session.last_response
        if pending is None or last is None or not last.response_id:
            self._ws_pending_baseline_input = None
            return
        full_now = self._frozen_responses_input(self._interface)
        base_len = len(pending)
        # Only treat the tail as server-added output when the interface still
        # strictly extends what we sent (it always should: we appended an
        # assistant turn). If it does not, leave ``items_added`` empty so the next
        # turn falls back to ``ws_full`` with a ``missing_output_items`` reason
        # rather than chaining off a baseline we cannot prove.
        if full_now[:base_len] == pending and base_len <= len(full_now):
            tail = full_now[base_len:]
            # Drop trailing SYNTHESIZED orphan tool-result placeholders from the
            # baseline. When the assistant turn ends on an unanswered
            # ``function_call``, ``to_responses_input`` injects a placeholder
            # ``function_call_output`` (issue #170 wire guard). That placeholder is
            # NOT what the next tool-result continuation actually sends (the real
            # output replaces it), so keeping it in the baseline guarantees a
            # ``prefix_mismatch`` and forces ``ws_full`` on every tool loop. Trim
            # the trailing placeholder(s) so the real continuation strictly extends
            # the baseline and stays ``ws_incremental``.
            while tail and _ws_is_synthesized_orphan_output(tail[-1]):
                tail = tail[:-1]
            last.items_added = tail
        else:
            last.items_added = []
        self._ws_pending_baseline_input = None

    def _close_ws_transport(self, transport=None) -> None:
        current = self._ws_transport
        if transport is not None and current is not transport:
            return
        self._ws_transport = None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass


    def _refresh_ws_epoch_reset_turn_limit(self) -> None:
        if getattr(self, "_ws_epoch_reset_turns_explicit", False):
            return
        self._ws_epoch_reset_turn_limit = _codex_ws_epoch_reset_turns()

    def _reset_ws_epoch(self, reason: str) -> None:
        """Start a fresh websocket response-id epoch.

        The Codex backend cannot delete already-accepted ``previous_response_id``
        state. Periodically forcing a full request from the current local history,
        while clearing the frozen tool-output map and old response id, prevents
        stale latest-meta bytes from living forever in the remote chain.
        """

        self._close_ws_transport()
        self._ws_session = _CodexWebsocketSession()
        self._ws_pending_baseline_input = None
        self._ws_frozen_outputs.clear()
        self._ws_turns_since_epoch_reset = 0
        self._ws_epoch_reset_reason_pending = reason

    def _maybe_reset_ws_epoch(self) -> None:
        self._refresh_ws_epoch_reset_turn_limit()
        limit = self._ws_epoch_reset_turn_limit
        if limit <= 0:
            return
        if self._ws_turns_since_epoch_reset < limit:
            return
        self._reset_ws_epoch("turn_count")

    def on_history_summarized(self, summarized_ids: list[str]) -> None:
        if not summarized_ids or not self._ws_enabled:
            return
        self._reset_ws_epoch("summarize")

    def on_notification_dismissed(self, channel: str | None = None) -> None:
        # Notification dismiss is high-frequency housekeeping, not context
        # compaction. It should not break the Codex previous_response_id
        # chain; only summarize rewrites old tool-result payloads enough to
        # require a fresh ws_full epoch.
        return None

    @staticmethod
    def _ws_cache_rate(input_tokens: int, cached_tokens: int) -> float | None:
        if input_tokens <= 0:
            return None
        return round(cached_tokens / input_tokens, 2)

    @staticmethod
    def _ws_tokens_k(tokens: int) -> float:
        return round(max(0, int(tokens or 0)) / 1000, 1)

    @staticmethod
    def _ws_request_mode_code(request_mode: str | None) -> str:
        if request_mode == "ws_full":
            return "F"
        if request_mode == "ws_incremental":
            return "I"
        return str(request_mode or "unknown")[:12]

    @staticmethod
    def _ws_reason_code(ws_diag: dict[str, Any] | None) -> str:
        if not isinstance(ws_diag, dict):
            return ""
        reason = str(ws_diag.get("reason") or "")
        if not reason or reason == "ok":
            return ""
        if reason == "epoch_reset":
            reset_reason = str(ws_diag.get("epoch_reset_reason") or "")
            reset_codes = {
                "summarize": "sum",
                "turn_count": "turns",
            }
            if reset_reason in reset_codes:
                return reset_codes[reset_reason]
            return f"epoch:{reset_reason}" if reset_reason else "epoch"
        reason_codes = {
            "prefix_mismatch": "pm",
            "no_baseline": "nb",
            "missing_response_id": "no_prev",
            "missing_baseline_request": "no_base",
            "missing_output_items": "no_out",
        }
        return reason_codes.get(reason, reason[:24])

    def _record_ws_cache_ledger(
        self,
        *,
        request_mode: str | None,
        usage: UsageMetadata,
        ws_diag: dict[str, Any] | None,
    ) -> None:
        if request_mode not in {"ws_full", "ws_incremental"}:
            return
        input_tokens = max(0, int(getattr(usage, "input_tokens", 0) or 0))
        cached_tokens = max(0, int(getattr(usage, "cached_tokens", 0) or 0))
        cached_tokens = min(cached_tokens, input_tokens)
        miss_tokens = max(0, input_tokens - cached_tokens)
        mode = self._ws_request_mode_code(request_mode)
        self._ws_cache_call_seq += 1
        self._ws_cache_ledger.append(
            {
                "seq": self._ws_cache_call_seq,
                "mode": mode,
                "cache": self._ws_cache_rate(input_tokens, cached_tokens),
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "miss_tokens": miss_tokens,
                "reason": self._ws_reason_code(ws_diag) if mode == "F" else "",
            }
        )

    def _ws_cache_ledger_comment(self) -> dict[str, Any]:
        entries = list(self._ws_cache_ledger)
        latest_seq = int(entries[-1]["seq"]) if entries else 0
        rows = [
            [
                latest_seq - int(entry["seq"]),
                entry["mode"],
                entry["cache"],
                self._ws_tokens_k(int(entry["input_tokens"])),
                self._ws_tokens_k(int(entry["miss_tokens"])),
                entry["reason"],
            ]
            for entry in entries
        ]
        total_input = sum(int(entry["input_tokens"]) for entry in entries)
        total_cached = sum(int(entry["cached_tokens"]) for entry in entries)
        total_miss = sum(int(entry["miss_tokens"]) for entry in entries)
        last_ws_full = next(
            (entry for entry in reversed(entries) if entry["mode"] == "F"),
            None,
        )
        if last_ws_full is None:
            last_ws_full_comment = {
                "api_calls_ago": None,
                "reason": "not_seen" if entries else "not_recorded",
            }
        else:
            last_ws_full_comment = {
                "api_calls_ago": latest_seq - int(last_ws_full["seq"]),
                "reason": last_ws_full["reason"] or "ws_full",
            }
        return {
            "window_api_calls": 20,
            "recorded_api_calls": len(entries),
            "cols": ["ago", "mode", "cache", "in_k", "miss_k", "reason"],
            "rows": rows,
            "summary": {
                "api_calls": len(entries),
                "cache_rate": self._ws_cache_rate(total_input, total_cached),
                "ws_full_count": sum(1 for entry in entries if entry["mode"] == "F"),
                "miss_k": self._ws_tokens_k(total_miss),
            },
            "last_ws_full": last_ws_full_comment,
            "legend": {
                "I": "ws_incremental",
                "F": "ws_full",
                "sum": "epoch_reset:summarize",
                "turns": "epoch_reset:turn_count",
                "pm": "prefix_mismatch",
                "nb": "no_baseline",
            },
        }

    @staticmethod
    def _ws_maintenance_hint(
        *,
        recorded_api_calls: int,
        last_ws_full_api_calls_ago: int | None,
    ) -> dict[str, Any]:
        if recorded_api_calls <= 0:
            return {
                "non_urgent_summarize": "unknown",
                "reason": "no Codex websocket cache ledger entries yet",
            }
        if last_ws_full_api_calls_ago is None:
            return {
                "non_urgent_summarize": "ok",
                "reason": "no ws_full in the last 20 Codex API calls",
            }
        wait_remaining = max(0, 5 - int(last_ws_full_api_calls_ago))
        if wait_remaining > 0:
            return {
                "non_urgent_summarize": "wait",
                "wait_until_last_ws_full_api_calls_ago": 5,
                "wait_api_calls_remaining": wait_remaining,
                "reason": (
                    f"last ws_full was {last_ws_full_api_calls_ago} API calls ago; "
                    f"wait {wait_remaining} more if not urgent"
                ),
            }
        return {
            "non_urgent_summarize": "ok",
            "wait_until_last_ws_full_api_calls_ago": 5,
            "wait_api_calls_remaining": 0,
            "reason": f"last ws_full was {last_ws_full_api_calls_ago} API calls ago",
        }

    def adapter_comment(self) -> dict[str, Any] | None:
        if not self._ws_enabled:
            return {
                "adapter": "codex",
                "feature": "stateless_full_replay",
                "ws_enabled": False,
                "summary": (
                    "Codex WebSocket continuation is disabled, so every request is a "
                    "full stateless replay. There is no ws_incremental/ws_full "
                    "previous_response_id chain to preserve."
                ),
                "summarize_ws_full_note": (
                    "Summarize can still compact redundant carried-forward context "
                    "before the next full replay. Notification dismiss only clears "
                    "notification state; it does not compact redundant context or "
                    "create a ws_full epoch boundary in stateless mode."
                ),
            }
        self._refresh_ws_epoch_reset_turn_limit()
        limit = self._ws_epoch_reset_turn_limit
        next_reset_in = None
        if limit > 0:
            next_reset_in = max(0, limit - self._ws_turns_since_epoch_reset)
        cache_note = (
            "Summarize rewrites older tool-result payloads, compacts redundant "
            "carried-forward context, and breaks Codex's previous_response_id/"
            "ws_incremental prefix; the next request must be ws_full, usually "
            "causing more cache miss. Wait until >=5 API calls after the last "
            "ws_full before non-urgent summarize. Notification dismiss is only "
            "notification cleanup: it does not compact redundant context and does "
            "not trigger a ws_full epoch reset."
        )
        cache_ledger = self._ws_cache_ledger_comment()
        last_ws_full = cache_ledger["last_ws_full"]
        last_ws_full_api_calls_ago = last_ws_full["api_calls_ago"]
        return {
            "adapter": "codex",
            "feature": "responses_websocket_epoch_reset",
            "ws_enabled": True,
            "epoch_reset_turns": limit,
            "turns_since_epoch_reset": self._ws_turns_since_epoch_reset,
            "next_reset_in": next_reset_in,
            "last_ws_full_api_calls_ago": last_ws_full_api_calls_ago,
            "last_ws_full_reason": last_ws_full["reason"],
            "summary": (
                "Codex reuses remote Responses state through previous_response_id. "
                "A fresh ws_full epoch clears only request-side continuation state "
                "and rebuilds the next request from local chat_history; local history "
                "is not deleted or summarized."
            ),
            "cache_ledger": cache_ledger,
            "maintenance_hint": self._ws_maintenance_hint(
                recorded_api_calls=cache_ledger["recorded_api_calls"],
                last_ws_full_api_calls_ago=last_ws_full_api_calls_ago,
            ),
            "cache_note": cache_note,
            "summarize_ws_full_note": cache_note,
        }

    def reset_provider_turn_state(self) -> None:
        self.reset_ws_turn()

    def reset_ws_turn(self) -> None:
        """Reset per-turn websocket state at a new user turn boundary.

        The official ``x-codex-turn-state`` is per-turn volatile: captured on the
        first request of a turn, replayed within the turn, and reset for the next
        user turn (``client.rs:227-240`` / ``turn_state.rs`` tests). Callers that
        track turn boundaries invoke this between user turns; within a tool loop
        it must NOT be called so the token (and incremental chain) persist.
        """
        self._ws_session.turn_state = None

    def _frozen_responses_input(self, iface: ChatInterface) -> list[dict[str, Any]]:
        """``to_responses_input`` with per-session tool-result output freezing.

        Routes every Codex WS conversion through ``_freeze_responses_outputs`` so
        the model-facing ``function_call_output.output`` for a given ``call_id``
        stays byte-identical across turns, even after the kernel moves latest-only
        resident-meta off an older result. All three WS conversion sites (full
        replay, per-turn delta, baseline tail) share ``self._ws_frozen_outputs`` so
        the baseline and the next full request remain strict-prefix comparable.
        """
        return _freeze_responses_outputs(
            to_responses_input(iface), self._ws_frozen_outputs
        )

    def _interface_entries_to_responses_input(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Serialize newly-added ChatInterface entries for stateful Codex turns."""

        if not entries:
            return []
        delta_interface = ChatInterface()
        # ChatInterface.entries intentionally exposes the mutable backing list;
        # populate a temporary interface so the normal converter preserves
        # reasoning/tool-result shapes and pairing behavior for the delta.
        delta_interface.entries.extend(entries)
        return self._frozen_responses_input(delta_interface)

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        # Maintain the canonical interface for local recovery and full-replay
        # fallback, but capture the entries added by this turn so subsequent
        # stored-response requests can send only the incremental delta.
        interface_start = len(self._interface.entries)
        trailing_entries = 0
        if message is None:
            pass
        elif isinstance(message, str):
            self._interface.add_user_message(message)
            trailing_entries = 1
        elif isinstance(message, list):
            # ToolResultBlock list, the canonical kernel shape coming back
            # from ToolExecutor via _make_tool_result_fn.
            if message and all(isinstance(b, ToolResultBlock) for b in message):
                self._interface.add_tool_results(message)
                trailing_entries = len(message)
            else:
                # Pre-built wire dicts (legacy / tests). Fall back to the
                # parent's converter below so behavior matches what callers
                # passing dicts expect.
                pass
        elif isinstance(message, dict):
            pass
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # Pre-request hook — kernel-side splice point. Include hook-spliced
        # entries in the delta so notification wake pairs remain visible in the
        # same request even when previous_response_id is active.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        try:
            self._interface.enforce_tool_pairing()
            prebuilt_items: list[dict[str, Any]] = []
            if isinstance(message, dict):
                prebuilt_items.append(message)
            elif isinstance(message, list) and not (
                message and all(isinstance(b, ToolResultBlock) for b in message)
            ):
                prebuilt_items.extend(self._convert_input(message))

            if self._ws_enabled:
                self._maybe_reset_ws_epoch()
            full_replay_input_items = self._frozen_responses_input(self._interface)
            full_replay_input_items.extend(prebuilt_items)

            delta_entries = self._interface.entries[interface_start:]
            delta_input_items = self._interface_entries_to_responses_input(delta_entries)
            delta_input_items.extend(prebuilt_items)

            stateful_store_enabled = False
            previous_response_id = (
                self._response_id if stateful_store_enabled and self._response_id and delta_input_items else None
            )
            input_items = delta_input_items if previous_response_id else full_replay_input_items
            request_mode = "stateful_incremental" if previous_response_id else "stateless_full_store_forced_false"

            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": input_items,
                "stream": True,
                **self._extra_kwargs,
            }
            # The ChatGPT Codex endpoint explicitly rejects store=True with
            # `{'detail': 'Store must be set to false'}`. Keep store=false for
            # normal turns; future turn-state experiments must use Codex-specific
            # state headers/body, not Responses API storage.
            kwargs["store"] = bool(previous_response_id)
            # Ensure reasoning.encrypted_content is requested so the raw
            # reasoning item can be preserved for prompt-cache-stable replay.
            existing_include = kwargs.get("include") or []
            if isinstance(existing_include, str):
                existing_include = [existing_include]
            else:
                try:
                    existing_include = list(existing_include)
                except TypeError:
                    existing_include = [existing_include]
            if "reasoning.encrypted_content" not in existing_include:
                kwargs["include"] = existing_include + ["reasoning.encrypted_content"]
            if self._instructions:
                kwargs["instructions"] = self._instructions
            if self._tools:
                kwargs["tools"] = self._tools
                if self._tool_choice:
                    kwargs["tool_choice"] = self._tool_choice
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            if self._compact_threshold:
                kwargs["context_management"] = [
                    {"type": "compaction", "compact_threshold": self._compact_threshold}
                ]
            # Resolve this request's cache-affinity values — the single stable
            # per-agent id (a pure hash of the agent path). All three levers
            # (prompt_cache_key / session_id / thread_id) carry the same value on
            # every request and never change for the life of the session.
            effective_cache_key, affinity_headers = self._effective_affinity()
            # Opt into Codex prompt caching with the resolved key. We send only
            # `prompt_cache_key`; the Codex backend rejects `prompt_cache_retention`
            # (Unsupported parameter), so it is deliberately never sent.
            if effective_cache_key:
                kwargs["prompt_cache_key"] = effective_cache_key
            # REST cache-affinity headers (issue #378). Sent as HTTP headers via
            # the SDK's per-request ``extra_headers``, never as request-body
            # fields. ``session_id`` / ``thread_id`` route the per-agent cache
            # slot and are a single stable per-agent value (never rotated).
            # Client identity (#436 → #471 experiment) forms the base;
            # cache-affinity and caller-supplied headers layer on top so they
            # always win. The default app-name identity is honest LingTai;
            # stable affinity/metadata headers remain LingTai-owned.
            # see ``_codex_identity_headers`` / ``_CODEX_IMPERSONATE_OFFICIAL_CLI``.
            extra_headers = {
                **_codex_identity_headers(),
                **affinity_headers,
                **self._codex_metadata_headers(),
            }
            # The user's own ChatGPT account id, when available. Canonical
            # official spelling ``ChatGPT-Account-ID`` (HTTP header names are
            # case-insensitive). Attributes the request to the right ChatGPT
            # account; orthogonal to the app-name identity above. Omitted
            # entirely when no account id is known.
            if self._account_id:
                extra_headers["ChatGPT-Account-ID"] = self._account_id
            if extra_headers:
                kwargs["extra_headers"] = {
                    **extra_headers,
                    **kwargs.get("extra_headers", {}),
                }
            client_metadata = self._codex_client_metadata()
            if client_metadata:
                extra_body = dict(kwargs.get("extra_body") or {})
                existing_client_metadata = dict(extra_body.get("client_metadata") or {})
                extra_body["client_metadata"] = {**existing_client_metadata, **client_metadata}
                kwargs["extra_body"] = extra_body
            acc = StreamingAccumulator()
            response_id = None
            usage = UsageMetadata()
            seen_reasoning_summary_items: set[str] = set()
            # Raw reasoning item dicts for replay, in provider output order.
            raw_reasoning_items: list[dict[str, Any]] = []
            trace_path = _codex_responses_trace_path()
            fallback_error_type: str | None = None
            fallback_error_message: str | None = None

            request_store = bool(kwargs.get("store"))

            # EXPERIMENTAL websocket path (#471). When enabled, try to send a
            # Responses ``response.create`` frame over the websocket with an
            # incremental delta + ``previous_response_id`` (or full input on the
            # first request / on any mismatch). On ANY websocket problem
            # (handshake 426, connect/auth error, missing runtime, delta
            # mismatch) we raise ``_CodexWsFallback`` internally and drop to the
            # existing HTTP full-replay path below. ``store`` stays ``false``.
            ws_stream = None
            if self._ws_enabled:
                try:
                    ws_stream, ws_mode, ws_prev_id = self._codex_ws_open(
                        kwargs,
                        full_replay_input_items=full_replay_input_items,
                    )
                except _CodexWsFallback as exc:
                    logger.info(
                        "Codex websocket path unavailable; using HTTP full replay: %s",
                        str(exc)[:240],
                    )
                    ws_stream = None
                else:
                    request_mode = ws_mode
                    previous_response_id = ws_prev_id
                    request_store = False

            ws_stream_was_used = ws_stream is not None
            if ws_stream is not None:
                stream = ws_stream
            else:
                try:
                    stream = self._client.responses.create(**kwargs)
                except Exception as exc:
                    if not (previous_response_id or request_store):
                        raise
                    fallback_error_type = type(exc).__name__
                    fallback_error_message = str(exc)
                    logger.info(
                        "Codex stateful Responses request failed; falling back to full replay store=false: %s: %s",
                        fallback_error_type,
                        fallback_error_message[:240],
                    )
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs["input"] = full_replay_input_items
                    fallback_kwargs["store"] = False
                    fallback_kwargs.pop("previous_response_id", None)
                    request_mode = "stateless_full_fallback"
                    previous_response_id = None
                    request_store = False
                    stream = self._client.responses.create(**fallback_kwargs)
            for event in stream:
                thoughts_before = acc.thoughts
                pending_thought_chars_before = len("".join(acc._thought_parts))
                accepted_reasoning = _handle_responses_reasoning_event(
                    event,
                    acc,
                    seen_reasoning_summary_items,
                )
                _codex_responses_trace_record(
                    event=event,
                    accepted_reasoning=accepted_reasoning,
                    thoughts_before=thoughts_before,
                    thoughts_after=acc.thoughts,
                    pending_thought_chars_before=pending_thought_chars_before,
                    pending_thought_chars_after=len("".join(acc._thought_parts)),
                    trace_path=trace_path,
                )
                if accepted_reasoning:
                    # Capture raw reasoning item when output_item.done carries
                    # encrypted_content, so it can be replayed verbatim next turn.
                    if (
                        event.type == "response.output_item.done"
                        and getattr(event.item, "type", None) == "reasoning"
                    ):
                        enc = getattr(event.item, "encrypted_content", None)
                        if enc:
                            item_id = getattr(event.item, "id", None) or ""
                            summaries = []
                            for s in getattr(event.item, "summary", None) or []:
                                summaries.append({
                                    "type": getattr(s, "type", None),
                                    "text": getattr(s, "text", None),
                                })
                            content = []
                            for c in getattr(event.item, "content", None) or []:
                                if hasattr(c, "model_dump"):
                                    content.append(c.model_dump(exclude_none=True))
                                elif isinstance(c, dict):
                                    content.append(c)
                                else:
                                    logger.warning(
                                        "codex.responses.reasoning_content_ignored",
                                        extra={
                                            "item_id": item_id,
                                            "content_type": type(c).__name__,
                                        },
                                    )
                            raw_reasoning_items.append({
                                "type": "reasoning",
                                "id": item_id,
                                "summary": summaries,
                                "content": content,
                                "encrypted_content": enc,
                            })
                    continue
                if event.type == "response.output_text.delta":
                    acc.add_text(event.delta)
                    if on_chunk:
                        on_chunk(event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    acc.add_tool_args(event.delta)
                elif event.type == "response.output_item.added":
                    if getattr(event.item, "type", None) == "function_call":
                        acc.start_tool(id=event.item.call_id, name=event.item.name)
                elif event.type == "response.output_item.done":
                    if getattr(event.item, "type", None) == "function_call":
                        acc.finish_tool()
                elif event.type == "response.completed":
                    response_id = event.response.id
                    if event.response.usage:
                        cached = getattr(event.response.usage, "input_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        input_tokens = getattr(event.response.usage, "input_tokens", 0) or 0
                        usage = UsageMetadata(
                            input_tokens=input_tokens,
                            output_tokens=getattr(event.response.usage, "output_tokens", 0) or 0,
                            thinking_tokens=getattr(
                                event.response.usage, "output_tokens_details", None
                            )
                            and getattr(
                                event.response.usage.output_tokens_details,
                                "reasoning_tokens",
                                0,
                            )
                            or 0,
                            cached_tokens=cached_tokens,
                            extra=self._usage_extra(
                                affinity_headers,
                                effective_cache_key,
                                request_mode=request_mode,
                                previous_response_id=previous_response_id,
                                store=request_store,
                                fallback_error_type=fallback_error_type,
                                fallback_error_message=fallback_error_message,
                                ws_diag=(self._ws_last_diag if self._ws_enabled else None),
                            ),
                        )
        except Exception:
            # Revert the trailing user entry we just added so the next retry
            # doesn't double-record it. Mirrors OpenAIChatSession.send's
            # error path. ToolResultBlock entries also revert — the executor
            # will re-supply them when AED rebuilds the loop.
            self._interface.drop_trailing(lambda e: e.role == "user")
            raise

        result = acc.finalize(usage=usage)

        # Record assistant response into the interface so it rides along on
        # the next request. Without this, the stateless backend would never
        # see the assistant's own prior turns.
        blocks: list = []
        raw_items = raw_reasoning_items
        if result.thoughts or raw_items:
            joined = "\n".join(t for t in result.thoughts if t)
            if raw_items:
                # Attach every raw reasoning item (with encrypted_content), even
                # when the provider returned no summary_text. Codex commonly
                # returns summary=[] with encrypted_content; dropping the block
                # in that case would lose the cache-stable replay state.
                for idx, raw_item in enumerate(raw_items):
                    item_summary_text = "\n".join(
                        str(s.get("text"))
                        for s in raw_item.get("summary", [])
                        if isinstance(s, dict)
                        and s.get("type") == "summary_text"
                        and s.get("text")
                    )
                    blocks.append(
                        ThinkingBlock(
                            text=item_summary_text or (joined if idx == 0 else ""),
                            provider_data={
                                "openai_responses_reasoning_item": raw_item,
                            },
                        )
                    )
            elif joined:
                blocks.append(ThinkingBlock(text=joined))
        if result.text:
            blocks.append(TextBlock(text=result.text))
        for tc in result.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="codex",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
            },
        )

        # Fix the ``ws_full``-every-turn root cause: now that the assistant turn
        # is in the canonical interface, recompute the websocket delta baseline in
        # the SAME converter schema the next full request will use, so a strict
        # prefix can actually match. Only runs on the websocket path; a no-op on
        # the HTTP fallback. See ``_ws_record_baseline_from_interface``.
        if self._ws_enabled:
            self._ws_record_baseline_from_interface()
            if locals().get("ws_stream_was_used"):
                self._ws_turns_since_epoch_reset += 1
                self._record_ws_cache_ledger(
                    request_mode=request_mode,
                    usage=usage,
                    ws_diag=self._ws_last_diag,
                )

        # Stateless: don't persist the response_id beyond this single turn.
        # Stored only as a transient debug aid; never threaded into the next
        # request.
        self._response_id = response_id
        return result


class CodexOpenAIAdapter(OpenAIAdapter):
    """OpenAIAdapter variant that builds CodexResponsesSession instead of the
    standard server-stateful OpenAIResponsesSession.

    Use this with `provider=codex` only. Always set `use_responses=True,
    force_responses=True`. `base_url` defaults to the official Codex endpoint
    (`https://chatgpt.com/backend-api/codex`) but is configurable — the `codex`
    factory forwards an explicit `manifest.llm['base_url']` so a future local
    `lingtai-codex-pool` can front this provider without a separate adapter.
    """

    def __init__(
        self,
        *args,
        codex_session_id: str | None = None,
        codex_session_anchor: str | None = None,
        codex_thread_salt: str | None = None,
        codex_account_id: str | None = None,
        codex_base_urls: object = None,
        codex_molt_count: int | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # Codex REST cache-affinity identity: ONE stable per-agent value used
        # byte-identically for ``session_id``, ``thread_id``, and the default
        # ``prompt_cache_key``. It is a PURE deterministic hash of the agent's
        # durable identity anchor (the resolved ``init.json`` / agent-dir path) —
        # no epoch, no time, no rotation. The same agent path always yields the
        # same id across restarts/refresh/molt, so the agent keeps routing to the
        # same sticky-warm backend cache slot. The adapter has no per-agent
        # identity of its own; the host wiring passes the anchor down by default
        # via these kwargs (also an internal override / testing escape hatch):
        #
        #   codex_session_id=str     -> use this exact string verbatim for all
        #                               three (explicit operator override)
        #   codex_session_anchor=str -> hash the per-agent anchor (the resolved
        #                               init.json path) into the id for all three
        #   (neither set)            -> no session_id/thread_id (bare/test path)
        #
        # ``codex_thread_salt`` is accepted only as a legacy manifest
        # pass-through; it is intentionally NOT used to derive a separate thread
        # id. The root/main thread tracks the session id exactly so the three
        # values stay byte-identical.
        self._codex_session_anchor = (
            str(codex_session_anchor) if codex_session_anchor else None
        )
        if codex_session_id:
            # Explicit override: used verbatim.
            self._codex_id: str | None = str(codex_session_id)
        elif self._codex_session_anchor:
            self._codex_id = _codex_session_id(self._codex_session_anchor)
        else:
            self._codex_id = None  # no per-agent identity -> no headers
        self._codex_thread_salt = codex_thread_salt  # legacy pass-through; unused
        installation_anchor = self._codex_session_anchor or self._codex_id
        self._codex_installation_id = _codex_installation_id(installation_anchor)
        # The user's own ChatGPT account id, resolved upstream from their OAuth
        # auth data (explicit ``account_id`` field or decoded id_token claim).
        # Mutable so the token-refresh path can keep it current if refreshed
        # auth data changes it. ``None`` -> no ``ChatGPT-Account-ID`` header.
        self.codex_account_id: str | None = (
            str(codex_account_id) if codex_account_id else None
        )
        # Optional Codex-only endpoint POOL (molt-boundary shuffle). When this
        # carries 2+ valid entries, ``create_chat`` chooses one at request time
        # by (stable per-agent offset + current molt_count) so the endpoint is
        # stable within a molt segment and rotates only at a molt boundary. An
        # empty pool -> the single ``base_url`` resolved at construction (PR #495
        # behavior, untouched). This NEVER affects the cache identity
        # (``session_id`` / ``thread_id`` / ``prompt_cache_key``), which is keyed
        # purely on ``self._codex_id`` above.
        self._codex_base_urls: tuple[str, ...] = _parse_codex_base_urls(codex_base_urls)
        # ``base_url`` resolved at construction — the fall-back when the pool is
        # empty, and the value ``create_chat`` compares against to detect a
        # molt-driven endpoint change. (``self.base_url`` is mutated in place on
        # a switch; this fixed copy is the legacy single-endpoint anchor.)
        self._codex_fixed_base_url = self.base_url
        # Explicit molt_count override (tests / hosts). When set, used instead of
        # reading ``<working_dir>/.agent.json``.
        self._codex_molt_count_override = codex_molt_count
        # ``<working_dir>/.agent.json`` is the sibling of the per-agent anchor
        # (the resolved ``init.json`` path). Derived once; read fresh per request
        # so a live process observes molt_count changes without a rebuild.
        self._codex_agent_json_path: Path | None = (
            Path(self._codex_session_anchor).parent / ".agent.json"
            if self._codex_session_anchor
            else None
        )
        # Stable per-agent offset so different agents distribute across the pool.
        # Prefer the agent-path anchor; fall back to the cache id, then a fixed
        # constant for a bare/no-identity adapter (degenerate but deterministic).
        offset_seed = self._codex_session_anchor or self._codex_id or "codex"
        self._codex_pool_offset = int(
            hashlib.sha256(offset_seed.encode("utf-8")).hexdigest(), 16
        )

    def _current_molt_count(self) -> int:
        """Current molt_count: explicit override, else ``.agent.json``, else 0."""
        if self._codex_molt_count_override is not None:
            try:
                return int(self._codex_molt_count_override)
            except (TypeError, ValueError):
                return 0
        if self._codex_agent_json_path is not None:
            return _read_molt_count(self._codex_agent_json_path)
        return 0

    def _select_codex_endpoint(self) -> str | None:
        """Pick this request's Codex endpoint from the pool (or the fixed one).

        - Empty pool   -> the single ``base_url`` resolved at construction
                          (``None`` means the official endpoint, set by the
                          factory; kept verbatim).
        - 1 valid entry -> always that entry.
        - 2+ entries   -> ``pool[(offset + molt_count) % len]`` — stable while
                          molt_count is unchanged, rotates to an adjacent slot at
                          each molt boundary (so adjacent molts actually move).
        """
        pool = self._codex_base_urls
        if not pool:
            return self._codex_fixed_base_url
        if len(pool) == 1:
            return pool[0]
        idx = (self._codex_pool_offset + self._current_molt_count()) % len(pool)
        return pool[idx]

    def _repoint_client_if_needed(self, endpoint: str | None) -> None:
        """Re-point the OpenAI client at ``endpoint`` if it changed.

        Called at request time (``create_chat``) so a molt-driven endpoint
        change takes effect on a LIVE adapter without a service rebuild. The old
        client is replaced wholesale: any websocket / ``previous_response_id`` /
        continuation state owned by sessions built against the previous endpoint
        is on the old client object and is dropped, so it can never cross
        endpoints. The cache identity is untouched (it is endpoint-independent).

        The Codex factory's pre-call OAuth refresh hook mutates
        ``self._client.api_key`` in place each turn; carry that LIVE token onto
        the rebuilt client (and into ``_client_kwargs``, which the session reads
        for the WS path) so a switch never reverts to the stale boot token.
        """
        if endpoint == self.base_url:
            return
        live_api_key = getattr(self._client, "api_key", None)
        self.base_url = endpoint
        new_kwargs = dict(self._client_kwargs)
        if live_api_key is not None:
            new_kwargs["api_key"] = live_api_key
        if endpoint:
            new_kwargs["base_url"] = endpoint
        else:
            new_kwargs.pop("base_url", None)
        self._client_kwargs = new_kwargs
        self._client = openai.OpenAI(**new_kwargs)

    def create_chat(self, *args, **kwargs) -> ChatSession:
        # Select the molt-stable endpoint and re-point the client BEFORE the
        # session is built, so the new ``CodexResponsesSession`` (and its
        # ``_ws_base_url`` / client) use the selected endpoint. With an empty
        # pool this is a no-op and the PR #495 single-endpoint path is unchanged.
        self._repoint_client_if_needed(self._select_codex_endpoint())
        return super().create_chat(*args, **kwargs)

    def _resolve_codex_ids(self, model: str) -> tuple[str | None, str | None]:
        """Resolve the (session_id, thread_id) headers for ``model``.

        Returns ``(None, None)`` only when no per-agent identity was passed in
        (e.g. a bare adapter built directly in a test). In the normal host path
        the agent path is always supplied, so both ids are the same stable
        per-agent hash — the thread id tracks the session id exactly.

        Restored to the stable-identity form (was an experimental
        ``(None, None)`` prompt-cache-only probe) because the official Codex CLI
        source path depends on a stable ``session_id`` / ``thread_id`` /
        ``prompt_cache_key`` identity: the websocket incremental
        ``previous_response_id`` path and per-turn ``x-codex-turn-state`` sticky
        routing all ride on top of a stable session/thread (see
        ``codex-rs/core/src/client.rs:863-864, 873`` — ``build_session_headers``
        with both ids on every request). Dropping the headers would defeat the
        official path this experiment is mirroring.
        """
        return self._codex_id, self._codex_id

    def _default_prompt_cache_key(self, model: str) -> str:
        # On the normal/root path the cache key is the SAME per-agent value as
        # session_id / thread_id — byte-identical, so all three cache-affinity
        # levers point at one stable slot for the agent's durable identity (a
        # pure hash of the agent path, never time/epoch dependent). Never paired
        # with `prompt_cache_retention` (Codex rejects it).
        #
        # The model-keyed ``lingtai-codex:{model}:v1`` form survives only for the
        # truly bare/no-anchor path (e.g. a standalone unit test), where the
        # adapter has no per-agent identity to hash.
        if self._codex_id:
            return self._codex_id
        return f"lingtai-codex:{model}:v1"

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
    ) -> CodexResponsesSession:
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        extra_kwargs.update(_responses_reasoning_kwargs(thinking))

        # Codex's backend doesn't accept context_management compaction —
        # leave compact_threshold unset.
        session_id, thread_id = self._resolve_codex_ids(model)
        return CodexResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=None,
            interface=interface,
            # On the normal/root path this resolves to the SAME stable per-agent
            # hash as session_id / thread_id (see ``_default_prompt_cache_key``);
            # it honors an explicit override or a ``prompt_cache_key=False``
            # disable passed to the adapter. Only the bare/no-anchor path falls
            # back to ``lingtai-codex:{model}:v1``.
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            # Stable REST cache-affinity headers: both the per-agent hash,
            # byte-identical, passed down by the host; ``(None, None)`` only for
            # a bare/test adapter. Never rotated.
            session_id=session_id,
            thread_id=thread_id,
            # The user's own ChatGPT account id (read fresh from the adapter so a
            # token refresh that changes it is reflected on newly built sessions).
            account_id=self.codex_account_id,
            installation_id=self._codex_installation_id,
            base_url=self.base_url,
            api_key=self._client_kwargs.get("api_key"),
        )
