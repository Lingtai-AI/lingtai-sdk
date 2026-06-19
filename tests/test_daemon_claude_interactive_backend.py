"""Tests for the interactive Claude daemon backend."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import textwrap
import threading
from unittest.mock import MagicMock, patch

import pytest

from lingtai.agent import Agent
from lingtai.core.daemon import get_schema
from lingtai.core.daemon.claude_interactive import ClaudeInteractiveError, run_claude_interactive
from lingtai.core.daemon.run_dir import DaemonRunDir
from lingtai_kernel.config import AgentConfig


def _make_agent(tmp_path: Path) -> Agent:
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["daemon"],
        config=AgentConfig(),
    )


def _make_run_dir(tmp_path: Path, *, backend: str = "claude") -> DaemonRunDir:
    parent = tmp_path / "daemon-agent"
    parent.mkdir(parents=True, exist_ok=True)
    return DaemonRunDir(
        parent_working_dir=parent,
        handle="em-1",
        task="interactive task",
        tools=[],
        model=backend,
        max_turns=30,
        timeout_s=30,
        parent_addr="daemon-agent",
        parent_pid=os.getpid(),
        system_prompt="[claude interactive backend]",
        backend=backend,
    )


def _write_fake_claude(bin_dir: Path, transcript_text: str = "fake interactive answer") -> Path:
    fake = bin_dir / "claude"
    fake.write_text(textwrap.dedent(f"""
        #!/usr/bin/env python3
        from __future__ import annotations
        import json
        from pathlib import Path
        import subprocess
        import sys
        import time

        args = sys.argv[1:]
        settings = None
        resume_session = None
        i = 0
        while i < len(args):
            if args[i] == "--settings":
                settings = json.loads(args[i + 1])
                i += 2
            elif args[i] == "--resume":
                resume_session = args[i + 1]
                i += 2
            else:
                i += 1
        if settings is None:
            raise SystemExit("missing --settings")

        def hook_command(event):
            for group in settings["hooks"][event]:
                for hook in group["hooks"]:
                    return hook["command"]
            raise SystemExit(f"missing hook {{event}}")

        session_id = resume_session or "claude-session-123"
        transcript = Path.cwd() / "fake-claude-transcript.jsonl"

        # Exercise the bridge's terminal probe responder.  The fake does not
        # need the responses; real Claude/Ink does.
        sys.stdout.buffer.write(b"\\x1b[c\\x1b[>c\\x1b[6n\\x1b[>q\\x1b[18t")
        sys.stdout.buffer.flush()

        start_payload = {{"session_id": session_id}}
        subprocess.run(
            hook_command("SessionStart"),
            input=json.dumps(start_payload),
            text=True,
            shell=True,
            check=True,
        )

        # Read the prompt pasted by the bridge.  It arrives as bracketed paste
        # plus CR; stop at CR/LF so the process can finish deterministically.
        got = bytearray()
        deadline = time.time() + 5
        while time.time() < deadline:
            ch = sys.stdin.buffer.read(1)
            if not ch:
                time.sleep(0.01)
                continue
            got += ch
            if ch in (b"\\r", b"\\n"):
                break
        if b"interactive task" not in got and b"follow-up message" not in got:
            raise SystemExit(f"prompt not received: {{got!r}}")

        with transcript.open("w", encoding="utf-8") as f:
            f.write(json.dumps({{"type": "custom-title", "customTitle": "em-1", "sessionId": session_id}}) + "\\n")
            f.write(json.dumps({{
                "type": "assistant",
                "session_id": session_id,
                "message": {{
                    "role": "assistant",
                    "content": [{{"type": "text", "text": {transcript_text!r}}}],
                }},
            }}) + "\\n")

        stop_payload = {{
            "session_id": session_id,
            "transcript_path": str(transcript),
            "last_assistant_message": {transcript_text!r},
        }}
        subprocess.run(
            hook_command("Stop"),
            input=json.dumps(stop_payload),
            text=True,
            shell=True,
            check=True,
        )
    """).lstrip(), encoding="utf-8")
    fake.chmod(0o755)
    return fake


def test_schema_hides_interactive_claude_backends_keeps_print_mode():
    # The legacy interactive Claude Code backend (claude / claude-interactive)
    # is no longer a user-selectable daemon backend: hidden from the enum and
    # the human-facing description. Print mode (claude-p / claude-code) stays.
    backend = get_schema()["properties"]["backend"]
    assert "claude" not in backend["enum"]
    assert "claude-interactive" not in backend["enum"]
    assert "claude-p" in backend["enum"]
    # Backward compatibility for existing callers and stored daemon entries.
    assert "claude-code" in backend["enum"]

    desc = backend["description"]
    assert "claude-interactive" not in desc
    assert "interactive" not in desc.lower()
    assert "claude-p" in desc
    assert "claude-code" in desc


def test_emanate_claude_dispatches_interactive_runner(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["backend"] = run_dir._state["backend"]
        captured["task"] = task
        captured["backend_argv"] = list(backend_argv or [])
        run_dir._state["claude_session_id"] = "session-from-fake"
        run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
        run_dir.mark_done("done")
        return "done"

    with patch.object(mgr, "_run_claude_interactive_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "claude",
            "tasks": [{
                "task": "Use interactive Claude",
                "tools": [],
                "backend_options": {"model": "opus", "verbose": True},
            }],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured == {
        "backend": "claude",
        "task": "Use interactive Claude",
        "backend_argv": ["--model", "opus", "--verbose"],
    }


def test_emanate_claude_p_dispatches_legacy_print_runner(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["backend"] = run_dir._state["backend"]
        captured["task"] = task
        run_dir.mark_done("done")
        return "done"

    with patch.object(mgr, "_run_claude_code_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "claude-p",
            "tasks": [{"task": "Use print mode", "tools": []}],
        })
        assert result["status"] == "dispatched"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured == {"backend": "claude-p", "task": "Use print mode"}



def test_claude_reserved_backend_options_are_rejected(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    result = mgr.handle({
        "action": "emanate",
        "backend": "claude",
        "tasks": [{
            "task": "should not spawn",
            "tools": [],
            "backend_options": {"settings": "{}"},
        }],
    })

    assert result["status"] == "error"
    assert "--settings is reserved" in result["message"]
    assert mgr._emanations == {}


def test_claude_interactive_system_prompt_backend_option_is_rejected(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    result = mgr.handle({
        "action": "emanate",
        "backend": "claude",
        "tasks": [{
            "task": "should not spawn",
            "tools": [],
            "backend_options": {"append_system_prompt_file": "/tmp/override.md"},
        }],
    })

    assert result["status"] == "error"
    assert "--append-system-prompt-file is reserved" in result["message"]
    assert mgr._emanations == {}


def test_run_claude_interactive_fake_cli_hooks_and_transcript(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir, transcript_text="fake interactive answer")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    run_dir = _make_run_dir(tmp_path)
    result = run_claude_interactive(
        em_id="em-1",
        run_dir=run_dir,
        working_dir=tmp_path / "daemon-agent",
        task="interactive task",
        cancel_event=threading.Event(),
        env=os.environ.copy(),
    )

    assert result.final_text == "fake interactive answer"
    assert result.session_id == "claude-session-123"
    assert result.transcript_path is not None
    assert result.raw_pty_log_path is not None

    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["claude_session_id"] == "claude-session-123"
    assert state["claude_interactive_transcript_path"] == result.transcript_path
    assert state["claude_interactive_prompt_sent"] is True
    assert Path(state["claude_interactive_raw_pty_log"]).exists()

    events = run_dir.events_path.read_text(encoding="utf-8")
    assert "fake interactive answer" in events
    assert "claude interactive SessionStart" in events
    assert "claude interactive Stop" in events




def test_run_claude_interactive_rejects_invalid_managed_worktree_source(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\necho should-not-run >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LINGTAI_CLAUDE_MANAGED_ROOT", str(tmp_path / "managed-claude"))

    run_dir = _make_run_dir(tmp_path)
    with pytest.raises(ClaudeInteractiveError, match="managed-worktree-from must point inside a git repository"):
        run_claude_interactive(
            em_id="em-1",
            run_dir=run_dir,
            working_dir=tmp_path / "daemon-agent",
            task="interactive task",
            cancel_event=threading.Event(),
            backend_argv=["--managed-worktree-from", str(tmp_path / "not-a-repo")],
            env=os.environ.copy(),
        )


def test_run_claude_interactive_checks_out_explicit_managed_worktree_source(tmp_path, monkeypatch):
    source = tmp_path / "source-repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init"], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Tester"], check=True)
    (source / "tracked.txt").write_text("from explicit source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "seed"], check=True, stdout=subprocess.PIPE)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir, transcript_text="explicit managed source answer")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LINGTAI_CLAUDE_MANAGED_ROOT", str(tmp_path / "managed-claude"))

    run_dir = _make_run_dir(tmp_path)
    result = run_claude_interactive(
        em_id="em-1",
        run_dir=run_dir,
        working_dir=tmp_path / "daemon-agent",
        task="interactive task",
        cancel_event=threading.Event(),
        backend_argv=["--managed-worktree-from", str(source)],
        env=os.environ.copy(),
    )

    assert result.final_text == "explicit managed source answer"
    state = json.loads(run_dir.daemon_json_path.read_text())
    worktree = Path(state["claude_interactive_managed_worktree"])
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "from explicit source\n"
    assert state["claude_interactive_managed_source"] == str(source)
    assert state["claude_interactive_managed_source_request"] == str(source)

def test_run_claude_interactive_auto_trusts_only_managed_workspace(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(r'''#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
settings = None
system_prompt_path = None
i = 0
while i < len(args):
    if args[i] == "--settings":
        settings = json.loads(args[i + 1])
        i += 2
    elif args[i] == "--append-system-prompt-file":
        system_prompt_path = Path(args[i + 1])
        i += 2
    else:
        i += 1
if settings is None:
    raise SystemExit("missing --settings")
if system_prompt_path is None or not system_prompt_path.exists():
    raise SystemExit("missing managed system prompt")
if "LingTai-managed ephemeral workspace" not in system_prompt_path.read_text():
    raise SystemExit("unexpected system prompt")

managed_root = Path(os.environ["LINGTAI_CLAUDE_MANAGED_ROOT"]).resolve()
cwd = Path.cwd().resolve()
if not cwd.is_relative_to(managed_root / "runs"):
    raise SystemExit(f"cwd not in managed root: {cwd}")
if cwd.name != "worktree":
    raise SystemExit(f"cwd is not managed worktree: {cwd}")

def hook_command(event):
    for group in settings["hooks"][event]:
        for hook in group["hooks"]:
            return hook["command"]
    raise SystemExit(f"missing hook {event}")

# Simulate Claude Code's workspace trust prompt. The bridge may answer
# this only because cwd is inside the LingTai-managed workspace root.
# Claude's Ink TUI may repaint the same prompt before consuming the answer;
# duplicate frames must not make LingTai fail as if this were an arbitrary cwd.
for _ in range(2):
    sys.stdout.write("Quick safety check: Is this a project you created or one you trust?\n")
    sys.stdout.write("1. Yes, I trust this folder\n2. No, exit\n")
    sys.stdout.flush()
    time.sleep(0.05)
answer = sys.stdin.buffer.read(2)
if answer not in (b"1\r", b"1\n"):
    raise SystemExit(f"trust answer not received: {answer!r}")

session_id = "managed-session-123"
transcript = Path.cwd() / "managed-transcript.jsonl"
subprocess.run(
    hook_command("SessionStart"),
    input=json.dumps({"session_id": session_id}),
    text=True,
    shell=True,
    check=True,
)

got = bytearray()
deadline = time.time() + 5
while time.time() < deadline:
    ch = sys.stdin.buffer.read(1)
    if not ch:
        time.sleep(0.01)
        continue
    got += ch
    if ch in (b"\r", b"\n"):
        break
if b"interactive task" not in got:
    raise SystemExit(f"prompt not received: {got!r}")

with transcript.open("w", encoding="utf-8") as f:
    f.write(json.dumps({
        "type": "assistant",
        "session_id": session_id,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "managed trust answer"}],
        },
    }) + "\n")
subprocess.run(
    hook_command("Stop"),
    input=json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript),
        "last_assistant_message": "managed trust answer",
    }),
    text=True,
    shell=True,
    check=True,
)
''', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LINGTAI_CLAUDE_MANAGED_ROOT", str(tmp_path / "managed-claude"))

    run_dir = _make_run_dir(tmp_path)
    result = run_claude_interactive(
        em_id="em-1",
        run_dir=run_dir,
        working_dir=tmp_path / "daemon-agent",
        task="interactive task",
        cancel_event=threading.Event(),
        env=os.environ.copy(),
    )

    assert result.final_text == "managed trust answer"
    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["claude_interactive_managed_trust_answered"] is True
    assert Path(state["claude_interactive_managed_worktree"]).exists()
    assert Path(state["claude_interactive_system_prompt"]).exists()
    assert Path(state["claude_interactive_raw_pty_log"]).is_relative_to(
        tmp_path / "managed-claude" / "runs"
    )
    events = run_dir.events_path.read_text(encoding="utf-8")
    assert "auto-selected workspace trust" in events
