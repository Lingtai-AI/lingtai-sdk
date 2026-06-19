"""Token ledger — append-only JSONL log of per-LLM-call token usage.

Single source of truth for lifetime token statistics.
Written alongside chat_history after every LLM call.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


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
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


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


def sum_token_ledger(path: Path | str) -> dict:
    """Sum all entries in the token ledger.

    Returns dict with keys: input_tokens, output_tokens, thinking_tokens,
    cached_tokens, api_calls (= number of valid entries).

    Skips corrupt lines gracefully.
    """
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
        totals["input_tokens"] += entry.get("input", 0)
        totals["output_tokens"] += entry.get("output", 0)
        totals["thinking_tokens"] += entry.get("thinking", 0)
        totals["cached_tokens"] += entry.get("cached", 0)
        totals["api_calls"] += 1
    return totals
