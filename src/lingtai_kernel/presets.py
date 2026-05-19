"""Preset library — atomic {llm, capabilities} bundles for agent runtime swap.

A preset lives as a single JSON or JSONC file anywhere on disk. The preset's
**name is its path** — there is no separate "stem" identity. Names accepted
by `load_preset` and stored in `manifest.preset.active` / `default` may be:

- absolute (`/Users/me/.lingtai-tui/presets/cheap.json`)
- home-relative (`~/.lingtai-tui/presets/cheap.json`)
- working-dir-relative (`./presets/cheap.json`)

The kernel `expanduser`s and resolves at read time only — what you wrote is
what's stored. There is **no canonicalization on write** and no implicit
search path: if `active` says `~/foo.json`, that exact file is loaded.

The agent's **allowed preset set** is declared explicitly in
`manifest.preset.allowed` as a list of path strings. There is no implicit
"everything in some directory" fallback — registration in `allowed` IS
authorization. `default` and `active` MUST both appear in `allowed`.

This module owns:
- `discover_presets_in_dirs`: enumerate preset *paths* in one or more
  directories (helper for the TUI library screen — the kernel itself
  does not scan directories at runtime)
- `resolve_allowed_presets`: resolve manifest.preset.allowed → list[Path]
- `load_preset`: read + validate one preset by path
- `expand_inherit`: resolve `"provider": "inherit"` sentinels against main LLM
- `default_presets_path`: the per-machine library at ~/.lingtai-tui/presets/
- `home_shortened`: render an absolute path with `~/...` when under $HOME

The on-disk shape is `{name, description, manifest: {...}}`. The
`description` field is a structured object with a required `summary`
string and optional `tier` (the cost/quality ladder, "1".."5"). Authors
may add arbitrary extra keys (`gains`, `loses`, `recommended_for`, ...);
the kernel surfaces the whole object verbatim to the agent.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def default_presets_path() -> Path:
    """The per-machine preset library directory."""
    return Path.home() / ".lingtai-tui" / "presets"


def home_shortened(path: Path | str) -> str:
    """Render a path with `~/...` shorthand when it lives under $HOME.

    This is a *display* helper — the kernel's canonical form is whatever the
    operator wrote. Use this in listings and logs for readable output.

    Returns:
        ``~/.lingtai-tui/...`` style when the resolved path is under
        ``Path.home()``; otherwise the absolute string form. Never raises —
        falls back to ``str(path)`` if anything is unresolvable.
    """
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            return str(path)
        home = Path.home()
        try:
            rel = p.relative_to(home)
            return str(Path("~") / rel)
        except ValueError:
            return str(p)
    except (TypeError, ValueError, OSError):
        return str(path)


def resolve_preset_name(name: str, working_dir: Path) -> Path:
    """Resolve a preset name (path string) to an absolute Path.

    Accepts the three input forms — absolute, ~-prefixed, working-dir-relative —
    and returns an absolute Path with `~` expanded. Does NOT resolve symlinks
    or canonicalize: the returned path matches what the user wrote.

    Args:
        name: the preset path as a string. Must be non-empty.
        working_dir: directory to resolve relative paths against.

    Raises:
        ValueError: name is empty or not a string.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"preset name must be a non-empty string, got {name!r}")
    p = Path(name).expanduser()
    if p.is_absolute():
        return p
    return (working_dir / p).resolve()


def resolve_allowed_presets(manifest: dict, working_dir: Path) -> list[Path]:
    """Resolve manifest.preset.allowed entries to absolute Paths.

    Returns a list[Path] in declared order. Returns [] when the umbrella
    is absent, allowed is missing/empty, or the agent has no preset block.

    Relative paths are resolved against working_dir (not the process CWD)
    so an agent's preset reference remains valid regardless of where the
    process was launched. Tilde-prefixed paths are expanded.
    """
    preset_block = manifest.get("preset") or {}
    raw = preset_block.get("allowed") if isinstance(preset_block, dict) else None
    if not isinstance(raw, list):
        return []

    resolved: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            continue
        p = Path(entry).expanduser()
        resolved.append(p if p.is_absolute() else (working_dir / p).resolve())
    return resolved


