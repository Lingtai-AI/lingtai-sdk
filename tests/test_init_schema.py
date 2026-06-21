import json
import pytest
from lingtai.init_schema import validate_init


def _valid_init() -> dict:
    """Return a minimal valid init.json dict."""
    return {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "api_key": None,
                "base_url": None,
            },
            "capabilities": {},
            "soul": {"delay": 120},
            "stamina": 3600,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "prompt": "",
        "soul": "",
    }


def test_valid_init_passes():
    validate_init(_valid_init())  # should not raise


def test_missing_top_level_key():
    data = _valid_init()
    del data["covenant"]
    with pytest.raises(ValueError, match="covenant"):
        validate_init(data)


def test_missing_manifest_field():
    """Only manifest.llm is truly required — other fields are optional."""
    data = _valid_init()
    del data["manifest"]["llm"]
    with pytest.raises(ValueError, match="manifest.llm"):
        validate_init(data)


def test_minimal_init_passes():
    """Bare-minimum init.json: only manifest.llm with provider+model."""
    data = {
        "manifest": {
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
            },
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "prompt": "",
        "soul": "",
    }
    validate_init(data)  # should not raise


def test_missing_llm_field():
    data = _valid_init()
    del data["manifest"]["llm"]["provider"]
    with pytest.raises(ValueError, match="manifest.llm.provider"):
        validate_init(data)


def test_wrong_type_top_level():
    data = _valid_init()
    data["covenant"] = 123
    with pytest.raises(ValueError, match="covenant.*str"):
        validate_init(data)


def test_wrong_type_manifest_field():
    data = _valid_init()
    data["manifest"]["stamina"] = "one hour"
    with pytest.raises(ValueError, match="manifest.stamina.*(int|float|number)"):
        validate_init(data)


def test_summarize_notification_threshold_rejects_negative():
    data = _valid_init()
    data["manifest"]["summarize_notification_threshold"] = -1
    with pytest.raises(ValueError, match="summarize_notification_threshold"):
        validate_init(data)


def test_summarize_notification_threshold_rejects_bool():
    data = _valid_init()
    data["manifest"]["summarize_notification_threshold"] = True
    with pytest.raises(ValueError, match="summarize_notification_threshold"):
        validate_init(data)


def test_summarize_notification_threshold_allows_zero():
    data = _valid_init()
    data["manifest"]["summarize_notification_threshold"] = 0
    validate_init(data)  # 0 intentionally disables large-result notifications.


def test_wrong_type_capabilities():
    data = _valid_init()
    data["manifest"]["capabilities"] = ["file", "bash"]
    with pytest.raises(ValueError, match="manifest.capabilities.*object"):
        validate_init(data)


def test_wrong_type_streaming():
    data = _valid_init()
    data["manifest"]["streaming"] = "yes"
    with pytest.raises(ValueError, match="manifest.streaming.*bool"):
        validate_init(data)


def test_bool_rejected_for_numeric_field():
    """bool is a subclass of int in Python — must be rejected for numeric fields."""
    data = _valid_init()
    data["manifest"]["stamina"] = True
    with pytest.raises(ValueError, match="manifest.stamina.*number.*bool"):
        validate_init(data)


# --- optional fields ---


def test_env_file_optional():
    data = _valid_init()
    validate_init(data)  # no env_file — should pass
    data["env_file"] = "~/.lingtai/.env"
    validate_init(data)  # with env_file — should pass


def test_env_file_wrong_type():
    data = _valid_init()
    data["env_file"] = 123
    with pytest.raises(ValueError, match="env_file.*str"):
        validate_init(data)


def test_api_key_env_optional():
    data = _valid_init()
    data["manifest"]["llm"]["api_key_env"] = "MY_KEY"
    data["env_file"] = ".env"  # required when api_key_env is used without api_key
    validate_init(data)


def test_api_key_env_wrong_type():
    data = _valid_init()
    data["manifest"]["llm"]["api_key_env"] = 123
    with pytest.raises(ValueError, match="api_key_env.*str"):
        validate_init(data)


@pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh"])
def test_llm_thinking_valid_values(value):
    data = _valid_init()
    data["manifest"]["llm"]["provider"] = "codex"
    data["manifest"]["llm"]["thinking"] = value
    validate_init(data)


@pytest.mark.parametrize("value", ["default", "ultra", 1, None])
def test_llm_thinking_invalid_values(value):
    data = _valid_init()
    data["manifest"]["llm"]["provider"] = "codex"
    data["manifest"]["llm"]["thinking"] = value
    with pytest.raises(ValueError, match="manifest.llm.thinking"):
        validate_init(data)


@pytest.mark.parametrize("value", ["high", "default", None])
def test_llm_thinking_rejected_for_non_codex_provider(value):
    data = _valid_init()
    data["manifest"]["llm"]["provider"] = "anthropic"
    data["manifest"]["llm"]["thinking"] = value
    with pytest.raises(ValueError, match=r"manifest\.llm\.thinking.*Codex"):
        validate_init(data)


