import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch


def _write_init(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a valid init.json to tmp_path and return the path."""
    data = {
        "manifest": {
            "agent_name": "test-agent",
            "language": "en",
            "llm": {
                "provider": "anthropic",
                "model": "test-model",
                "api_key": "test-key",
                "base_url": None,
            },
            "capabilities": {},
            "soul": {"delay": 30},
            "stamina": 60,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 10,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "",
        "covenant": "Be helpful.",
        "pad": "I remember nothing.",
        "prompt": "",
    }
    if overrides:
        # Deep merge manifest if provided
        for k, v in overrides.items():
            if k == "manifest" and isinstance(v, dict):
                data["manifest"].update(v)
            else:
                data[k] = v
    init_path = tmp_path / "init.json"
    init_path.write_text(json.dumps(data))
    return tmp_path


def test_load_init_reads_file(tmp_path):
    from lingtai.cli import load_init
    _write_init(tmp_path)
    data = load_init(tmp_path)
    assert data["manifest"]["agent_name"] == "test-agent"


def test_load_init_missing_file(tmp_path):
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


def test_load_init_invalid_json(tmp_path):
    (tmp_path / "init.json").write_text("{bad json")
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


def test_load_init_validation_error(tmp_path):
    (tmp_path / "init.json").write_text(json.dumps({"manifest": {}}))
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_constructs_correctly(mock_mail, mock_agent, mock_llm, tmp_path):
    from lingtai.cli import load_init, build_agent
    _write_init(tmp_path)
    data = load_init(tmp_path)
    build_agent(data, tmp_path)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"
    assert llm_kwargs["api_key"] == "test-key"
    assert llm_kwargs["base_url"] is None
    mock_mail.assert_called_once()
    assert mock_mail.call_args.kwargs["working_dir"] == tmp_path
    mock_agent.assert_called_once()
    call_kwargs = mock_agent.call_args
    assert call_kwargs.kwargs["agent_name"] == "test-agent"
    assert call_kwargs.kwargs["working_dir"] == tmp_path
    assert call_kwargs.kwargs["streaming"] is False
    # covenant, memory, capabilities, addons no longer passed to constructor —
    # they are loaded by _setup_from_init() from init.json
    assert "covenant" not in call_kwargs.kwargs
    assert "pad" not in call_kwargs.kwargs
    assert "capabilities" not in call_kwargs.kwargs
    assert "addons" not in call_kwargs.kwargs
    # _setup_from_init() is called on the constructed agent
    mock_agent.return_value._setup_from_init.assert_called_once()


# --- env file and env var resolution ---


def test_load_env_file(tmp_path):
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text("TEST_CLI_KEY=secret123\nTEST_CLI_OTHER='quoted'\n")

    # Clean up after test
    for k in ("TEST_CLI_KEY", "TEST_CLI_OTHER"):
        os.environ.pop(k, None)

    load_env_file(env_path)
    assert os.environ["TEST_CLI_KEY"] == "secret123"
    assert os.environ["TEST_CLI_OTHER"] == "quoted"

    # Does not overwrite existing by default
    os.environ["TEST_CLI_KEY"] = "original"
    load_env_file(env_path)
    assert os.environ["TEST_CLI_KEY"] == "original"

    # Explicit refresh reloads may choose to overwrite stale process env.
    load_env_file(env_path, overwrite=True)
    assert os.environ["TEST_CLI_KEY"] == "secret123"

    # Clean up
    os.environ.pop("TEST_CLI_KEY", None)
    os.environ.pop("TEST_CLI_OTHER", None)


def test_load_env_file_missing():
    from lingtai.cli import load_env_file
    # Should not raise on missing file
    load_env_file("/nonexistent/.env")


def test_resolve_env_prefers_env_var():
    from lingtai.cli import resolve_env
    os.environ["TEST_RESOLVE_KEY"] = "from-env"
    try:
        assert resolve_env("raw-value", "TEST_RESOLVE_KEY") == "from-env"
    finally:
        os.environ.pop("TEST_RESOLVE_KEY", None)


def test_resolve_env_falls_back_to_raw():
    from lingtai.cli import resolve_env
    os.environ.pop("NONEXISTENT_KEY_12345", None)
    assert resolve_env("raw-value", "NONEXISTENT_KEY_12345") == "raw-value"


def test_resolve_env_no_env_name():
    from lingtai.cli import resolve_env
    assert resolve_env("raw-value", None) == "raw-value"
    assert resolve_env(None, None) is None


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_resolves_api_key_env(mock_mail, mock_agent, mock_llm, tmp_path):
    """api_key_env resolves from environment, overriding raw api_key."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_LLM_KEY"
    data["manifest"]["llm"]["api_key"] = "fallback-key"

    os.environ["TEST_LLM_KEY"] = "env-key-value"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_LLM_KEY", None)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["api_key"] == "env-key-value"
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_env_file_loaded(mock_mail, mock_agent, mock_llm, tmp_path):
    """env_file is loaded before resolving env vars."""
    from lingtai.cli import load_init, build_agent

    env_path = tmp_path / "secrets.env"
    env_path.write_text("TEST_ENV_FILE_KEY=from-file\n")

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["env_file"] = str(env_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_ENV_FILE_KEY"

    os.environ.pop("TEST_ENV_FILE_KEY", None)
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_ENV_FILE_KEY", None)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["api_key"] == "from-file"
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_env_file_overwrites_on_refresh_marker(mock_mail, mock_agent, mock_llm, tmp_path):
    """Refresh relaunches should let an edited env_file replace stale
    inherited process environment values before resolving api_key_env.
    """
    from lingtai.cli import load_init, build_agent

    env_path = tmp_path / "secrets.env"
    env_path.write_text("TEST_ENV_FILE_REFRESH_KEY=fresh-from-file\n")

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["env_file"] = str(env_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_ENV_FILE_REFRESH_KEY"

    os.environ["TEST_ENV_FILE_REFRESH_KEY"] = "stale-process-value"
    os.environ["LINGTAI_REFRESH_ENV_OVERWRITE"] = "1"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_ENV_FILE_REFRESH_KEY", None)
        os.environ.pop("LINGTAI_REFRESH_ENV_OVERWRITE", None)

    mock_llm.assert_called_once()
    assert mock_llm.call_args.kwargs["api_key"] == "fresh-from-file"
    assert "LINGTAI_REFRESH_ENV_OVERWRITE" not in os.environ


# --- addons ---


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_passes_addons(mock_mail, mock_agent, mock_llm, tmp_path):
    """Addons from init.json are handled by _setup_from_init, not constructor."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password": "secret",
            "imap_host": "imap.gmail.com",
            "smtp_host": "smtp.gmail.com",
        },
    }

    build_agent(data, tmp_path)

    # Addons no longer passed to constructor — handled by _setup_from_init
    call_kwargs = mock_agent.call_args.kwargs
    assert "addons" not in call_kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_resolves_addon_env(mock_mail, mock_agent, mock_llm, tmp_path):
    """Addon *_env fields are resolved by _setup_from_init via init.json."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password_env": "TEST_IMAP_PASS",
        },
        "telegram": {
            "bot_token_env": "TEST_TG_TOKEN",
        },
    }

    os.environ["TEST_IMAP_PASS"] = "imap-secret"
    os.environ["TEST_TG_TOKEN"] = "tg-secret"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_IMAP_PASS", None)
        os.environ.pop("TEST_TG_TOKEN", None)

    # Addons no longer passed to constructor — handled by _setup_from_init
    assert "addons" not in mock_agent.call_args.kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


@patch("lingtai_cli.host.LLMService")
@patch("lingtai_cli.host.Agent")
@patch("lingtai_cli.host.FilesystemMailService")
def test_build_agent_no_addons(mock_mail, mock_agent, mock_llm, tmp_path):
    """No addons field — _setup_from_init handles this gracefully."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    build_agent(data, tmp_path)

    assert "addons" not in mock_agent.call_args.kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


def test_log_rebuild_doctor_query_cli(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(json.dumps({"type": "cli_event", "ts": 1}) + "\n", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "log", "rebuild", str(tmp_path)]
        main()
        rebuild_out = json.loads(capsys.readouterr().out)
        assert rebuild_out["status"] == "ok"

        sys.argv = ["lingtai-agent", "log", "doctor", str(tmp_path)]
        main()
        doctor_out = json.loads(capsys.readouterr().out)
        assert doctor_out["event_count"] == 1
        assert doctor_out["chat_entry_count"] == 0

        sys.argv = ["lingtai-agent", "log", "query", str(tmp_path), "SELECT type FROM events"]
        main()
        query_out = json.loads(capsys.readouterr().out)
        assert query_out == [{"type": "cli_event"}]
    finally:
        sys.argv = old_argv


def test_log_query_missing_sqlite_requires_rebuild_cli(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(json.dumps({"type": "cli_event", "ts": 1}) + "\n", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "log", "query", str(tmp_path), "SELECT type FROM events"]
        try:
            main()
            assert False, "query should exit when sqlite sidecar is missing"
        except SystemExit as exc:
            assert exc.code == 1
        captured = capsys.readouterr()
        assert "rebuild" in captured.err
        assert not (logs / "log.sqlite").exists()
    finally:
        sys.argv = old_argv


def test_load_init_runs_agent_migrations_before_validation(tmp_path):
    """CLI boot must normalize legacy procedures fields before validating init.json."""
    import hashlib

    from lingtai.cli import load_init

    legacy = "legacy CLI procedures"
    init = {
        "manifest": {
            "agent_name": "cli-agent",
            "llm": {"provider": "p", "model": "m"},
            "capabilities": {},
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "prompt": "",
        "procedures": legacy,
        "procedures_file": "old/procedures.md",
    }
    (tmp_path / "init.json").write_text(json.dumps(init), encoding="utf-8")

    data = load_init(tmp_path)

    assert "procedures" not in data
    assert "procedures_file" not in data
    on_disk = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert "procedures" not in on_disk
    assert "procedures_file" not in on_disk
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    archive = tmp_path / "system" / "migrations" / f"init-procedures-{digest}.md"
    assert archive.read_text(encoding="utf-8") == legacy
