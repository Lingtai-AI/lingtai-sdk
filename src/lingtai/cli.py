"""lingtai-agent run <working_dir> — boot agent into ASLEEP, wake on external messages."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from lingtai_kernel.config_resolve import (
    resolve_env,
    load_env_file,
)
from lingtai.init_schema import validate_init
from lingtai.llm.service import LLMService, build_provider_defaults_from_manifest_llm
from lingtai.agent import Agent
from lingtai_kernel.services.mail import FilesystemMailService


def load_init(working_dir: Path) -> dict:
    """Read and validate init.json from working_dir. Exits on error.

    If ``manifest.preset.active`` is set, the active preset's ``llm`` and
    ``capabilities`` are materialized into the manifest before validation,
    so downstream code (and the schema check) sees a fully-resolved manifest.
    This mirrors ``Agent._read_init`` so boot and live-refresh agree.
    """
    from lingtai_kernel.config_resolve import resolve_paths
    from lingtai.presets import materialize_active_preset
    from lingtai.capabilities import CORE_DEFAULTS

    from lingtai_kernel.migrate import run_agent_migrations

    run_agent_migrations(working_dir)

    init_path = working_dir / "init.json"
    if not init_path.is_file():
        print(f"error: {init_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(init_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"error: failed to read {init_path}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        materialize_active_preset(data, working_dir, core_defaults=CORE_DEFAULTS)
    except (KeyError, ValueError) as e:
        print(f"error: failed to materialize active preset: {e}", file=sys.stderr)
        sys.exit(1)

    # Strip deprecated fields before validation so they don't trigger
    # warnings or interfere with the refresh path.
    from lingtai.init_schema import strip_deprecated
    stripped = strip_deprecated(data)
    if stripped:
        # Persist cleanup to disk so the fields don't come back.
        init_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    try:
        warnings = validate_init(data)
    except ValueError as e:
        print(f"error: invalid init.json: {e}", file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f"warning: init.json: {w}", file=sys.stderr)

    resolve_paths(data, working_dir)
    return data


def build_agent(data: dict, working_dir: Path) -> Agent:
    """Construct Agent from validated init data.

    Creates a minimal Agent (LLMService + working_dir + mail_service),
    then delegates all setup to _perform_refresh() which reads init.json.
    This ensures boot and live refresh share one code path.
    """
    # Load env file if specified (needed for LLM API key resolution).
    # Refresh relaunches inherit the old process env; a watcher marker lets
    # the freshly edited env_file replace those stale values once, then is
    # consumed so later child processes keep normal boot semantics.
    env_file = data.get("env_file")
    overwrite_env_file = os.environ.get("LINGTAI_REFRESH_ENV_OVERWRITE") == "1"
    if env_file:
        load_env_file(env_file, overwrite=overwrite_env_file)
    if overwrite_env_file:
        os.environ.pop("LINGTAI_REFRESH_ENV_OVERWRITE", None)

    m = data["manifest"]
    llm = m["llm"]

    api_key = resolve_env(llm.get("api_key"), llm.get("api_key_env"))

    # Default 60 matches AgentConfig.max_rpm — agents whose init.json
    # predates this field cooperatively share the network-wide 60 RPM cap
    # by default. Set to 0 in init.json to disable gating.
    max_rpm = m.get("max_rpm", 60)
    # Pass working_dir so Codex agents get their per-agent session/thread
    # identity (agent path + last molt time) wired in by default.
    provider_defaults = build_provider_defaults_from_manifest_llm(
        llm, max_rpm=max_rpm, working_dir=working_dir
    )
    service = LLMService(
        provider=llm["provider"],
        model=llm["model"],
        api_key=api_key,
        base_url=llm.get("base_url"),
        context_window=m.get("context_limit", 200_000),
        provider_defaults=provider_defaults,
    )

    mail_service = FilesystemMailService(
        working_dir=working_dir,
        pseudo_agent_subscriptions=m.get("pseudo_agent_subscriptions", ["../human"]),
    )

    # Minimal construction — _perform_refresh reads init.json for everything else
    agent = Agent(
        service,
        agent_name=m.get("agent_name"),
        working_dir=working_dir,
        mail_service=mail_service,
        streaming=m.get("streaming", False),
    )

    # Full setup from init.json (capabilities, addons, config, covenant, etc.)
    agent._setup_from_init()

    # Restore molt count from previous run (if resuming)
    prev_manifest = working_dir / ".agent.json"
    if prev_manifest.is_file():
        try:
            prev = json.loads(prev_manifest.read_text(encoding="utf-8"))
            agent._molt_count = prev.get("molt_count", 0)
        except (json.JSONDecodeError, OSError):
            pass

    return agent


def _clean_signal_files(working_dir: Path) -> None:
    """Remove stale .suspend / .sleep files left over from a previous run."""
    for name in (".suspend", ".sleep", ".refresh"):
        f = working_dir / name
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass


def _install_signal_handlers(working_dir: Path, agent: Agent) -> None:
    """SIGTERM/SIGINT → touch .suspend and unblock main thread."""
    suspend_file = working_dir / ".suspend"

    def _handler(signum, frame):
        suspend_file.touch()
        agent._shutdown.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _check_duplicate_process(working_dir: Path) -> None:
    """Abort if another `lingtai run <working_dir>` process is already alive.

    Defense-in-depth alongside the kernel's flock — the flock prevents
    data corruption, but a duplicate Python process still shows up in
    `ps` and can mislead users.  This check catches the case where the
    old process is mid-teardown (heartbeat file gone, lock about to be
    released) but still visible in `ps`.
    """
    import subprocess as _sp
    abs_dir = str(working_dir.resolve())
    try:
        out = _sp.check_output(
            ["ps", "-eo", "pid=,command="], stderr=_sp.DEVNULL, text=True
        )
    except Exception:
        return  # ps unavailable — fall through to flock
    # Match the working-dir argument exactly. A substring test false-positives
    # on prefix-sharing siblings (e.g. starting "codex" while "codex_colleague"
    # is alive — "codex" is a prefix of "codex_colleague") and on shell wrappers
    # whose argv merely contains the path, so tokenize each command line and
    # require a `lingtai ... run <abs_dir>` invocation where the token after
    # `run` resolves to exactly our working dir.
    import shlex as _shlex
    my_pid = os.getpid()
    for line in out.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        parts = trimmed.split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, command = parts
        if not pid_str.isdigit() or int(pid_str) == my_pid:
            continue
        try:
            tokens = _shlex.split(command)
        except ValueError:
            continue  # unparseable command line — let flock be the backstop
        matched = False
        for i in range(1, len(tokens) - 1):
            if tokens[i] != "run" or "lingtai" not in tokens[i - 1]:
                continue
            candidate = tokens[i + 1]
            try:
                candidate = str(Path(candidate).resolve())
            except Exception:
                pass
            if candidate == abs_dir:
                matched = True
                break
        if not matched:
            continue
        print(
            f"error: another lingtai agent is already running in {abs_dir}\n"
            f"  PID {pid_str}: {trimmed}\n"
            f"  If this is a stale process, kill it first.",
            file=sys.stderr,
        )
        sys.exit(1)


def run(working_dir: Path) -> None:
    """Boot agent into ASLEEP — wakes on external messages (mail/imap/telegram)."""
    _check_duplicate_process(working_dir)
    _clean_signal_files(working_dir)
    data = load_init(working_dir)

    # Resolve venv and store on agent for CPR/avatar to use
    from lingtai.venv_resolve import resolve_venv
    venv_dir = resolve_venv(data)
    # Write back to init.json if not already set (self-sufficient)
    if not data.get("venv_path") or data["venv_path"] != str(venv_dir):
        data["venv_path"] = str(venv_dir)
        init_path = working_dir / "init.json"
        init_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    agent = build_agent(data, working_dir)
    agent._venv_path = str(venv_dir)
    _install_signal_handlers(working_dir, agent)

    from lingtai_kernel.state import AgentState
    agent._asleep.set()
    agent._state = AgentState.ASLEEP

    # Detect refresh boot — old process renamed .refresh → .refresh.taken
    taken_file = working_dir / ".refresh.taken"
    is_refresh = taken_file.is_file()
    if is_refresh:
        taken_file.unlink()

    try:
        agent.start()

        # Kick-start after refresh — wake agent with a system message
        if is_refresh:
            from lingtai_kernel.i18n import t
            lang = agent._config.language
            agent.send(t(lang, "system.refresh_successful"), sender="system")

        agent._shutdown.wait()
    finally:
        try:
            agent.stop(timeout=10.0)
        except Exception:
            pass


def _emit_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _handle_log_command(args) -> None:
    from lingtai_kernel.services.logging import (
        doctor_sqlite_event_index,
        query_sqlite_event_index,
        rebuild_sqlite_event_index,
    )

    agent_dir = args.agent_dir.resolve()
    if not agent_dir.is_dir():
        print(f"error: {agent_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.log_command == "rebuild":
        try:
            _emit_json(rebuild_sqlite_event_index(agent_dir))
        except Exception as e:
            print(f"error: failed to rebuild sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.log_command == "doctor":
        try:
            _emit_json(doctor_sqlite_event_index(agent_dir))
        except Exception as e:
            print(f"error: failed to inspect sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.log_command == "query":
        try:
            _emit_json(query_sqlite_event_index(agent_dir, args.sql))
        except Exception as e:
            print(f"error: failed to query sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("error: missing log subcommand", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lingtai-agent",
        description="lingtai agent runtime",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Boot agent into sleep — wakes on external messages")
    run_parser.add_argument("working_dir", type=Path, help="Agent working directory containing init.json")

    sub.add_parser("check-caps", help="Output capability provider metadata as JSON")

    log_parser = sub.add_parser("log", help="Inspect the additive SQLite log index")
    log_sub = log_parser.add_subparsers(dest="log_command", required=True)

    log_rebuild = log_sub.add_parser("rebuild", help="Rebuild logs/log.sqlite from agent events, chat history, and daemon JSONL")
    log_rebuild.add_argument("agent_dir", type=Path, help="Agent working directory")

    log_doctor = log_sub.add_parser("doctor", help="Check logs/log.sqlite integrity and counts")
    log_doctor.add_argument("agent_dir", type=Path, help="Agent working directory")

    log_query = log_sub.add_parser("query", help="Run a read-only SQL query against logs/log.sqlite")
    log_query.add_argument("agent_dir", type=Path, help="Agent working directory")
    log_query.add_argument("sql", help="SQL query to execute")

    args = parser.parse_args()

    if args.command == "run":
        working_dir = args.working_dir.resolve()
        if not working_dir.is_dir():
            print(f"error: {working_dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        run(working_dir)
    elif args.command == "check-caps":
        from lingtai.capabilities import get_all_providers
        print(json.dumps(get_all_providers()))
    elif args.command == "log":
        _handle_log_command(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