# --- addons (list of curated MCP names; mcp capability handles the rest) ---


def test_addons_optional():
    data = _valid_init()
    validate_init(data)  # no addons — should pass


def test_addons_list_of_names_valid():
    data = _valid_init()
    data["addons"] = ["imap", "telegram", "feishu"]
    validate_init(data)


def test_addons_empty_list_valid():
    data = _valid_init()
    data["addons"] = []
    validate_init(data)


def test_addons_dict_shape_rejected():
    """Legacy dict shape was removed in v0.7.3; the migration converts."""
    data = _valid_init()
    data["addons"] = {"imap": {"config": "imap.json"}}
    with pytest.raises(ValueError, match="addons.*list"):
        validate_init(data)


def test_addons_non_string_entries_warn():
    data = _valid_init()
    data["addons"] = ["imap", 42]
    warnings = validate_init(data)
    assert any("strings" in w for w in warnings)


def test_mcp_section_optional():
    data = _valid_init()
    validate_init(data)  # no mcp — should pass


def test_mcp_section_dict_valid():
    data = _valid_init()
    data["mcp"] = {
        "imap": {
            "type": "stdio",
            "command": "/usr/bin/python",
            "args": ["-m", "lingtai.mcp_servers.imap"],
        },
    }
    validate_init(data)


def test_mcp_section_wrong_type_rejected():
    data = _valid_init()
    data["mcp"] = ["imap"]
    with pytest.raises(ValueError, match="mcp.*object"):
        validate_init(data)


def test_time_awareness_field_valid_bool():
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "time_awareness": False,
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    warnings = validate_init(data)
    assert all("time_awareness" not in w for w in warnings)


def test_time_awareness_field_wrong_type_raises():
    import pytest
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "time_awareness": "yes",
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    with pytest.raises(ValueError):
        validate_init(data)


def test_timezone_awareness_field_valid_bool():
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "timezone_awareness": False,
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    warnings = validate_init(data)
    assert all("timezone_awareness" not in w for w in warnings)


def test_timezone_awareness_field_wrong_type_raises():
    import pytest
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "timezone_awareness": "yes",
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    with pytest.raises(ValueError):
        validate_init(data)


# --- schema self-consistency (drift prevention) ---
#
# The schema maintains two parallel structures per scope: an OPTIONAL dict
# (field name -> expected type, used for type validation) and a KNOWN set
# (used to suppress "unknown field" warnings). When a new field is added,
# both must be updated. These tests catch the common drift where one is
# updated and the other is forgotten.


def test_manifest_optional_fields_all_in_known():
    """Every optional manifest field must also be in MANIFEST_KNOWN,
    otherwise a valid use of that field would produce a spurious
    'unknown field' warning."""
    from lingtai.init_schema import MANIFEST_OPTIONAL, MANIFEST_KNOWN
    missing = set(MANIFEST_OPTIONAL) - MANIFEST_KNOWN
    assert not missing, (
        f"Fields in MANIFEST_OPTIONAL but not in MANIFEST_KNOWN "
        f"(would trigger unknown-field warning): {sorted(missing)}"
    )


def test_manifest_required_fields_all_in_known():
    """Every required manifest field must also be in MANIFEST_KNOWN."""
    from lingtai.init_schema import MANIFEST_REQUIRED, MANIFEST_KNOWN
    missing = set(MANIFEST_REQUIRED) - MANIFEST_KNOWN
    assert not missing, (
        f"Fields in MANIFEST_REQUIRED but not in MANIFEST_KNOWN: {sorted(missing)}"
    )


def test_manifest_known_fields_all_typed():
    """Every field in MANIFEST_KNOWN must appear in either MANIFEST_REQUIRED
    or MANIFEST_OPTIONAL, otherwise a user-supplied value passes without
    any type check."""
    from lingtai.init_schema import MANIFEST_OPTIONAL, MANIFEST_REQUIRED, MANIFEST_KNOWN
    typed = set(MANIFEST_OPTIONAL) | set(MANIFEST_REQUIRED)
    untyped = MANIFEST_KNOWN - typed
    assert not untyped, (
        f"Fields in MANIFEST_KNOWN but not type-checked (missing from "
        f"MANIFEST_OPTIONAL or MANIFEST_REQUIRED): {sorted(untyped)}"
    )


def test_top_optional_fields_all_in_known():
    """Every optional top-level field must also be in TOP_KNOWN."""
    from lingtai.init_schema import TOP_OPTIONAL, TOP_KNOWN
    missing = set(TOP_OPTIONAL) - TOP_KNOWN
    assert not missing, (
        f"Fields in TOP_OPTIONAL but not in TOP_KNOWN "
        f"(would trigger unknown-field warning): {sorted(missing)}"
    )


