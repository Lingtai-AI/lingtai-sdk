"""Token ledger — append-only JSONL log of per-LLM-call token usage.

Single source of truth for lifetime token statistics.
Written alongside chat_history after every LLM call.

Reporting-scope contract
-------------------------
One agent's ledger is a *mixed* stream: ordinary main-chat turns
(``source="main"``), soul fan-out (``source="soul"``), involuntary
control-flow splices (``source="tc_wake"``, ``"heal"``,
``"notification_sync"``, ``"retroactive_compaction"``, ``"summarize"``),
legacy untagged rows, and — on a *parent* agent's ledger — rows emitted by
the daemons it spawned (``source="daemon"`` plus ``em_id``/``run_id``;
written by ``lingtai.core.daemon.run_dir.RunDir.append_tokens``). The daemon
rows in a parent ledger are **intentionally retained**: the parent paid for
that spend, so its lifetime totals must include it. Nothing in this module
removes, strips, or relocates them.

Because the stream is mixed, *which rows a report counts depends on the
question being asked*. Three canonical scopes (see ``sum_token_ledger`` and
``is_daemon_entry``):

- **Main-agent-only** — "what did *this* agent's own main chat cost?" Must
  row-level **exclude** daemon rows: any row with ``source=="daemon"`` *or*
  carrying ``em_id``/``run_id`` is a daemon row, regardless of how it is
  tagged, and is dropped. ``tc_wake`` is **not** a daemon row — it is an
  involuntary splice in the parent's own context — so a main-agent report
  keeps it unless it deliberately narrows to ``source=="main"`` (which
  ``count_main_api_calls`` does).
- **All / parent-total** — "what did this agent *and everything it spawned*
  cost?" Includes every row, daemon rows included. This is the default and
  matches lifetime-total restore on startup.
- **Parent + child aggregation** — summing a parent ledger together with a
  daemon's own ledger **double-counts**, because the daemon mirrors each call
  into both ledgers. Cross-ledger aggregation must therefore be
  **dedup-aware** (e.g. key on ``run_id`` + ``ts``, or sum only the parent
  ledger, which already contains the daemon rows).

The durable on-disk rows are never rewritten by reporting. Scope selection is
a pure read-side filter so every consumer can ask its own question of the
same immutable log.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _mirror_token_entry_to_sqlite(path: Path, entry: dict, source_offset: int) -> None:
    """Best-effort SQLite mirror for standard token ledgers.

    ``logs/token_ledger.jsonl`` remains the source of truth.  The sibling
    ``logs/log.sqlite`` row is a rebuildable sidecar and must never make an
    otherwise successful JSONL append fail.
    """
    if path.name != "token_ledger.jsonl":
        return
    try:
        from .services.logging import (
            DEFAULT_SQLITE_NAME,
            SQLiteEventIndex,
            _classify_token_ledger_path,
        )

        resolved = path.resolve()
        source_kind, scope, run_id = _classify_token_ledger_path(resolved)
        index = SQLiteEventIndex(resolved.with_name(DEFAULT_SQLITE_NAME))
        try:
            index.log_token_entry(
                entry,
                source_file=str(resolved),
                source_offset=source_offset,
                source_kind=source_kind,
                scope=scope,
                run_id=run_id,
            )
        finally:
            index.close()
    except Exception:
        return


def is_daemon_entry(entry: dict) -> bool:
    """Return True if a ledger row was emitted by a daemon emanation.

    A row is a daemon row if it is tagged ``source="daemon"`` *or* carries
    ``em_id``/``run_id`` attribution. Both checks matter: ``source`` is the
    canonical tag, but keying on ``em_id``/``run_id`` as well keeps the
    predicate correct for any row written with daemon attribution even if the
    ``source`` tag were ever absent.

    This is the row-level filter a **main-agent-only** report applies to
    exclude daemon spend. ``source="tc_wake"`` (and other involuntary splices
    such as ``"heal"``/``"soul"``) are **not** daemon rows — they run in the
    agent's own context — so this returns False for them.

    Reads only; never mutates the entry or the durable ledger.
    """
    if entry.get("source") == "daemon":
        return True
    return "em_id" in entry or "run_id" in entry


def append_token_entry(
    path: Path | str,
    *,
    input: int,
    output: int,
    thinking: int,
    cached: int,
    model: str | None = None,
    endpoint: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append one token usage entry to the ledger.

    Creates parent directories and the file if they don't exist.

    `model` and `endpoint` are first-class attribution fields written to the
    top level of the entry when provided. They identify which model produced
    the tokens and which API endpoint (base_url) served the call — useful for
    cost analytics across providers and for distinguishing the soul session
    from the main agent session.

    `extra` is an optional dict of additional fields merged into the entry.
    Required fields (ts/input/output/thinking/cached/model/endpoint) take
    precedence — if a caller passes `extra={"input": 999}`, the explicit
    input value still wins. Used by the daemon capability to tag entries
    with source/em_id/run_id so the parent's ledger preserves per-daemon
    attribution.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict = {}
    if extra:
        entry.update(extra)
    entry.update({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": input,
        "output": output,
        "thinking": thinking,
        "cached": cached,
    })
    if model is not None:
        entry["model"] = model
    if endpoint is not None:
        entry["endpoint"] = endpoint
    payload = (json.dumps(entry) + "\n").encode("utf-8")
    with open(path, "ab") as f:
        source_offset = f.tell()
        f.write(payload)
        f.flush()
    _mirror_token_entry_to_sqlite(path, entry, source_offset)


def count_main_api_calls(path: Path | str) -> int:
    """Count ledger entries tagged ``source="main"``.

    Used as the canonical "main-chat LLM calls so far" signal — a useful
    diagnostic helper. Reading
    the ledger is the single source of truth: an in-memory turn counter
    drifts whenever an involuntary tool-call splice (mail, MCP
    notifications, soul-flow's own appendix landing) calls a
    "post-LLM-call" hook, which is wrong because those splices are not
    main-chat turns. The ledger is already tagged at write time
    (``source="main"`` for ``_session.send`` from the main loop,
    ``source="soul"`` for soul consultation fan-out), so counting
    matching entries is unambiguous.

    Untagged entries (older agents whose ledger predates the source
    tag) are treated as "not main" and skipped — conservative drift,
    same as starting fresh.

    Returns 0 if the file is missing.
    """
    path = Path(path)
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("source") == "main":
            n += 1
    return n


def sum_token_ledger(path: Path | str, *, scope: str = "all") -> dict:
    """Sum entries in the token ledger, optionally narrowed by reporting scope.

    Returns dict with keys: input_tokens, output_tokens, thinking_tokens,
    cached_tokens, api_calls (= number of counted entries).

    ``scope`` selects which rows are counted (see the module docstring for the
    full reporting-scope contract):

    - ``"all"`` (default) — every valid row, daemon rows included. This is the
      **parent-total** / lifetime view and preserves the historical behavior
      of this function, so existing callers (e.g. startup token-state restore)
      are unaffected.
    - ``"main_agent"`` — row-level **excludes** daemon rows (``is_daemon_entry``
      is True), answering "what did this agent's own context cost?" without the
      spend of daemons it spawned. ``tc_wake`` and other involuntary splices in
      the agent's own context are kept. This scope is intended for a **parent
      agent's** ledger (a mixed stream of main + daemon rows); applied to a
      **daemon-local** ledger — every row of which is daemon-attributed
      (``source="daemon"`` + ``em_id``/``run_id``; see
      ``lingtai.core.daemon.run_dir.RunDir.append_tokens``) — it correctly
      excludes all rows and returns zero. Use ``"all"`` to total a daemon-local
      ledger.

    This is a pure read-side filter — the durable rows on disk are never
    rewritten. Skips corrupt lines gracefully. Unknown ``scope`` values raise
    ``ValueError`` so a typo can't silently fall through to ``"all"``.
    """
    if scope not in ("all", "main_agent"):
        raise ValueError(
            f"unknown scope {scope!r}; expected 'all' or 'main_agent'"
        )
    path = Path(path)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_tokens": 0,
        "api_calls": 0,
    }
    if not path.is_file():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if scope == "main_agent" and is_daemon_entry(entry):
            continue
        totals["input_tokens"] += entry.get("input", 0)
        totals["output_tokens"] += entry.get("output", 0)
        totals["thinking_tokens"] += entry.get("thinking", 0)
        totals["cached_tokens"] += entry.get("cached", 0)
        totals["api_calls"] += 1
    return totals
