# tests/test_presets.py
import json
import os
from pathlib import Path

import pytest

from lingtai_kernel.presets import (
    discover_presets,
    load_preset,
    default_presets_path,
    expand_inherit,
    home_shortened,
    materialize_active_preset,
    preset_context_limit,
    preset_tier,
    resolve_preset_name,
    resolve_allowed_presets,
    TIER_VALUES,
)


# ---------------------------------------------------------------------------
# discover_presets — returns path-string keys (one entry per distinct file)
# ---------------------------------------------------------------------------

def test_discover_presets_empty_dir(tmp_path):
    """Empty directory returns empty dict."""
    assert discover_presets(tmp_path) == {}


def test_discover_presets_lists_json_files_keyed_by_path(tmp_path):
    """Top-level *.json files are discovered, keyed by their path string."""
    (tmp_path / "alpha.json").write_text(
        '{"name": "alpha", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    (tmp_path / "beta.json").write_text(
        '{"name": "beta", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    result = discover_presets(tmp_path)
    assert len(result) == 2
    keys = sorted(result.keys())
    assert all(k.endswith(".json") for k in keys)
    for k, v in result.items():
        assert v.is_file()
        assert k.endswith(v.name)


def test_discover_presets_ignores_non_json(tmp_path):
    """Non-.json files (README.md, etc.) are ignored."""
    (tmp_path / "preset.json").write_text(
        '{"name": "p", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    (tmp_path / "README.md").write_text("# Library docs")
    (tmp_path / "notes.txt").write_text("scratch")
    result = discover_presets(tmp_path)
    assert len(result) == 1
    assert next(iter(result.keys())).endswith("preset.json")


def test_discover_presets_ignores_subdirs(tmp_path):
    """Subdirectories are not recursed into."""
    (tmp_path / "top.json").write_text(
        '{"name": "top", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.json").write_text(
        '{"name": "nested", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    result = discover_presets(tmp_path)
    assert len(result) == 1
    assert next(iter(result.keys())).endswith("top.json")


def test_discover_presets_accepts_jsonc(tmp_path):
    """*.jsonc files are also discovered."""
    (tmp_path / "with_comments.jsonc").write_text(
        '{"name": "with_comments", "description": {"summary": "ok"}, "manifest": {"llm": {"provider": "x", "model": "y"}}}'
    )
    result = discover_presets(tmp_path)
    assert len(result) == 1
    assert next(iter(result.keys())).endswith("with_comments.jsonc")


def test_discover_presets_missing_dir(tmp_path):
    """Nonexistent directory returns empty dict (no error)."""
    missing = tmp_path / "does_not_exist"
    assert discover_presets(missing) == {}


def test_default_presets_path_returns_correct_location():
    """default_presets_path returns ~/.lingtai-tui/presets/ as a Path."""
    p = default_presets_path()
    assert isinstance(p, Path)
    assert p.parts[-2:] == (".lingtai-tui", "presets")


# ---------------------------------------------------------------------------
# load_preset — name is a path string (~, ./, or absolute)
# ---------------------------------------------------------------------------

def _write_preset(dir: Path, name: str, content: dict) -> Path:
    p = dir / f"{name}.json"
    p.write_text(json.dumps(content))
    return p


def _valid_preset(name: str = "test") -> dict:
    return {
        "name": name,
        "description": {"summary": "test preset"},
        "manifest": {
            "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                    "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
            "capabilities": {"file": {}, "email": {}},
        },
    }


# Minimal-but-valid description block used inline in tests where the test
# wants to focus on some other field while still passing description
# validation. Every preset on disk MUST have description.summary.
_DESC = {"summary": "ok"}


def test_load_preset_absolute_path(tmp_path):
    """Absolute path loads directly."""
    p = _write_preset(tmp_path, "alpha", _valid_preset("alpha"))
    loaded = load_preset(str(p))
    assert loaded["name"] == "alpha"


def test_load_preset_relative_path(tmp_path):
    """Relative path resolves against working_dir."""
    _write_preset(tmp_path, "alpha", _valid_preset("alpha"))
    loaded = load_preset("./alpha.json", working_dir=tmp_path)
    assert loaded["name"] == "alpha"


def test_load_preset_relative_without_working_dir_raises(tmp_path):
    """Relative path with no working_dir is a ValueError."""
    with pytest.raises(ValueError, match="working_dir"):
        load_preset("./alpha.json")


def test_load_preset_home_relative_path(tmp_path, monkeypatch):
    """`~/...` form is expanded to $HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _write_preset(fake_home, "alpha", _valid_preset("alpha"))
    loaded = load_preset("~/alpha.json")
    assert loaded["name"] == "alpha"


def test_load_preset_missing_extension_rejected(tmp_path):
    """Bare stems (no .json/.jsonc extension) are rejected — names must be paths."""
    _write_preset(tmp_path, "alpha", _valid_preset("alpha"))
    with pytest.raises(ValueError, match=r"\.json"):
        load_preset(str(tmp_path / "alpha"))


def test_load_preset_missing_file_raises_key_error(tmp_path):
    """Missing file → KeyError."""
    with pytest.raises(KeyError, match="not found"):
        load_preset(str(tmp_path / "nonexistent.json"))


def test_load_preset_empty_name_raises_value_error():
    with pytest.raises(ValueError, match="non-empty"):
        load_preset("")


def test_load_preset_jsonc_strips_comments(tmp_path):
    """JSONC with // comments and trailing commas parses correctly."""
    body = '''{
      "name": "withcomments",   // inline comment
      "description": {"summary": "tests JSONC"},
      "manifest": {
        "llm": {"provider": "x", "model": "y"},
        "capabilities": {"file": {}},   // trailing comma here
      },
    }'''
    p = tmp_path / "withcomments.jsonc"
    p.write_text(body)
    loaded = load_preset(str(p))
    assert loaded["name"] == "withcomments"


def test_load_preset_missing_manifest_raises(tmp_path):
    bad = {"name": "bad", "description": "x"}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="manifest"):
        load_preset(str(p))


def test_load_preset_missing_llm_raises(tmp_path):
    bad = {"name": "bad", "manifest": {"capabilities": {}}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="manifest.llm"):
        load_preset(str(p))


def test_load_preset_empty_provider_raises(tmp_path):
    bad = {"name": "bad", "manifest": {"llm": {"provider": "", "model": "y"}, "capabilities": {}}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="provider"):
        load_preset(str(p))


def test_load_preset_empty_model_raises(tmp_path):
    bad = {"name": "bad", "manifest": {"llm": {"provider": "x", "model": ""}, "capabilities": {}}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="model"):
        load_preset(str(p))


def test_load_preset_malformed_json_raises(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{ not valid json }")
    with pytest.raises(ValueError, match="parse"):
        load_preset(str(p))


def test_load_preset_accepts_context_limit_inside_llm_block(tmp_path):
    p = {
        "name": "okllm",
        "description": _DESC,
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": 65536},
            "capabilities": {},
        },
    }
    f = tmp_path / "okllm.json"
    f.write_text(json.dumps(p))
    loaded = load_preset(str(f))
    assert preset_context_limit(loaded["manifest"]) == 65536


def test_load_preset_relocates_legacy_root_context_limit(tmp_path):
    """Legacy on-disk layout: context_limit at manifest root.

    The kernel migration system (m001) runs from inside load_preset and
    relocates the field into manifest.llm before validation.
    """
    p = {
        "name": "legacy",
        "description": _DESC,
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
            "context_limit": 32768,
        },
    }
    f = tmp_path / "legacy.json"
    f.write_text(json.dumps(p))

    loaded = load_preset(str(f))

    assert preset_context_limit(loaded["manifest"]) == 32768
    assert loaded["manifest"]["llm"]["context_limit"] == 32768
    on_disk = json.loads(f.read_text())
    assert "context_limit" not in on_disk["manifest"]
    assert on_disk["manifest"]["llm"]["context_limit"] == 32768


def test_load_preset_drops_duplicate_legacy_root_context_limit(tmp_path):
    """Both locations with the same value are accepted in memory."""
    from lingtai_kernel.migrate.migrate import reset_process_cache
    reset_process_cache()
    p = {
        "name": "dup",
        "description": _DESC,
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": 32768},
            "capabilities": {},
            "context_limit": 32768,
        },
    }
    f = tmp_path / "dup.json"
    f.write_text(json.dumps(p))

    loaded = load_preset(str(f))

    assert preset_context_limit(loaded["manifest"]) == 32768
    assert "context_limit" not in loaded["manifest"]


