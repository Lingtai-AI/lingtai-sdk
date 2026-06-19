"""Soul config and voice handling.

Manages init.json persistence for soul cadence (delay_seconds) and
voice profile (voice name + optional custom prompt). Also resolves
voice profiles to system prompts.
"""
from __future__ import annotations

# Lower bound on agent-set soul delay. Below this, the consultation cost
# (M parallel LLM calls per fire) dominates the agent's own turns.
SOUL_DELAY_MIN_SECONDS = 30.0
# Bounds on K — past-self voice count per fire. 0 = insights-only fires;
# 5 caps M=6 LLM calls per fire (cost + chat-history bloat).
CONSULTATION_PAST_COUNT_MIN = 0
CONSULTATION_PAST_COUNT_MAX = 5

# Built-in voice profile names. The agent can switch between these or set
# a custom prompt via soul(action='voice'). Order here = order shown in
# the read response.
SOUL_VOICE_BUILTINS = ("inner", "observer")
# Cap on agent-supplied custom voice prompts. Comfortable budget — the
# observer preset is ~580 chars; 4000 is generous without inviting
# system-prompt-stuffing as a side channel.
SOUL_VOICE_PROMPT_MAX = 4000


def _handle_config(agent, args: dict) -> dict:
    """Handle action='config' — adjust soul flow knobs.

    Accepts any subset of: delay_seconds, consultation_past_count.
    Validates each provided field, updates live state, restarts the
    wall-clock timer if delay changed, persists to init.json. Returns
    old and new values for every field that was actually changed
    (untouched fields are absent from the response).
    """
    provided: dict = {}
    if "delay_seconds" in args:
        provided["delay_seconds"] = args["delay_seconds"]
    if "consultation_past_count" in args:
        provided["consultation_past_count"] = args["consultation_past_count"]
    if not provided:
        return {
            "error": (
                "config requires at least one of: delay_seconds, "
                "consultation_past_count."
            ),
        }

    new_values: dict = {}
    old_values: dict = {}

    if "delay_seconds" in provided:
        raw = provided["delay_seconds"]
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return {"error": f"delay_seconds must be a number, got {type(raw).__name__}."}
        if v != v:  # NaN
            return {"error": "delay_seconds must be a finite number, got NaN."}
        if v < SOUL_DELAY_MIN_SECONDS:
            return {
                "error": (
                    f"delay_seconds must be at least {SOUL_DELAY_MIN_SECONDS}s "
                    f"(got {v}). Below this, consultation cost dominates "
                    "the main agent loop."
                ),
            }
        old_values["delay_seconds"] = float(agent._soul_delay)
        agent._soul_delay = v
        new_values["delay_seconds"] = v

    if "consultation_past_count" in provided:
        raw = provided["consultation_past_count"]
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return {"error": f"consultation_past_count must be an integer, got {type(raw).__name__}."}
        if v < CONSULTATION_PAST_COUNT_MIN or v > CONSULTATION_PAST_COUNT_MAX:
            return {
                "error": (
                    f"consultation_past_count must be in "
                    f"[{CONSULTATION_PAST_COUNT_MIN}, {CONSULTATION_PAST_COUNT_MAX}] "
                    f"(got {v}). 0 = insights-only fires; cap protects against "
                    "fan-out cost and chat-history bloat."
                ),
            }
        old_values["consultation_past_count"] = int(getattr(agent._config, "consultation_past_count", 2))
        agent._config.consultation_past_count = v
        new_values["consultation_past_count"] = v

    # Restart the wall-clock timer if delay changed (or if any change
    # happened — restarting on every config call keeps the cadence in
    # sync without surprising drift; cheap operation).
    if "delay_seconds" in new_values:
        try:
            if not agent._shutdown.is_set():
                agent._start_soul_timer()
        except Exception as e:
            agent._log("soul_config_restart_failed", error=str(e)[:200])

    persist_error = _persist_soul_config(agent, new_values)

    log_kw: dict = {"old": old_values, "new": new_values}
    if persist_error:
        log_kw["persist_error"] = persist_error
    agent._log("soul_config", **log_kw)

    return {
        "status": "ok",
        "old": old_values,
        "new": new_values,
    }