def discover_presets_in_dirs(
    dirs: Path | str | list[Path | str],
) -> dict[str, Path]:
    """Enumerate preset files across one or more library directories.

    Helper for the TUI library screen and other UI surfaces. The kernel
    runtime itself does NOT scan directories — runtime authorization is
    declared explicitly in `manifest.preset.allowed`. This function is
    here to power the "build a new agent" flow where the wizard needs to
    list every preset on disk so the user can pick which to allow.

    Returns a mapping of **path-string → Path** for top-level *.json[c]
    files. The key is the absolute path string (with `~/...` shortening
    when under $HOME) — agents and UIs can pass it straight back to
    `load_preset`. Two libraries each containing `cheap.json` appear as
    two distinct entries — no collisions, no shadowing.

    Nonexistent directories are silently skipped — they're not an error.

    Triggers any pending kernel-side preset migrations against each path
    before listing — see lingtai_kernel.migrate. Migrations are idempotent
    and process-cached, so repeated calls share the work.
    """
    from lingtai_kernel.migrate import run_migrations
    from lingtai_kernel.migrate.migrate import meta_filename

    if isinstance(dirs, (str, Path)):
        normalized: list[Path] = [Path(dirs)]
    else:
        normalized = [Path(p) for p in dirs]
    skip = meta_filename()
    out: dict[str, Path] = {}

    for p in normalized:
        if not p.is_dir():
            continue
        run_migrations(p)
        for entry in p.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix not in (".json", ".jsonc"):
                continue
            if entry.name == skip:
                continue
            key = home_shortened(entry)
            out[key] = entry
    return out


# Back-compat alias — old name kept until callers migrate. Prefer
# `discover_presets_in_dirs` in new code.
discover_presets = discover_presets_in_dirs


def load_preset(
    name: str,
    working_dir: Path | None = None,
) -> dict:
    """Load and validate a preset by **path name**.

    Args:
        name: the preset's path. Accepts:
            - absolute: `/Users/me/.lingtai-tui/presets/cheap.json`
            - home-relative: `~/.lingtai-tui/presets/cheap.json`
            - working-dir-relative: `./presets/cheap.json` (requires working_dir)
            Both `.json` and `.jsonc` extensions are accepted; the name MUST
            include the extension — there is no implicit extension probing.
        working_dir: directory to resolve relative names against. Required
            iff `name` is relative. Pass `Path.cwd()` for one-off scripts.

    Returns:
        The parsed preset dict with shape {name, description, manifest: {...}}.

    Raises:
        KeyError: the file does not exist.
        ValueError: the name is malformed, the file is malformed, or
            required fields are missing.
    """
    from .config_resolve import load_jsonc
    from lingtai_kernel.migrate import run_migrations

    if not isinstance(name, str) or not name:
        raise ValueError(f"preset name must be a non-empty string, got {name!r}")

    p = Path(name).expanduser()
    if not p.is_absolute():
        if working_dir is None:
            raise ValueError(
                f"preset name {name!r} is relative but no working_dir provided"
            )
        p = (working_dir / p).resolve()

    if p.suffix not in (".json", ".jsonc"):
        raise ValueError(
            f"preset name {name!r}: must end in .json or .jsonc"
        )

    if not p.is_file():
        raise KeyError(f"preset not found: {name!r} (resolved to {p})")

    # Run kernel migrations on the containing directory so legacy on-disk
    # shapes are normalized before validation. Idempotent and process-cached.
    if p.parent.is_dir():
        run_migrations(p.parent)

    try:
        data = load_jsonc(p)
    except Exception as e:
        raise ValueError(f"failed to parse preset {name!r} ({p}): {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} ({p}): expected object, got {type(data).__name__}")

    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError(f"preset {name!r} ({p}): missing or invalid 'manifest' object")

    llm = manifest.get("llm")
    if not isinstance(llm, dict):
        raise ValueError(f"preset {name!r} ({p}): missing or invalid 'manifest.llm' object")

    if not llm.get("provider") or not llm.get("model"):
        raise ValueError(f"preset {name!r} ({p}): manifest.llm requires non-empty 'provider' and 'model'")

    # context_limit lives inside manifest.llm. Migration m001 relocated any
    # legacy root-level placements; presets that still have it at the root
    # are ambiguous (migration explicitly skips both-locations) or hand-edited
    # regressions. Reject either case with a pointed error.
    if "context_limit" in manifest:
        raise ValueError(
            f"preset {name!r} ({p}): context_limit must live inside "
            f"manifest.llm, not at manifest root — move it under llm and retry"
        )
    ctx_limit = llm.get("context_limit")
    if ctx_limit is not None and not isinstance(ctx_limit, int):
        raise ValueError(
            f"preset {name!r} ({p}): context_limit must be an integer (got {type(ctx_limit).__name__})"
        )

    caps = manifest.get("capabilities", {})
    if not isinstance(caps, dict):
        raise ValueError(f"preset {name!r} ({p}): manifest.capabilities must be an object")

    # Required top-level `description` object. Required keys: summary
    # (non-empty string). Optional: tier (string in TIER_VALUES). Extra
    # keys are accepted as-is and surfaced verbatim to the agent.
    description = data.get("description")
    if not isinstance(description, dict):
        raise ValueError(
            f"preset {name!r} ({p}): 'description' must be an object "
            f"with at least a 'summary' field"
        )
    summary = description.get("summary")
    if not isinstance(summary, str) or not summary:
        raise ValueError(
            f"preset {name!r} ({p}): 'description.summary' must be a "
            f"non-empty string"
        )
    tier = description.get("tier")
    if tier is not None:
        if not isinstance(tier, str) or tier not in TIER_VALUES:
            raise ValueError(
                f"preset {name!r} ({p}): 'description.tier' must be one of "
                f"{TIER_VALUES} (got {tier!r})"
            )

    return data