def test_load_preset_conflicting_legacy_root_context_limit_preserves_llm(tmp_path):
    """When both locations disagree, canonical manifest.llm wins."""
    from lingtai_kernel.migrate.migrate import reset_process_cache
    reset_process_cache()
    p = {
        "name": "dup",
        "description": _DESC,
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": 16384},
            "capabilities": {},
            "context_limit": 32768,
        },
    }
    f = tmp_path / "dup.json"
    f.write_text(json.dumps(p))

    loaded = load_preset(str(f))

    assert preset_context_limit(loaded["manifest"]) == 16384
    assert loaded["manifest"]["llm"]["context_limit"] == 16384
    assert "context_limit" not in loaded["manifest"]


def test_load_preset_rejects_non_integer_context_limit(tmp_path):
    p = {
        "name": "bad",
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": "65536"},
            "capabilities": {},
        },
    }
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="integer"):
        load_preset(str(f))


def test_preset_context_limit_reads_from_llm_block():
    manifest = {
        "llm": {"provider": "x", "model": "y", "context_limit": 16384},
        "capabilities": {},
    }
    assert preset_context_limit(manifest) == 16384


def test_preset_context_limit_returns_none_when_unset():
    manifest = {"llm": {"provider": "x", "model": "y"}, "capabilities": {}}
    assert preset_context_limit(manifest) is None