def test_manifest_accepts_pseudo_agent_subscriptions():
    data = _valid_init()
    data["manifest"]["pseudo_agent_subscriptions"] = ["../human", "../announcements"]
    warnings = validate_init(data)
    # No warnings related to this field.
    for w in warnings:
        assert "pseudo_agent_subscriptions" not in w, f"unexpected warning: {w}"


def test_manifest_rejects_non_list_pseudo_agent_subscriptions():
    import pytest
    data = _valid_init()
    data["manifest"]["pseudo_agent_subscriptions"] = "../human"  # string, not list
    with pytest.raises(ValueError, match="pseudo_agent_subscriptions"):
        validate_init(data)


def test_preset_block_minimum():
    """manifest.preset with active + default + allowed is valid."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": ["minimax"],
    }
    validate_init(data)  # should not raise


def test_preset_block_allowed_with_multiple_entries():
    """manifest.preset.allowed with multiple paths is valid as long as
    default and active both appear in the list."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": ["minimax", "zhipu", "deepseek"],
    }
    validate_init(data)  # should not raise


def test_preset_block_missing_active_raises():
    """`manifest.preset` without `active` raises."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "default": "minimax",
        "allowed": ["minimax"],
    }
    with pytest.raises(ValueError, match="manifest.preset.active"):
        validate_init(data)


def test_preset_block_missing_default_raises():
    """`manifest.preset` without `default` raises."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "allowed": ["minimax"],
    }
    with pytest.raises(ValueError, match="manifest.preset.default"):
        validate_init(data)


def test_preset_block_active_wrong_type_raises():
    """`manifest.preset.active` must be a string."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": 42,
        "default": "minimax",
        "allowed": ["minimax"],
    }
    with pytest.raises(ValueError, match="manifest.preset.active"):
        validate_init(data)


def test_preset_block_default_wrong_type_raises():
    """`manifest.preset.default` must be a string."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": 42,
        "allowed": ["minimax"],
    }
    with pytest.raises(ValueError, match="manifest.preset.default"):
        validate_init(data)


def test_preset_block_missing_allowed_raises():
    """`manifest.preset` without `allowed` raises — there is no implicit
    library scan."""
    data = _valid_init()
    data["manifest"]["preset"] = {"active": "minimax", "default": "minimax"}
    with pytest.raises(ValueError, match="manifest.preset.allowed"):
        validate_init(data)


def test_preset_block_allowed_must_be_list():
    """`manifest.preset.allowed` must be a list."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": "minimax",  # string, not list
    }
    with pytest.raises(ValueError, match="manifest.preset.allowed"):
        validate_init(data)


def test_preset_block_allowed_empty_raises():
    """`manifest.preset.allowed` must contain at least one entry."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": [],
    }
    with pytest.raises(ValueError, match="manifest.preset.allowed"):
        validate_init(data)


def test_preset_block_allowed_rejects_non_string_element():
    """Inside `allowed`, every entry must be a non-empty string."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": ["minimax", 42],
    }
    with pytest.raises(ValueError, match=r"manifest.preset.allowed\[1\]"):
        validate_init(data)


def test_preset_block_default_must_be_in_allowed():
    """`default` must appear in `allowed`."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "zhipu",
        "default": "minimax",
        "allowed": ["zhipu"],
    }
    with pytest.raises(ValueError, match="manifest.preset.default"):
        validate_init(data)


def test_preset_block_active_must_be_in_allowed():
    """`active` must appear in `allowed`."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "zhipu",
        "allowed": ["zhipu"],
    }
    with pytest.raises(ValueError, match="manifest.preset.active"):
        validate_init(data)


def test_preset_block_unknown_field_warns():
    """Unknown fields inside manifest.preset produce a warning, not an error."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": ["minimax"],
        "extra_key": "foo",
    }
    warnings = validate_init(data)
    assert any("unknown field in manifest.preset" in w for w in warnings)


def test_preset_block_old_path_field_warns_as_unknown():
    """The retired `path` field is now an unknown key, so it warns rather
    than being silently accepted."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "allowed": ["minimax"],
        "path": "/some/legacy/lib",
    }
    warnings = validate_init(data)
    assert any("unknown field in manifest.preset: path" in w for w in warnings)


def test_preset_block_unmigrated_init_points_at_m029():
    """When an init.json predates m029 (has `path` but no `allowed`),
    the schema error must point the operator at the migration so they
    don't have to guess what to do."""
    data = _valid_init()
    data["manifest"]["preset"] = {
        "active": "minimax",
        "default": "minimax",
        "path": "~/.lingtai-tui/presets",
    }
    with pytest.raises(ValueError, match="m029") as exc_info:
        validate_init(data)
    assert "lingtai-tui" in str(exc_info.value)