def materialize_active_preset(data: dict, working_dir: Path) -> None:
    """Substitute the active preset's llm + capabilities into init.json data.

    If ``manifest.preset.active`` is set, load that preset and copy its
    ``manifest.llm`` and ``manifest.capabilities`` into ``data["manifest"]``
    so downstream validators and consumers see a fully-resolved manifest.

    Capabilities are wholesale-replaced by the preset (atomic swap is the
    whole point of presets) with one carve-out: ``skills.paths`` from
    init.json appends to the preset's skills paths, deduped, preset defaults
    first. Skill paths are project-truth (where this agent's extra skills
    live) — swapping presets should not erase them.

    Mutates ``data`` in place. No-op when ``manifest.preset.active`` is unset
    or when the manifest already has a literal ``llm`` and no preset block.

    When the active preset file is missing (the file referenced by
    ``manifest.preset.active`` does not exist on disk — e.g. a hard-copied
    project where the previous machine's preset library wasn't carried over),
    fall back to ``manifest.preset.default`` if it points to a different,
    loadable preset. The fallback rewrites ``manifest.preset.active`` in place
    so the corrected value is persisted on the next init.json regen and the
    schema's "active must appear in allowed" invariant still holds (default
    is always in allowed by construction).

    Raises:
        KeyError: the active preset is missing AND no usable default exists.
        ValueError: the active preset file exists but is malformed. Malformed
            presets are an authoring error and surface unchanged — they are
            not silently swapped for the default.
    """
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        return
    preset_block = manifest.get("preset")
    if not isinstance(preset_block, dict) or not preset_block.get("active"):
        return

    active_ref = preset_block["active"]
    try:
        preset = load_preset(active_ref, working_dir=working_dir)
    except KeyError:
        default_ref = preset_block.get("default")
        if (
            isinstance(default_ref, str)
            and default_ref
            and default_ref != active_ref
        ):
            preset = load_preset(default_ref, working_dir=working_dir)
            log.warning(
                "active preset %r is missing on this machine; "
                "falling back to default %r and updating manifest.preset.active",
                active_ref, default_ref,
            )
            preset_block["active"] = default_ref
        else:
            raise
    preset_manifest = preset.get("manifest", {})

    # context_limit lives inside manifest.llm in the preset, but at manifest
    # root in init.json — strip it from the llm dict before substitution and
    # write it to the root.
    preset_llm = dict(preset_manifest.get("llm") or manifest.get("llm") or {})
    preset_ctx = preset_llm.pop("context_limit", None)
    manifest["llm"] = preset_llm

    # Capabilities are wholesale-replaced by the preset (atomic swap is the
    # whole point of presets). One carve-out preserves project-truth across
    # preset switches:
    #   - skills.paths: extras declared in init.json append to the preset's
    #     defaults. Skill paths are project-truth (where this agent's extra
    #     skills live), not runtime-tier choices, so swapping presets should
    #     not erase them.
    # If you need to add another carve-out, do it here — keep the list short.
    init_caps = manifest.get("capabilities", {}) if isinstance(
        manifest.get("capabilities"), dict) else {}
    init_skill_paths: list[str] = []
    cfg = init_caps.get("skills")
    if isinstance(cfg, dict) and isinstance(cfg.get("paths"), list):
        for path in cfg.get("paths", []):
            if isinstance(path, str) and path not in init_skill_paths:
                init_skill_paths.append(path)

    new_caps = preset_manifest.get("capabilities", init_caps)
    if isinstance(new_caps, dict) and init_skill_paths:
        # Make sure we have a skills entry to merge into. If the preset didn't
        # enable skills, init.json's paths alone are enough to enable it.
        skills_kwargs = new_caps.get("skills", {})
        if not isinstance(skills_kwargs, dict):
            skills_kwargs = {}
        preset_paths = skills_kwargs.get("paths", []) or []
        # Order: preset paths first (the curated defaults), then init.json
        # extras unique to it. Dedupe by raw string — `~/foo` and the
        # absolute form count as different so the user's intent shows up
        # in info() output verbatim.
        merged: list[str] = list(preset_paths)
        seen = set(preset_paths)
        for p in init_skill_paths:
            if p not in seen:
                merged.append(p)
                seen.add(p)
        skills_kwargs = dict(skills_kwargs)
        skills_kwargs["paths"] = merged
        new_caps = dict(new_caps)
        new_caps["skills"] = skills_kwargs

    manifest["capabilities"] = new_caps
    if preset_ctx is not None:
        manifest["context_limit"] = preset_ctx


