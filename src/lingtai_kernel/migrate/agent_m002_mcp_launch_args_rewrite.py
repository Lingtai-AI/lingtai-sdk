"""agent m002 — rewrite legacy curated MCP launch module args.

The curated MCP implementations now live only under
``lingtai.mcp_servers.<name>``. Older agent workdirs may still carry
``["-m", "lingtai_<name>"]`` in ``mcp_registry.jsonl`` or ``init.json``.
This migration rewrites those launch args in-place before MCP subprocesses are
started.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_LEGACY_TO_CANONICAL = {
    "lingtai_imap": "lingtai.mcp_servers.imap",
    "lingtai_telegram": "lingtai.mcp_servers.telegram",
    "lingtai_feishu": "lingtai.mcp_servers.feishu",
    "lingtai_wechat": "lingtai.mcp_servers.wechat",
    "lingtai_whatsapp": "lingtai.mcp_servers.whatsapp",
}


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _rewrite_args(args: Any) -> bool:
    """Rewrite legacy ``-m lingtai_<name>`` module args in-place."""
    if not isinstance(args, list):
        return False
    changed = False
    for idx, value in enumerate(args):
        if value in _LEGACY_TO_CANONICAL and idx > 0 and args[idx - 1] == "-m":
            args[idx] = _LEGACY_TO_CANONICAL[value]
            changed = True
    return changed


def _append_agent_event(working_dir: Path, event_type: str, **fields: Any) -> None:
    try:
        init_data: dict[str, Any] = {}
        try:
            loaded = json.loads((working_dir / "init.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                init_data = loaded
        except Exception:
            init_data = {}
        manifest = init_data.get("manifest") if isinstance(init_data.get("manifest"), dict) else {}
        log_dir = working_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "type": event_type,
            "address": working_dir.name,
            "agent_name": manifest.get("agent_name"),
            "ts": time.time(),
            **fields,
        }
        with (log_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _rewrite_registry(working_dir: Path) -> int:
    path = working_dir / "mcp_registry.jsonl"
    if not path.is_file():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    changed_count = 0
    changed_file = False
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(record, dict) and _rewrite_args(record.get("args")):
            changed_count += 1
            changed_file = True
            out.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        else:
            out.append(line)
    if changed_file:
        _write_text_atomic(path, "\n".join(out) + ("\n" if lines else ""))
    return changed_count


def _rewrite_init(working_dir: Path) -> int:
    path = working_dir / "init.json"
    if not path.is_file():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return 0
    changed_count = 0
    for record in mcp.values():
        if isinstance(record, dict) and _rewrite_args(record.get("args")):
            changed_count += 1
    if changed_count:
        _write_json_atomic(path, data)
    return changed_count


def migrate_mcp_launch_args_rewrite(working_dir: Path) -> None:
    """Rewrite stale curated MCP ``python -m lingtai_<name>`` launch args."""
    registry_rewrites = _rewrite_registry(working_dir)
    init_rewrites = _rewrite_init(working_dir)
    if registry_rewrites or init_rewrites:
        _append_agent_event(
            working_dir,
            "mcp_launch_args_rewrite_migrated",
            registry_rewrites=registry_rewrites,
            init_rewrites=init_rewrites,
        )
