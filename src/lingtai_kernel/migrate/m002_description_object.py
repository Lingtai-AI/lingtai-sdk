"""m002 — promote `description` to a structured object.

The earlier shipped shape allowed `description` to be either a string or
an object, plus a parallel top-level `tags: ["tier:N"]` array for the
TUI's tier chip rendering. The new shape unifies these: `description` is
a required object with `summary` (string) and optional `tier` (string in
"1".."5"). Tags are gone.

This migration walks every preset file in the library and:

1. If `description` is a non-empty string → wrap as `{"summary": <str>}`.
2. If `description` is missing → set `{"summary": ""}`. Operators have
   to fix this to a non-empty summary before `load_preset` will accept
   the file; we don't synthesize content.
3. If `tags` is a list and contains a `tier:N` string → set
   `description["tier"] = "N"`.
4. Delete `tags` regardless of contents — the namespace was only used
   for tier in shipped builds.

Idempotent: a file whose `description` is already an object and has no
`tags` key is left untouched.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_PRESET_SUFFIXES = (".json", ".jsonc")
_TIER_PREFIX = "tier:"
_TIER_VALUES = {"1", "2", "3", "4", "5"}


def _load_jsonc(path: Path):
    """Local copy of the jsonc reader to avoid importing from lingtai."""
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonc":
        import re
        raw = re.sub(r"//[^\n]*", "", raw)
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    return json.loads(raw)


def _extract_tier(tags) -> str | None:
    """Return the tier value from a tags list, or None.

    Accepts only `tier:N` where N is in 1..5. Other tier-prefixed values
    (e.g. legacy `tier:opus`) are dropped — they map to nothing in the
    new vocabulary.
    """
    if not isinstance(tags, list):
        return None
    for t in tags:
        if isinstance(t, str) and t.startswith(_TIER_PREFIX):
            value = t[len(_TIER_PREFIX):]
            if value in _TIER_VALUES:
                return value
    return None


def migrate_description_object(presets_path: Path) -> None:
    """Walk preset files and rewrite description into structured form.

    Args:
        presets_path: directory containing the preset files. Must exist
            (the caller checks).

    Side effects:
        Rewrites preset files in place atomically (tmp + os.replace).
        Logs each rewrite at INFO level.
    """
    rewrote = 0
    skipped = 0

    for entry in sorted(presets_path.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix not in _PRESET_SUFFIXES:
            continue
        if entry.name.startswith("_"):
            continue  # internal files like _kernel_meta.json

        try:
            data = _load_jsonc(entry)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("m002: skipping unreadable preset %s: %s", entry, e)
            skipped += 1
            continue

        if not isinstance(data, dict):
            continue

        changed = False

        # 1. description: string → {summary: string}; missing → {summary: ""}
        desc = data.get("description")
        if isinstance(desc, str):
            data["description"] = {"summary": desc}
            desc = data["description"]
            changed = True
        elif desc is None:
            data["description"] = {"summary": ""}
            desc = data["description"]
            changed = True
        elif not isinstance(desc, dict):
            log.warning(
                "m002: %s has non-string non-object description (%r) — leaving unchanged",
                entry, type(desc).__name__,
            )
            skipped += 1
            continue

        # 2. fold tags:[tier:N] → description.tier
        if "tags" in data:
            tier = _extract_tier(data.get("tags"))
            if tier is not None and "tier" not in desc:
                desc["tier"] = tier
            del data["tags"]
            changed = True

        if not changed:
            continue

        try:
            tmp = entry.with_suffix(entry.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(entry))
        except OSError as e:
            log.warning("m002: failed to rewrite %s: %s", entry, e)
            skipped += 1
            continue

        log.info("m002: promoted description in %s", entry.name)
        rewrote += 1

    log.info(
        "m002 complete: rewrote=%d skipped=%d (presets_path=%s)",
        rewrote, skipped, presets_path,
    )
