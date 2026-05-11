"""Preset management — refresh, swap, list presets."""
from __future__ import annotations


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def _preset_ref_in(name: str, refs: list) -> bool:
    """Membership test on a list of preset path strings, normalized so
    `~/...` and the equivalent absolute path compare equal.

    Used by the `_refresh` allowed-gate so an agent passing the form it
    received from `_presets` (home-shortened) is not refused when the
    on-disk `allowed` entry was written in absolute form, or vice versa.
    """
    if not isinstance(name, str) or not name:
        return False
    from pathlib import Path
    try:
        target = Path(name).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        target = None
    for ref in refs:
        if not isinstance(ref, str):
            continue
        if ref == name:
            return True
        if target is None:
            continue
        try:
            if Path(ref).expanduser().resolve(strict=False) == target:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _check_context_fits(agent, preset_name: str) -> tuple:
    """Read the target preset's context_limit and verify the agent's current
    context usage fits.

    `preset_name` is a path string (~/foo.json, ./foo.json, or absolute).

    Returns (fits, error_message, log_extra). When fits=True, message is None.
    When fits=False, returns a user-facing error message and a dict of fields
    for the preset_swap_refused_oversize log event.
    """
    from ...presets import load_preset, preset_context_limit

    try:
        preset = load_preset(preset_name, working_dir=agent._working_dir)
    except (KeyError, ValueError):
        return True, None, None  # let activate_preset surface the error

    target_limit = preset_context_limit(preset.get("manifest", {}))
    if target_limit is None or target_limit <= 0:
        return True, None, None  # no usable limit → no guard

    try:
        usage = agent.get_token_usage()
        current = usage.get("ctx_total_tokens", 0)
    except Exception:
        return True, None, None  # can't measure — fail open (allow swap)

    if current > target_limit:
        return False, (
            f"current context ({current} tokens) exceeds preset {preset_name!r}'s "
            f"context_limit ({target_limit} tokens) — molt first to clear chat history, "
            f"then retry the swap"
        ), {
            "preset": preset_name,
            "current_tokens": current,
            "target_limit": target_limit,
        }
    return True, None, None


def _refresh(agent, args: dict) -> dict:
    from ...i18n import t
    reason = args.get("reason", "")
    preset_name = args.get("preset")
    revert_preset = args.get("revert_preset", False)

    # Conflict: cannot specify both 'preset' and 'revert_preset'.
    if preset_name is not None and revert_preset:
        return {
            "status": "error",
            "message": "cannot specify both 'preset' and 'revert_preset' — choose one",
        }

    # Revert path: read default name from disk, then route through the same
    # context-limit guard and activation as a named swap.
    if revert_preset:
        try:
            import json as _json
            init_path = agent._working_dir / "init.json"
            data = _json.loads(init_path.read_text(encoding="utf-8"))
            preset_block = data.get("manifest", {}).get("preset") or {}
            default_name = preset_block.get("default") if isinstance(preset_block, dict) else None
        except Exception as e:
            return {"status": "error",
                    "message": f"failed to read default preset: {e}"}
        if not default_name:
            return {"status": "error",
                    "message": "no default preset configured — manifest.preset.default is missing"}
        preset_name = default_name

    if preset_name is not None:
        # Guard: refuse swap if the requested preset is not in the agent's
        # `allowed` list. Authorization is declared up front in init.json;
        # runtime is not allowed to silently broaden it.
        #
        # Path matching is normalized so `~/foo.json` and the absolute
        # form of the same path compare equal. Without this, an agent
        # that received a preset name from `_presets` (which renders with
        # `home_shortened`) would be refused if the on-disk `allowed`
        # entry was written in absolute form (or vice versa).
        try:
            import json as _json
            init_path = agent._working_dir / "init.json"
            data = _json.loads(init_path.read_text(encoding="utf-8"))
            preset_block = data.get("manifest", {}).get("preset") or {}
            allowed = preset_block.get("allowed") if isinstance(preset_block, dict) else None
        except Exception:
            allowed = None
        if isinstance(allowed, list) and not _preset_ref_in(preset_name, allowed):
            agent._log("preset_swap_refused_unauthorized",
                       requested=preset_name)
            return {
                "status": "error",
                "message": (
                    f"preset {preset_name!r} is not in this agent's allowed "
                    f"list — call system(action='presets') to see what's available"
                ),
            }

        # Guard: refuse swap if the target preset's context_limit is smaller
        # than the agent's current context usage. The agent must molt first
        # to clear history before the new (narrower) preset can hold it.
        fits, refuse_msg, log_extra = _check_context_fits(agent, preset_name)
        if not fits:
            agent._log("preset_swap_refused_oversize", **log_extra)
            return {"status": "error", "message": refuse_msg}

        try:
            if revert_preset:
                agent._activate_default_preset()
            else:
                agent._activate_preset(preset_name)
        except KeyError:
            agent._log("preset_swap_failed",
                       requested=preset_name,
                       reason="not_found")
            return {"status": "error",
                    "message": f"preset {preset_name!r} not found — call system(action='presets') to see available presets"}
        except (ValueError, OSError, NotImplementedError, RuntimeError) as e:
            agent._log("preset_swap_failed",
                       requested=preset_name,
                       reason=str(e))
            return {"status": "error",
                    "message": f"failed to activate preset {preset_name!r}: {e}"}
        agent._log("preset_swap_started",
                   preset=preset_name, reason=reason, revert=revert_preset)

    agent._log("refresh_requested", reason=reason)

    # Re-spawn any init.json MCPs whose subprocess exited at boot (or has
    # since died). The Agent subclass owns the retry — BaseAgent has no
    # MCP machinery — so the call is gated on hasattr(). Failures are
    # logged and swallowed so a flaky MCP cannot block refresh itself.
    # Closes Lingtai-AI/lingtai#34.
    retry = getattr(agent, "_retry_failed_mcps", None)
    if callable(retry):
        try:
            report = retry()
            if report.get("retried"):
                agent._log("mcp_retry_summary", **report)
        except Exception as e:
            agent._log("mcp_retry_error", error=str(e))

    agent._perform_refresh()
    return {
        "status": "ok",
        "message": t(agent._config.language, "system_tool.refresh_message"),
    }