def _handle_voice(agent, args: dict) -> dict:
    """Handle action='voice' — read, switch preset, or set a custom soul
    voice prompt. The agent owns this — it chooses how its own inner
    voice sounds in soul-flow consultations.

    Modes:
      - bare (no ``set``): read current voice, list available presets,
        return the resolved system prompt as it stands.
      - ``set=<preset>`` (one of SOUL_VOICE_BUILTINS): switch to a
        built-in profile. Clears any prior custom prompt.
      - ``set="custom"``: requires a non-empty ``prompt`` field
        (length-capped at SOUL_VOICE_PROMPT_MAX). Stores the prompt and
        marks the voice as custom; takes effect on the next consultation
        fire.

    Persists changes to manifest.soul in init.json so they survive
    restart.
    """
    set_to = args.get("set")
    current_voice = getattr(agent._config, "soul_voice", "inner") or "inner"
    current_prompt = getattr(agent._config, "soul_voice_prompt", "") or ""

    # ---- Read mode ----------------------------------------------------
    if set_to is None:
        try:
            resolved = _build_soul_system_prompt(agent, kind="insights")
        except Exception as e:
            resolved = f"<resolution failed: {e!s}>"
        return {
            "status": "ok",
            "current": current_voice,
            "available": list(SOUL_VOICE_BUILTINS),
            "prompt": resolved,
            **(
                {"custom_prompt": current_prompt}
                if current_voice == "custom" and current_prompt
                else {}
            ),
        }

    # ---- Validate set value ------------------------------------------
    if not isinstance(set_to, str):
        return {"error": f"set must be a string, got {type(set_to).__name__}."}
    set_to = set_to.strip()
    if not set_to:
        return {"error": "set is empty — pass a profile name or 'custom'."}

    valid = set(SOUL_VOICE_BUILTINS) | {"custom"}
    if set_to not in valid:
        return {
            "error": (
                f"Unknown voice profile: {set_to!r}. "
                f"Valid: {sorted(SOUL_VOICE_BUILTINS) + ['custom']}."
            ),
        }

    # ---- Custom mode --------------------------------------------------
    new_prompt = ""
    if set_to == "custom":
        raw_prompt = args.get("prompt")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            return {
                "error": (
                    "set='custom' requires a non-empty 'prompt' field — "
                    "this is the system prompt your soul-flow voice will "
                    "use. Speak as the soul; describe how you want to be "
                    "framed when reading your own diary."
                ),
            }
        if len(raw_prompt) > SOUL_VOICE_PROMPT_MAX:
            return {
                "error": (
                    f"prompt is too long ({len(raw_prompt)} chars). "
                    f"Maximum is {SOUL_VOICE_PROMPT_MAX}."
                ),
            }
        new_prompt = raw_prompt

    # ---- Apply --------------------------------------------------------
    old_voice = current_voice
    old_prompt = current_prompt
    agent._config.soul_voice = set_to
    # Switching away from custom clears the stored custom prompt so it
    # does not silently re-activate later. Switching INTO custom stores
    # the new prompt; switching between built-in presets clears.
    agent._config.soul_voice_prompt = new_prompt if set_to == "custom" else ""

    persist_error = _persist_soul_voice(
        agent, voice=set_to, voice_prompt=agent._config.soul_voice_prompt,
    )

    log_kw: dict = {"old_voice": old_voice, "new_voice": set_to}
    if old_voice == "custom" or set_to == "custom":
        # Only log prompt previews when custom is involved; preset
        # switches don't carry meaningful prompt content.
        log_kw["old_prompt_chars"] = len(old_prompt)
        log_kw["new_prompt_chars"] = len(new_prompt)
    if persist_error:
        log_kw["persist_error"] = persist_error
    agent._log("soul_voice", **log_kw)

    return {
        "status": "ok",
        "old": old_voice,
        "new": set_to,
    }


