"""init.json validation — required fields are strict, unknown fields warn."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Schema tables lifted to module scope so tests can assert internal consistency
# (every optional field has a type, no known field is missing from the other).
# When adding a new manifest field, update BOTH MANIFEST_OPTIONAL and
# MANIFEST_KNOWN — test_init_schema.py enforces this.

TOP_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    "env_file": str,
    "venv_path": str,
    # addons is a list of curated MCP names — looked up in the kernel
    # catalog and decompressed into mcp_registry.jsonl by the `mcp`
    # capability on agent boot.
    "addons": list,
    # mcp is the per-MCP activation map — see core/mcp/manual/SKILL.md.
    # Keys must match registered names; values are subprocess specs.
    "mcp": dict,
}

# Top-level fields that were retired in past versions and still have simple
# shape-only cleanup semantics. strip_deprecated() removes them from the data
# dict (and optionally from disk) so they never reach validate_init(). Fields
# that need archive/event/version tracking belong in lingtai_kernel.migrate
# agent-domain migrations instead.
DEPRECATED_TOP_FIELDS: set[str] = {
    # "soul" / "soul_file" — retired in v0.7.6. The soul-flow voice is
    # now owned by the agent via soul(action='voice') and stored under
    # manifest.soul.{voice,voice_prompt}.
    "soul", "soul_file",
}

# Legacy fields removed by version-controlled agent-domain migrations. They are
# known to validation only so stale/restored init.json files do not look like
# active supported schema fields and do not get type-checked as prompt sections.
LEGACY_MIGRATED_TOP_FIELDS: set[str] = {"procedures", "procedures_file"}

TOP_KNOWN: set[str] = {
    "manifest", "env_file", "venv_path", "addons", "mcp",
    "principle", "principle_file", "covenant", "covenant_file",
    "substrate", "substrate_file",
    "brief", "brief_file",
    "pad", "pad_file", "prompt", "prompt_file",
    "comment", "comment_file",
} | DEPRECATED_TOP_FIELDS | LEGACY_MIGRATED_TOP_FIELDS

MANIFEST_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "llm": dict,
}

MANIFEST_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    "agent_name": (str, type(None)),
    "language": str,
    "capabilities": dict,
    "disable": list,
    "soul": dict,
    "stamina": (int, float),
    "context_limit": (int, type(None)),
    "molt_pressure": (int, float),
    "molt_prompt": str,
    "max_turns": int,
    "max_rpm": int,
    "admin": dict,
    "streaming": bool,
    "time_awareness": bool,
    "timezone_awareness": bool,
    "pseudo_agent_subscriptions": list,
    "preset": dict,
    # Large-result notification threshold.  When a tool result's serialized
    # length exceeds this value it becomes a pending large-result case; the
    # system-channel notification fires only once the combined length of all
    # pending large-result cases exceeds 50000 chars (the total-length gate).
    # Default: 3000.  0 disables notifications.  Runtime mutation via the system
    # tool is not supported — change this field and refresh.
    "summarize_notification_threshold": int,
}

MANIFEST_KNOWN: set[str] = set(MANIFEST_REQUIRED) | set(MANIFEST_OPTIONAL)


def strip_deprecated(data: dict) -> list[str]:
    """Remove deprecated top-level fields from *data* in-place.

    Returns the list of field names that were removed (empty if none).
    """
    removed: list[str] = []
    for key in DEPRECATED_TOP_FIELDS:
        if key in data:
            del data[key]
            removed.append(key)

    if removed:
        log.debug("stripped deprecated init.json fields: %s", ", ".join(sorted(removed)))
    return removed


def validate_init(data: dict) -> list[str]:
    """Validate an init.json dict.

    Raises ValueError for missing required fields or wrong types on known fields.
    Returns a list of warning strings for unknown/unexpected fields.
    """
    warnings: list[str] = []

    _require_keys(data, {
        "manifest": dict,
    }, prefix="")

    # Text fields: inline value OR _file path (at least one required).
    # Note: "soul" / "soul_file" was removed in v0.7.6 — the soul-flow
    # voice lives at manifest.soul.{voice,voice_prompt} now. The legacy
    # fields are kept in TOP_KNOWN for silent ignore (no warning).
    for key in ("principle", "covenant", "pad", "prompt"):
        file_key = f"{key}_file"
        has_inline = key in data
        has_file = file_key in data
        if not has_inline and not has_file:
            raise ValueError(f"missing required field: {key} (or {file_key})")
        if has_inline and not isinstance(data[key], str):
            raise ValueError(f"{key}: expected str, got {type(data[key]).__name__}")
        if has_file and not isinstance(data[file_key], str):
            raise ValueError(f"{file_key}: expected str, got {type(data[file_key]).__name__}")

    # Optional text fields: inline value OR _file path (neither required).
    # `substrate` is the kernel-owned, cross-app-stable system prompt
    # section that describes the agent's architecture to itself (tool
    # tiers, data-flow topology, life states, channel discipline,
    # attention model). Injected between covenant and tools by the prompt
    # manager. See lingtai issue #39. Opt-in: agents without substrate
    # configured render the same prompt they did before.
    for key in ("comment", "brief", "substrate"):
        file_key = f"{key}_file"
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"{key}: expected str, got {type(data[key]).__name__}")
        if file_key in data and not isinstance(data[file_key], str):
            raise ValueError(f"{file_key}: expected str, got {type(data[file_key]).__name__}")

    # Optional top-level fields — check types for known ones
    _optional_keys(data, TOP_OPTIONAL, prefix="")

    # Warn about unknown top-level keys
    for key in data:
        if key not in TOP_KNOWN:
            warnings.append(f"unknown top-level field: {key}")

    manifest = data["manifest"]
    _require_keys(manifest, MANIFEST_REQUIRED, prefix="manifest")
    _optional_keys(manifest, MANIFEST_OPTIONAL, prefix="manifest")

    # Validate manifest.preset umbrella if present.
    #
    # Schema (post path→allowed redesign): {default, active, allowed}.
    # - default: path string (the agent's home preset; AED auto-fallback target)
    # - active: path string (currently materialized preset)
    # - allowed: list[str] of preset paths the agent may swap to at runtime
    #
    # Both `default` and `active` MUST be members of `allowed`. Listing them
    # there is the only place the agent's authorized preset surface is
    # declared — there is no implicit "everything in the library directory"
    # fallback.
    preset = manifest.get("preset")
    if preset is not None:
        if not isinstance(preset, dict):
            raise ValueError(f"manifest.preset: expected object, got {type(preset).__name__}")
        if not preset.get("active"):
            raise ValueError("manifest.preset.active is required when manifest.preset is set")
        if not preset.get("default"):
            raise ValueError("manifest.preset.default is required when manifest.preset is set")
        if not isinstance(preset["active"], str):
            raise ValueError(f"manifest.preset.active: expected str, got {type(preset['active']).__name__}")
        if not isinstance(preset["default"], str):
            raise ValueError(f"manifest.preset.default: expected str, got {type(preset['default']).__name__}")
        allowed = preset.get("allowed")
        if allowed is None:
            # The legacy `path` field was retired in the path→allowed
            # redesign. If we see it, this init.json predates m029; point
            # the operator at the migration so they don't have to guess.
            hint = ""
            if "path" in preset:
                hint = (
                    " — this init.json predates the path→allowed schema; "
                    "run `lingtai-tui` once on the project so migration m029 "
                    "rewrites manifest.preset.path into manifest.preset.allowed"
                )
            raise ValueError(
                "manifest.preset.allowed is required when manifest.preset is set "
                "(list of preset paths this agent may use at runtime)" + hint
            )
        if not isinstance(allowed, list):
            raise ValueError(
                f"manifest.preset.allowed: expected list[str], got {type(allowed).__name__}"
            )
        if not allowed:
            raise ValueError(
                "manifest.preset.allowed must be non-empty — at minimum it "
                "must contain the default preset"
            )
        for i, entry in enumerate(allowed):
            if not isinstance(entry, str) or not entry:
                raise ValueError(
                    f"manifest.preset.allowed[{i}]: expected non-empty str, "
                    f"got {type(entry).__name__}"
                )
        if preset["default"] not in allowed:
            raise ValueError(
                f"manifest.preset.default ({preset['default']!r}) must appear "
                f"in manifest.preset.allowed"
            )
        if preset["active"] not in allowed:
            raise ValueError(
                f"manifest.preset.active ({preset['active']!r}) must appear "
                f"in manifest.preset.allowed"
            )
        # Warn on unknown keys
        for key in preset:
            if key not in {"active", "default", "allowed"}:
                warnings.append(f"unknown field in manifest.preset: {key}")

    for key in manifest:
        if key not in MANIFEST_KNOWN:
            warnings.append(f"unknown field: manifest.{key}")

    if "summarize_notification_threshold" in manifest:
        summarize_threshold = manifest["summarize_notification_threshold"]
        if isinstance(summarize_threshold, bool):
            raise ValueError(
                "manifest.summarize_notification_threshold: expected non-negative int, got bool"
            )
        if summarize_threshold < 0:
            raise ValueError(
                "manifest.summarize_notification_threshold: expected non-negative int"
            )

    soul = manifest.get("soul")
    if soul is not None:
        _optional_keys(soul, {
            "delay": (int, float),
            "consultation_past_count": int,
            "voice": str,
            "voice_prompt": str,
        }, prefix="manifest.soul")

    llm = manifest["llm"]
    _require_keys(llm, {
        "provider": str,
        "model": str,
    }, prefix="manifest.llm")
    _optional_keys(llm, {
        "api_key": (str, type(None)),
        "api_key_env": str,
        "base_url": (str, type(None)),
        "compact_threshold": (int, type(None)),
    }, prefix="manifest.llm")
    if "compact_threshold" in llm:
        compact_threshold = llm["compact_threshold"]
        if isinstance(compact_threshold, bool):
            raise ValueError("manifest.llm.compact_threshold: expected int | null, got bool")
        if isinstance(compact_threshold, int) and compact_threshold <= 0:
            raise ValueError(
                "manifest.llm.compact_threshold: expected positive int or null"
            )

    # If api_key_env is set without api_key, env_file must be provided
    if llm.get("api_key_env") and not llm.get("api_key"):
        if not data.get("env_file"):
            raise ValueError(
                "manifest.llm.api_key_env is set but no env_file provided "
                "— the agent cannot resolve the API key without it"
            )

    # Validate addons: must be a list of curated MCP names. The `mcp`
    # capability validates each catalog record at decompression time, so
    # there's no per-name validation here.
    addons = data.get("addons")
    if isinstance(addons, list):
        if not all(isinstance(x, str) for x in addons):
            warnings.append("addons: all entries must be strings (curated MCP names)")

    # Validate manifest.capabilities.skills shape if present.
    caps = manifest.get("capabilities") or {}
    if isinstance(caps, dict):
        cap_name = "skills"
        cfg = caps.get(cap_name)
        if cfg is not None:
            if not isinstance(cfg, dict):
                raise ValueError(
                    f"manifest.capabilities.{cap_name}: expected object, "
                    f"got {type(cfg).__name__}"
                )
            paths = cfg.get("paths")
            if paths is not None:
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    raise ValueError(
                        f"manifest.capabilities.{cap_name}.paths: expected list[str]"
                    )
            for key in cfg:
                if key != "paths":
                    warnings.append(
                        f"unknown field in manifest.capabilities.{cap_name}: {key}"
                    )

    return warnings


def _require_keys(
    data: dict,
    schema: dict[str, type | tuple[type, ...]],
    prefix: str,
) -> None:
    """Check that all keys exist in data with correct types."""
    for key, expected_type in schema.items():
        path = f"{prefix}.{key}" if prefix else key

        if key not in data:
            raise ValueError(f"missing required field: {path}")

        _check_type(data[key], expected_type, path)


def _optional_keys(
    data: dict,
    schema: dict[str, type | tuple[type, ...]],
    prefix: str,
) -> None:
    """Check types for keys that are present but not required."""
    for key, expected_type in schema.items():
        if key not in data:
            continue
        path = f"{prefix}.{key}" if prefix else key
        _check_type(data[key], expected_type, path)


def _check_type(
    value: object,
    expected_type: type | tuple[type, ...],
    path: str,
) -> None:
    """Validate a single value's type."""
    # bool is a subclass of int in Python — reject bools for numeric fields
    if isinstance(value, bool) and expected_type in (int, (int, float)):
        raise ValueError(f"{path}: expected number, got bool")

    if not isinstance(value, expected_type):
        if isinstance(expected_type, tuple):
            names = [t.__name__ for t in expected_type if t is not type(None)]
            type_str = (
                (" | ".join(names) + " | null")
                if type(None) in expected_type
                else " | ".join(names)
            )
        else:
            type_str = expected_type.__name__
            if expected_type is dict:
                type_str = "object"
        raise ValueError(
            f"{path}: expected {type_str}, got {type(value).__name__}"
        )