def _presets(agent, args: dict) -> dict:
    """List available presets in the agent's libraries, with active marker.

    Each preset's `name` is its **path** (~/.lingtai-tui/presets/foo.json
    style when under $HOME, otherwise absolute) — that's the same string an
    agent passes to `system(action='refresh', preset=...)` to swap. Two
    libraries each containing `cheap.json` appear as two distinct entries
    with different paths — no collisions, no shadowing.

    For each preset, includes a `connectivity` field reporting whether the
    preset's LLM endpoint is reachable RIGHT NOW. Probes run in parallel.
    No caching — every call is a fresh check.
    """
    import json
    from ...presets import load_preset, resolve_allowed_presets, home_shortened
    from ...preset_connectivity import check_many

    init_path = agent._working_dir / "init.json"
    try:
        raw = json.loads(init_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "message": f"failed to read init.json: {e}"}

    manifest = raw.get("manifest", {})
    preset_block = manifest.get("preset") or {}
    active = preset_block.get("active") if isinstance(preset_block, dict) else None
    # The allowed list IS the agent's preset surface — no directory scan,
    # no implicit fallback. If the umbrella is absent or allowed is empty,
    # the agent has no presets to swap to.
    allowed_paths = resolve_allowed_presets(manifest, agent._working_dir)

    available = []
    connectivity_specs = []
    # Sorted by display path for stable ordering. Skip duplicates that may
    # arise if the same path appears more than once in `allowed`.
    seen: set[str] = set()
    entries: list[tuple[str, "Path"]] = []
    for path in allowed_paths:
        key = home_shortened(path)
        if key in seen:
            continue
        seen.add(key)
        entries.append((key, path))
    entries.sort(key=lambda kv: kv[0])

    for name, _path in entries:
        try:
            preset = load_preset(name, working_dir=agent._working_dir)
        except (KeyError, ValueError):
            # Allowed entries that no longer exist on disk are reported as
            # malformed in their connectivity check rather than silently
            # dropped — but presets that fail load_preset's deeper validation
            # are skipped from the listing to keep the agent's view tidy.
            continue
        pm = preset.get("manifest", {})
        llm = pm.get("llm", {})
        available.append({
            "name": name,
            "description": preset.get("description", {}),
            "llm": {
                "provider": llm.get("provider"),
                "model": llm.get("model"),
            },
            "capabilities": pm.get("capabilities", {}),
        })
        connectivity_specs.append({
            "provider": llm.get("provider"),
            "base_url": llm.get("base_url"),
            "api_key_env": llm.get("api_key_env"),
        })

    # Probe all presets in parallel — fresh each call.
    connectivities = check_many(connectivity_specs)
    for entry, conn in zip(available, connectivities):
        entry["connectivity"] = conn

    return {
        "status": "ok",
        "active": active,
        "available": available,
    }