# ---------------------------------------------------------------------------
# expand_inherit
# ---------------------------------------------------------------------------

def test_expand_inherit_resolves_to_main_llm():
    main_llm = {
        "provider": "gemini", "model": "gemini-2.5-pro",
        "api_key": None, "api_key_env": "GEMINI_API_KEY",
        "base_url": None,
    }
    caps = {
        "web_search": {"provider": "inherit"},
        "vision":     {"provider": "inherit"},
        "file":       {},
    }
    expand_inherit(caps, main_llm)
    assert caps["web_search"]["provider"] == "gemini"
    assert caps["web_search"]["api_key_env"] == "GEMINI_API_KEY"
    assert caps["vision"]["provider"] == "gemini"
    assert caps["file"] == {}


def test_expand_inherit_does_not_inherit_model():
    main_llm = {
        "provider": "openai", "model": "gpt-5",
        "api_key": None, "api_key_env": "OPENAI_API_KEY",
    }
    caps = {"vision": {"provider": "inherit"}}
    expand_inherit(caps, main_llm)
    assert "model" not in caps["vision"]


def test_expand_inherit_no_op_for_explicit_provider():
    main_llm = {"provider": "gemini", "model": "x", "api_key_env": "GEMINI_API_KEY"}
    caps = {"web_search": {"provider": "duckduckgo"}}
    expand_inherit(caps, main_llm)
    assert caps["web_search"] == {"provider": "duckduckgo"}


def test_expand_inherit_handles_missing_main_llm_creds():
    main_llm = {"provider": "local", "model": "x"}
    caps = {"web_search": {"provider": "inherit"}}
    expand_inherit(caps, main_llm)
    assert caps["web_search"]["provider"] == "local"
    assert caps["web_search"].get("api_key_env") is None


