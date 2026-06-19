"""Tests for the kernel-published resolved-manifest artifact (issue #259).

``Agent._read_init`` materializes the active preset in memory; raw init.json
is only a creation-time snapshot. After every successful materialization +
validation + path resolution, the kernel publishes the fully-resolved manifest
to ``<agent>/system/manifest.resolved.json`` so consumers (TUI/portal) read
the effective config instead of re-implementing the merge.
"""
import json
from pathlib import Path


def _make_workdir(tmp_path: Path, active_preset: str | None = None,
                  manifest_extra: dict | None = None,
                  llm: dict | None = None) -> Path:
    """Create a working dir with init.json. Optionally points at a preset."""
    wd = tmp_path / "agent"
    wd.mkdir()
    manifest = {
        "agent_name": "alice",
        "language": "en",
        "llm": llm or {"provider": "deepseek", "model": "deepseek-v4-flash",
                       "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
        "capabilities": {"file": {}},
        "soul": {"delay": 120},
        "stamina": 3600,
        "molt_pressure": 0.8,
        "molt_prompt": "",
        "max_turns": 50,
        "admin": {"karma": True},
        "streaming": False,
    }
    if active_preset is not None:
        manifest["preset"] = {
            "active": active_preset,
            "default": active_preset,
            "allowed": [active_preset],
        }
    if manifest_extra:
        manifest.update(manifest_extra)
    env_file = wd / ".env"
    env_file.write_text("")
    init = {
        "manifest": manifest,
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "env_file": str(env_file),
    }
    (wd / "init.json").write_text(json.dumps(init))
    return wd


def _make_preset_lib(tmp_path: Path, presets: dict[str, dict]) -> Path:
    """Create a presets dir with the given name → preset-content mapping."""
    pdir = tmp_path / "presets"
    pdir.mkdir()
    for name, content in presets.items():
        (pdir / f"{name}.json").write_text(json.dumps(content))
    return pdir


def _make_probe_agent(wd: Path):
    """Minimal Agent shim exposing _read_init without full construction."""
    from lingtai.agent import Agent

    class _Probe(Agent):
        def __init__(self, working_dir):
            self._working_dir = Path(working_dir)
            self._log_events = []
        def _log(self, event, **kw):
            self._log_events.append((event, kw))
    return _Probe(wd)


def _read_artifact(wd: Path) -> dict:
    return json.loads(
        (wd / "system" / "manifest.resolved.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Redaction helper (kernel-owned)
# ---------------------------------------------------------------------------

def test_redact_secrets_drops_secret_keys_keeps_public():
    from lingtai_kernel.workdir import _redact_secrets

    value = {
        "llm": {
            "provider": "deepseek", "model": "v4", "base_url": "https://x",
            "api_compat": "openai", "context_limit": 128000,
            "api_key": "sk-live-SECRET", "api_key_env": "DEEPSEEK_API_KEY",
        },
        "capabilities": {
            "web_search": {"provider": "gemini", "api_key": "sk-2"},
            "telegram": {"botToken": "bot-secret", "chat_id": 123},
            "feishu": {"appSecret": "app-secret", "app_id": "cli_x"},
            "imap": {"accounts": [{"host": "h", "password": "hunter2",
                                   "auth_token": "tok-abc"}]},
            "daemon": {"max_emanations": 30, "max_tokens": 4096},
        },
        "secretary": {"enabled": True},
    }
    out = _redact_secrets(value)
    llm = out["llm"]
    assert llm["provider"] == "deepseek"
    assert llm["model"] == "v4"
    assert llm["base_url"] == "https://x"
    assert llm["api_compat"] == "openai"
    assert llm["context_limit"] == 128000
    assert "api_key" not in llm
    assert "api_key_env" not in llm  # consistent with .agent.json hygiene
    caps = out["capabilities"]
    assert "api_key" not in caps["web_search"]
    assert caps["telegram"] == {"chat_id": 123}
    assert caps["feishu"] == {"app_id": "cli_x"}
    account = caps["imap"]["accounts"][0]
    assert account == {"host": "h"}  # password + auth_token dropped
    # token-LIKE keys go, but plural "tokens" (e.g. max_tokens) is not a secret
    assert caps["daemon"] == {"max_emanations": 30, "max_tokens": 4096}
    # non-secret words that merely contain "secret" must survive
    assert out["secretary"] == {"enabled": True}
    # input untouched (pure function)
    assert value["llm"]["api_key"] == "sk-live-SECRET"


# ---------------------------------------------------------------------------
# Artifact written by _read_init after materialization
# ---------------------------------------------------------------------------

def test_artifact_publishes_materialized_skills_paths(tmp_path, monkeypatch):
    """The active preset's skills.paths show up in manifest.resolved.json even
    though raw init.json never mentions skills — the exact stale-snapshot
    failure from issue #259."""
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset with skills"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {"file": {},
                                 "skills": {"paths": ["~/skills/curated"]}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    wd = _make_workdir(tmp_path, active_preset=str(plib / "smart.json"))
    raw_before = json.loads((wd / "init.json").read_text())
    assert "skills" not in raw_before["manifest"]["capabilities"]

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None

    artifact = _read_artifact(wd)
    assert artifact["schema"] == "lingtai.manifest.resolved/v1"
    assert artifact["schema_version"] == 1
    assert artifact["source"] == "kernel"
    assert artifact["generated_at"].endswith("Z")
    assert artifact["preset"]["active"] == str(plib / "smart.json")
    caps = artifact["manifest"]["capabilities"]
    assert caps["skills"]["paths"] == ["~/skills/curated"]
    assert artifact["manifest"]["llm"]["provider"] == "gemini"

    # init.json stays user-owned input — the resolved manifest is NOT
    # written back (skills still absent in the raw file).
    raw_after = json.loads((wd / "init.json").read_text())
    assert "skills" not in raw_after["manifest"]["capabilities"]
    assert raw_after["manifest"]["llm"]["provider"] == "deepseek"


def test_artifact_merges_init_extras_per_materialize_semantics(tmp_path, monkeypatch):
    """init.json skills.paths extras append after the preset's curated paths
    (deduped), exactly as materialize_active_preset defines — and the merged
    result is what the artifact publishes."""
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {"skills": {"paths": ["~/skills/curated"]},
                                 "daemon": {"max_emanations": 10}},
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    wd = _make_workdir(
        tmp_path, active_preset=str(plib / "smart.json"),
        manifest_extra={"capabilities": {
            "skills": {"paths": ["~/skills/mine", "~/skills/curated"]},
            "daemon": {"max_emanations": 30},
        }},
    )
    a = _make_probe_agent(wd)
    assert a._read_init() is not None

    caps = _read_artifact(wd)["manifest"]["capabilities"]
    # preset paths first, init extras appended, duplicates dropped
    assert caps["skills"]["paths"] == ["~/skills/curated", "~/skills/mine"]
    # per-key override: init.json wins for daemon.max_emanations
    assert caps["daemon"]["max_emanations"] == 30


def test_artifact_redacts_api_key_like_secrets(tmp_path, monkeypatch):
    """Literal secrets in init.json (and those copied into capability kwargs
    by provider:inherit expansion) never reach the artifact."""
    secret = "sk-live-SUPERSECRET-123"
    wd = _make_workdir(
        tmp_path,
        llm={"provider": "deepseek", "model": "deepseek-v4-flash",
             "api_key": secret},
        manifest_extra={"capabilities": {
            "file": {},
            "web_search": {"provider": "inherit"},
        }},
    )
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    # inherit expansion really copied the secret into capability kwargs
    assert data["manifest"]["capabilities"]["web_search"]["api_key"] == secret

    artifact_text = (wd / "system" / "manifest.resolved.json").read_text()
    assert secret not in artifact_text
    artifact = _read_artifact(wd)
    llm = artifact["manifest"]["llm"]
    assert "api_key" not in llm
    assert llm["provider"] == "deepseek"
    assert llm["model"] == "deepseek-v4-flash"
    assert "api_key" not in artifact["manifest"]["capabilities"]["web_search"]
    # no half-written temp file left behind by the atomic write
    assert not (wd / "system" / "manifest.resolved.json.tmp").exists()


def test_refresh_rewrites_artifact_after_preset_change(tmp_path, monkeypatch):
    """End-to-end: boot via _setup_from_init publishes preset A; switching
    manifest.preset.active to B and refreshing republishes with B's llm."""
    from unittest.mock import MagicMock
    from lingtai.agent import Agent
    from lingtai_kernel.config import AgentConfig

    plib = _make_preset_lib(tmp_path, {
        "fast": {
            "name": "fast",
            "description": {"summary": "fast preset"},
            "manifest": {
                "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                        "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
                "capabilities": {"file": {}},
            },
        },
        "smart": {
            "name": "smart",
            "description": {"summary": "smart preset"},
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {"file": {}, "skills": {"paths": ["~/s"]}},
            },
        },
    })
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    fast, smart = str(plib / "fast.json"), str(plib / "smart.json")

    wd = _make_workdir(tmp_path, active_preset=fast)
    init = json.loads((wd / "init.json").read_text())
    init["manifest"]["preset"]["allowed"] = [fast, smart]
    (wd / "init.json").write_text(json.dumps(init))

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(svc, working_dir=wd, config=AgentConfig())
    agent._setup_from_init()

    artifact = _read_artifact(wd)
    assert artifact["manifest"]["llm"]["provider"] == "deepseek"
    assert artifact["preset"]["active"] == fast

    # Swap the active preset (what system(refresh) does before re-setup).
    agent._activate_preset(smart)
    agent._setup_from_init()

    artifact = _read_artifact(wd)
    assert artifact["manifest"]["llm"]["provider"] == "gemini"
    assert artifact["manifest"]["llm"]["model"] == "gemini-2.5-pro"
    assert artifact["preset"]["active"] == smart
    assert artifact["manifest"]["capabilities"]["skills"]["paths"] == ["~/s"]
