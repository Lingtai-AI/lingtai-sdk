"""Tests for LingTai agent process-command matching."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.process_match import match_agent_run


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "src" / "lingtai" / "intrinsic_skills" / "lingtai-doctor" / "scripts" / "doctor.py"

MATCH_CASES = [
    ("/v/bin/python -m lingtai run /a/foo", "/a/foo", "module"),
    ("python -m lingtai run /a/foo", "/a/foo", "module"),
    ("/usr/local/bin/lingtai-agent run /a/foo", "/a/foo", "console"),
    ("lingtai-agent run /a/foo", "/a/foo", "console"),
    ("/usr/local/bin/lingtai run /a/foo", "/a/foo", "legacy"),
    ("lingtai run /a/foo", "/a/foo", "legacy"),
    ("/v/bin/python -m lingtai run /a/my agent", "/a/my agent", "module"),
    ("/usr/local/bin/lingtai-agent run /a/my agent", "/a/my agent", "console"),
    ("/v/bin/python -m lingtai run /a/foobar", "/a/foo", None),
    ("/usr/local/bin/lingtai-agent run /a/foobar", "/a/foo", None),
    ("/v/bin/python -m lingtai run /a/foo/", "/a/foo", "module"),
    ("/usr/local/bin/lingtai-agent run /a/foo/", "/a/foo", "console"),
    ("grep lingtai run /a/foo", "/a/foo", None),
    ("grep lingtai-agent run /a/foo", "/a/foo", None),
    ("tail -f /var/log/x lingtai run /a/foo", "/a/foo", None),
    ("vim /a/foo/notes about lingtai run", "/a/foo", None),
    ("/v/bin/python -m lingtai poll /a/foo", "/a/foo", None),
]


def _load_doctor_module():
    spec = importlib.util.spec_from_file_location("_lingtai_doctor_process_match", DOCTOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _watcher_match_agent_run(tmp_path):
    from lingtai_kernel.base_agent import BaseAgent

    service = MagicMock()
    service.get_adapter.return_value = MagicMock()
    service.provider = "p"
    service.model = "m"

    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    agent = BaseAgent(service=service, agent_name="alice", working_dir=working_dir)
    agent._build_launch_cmd = lambda: ["python", "-c", "print('relaunch sentinel')"]

    with patch("subprocess.Popen") as mock_popen:
        agent._perform_refresh()
    assert mock_popen.called
    args, _kwargs = mock_popen.call_args
    script = args[0][2]
    function_source = script.split("def match_agent_run", 1)[1]
    function_source = "def match_agent_run" + function_source.split(
        "def _is_same_agent_run", 1
    )[0]
    ns: dict[str, object] = {}
    exec(compile("import os\n" + function_source, "<relaunch_matcher>", "exec"), ns)
    return ns["match_agent_run"]


@pytest.mark.parametrize(("cmdline", "working_dir", "expected"), MATCH_CASES)
def test_canonical_match_agent_run_matrix(cmdline, working_dir, expected):
    assert match_agent_run(cmdline, working_dir) == expected


def test_doctor_copy_matches_canonical_matrix():
    doctor = _load_doctor_module()
    for cmdline, working_dir, expected in MATCH_CASES:
        assert doctor.match_agent_run(cmdline, working_dir) == expected


def test_refresh_watcher_copy_matches_canonical_matrix(tmp_path):
    watcher_match = _watcher_match_agent_run(tmp_path)
    for cmdline, working_dir, expected in MATCH_CASES:
        assert watcher_match(cmdline, working_dir) == expected


def test_cli_duplicate_process_detects_console_script(tmp_path):
    from lingtai.cli import _check_duplicate_process

    working_dir = tmp_path / "agent"
    working_dir.mkdir()

    ps_out = f"4242 /usr/local/bin/lingtai-agent run {working_dir.resolve()}\n"
    with patch("subprocess.check_output", return_value=ps_out):
        with pytest.raises(SystemExit):
            _check_duplicate_process(working_dir)


def test_cli_duplicate_process_rejects_argument_position_false_positive(tmp_path):
    from lingtai.cli import _check_duplicate_process

    working_dir = tmp_path / "agent"
    working_dir.mkdir()

    ps_out = f"4242 tail -f /var/log/x lingtai run {working_dir.resolve()}\n"
    with patch("subprocess.check_output", return_value=ps_out):
        _check_duplicate_process(working_dir)


def test_doctor_collect_process_detects_console_script(tmp_path):
    doctor = _load_doctor_module()

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    report = doctor.Report(agent_dir, None)
    stdout = f"4242 /usr/local/bin/lingtai-agent run {agent_dir}\n"

    with patch.object(
        doctor.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    ):
        doctor.collect_process(report)

    process_section = report.sections[-1]
    assert process_section.findings[0].severity == "OK"
    assert process_section.findings[0].title == "lingtai process found"


def test_doctor_collect_process_rejects_prefix_sibling(tmp_path):
    doctor = _load_doctor_module()

    agent_dir = tmp_path / "agent"
    sibling_dir = tmp_path / "agent_extra"
    agent_dir.mkdir()
    sibling_dir.mkdir()
    report = doctor.Report(agent_dir, None)
    stdout = f"4242 /usr/local/bin/lingtai-agent run {sibling_dir}\n"

    with patch.object(
        doctor.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    ):
        doctor.collect_process(report)

    process_section = report.sections[-1]
    assert process_section.findings[0].severity == "WARN"
    assert process_section.findings[0].title == "no lingtai process found"