def _persist_soul_config(agent, new_values: dict) -> str | None:
    """Write changed soul knobs into manifest.soul.* in init.json.

    Maps:
      - delay_seconds            -> manifest.soul.delay
      - consultation_past_count  -> manifest.soul.consultation_past_count

    Atomic via temp-file-then-rename. Returns ``None`` on success, or a
    short error string on failure (caller logs it; runtime state is
    unaffected).
    """
    import json
    import os
    from pathlib import Path

    init_path: Path = agent._working_dir / "init.json"
    try:
        with init_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return f"init.json not found at {init_path}"
    except Exception as e:
        return f"failed to read init.json: {e!s}"[:200]

    if not isinstance(data, dict):
        return "init.json root is not an object"
    manifest = data.setdefault("manifest", {})
    if not isinstance(manifest, dict):
        return "manifest is not an object"
    soul_block = manifest.get("soul")
    if not isinstance(soul_block, dict):
        soul_block = {}
        manifest["soul"] = soul_block

    if "delay_seconds" in new_values:
        soul_block["delay"] = new_values["delay_seconds"]
    if "consultation_past_count" in new_values:
        soul_block["consultation_past_count"] = new_values["consultation_past_count"]

    return _atomic_write_init(init_path, data)


def _persist_soul_voice(agent, *, voice: str, voice_prompt: str) -> str | None:
    """Write soul voice profile + (optional) custom prompt into
    manifest.soul in init.json.

    Maps:
      - voice         -> manifest.soul.voice
      - voice_prompt  -> manifest.soul.voice_prompt (only when voice == "custom";
                          deleted from manifest when switching back to a preset
                          to avoid stale prompts re-activating later)

    Atomic via temp-file-then-rename. Returns ``None`` on success, or a
    short error string on failure (caller logs it; runtime state is
    unaffected).
    """
    import json
    from pathlib import Path

    init_path: Path = agent._working_dir / "init.json"
    try:
        with init_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return f"init.json not found at {init_path}"
    except Exception as e:
        return f"failed to read init.json: {e!s}"[:200]

    if not isinstance(data, dict):
        return "init.json root is not an object"
    manifest = data.setdefault("manifest", {})
    if not isinstance(manifest, dict):
        return "manifest is not an object"
    soul_block = manifest.get("soul")
    if not isinstance(soul_block, dict):
        soul_block = {}
        manifest["soul"] = soul_block

    soul_block["voice"] = voice
    if voice == "custom":
        soul_block["voice_prompt"] = voice_prompt
    else:
        soul_block.pop("voice_prompt", None)

    return _atomic_write_init(init_path, data)


def _atomic_write_init(init_path, data) -> str | None:
    """Write ``data`` to ``init_path`` via temp-file-then-rename.

    Used by both _persist_soul_config and _persist_soul_voice. Returns
    ``None`` on success or a short error string on failure.
    """
    import json
    import os

    tmp_path = init_path.with_suffix(init_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, init_path)
        return None
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return f"failed to write init.json: {e!s}"[:200]


def _build_soul_system_prompt(agent, kind: str = "inquiry") -> str:
    """Build the soul session's system prompt.

    kind:
        "inquiry"           — synchronous mirror session (soul_inquiry).
                              Frames the consultee as a deep copy of the
                              current agent answering a question. Uses the
                              static soul.system_prompt key.
        "insights" / "past" — soul-flow consultation voices. Both resolve
                              from the agent's chosen voice profile (the
                              system prompt is now kind-agnostic — the
                              per-fire cue text differentiates whose diary
                              the consultee is reading).

    For the flow kinds, profile resolution is:
      - ``soul_voice == "custom"`` → use ``_config.soul_voice_prompt`` verbatim
      - any other profile name    → look up ``soul.voice.<name>.prompt`` from i18n
      - missing/empty profile     → fall back to "inner"
    """
    from ...i18n import t
    if kind == "inquiry":
        return t(agent._config.language, "soul.system_prompt")

    voice = getattr(agent._config, "soul_voice", "inner") or "inner"
    if voice == "custom":
        prompt = getattr(agent._config, "soul_voice_prompt", "") or ""
        if prompt.strip():
            return prompt
        # Empty custom prompt → fall back to inner so the agent never
        # runs a consultation with no system prompt at all.
        voice = "inner"
    return t(agent._config.language, f"soul.voice.{voice}.prompt")