def test_expand_inherit_propagates_api_compat():
    # Custom anthropic-compat proxy (e.g. local GLM-5.1 via JoyCodeProxy).
    # Vision capability fallback dispatches on api_compat — if it isn't
    # inherited, the capability silently routes through the OpenAI adapter
    # and chokes on the response shape.
    main_llm = {
        "provider": "custom",
        "api_compat": "anthropic",
        "model": "GLM-5.1",
        "api_key_env": "CUSTOM_6_API_KEY",
        "base_url": "http://127.0.0.1:34891",
    }
    caps = {"vision": {"provider": "inherit"}}
    expand_inherit(caps, main_llm)
    assert caps["vision"]["api_compat"] == "anthropic"
    assert caps["vision"]["base_url"] == "http://127.0.0.1:34891"


# ---------------------------------------------------------------------------
# resolve_preset_name
# ---------------------------------------------------------------------------

def test_resolve_preset_name_absolute(tmp_path):
    abs_p = tmp_path / "foo.json"
    out = resolve_preset_name(str(abs_p), tmp_path / "wd")
    assert out == abs_p


def test_resolve_preset_name_home_relative(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = resolve_preset_name("~/foo.json", tmp_path / "wd")
    assert out == tmp_path / "foo.json"


def test_resolve_preset_name_working_dir_relative(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    out = resolve_preset_name("./foo.json", wd)
    assert out == (wd / "foo.json").resolve()


def test_resolve_preset_name_empty_raises():
    with pytest.raises(ValueError):
        resolve_preset_name("", Path("/tmp"))


# ---------------------------------------------------------------------------
# home_shortened
# ---------------------------------------------------------------------------

def test_home_shortened_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".lingtai-tui" / "presets" / "deepseek.json"
    assert home_shortened(p) == os.path.join("~", ".lingtai-tui", "presets", "deepseek.json")


def test_home_shortened_outside_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = tmp_path / "elsewhere" / "foo.json"
    assert home_shortened(p) == str(p)


def test_home_shortened_relative_returns_unchanged():
    assert home_shortened("./foo.json") == "./foo.json"


# ---------------------------------------------------------------------------
# resolve_allowed_presets — manifest.preset.allowed → list[Path]
# ---------------------------------------------------------------------------

def test_resolve_allowed_presets_absolute(tmp_path):
    p = tmp_path / "presets" / "minimax.json"
    manifest = {"preset": {
        "active": str(p),
        "default": str(p),
        "allowed": [str(p)],
    }}
    result = resolve_allowed_presets(manifest, tmp_path / "wd")
    assert result == [p]


def test_resolve_allowed_presets_relative(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    manifest = {"preset": {
        "active": "./my.json",
        "default": "./my.json",
        "allowed": ["./my.json"],
    }}
    result = resolve_allowed_presets(manifest, wd)
    assert result == [(wd / "my.json").resolve()]


def test_resolve_allowed_presets_missing_block_returns_empty(tmp_path):
    assert resolve_allowed_presets({}, tmp_path) == []


def test_resolve_allowed_presets_missing_allowed_returns_empty(tmp_path):
    manifest = {"preset": {"active": "x.json", "default": "x.json"}}
    assert resolve_allowed_presets(manifest, tmp_path) == []


def test_resolve_allowed_presets_multiple_entries(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    abs_a = tmp_path / "a.json"
    manifest = {"preset": {
        "active": str(abs_a),
        "default": str(abs_a),
        "allowed": [str(abs_a), "./b.json"],
    }}
    result = resolve_allowed_presets(manifest, wd)
    assert result == [abs_a, (wd / "b.json").resolve()]


def test_resolve_allowed_presets_skips_non_string_entries(tmp_path):
    abs_a = tmp_path / "a.json"
    manifest = {"preset": {
        "active": str(abs_a),
        "default": str(abs_a),
        "allowed": [str(abs_a), 42, "", None],
    }}
    result = resolve_allowed_presets(manifest, tmp_path)
    assert result == [abs_a]


# ---------------------------------------------------------------------------
# discover_presets across multiple libraries
# ---------------------------------------------------------------------------

def test_discover_presets_unions_across_libraries(tmp_path):
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib1, "alpha", _valid_preset("alpha"))
    _write_preset(lib2, "beta", _valid_preset("beta"))
    result = discover_presets([lib1, lib2])
    assert len(result) == 2
    keys = sorted(result.keys())
    assert any(k.endswith("alpha.json") for k in keys)
    assert any(k.endswith("beta.json") for k in keys)


def test_discover_presets_same_stem_in_two_libraries_yields_two_entries(tmp_path):
    """The headline of the redesign: same stem in two libraries → two distinct
    entries, no collisions, no shadowing.
    """
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib1, "shared", _valid_preset("from_lib1"))
    _write_preset(lib2, "shared", _valid_preset("from_lib2"))
    result = discover_presets([lib1, lib2])
    assert len(result) == 2
    parents = {v.parent for v in result.values()}
    assert parents == {lib1, lib2}


def test_discover_presets_skips_missing_dirs_in_list(tmp_path):
    lib1 = tmp_path / "lib1"
    lib1.mkdir()
    _write_preset(lib1, "alpha", _valid_preset("alpha"))
    missing = tmp_path / "does_not_exist"
    result = discover_presets([missing, lib1])
    assert len(result) == 1
    assert next(iter(result.values())).parent == lib1


def test_discover_presets_listing_keys_round_trip_through_load(tmp_path):
    """Path keys returned by discover_presets pass straight back to load_preset."""
    _write_preset(tmp_path, "alpha", _valid_preset("alpha"))
    listing = discover_presets(tmp_path)
    name = next(iter(listing.keys()))
    loaded = load_preset(name)
    assert loaded["name"] == "alpha"


# ---------------------------------------------------------------------------
# description object validation + tier vocabulary
# ---------------------------------------------------------------------------

def test_load_preset_missing_description_synthesized_then_rejected(tmp_path):
    """Missing description is normalized by m002 to {summary: ""}, which then
    fails non-empty-summary validation. The user is forced to fix the file
    rather than have a silent default mask the absent commentary."""
    bad = {
        "name": "nodesc",
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "nodesc.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="summary.*non-empty"):
        load_preset(str(f))


def test_load_preset_string_description_promoted_by_migration(tmp_path):
    """Plain-string descriptions are promoted to {summary: "<old>"} by m002,
    so a legacy file loads cleanly without operator intervention."""
    legacy = {
        "name": "stringdesc",
        "description": "legacy text-only description",
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "stringdesc.json"
    f.write_text(json.dumps(legacy))
    loaded = load_preset(str(f))
    assert loaded["description"] == {"summary": "legacy text-only description"}


def test_load_preset_requires_non_empty_summary(tmp_path):
    bad = {
        "name": "emptysum",
        "description": {"summary": ""},
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "emptysum.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="summary.*non-empty"):
        load_preset(str(f))


def test_load_preset_accepts_tier_value(tmp_path):
    p = {
        "name": "tiered",
        "description": {"summary": "ok", "tier": "4"},
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "tiered.json"
    f.write_text(json.dumps(p))
    loaded = load_preset(str(f))
    assert loaded["description"]["tier"] == "4"


def test_load_preset_rejects_invalid_tier(tmp_path):
    bad = {
        "name": "badtier",
        "description": {"summary": "ok", "tier": "godlike"},
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "badtier.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="tier.*one of"):
        load_preset(str(f))


def test_load_preset_preserves_extra_description_keys(tmp_path):
    """Author-chosen extra keys (gains/loses/etc.) survive load verbatim."""
    p = {
        "name": "rich",
        "description": {
            "summary": "ok",
            "tier": "3",
            "gains": ["1M context"],
            "loses": ["vision"],
            "recommended_for": "code review",
        },
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    f = tmp_path / "rich.json"
    f.write_text(json.dumps(p))
    loaded = load_preset(str(f))
    assert loaded["description"]["gains"] == ["1M context"]
    assert loaded["description"]["loses"] == ["vision"]
    assert loaded["description"]["recommended_for"] == "code review"


def test_preset_tier_reads_description_block():
    assert preset_tier({"description": {"summary": "x", "tier": "4"}}) == "4"


def test_preset_tier_returns_none_when_unset():
    assert preset_tier({"description": {"summary": "x"}}) is None
    assert preset_tier({}) is None
    assert preset_tier({"description": "old string form"}) is None


def test_tier_vocabulary_is_numeric_one_through_five():
    assert TIER_VALUES == ("1", "2", "3", "4", "5")


# ---------------------------------------------------------------------------
# materialize_active_preset — wholesale capability replacement (issue #114, Bug D)
# ---------------------------------------------------------------------------
#
# Bug D in #114 reports that init.json capability edits silently vanish on the
# next refresh because materialize_active_preset wholesale-replaces
# manifest.capabilities from the active preset. This is intentional design
# (presets are an atomic swap; skills.paths is the one carve-out). These tests
# pin that documented behavior so it stays explicit rather than surprising, and
# guard the skills.paths carve-out against regressions.


def _preset_content(name, llm, capabilities):
    return {
        "name": name,
        "description": {"summary": f"{name} preset", "tier": "3"},
        "manifest": {"llm": llm, "capabilities": capabilities},
    }


def test_materialize_init_capability_overrides_win_per_key(tmp_path):
    """Per-agent init.json capability kwargs win key-by-key over preset kwargs.

    The preset still owns the capability *set* (atomic swap), but for a
    capability the preset enables that init.json also configures, init.json's
    kwargs override the preset's per key. A user who hand-edits init.json's
    vision config keeps that override across preset materialization rather than
    having it silently clobbered (the previous "Bug D" wholesale-replace).
    """
    preset_path = _write_preset(
        tmp_path, "GLM5.1",
        _preset_content(
            "GLM5.1",
            llm={"provider": "custom", "api_compat": "anthropic", "model": "GLM-5.1"},
            capabilities={"vision": {"provider": "inherit"}},
        ),
    )
    data = {
        "manifest": {
            "preset": {"active": str(preset_path)},
            # user hand-edited init.json to point vision at a different model
            "capabilities": {
                "vision": {
                    "provider": "custom",
                    "api_compat": "openai",
                    "model": "Kimi-K2.6",
                    "base_url": "http://127.0.0.1:34891/v1",
                },
            },
        },
    }
    materialize_active_preset(data, working_dir=tmp_path)

    # init.json's per-key overrides win — vision keeps the user's model/base_url
    # and overrides the preset's provider:"inherit" with provider:"custom".
    assert data["manifest"]["capabilities"] == {
        "vision": {
            "provider": "custom",
            "api_compat": "openai",
            "model": "Kimi-K2.6",
            "base_url": "http://127.0.0.1:34891/v1",
        },
    }


def test_materialize_preserves_init_skills_paths_carveout(tmp_path):
    """The documented skills.paths carve-out: init.json extras append to the
    preset's skill paths (preset defaults first), surviving the wholesale swap.
    """
    preset_path = _write_preset(
        tmp_path, "withskills",
        _preset_content(
            "withskills",
            llm={"provider": "x", "model": "y"},
            capabilities={"skills": {"paths": ["~/preset-skills"]}},
        ),
    )
    data = {
        "manifest": {
            "preset": {"active": str(preset_path)},
            "capabilities": {"skills": {"paths": ["~/agent-skills"]}},
        },
    }
    materialize_active_preset(data, working_dir=tmp_path)

    assert data["manifest"]["capabilities"]["skills"]["paths"] == [
        "~/preset-skills",
        "~/agent-skills",
    ]
