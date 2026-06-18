"""``lingtai_cli`` — the thin product-assembly / host package.

Covers:
- import purity: a bare ``import lingtai_cli`` must stay light — it must NOT
  eagerly pull the ``lingtai`` wrapper or any heavy provider SDK.
- assembly: a minimal tmp ``init.json`` resolves to a ``RuntimeOptions`` plus a
  capability/addon/prompt/MCP summary, WITHOUT constructing an Agent.
- legacy shim: ``lingtai.cli`` re-exports/delegates to ``lingtai_cli.host`` so
  existing imports and ``lingtai-agent`` usage keep working.
- console script: pyproject declares ``lingtai-cli``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Heavy provider SDKs that must NOT be loaded by a bare ``import lingtai_cli``.
# Mirrors tests/test_sdk_import_purity.py — bare ``google`` is intentionally
# excluded (ambient namespace stub); only real provider submodules count.
_HEAVY_PROVIDERS = (
    "anthropic",
    "openai",
    "google.genai",
    "google.generativeai",
    "mcp",
    "trafilatura",
    "ddgs",
)


def _run(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _write_init(tmp_path: Path, overrides: dict | None = None) -> Path:
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
            "capabilities": {"read": {}, "bash": {"yolo": True}},
            "streaming": False,
            "context_limit": 123456,
        },
        "addons": ["imap"],
        "mcp": {"some-server": {"command": "echo"}},
        "principle": "",
        "covenant": "Be helpful.",
        "pad": "I remember nothing.",
        "prompt": "Do the thing.",
    }
    if overrides:
        for k, v in overrides.items():
            if k == "manifest" and isinstance(v, dict):
                data["manifest"].update(v)
            else:
                data[k] = v
    init_path = tmp_path / "init.json"
    init_path.write_text(json.dumps(data))
    return tmp_path


# --- import purity ---------------------------------------------------------


def test_import_lingtai_cli_does_not_load_wrapper_or_providers():
    code = (
        "import sys, lingtai_cli\n"
        f"providers = {_HEAVY_PROVIDERS!r}\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "bad += [m for m in sys.modules "
        "if any(m == p or m.startswith(p + '.') for p in providers)]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- assembly: project state -> RuntimeOptions + summary -------------------


def test_project_state_load_to_runtime_options(tmp_path):
    from lingtai_cli.assembly import ProjectState
    from lingtai_sdk.runtime import RuntimeOptions

    workdir = _write_init(tmp_path)
    project = ProjectState.load(workdir)

    options = project.to_runtime_options(backend="native")
    assert isinstance(options, RuntimeOptions)
    assert Path(options.working_dir) == workdir
    assert options.agent_name == "test-agent"
    assert options.provider == "anthropic"
    assert options.model == "test-model"
    assert options.api_key == "test-key"
    assert options.base_url is None
    assert options.streaming is False
    # capabilities/addons flow through
    assert options.capabilities == {"read": {}, "bash": {"yolo": True}}
    assert options.addons == ["imap"]


def test_project_state_assemble_returns_runtime_and_plan(tmp_path):
    from lingtai_cli.assembly import CLIAssembly, ProjectState

    workdir = _write_init(tmp_path, {"tools": {"custom": {"type": "stdio"}}})
    assembly = ProjectState.load(workdir).assemble(backend="native")

    assert isinstance(assembly, CLIAssembly)
    assert assembly.backend == "native"
    assert assembly.runtime_options.agent_name == "test-agent"
    assert assembly.capability_bundles == {"read": {}, "bash": {"yolo": True}}
    assert assembly.addons == ["imap"]
    assert "covenant" in assembly.prompt_assets
    assert assembly.mcp == {"some-server": {"command": "echo"}}
    assert assembly.custom_tools == {"custom": {"type": "stdio"}}


def test_project_state_summary_fields(tmp_path):
    from lingtai_cli.assembly import ProjectState

    workdir = _write_init(tmp_path)
    project = ProjectState.load(workdir)

    assert sorted(project.capabilities) == ["bash", "read"]
    assert project.addons == ["imap"]
    assert "some-server" in project.mcp
    # prompt sections surfaced from init.json
    assert project.prompt_sections["covenant"] == "Be helpful."
    assert project.prompt_sections["prompt"] == "Do the thing."


def test_project_state_does_not_construct_agent(tmp_path):
    # Loading + assembly must not import the wrapper Agent stack. We check in a
    # subprocess that to_runtime_options() leaves the wrapper unimported.
    workdir = _write_init(tmp_path)
    code = (
        "import sys\n"
        f"from lingtai_cli.assembly import ProjectState\n"
        f"p = ProjectState.load({str(workdir)!r})\n"
        "p.to_runtime_options(backend='native')\n"
        "bad = [m for m in sys.modules if m == 'lingtai.agent']\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_to_runtime_options_rejects_unknown_backend(tmp_path):
    import pytest

    from lingtai_cli.assembly import ProjectState

    workdir = _write_init(tmp_path)
    project = ProjectState.load(workdir)
    with pytest.raises(ValueError):
        project.to_runtime_options(backend="anthropic")


# --- legacy shim: lingtai.cli -> lingtai_cli.host -------------------------


def test_legacy_cli_delegates_to_host():
    import lingtai.cli as legacy
    import lingtai_cli.host as host

    for name in ("load_init", "build_agent", "run", "main"):
        assert getattr(legacy, name) is getattr(host, name), (
            f"lingtai.cli.{name} forked from lingtai_cli.host.{name}"
        )


def test_legacy_cli_main_still_runs_log_command(tmp_path, capsys):
    import sys

    from lingtai.cli import main

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(
        json.dumps({"type": "cli_event", "ts": 1}) + "\n", encoding="utf-8"
    )

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "log", "rebuild", str(tmp_path)]
        main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
    finally:
        sys.argv = old_argv


# --- console script declared in pyproject ---------------------------------


def test_pyproject_declares_lingtai_cli_script():
    import tomllib

    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["lingtai-cli"] == "lingtai_cli.cli:main"
    assert "lingtai-agent" in scripts
    # distribution name unchanged in this PR
    assert data["project"]["name"] == "lingtai"