# ---------------------------------------------------------------------------
# Tier taxonomy
# ---------------------------------------------------------------------------
#
# `description.tier` is a five-rung cost/quality ladder stored as plain
# numeric strings 1..5 — higher is better. The TUI renders these as
# locale-appropriate chips. Agents read the full description block via
# `system(action='presets')` and pick presets accordingly.
TIER_VALUES = ("1", "2", "3", "4", "5")


def preset_tier(preset: dict) -> str | None:
    """Return the preset's tier value (e.g. '4') or None.

    Reads `description.tier`. Returns None for malformed presets that
    didn't pass through `load_preset` validation.
    """
    if not isinstance(preset, dict):
        return None
    desc = preset.get("description")
    if not isinstance(desc, dict):
        return None
    tier = desc.get("tier")
    return tier if isinstance(tier, str) else None


def preset_context_limit(preset_manifest: dict) -> int | None:
    """Return the preset's context_limit (lives inside manifest.llm).

    context_limit is a property of the model, so it's stored next to
    provider/model. Returns None when unset.

    For presets read via `load_preset`, the kernel migration system has
    already relocated any legacy root-level placements before validation —
    so by the time this helper is called, only the canonical location is
    populated.
    """
    if not isinstance(preset_manifest, dict):
        return None
    llm = preset_manifest.get("llm")
    if isinstance(llm, dict):
        return llm.get("context_limit")
    return None


def expand_inherit(capabilities: dict, main_llm: dict) -> dict:
    """Resolve `"provider": "inherit"` sentinels in capability configs.

    For each capability whose kwargs has `provider == "inherit"`, replace it
    with the main LLM's provider plus its credentials (api_key, api_key_env,
    base_url) and wire-protocol flag (api_compat). The `model` field is NOT
    inherited — capabilities pick their own model independently.

    api_compat must inherit too: capability fallbacks (e.g. vision) dispatch
    between OpenAI / Anthropic / Gemini adapters based on it, and an inheriting
    capability that drops api_compat silently routes through the wrong adapter.

    Mutates `capabilities` in place. Returns the same dict for convenience.
    """
    for cap_name, kwargs in capabilities.items():
        if not isinstance(kwargs, dict):
            continue
        if kwargs.get("provider") != "inherit":
            continue
        kwargs["provider"]    = main_llm.get("provider")
        kwargs["api_key"]     = main_llm.get("api_key")
        kwargs["api_key_env"] = main_llm.get("api_key_env")
        kwargs["base_url"]    = main_llm.get("base_url")
        kwargs["api_compat"]  = main_llm.get("api_compat")
    return capabilities
