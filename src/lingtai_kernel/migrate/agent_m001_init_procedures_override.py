"""agent m001 — retire init.json procedures overrides.

``procedures.md`` is kernel-owned. Earlier agents could set
``init.json.procedures`` (inline prompt text) or ``init.json.procedures_file``
(file path override). This agent-domain migration preserves non-empty inline
legacy content under ``<workdir>/system/migrations/`` and removes both fields
from ``init.json``.

Idempotency is provided by the versioned migration runner: this migration is
run at most once per agent workdir version. Within the migration itself, missing
fields are a no-op.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def _load_json(path: Path) -> dict | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_agent_event(working_dir: Path, event_type: str, **fields) -> None:
    """Best-effort append to the agent JSONL event log before Agent exists.

    BaseAgent._log normally owns this schema. Agent-domain migrations may run
    during boot/refresh while init.json is being normalized, so they write the
    same minimal event shape directly.
    """
    try:
        try:
            init_data = _load_json(working_dir / "init.json") or {}
        except Exception:
            init_data = {}
        manifest = init_data.get("manifest") if isinstance(init_data.get("manifest"), dict) else {}
        agent_name = manifest.get("agent_name")
        log_dir = working_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "type": event_type,
            "address": working_dir.name,
            "agent_name": agent_name,
            "ts": time.time(),
            **fields,
        }
        with (log_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def migrate_init_procedures_override(working_dir: Path) -> None:
    """Archive/remove legacy procedures fields from an agent workdir init.json."""
    init_path = working_dir / "init.json"
    if not init_path.is_file():
        return
    data = _load_json(init_path)
    if data is None:
        raise ValueError(f"{init_path} did not contain a JSON object")

    has_inline = "procedures" in data
    has_file = "procedures_file" in data
    if not has_inline and not has_file:
        return

    legacy = data.get("procedures")
    should_archive = isinstance(legacy, str) and legacy != ""
    archive_rel: str | None = None
    content_hash: str | None = None
    byte_length = 0
    char_length = 0

    if should_archive:
        raw = legacy.encode("utf-8")
        content_hash = hashlib.sha256(raw).hexdigest()
        byte_length = len(raw)
        char_length = len(legacy)
        migrations_dir = working_dir / "system" / "migrations"
        archive_path = migrations_dir / f"init-procedures-{content_hash}.md"
        try:
            migrations_dir.mkdir(parents=True, exist_ok=True)
            archive_path.write_text(legacy, encoding="utf-8")
            archive_rel = archive_path.relative_to(working_dir).as_posix()
        except OSError as e:
            _append_agent_event(
                working_dir,
                "init_procedures_override_migration_failed",
                reason=str(e),
                content_hash=content_hash,
                byte_length=byte_length,
                char_length=char_length,
            )
            raise

    data.pop("procedures", None)
    data.pop("procedures_file", None)

    try:
        _write_json_atomic(init_path, data)
    except OSError as e:
        _append_agent_event(
            working_dir,
            "init_procedures_override_migration_failed",
            reason=str(e),
            archive_path=archive_rel,
            content_hash=content_hash,
            byte_length=byte_length,
            char_length=char_length,
        )
        raise

    _append_agent_event(
        working_dir,
        "init_procedures_override_migrated",
        archive_path=archive_rel,
        content_hash=content_hash,
        byte_length=byte_length,
        char_length=char_length,
        procedures_removed=has_inline,
        procedures_file_removed=has_file,
        field_removed=True,
    )
