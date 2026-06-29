#!/usr/bin/env python3
"""Read-only LingTai agent health diagnostics.

This script intentionally uses only the Python standard library and never writes
inside the target agent directory. It is designed to run from an intrinsic skill
bundle copied into an agent's `.library/intrinsic/capabilities/` tree.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITY_ORDER = {"OK": 0, "WARN": 1, "FAIL": 2}
SECRET_MARKERS = (
    "token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "bearer",
    "auth",
    "credential",
    "private_key",
)
PATH_MARKERS = ("path", "file", "dir", "home", "venv", "python", "command", "config")
ADDON_MODULES = {
    "telegram": "lingtai.mcp_servers.telegram",
    "feishu": "lingtai.mcp_servers.feishu",
    "wechat": "lingtai.mcp_servers.wechat",
    "whatsapp": "lingtai.mcp_servers.whatsapp",
    "imap": "lingtai.mcp_servers.imap",
}


def match_agent_run(cmdline: str, working_dir: str) -> str | None:
    """Return the launch form if ``cmdline`` is an agent run for ``working_dir``.

    Keep this stdlib-only copy in sync with
    ``lingtai_kernel.process_match.match_agent_run``. The doctor script is
    copied into agent skill bundles and cannot import kernel modules there.

    Residual limitation: ``ps command=`` is a flat string, not the original argv
    vector. A non-LingTai process can still match if its argument text is shaped
    exactly like an absolute LingTai program path followed by ``run <dir>``.
    """
    target = os.path.normpath(working_dir)
    for token, label, program_anchored in (
        (" -m lingtai run ", "module", False),
        ("lingtai-agent run ", "console", True),
        ("lingtai run ", "legacy", True),
    ):
        idx = cmdline.find(token)
        while idx != -1:
            if (not program_anchored) or idx == 0 or cmdline[idx - 1] == "/":
                tail = cmdline[idx + len(token):].strip()
                if tail and os.path.normpath(tail) == target:
                    return label
            idx = cmdline.find(token, idx + 1)
    return None


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Section:
    name: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> str:
        worst = "OK"
        for finding in self.findings:
            if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[worst]:
                worst = finding.severity
        return worst

    def add(self, severity: str, title: str, detail: str, **data: Any) -> None:
        if severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity {severity!r}")
        self.findings.append(Finding(severity, title, detail, clean_data(data)))


class Report:
    def __init__(self, agent_dir: Path, project_dir: Path | None):
        self.agent_dir = agent_dir
        self.project_dir = project_dir
        self.generated_at = now_iso()
        self.sections: list[Section] = []
        self.next_steps: list[str] = []

    @property
    def severity(self) -> str:
        worst = "OK"
        for section in self.sections:
            if SEVERITY_ORDER[section.severity] > SEVERITY_ORDER[worst]:
                worst = section.severity
        return worst

    def section(self, name: str) -> Section:
        sec = Section(name)
        self.sections.append(sec)
        return sec

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "generated_at": self.generated_at,
            "agent_dir": str(self.agent_dir),
            "project_dir": str(self.project_dir) if self.project_dir else None,
            "sections": [
                {
                    "name": section.name,
                    "severity": section.severity,
                    "findings": [
                        {
                            "severity": finding.severity,
                            "title": finding.title,
                            "detail": finding.detail,
                            "data": finding.data,
                        }
                        for finding in section.findings
                    ],
                }
                for section in self.sections
            ],
            "next_steps": self.next_steps,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
    except OSError:
        return None


def age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
    except OSError:
        return None


def clean_data(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if is_secret_key(str(key)):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = clean_data(item)
        return result
    if isinstance(value, list):
        return [clean_data(item) for item in value]
    if isinstance(value, tuple):
        return [clean_data(item) for item in value]
    return value


def is_secret_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in SECRET_MARKERS)


def looks_path_like(key: str, value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    lower_key = key.lower()
    if any(marker in lower_key for marker in PATH_MARKERS):
        return True
    return value.startswith(("/", "~", "./", "../")) or os.sep in value


def read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:  # noqa: BLE001 - diagnostic tool should keep going
        return None, f"{type(exc).__name__}: {exc}"


def summarize_file(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        return {"exists": False, "error": str(exc)}
    return {
        "exists": True,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "age_seconds": round(max(0.0, datetime.now(timezone.utc).timestamp() - st.st_mtime), 1),
    }


def read_text_preview(path: Path, max_chars: int = 200) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def parse_heartbeat(path: Path) -> dict[str, Any]:
    info = summarize_file(path)
    text = read_text_preview(path, 120)
    if text is not None:
        info["raw_preview"] = text
        try:
            # Current kernel writes Unix epoch seconds; some old runtimes wrote
            # arbitrary heartbeat text. Keep mtime as fallback for both.
            ts = float(text)
        except ValueError:
            info["parse_error"] = "not a float timestamp"
        else:
            now = datetime.now(timezone.utc).timestamp()
            info["timestamp_age_seconds"] = round(max(0.0, now - ts), 1)
            info["timestamp"] = datetime.fromtimestamp(ts, timezone.utc).isoformat(
                timespec="seconds"
            )
    return info


def newest_mtime(paths: Iterable[Path]) -> float | None:
    newest: float | None = None
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if newest is None or st.st_mtime > newest:
            newest = st.st_mtime
    return newest


def default_project_dir(agent_dir: Path) -> Path | None:
    # Common layout: <project>/.lingtai/<agent>. Return <project> when present.
    if agent_dir.parent.name == ".lingtai":
        return agent_dir.parent.parent
    return agent_dir.parent if agent_dir.parent != agent_dir else None


def collect_marker_files(report: Report) -> None:
    sec = report.section("marker files")
    markers = [
        ".agent.lock",
        ".refresh",
        ".suspend",
        ".agent.heartbeat",
        ".status.json",
        ".agent.json",
    ]
    seen = []
    for name in markers:
        path = report.agent_dir / name
        info = summarize_file(path)
        info["name"] = name
        if info.get("exists"):
            seen.append(info)
    sec.add("OK", "marker file footprint", f"Found {len(seen)} lifecycle marker file(s).", markers=seen)
    for name in (".refresh", ".suspend"):
        path = report.agent_dir / name
        if path.exists():
            sec.add(
                "WARN",
                f"{name} present",
                f"{name} exists; this may indicate a pending refresh/suspension or stale lifecycle signal.",
                file=summarize_file(path),
            )
    lock = report.agent_dir / ".agent.lock"
    if lock.exists():
        sec.add(
            "OK",
            ".agent.lock present",
            "Lock file exists. Presence alone is normal for a running agent; stale locks should be interpreted with heartbeat/process evidence.",
            file=summarize_file(lock),
        )


def collect_lifecycle(report: Report) -> dict[str, Any]:
    sec = report.section("lifecycle")
    agent_json, agent_err = read_json(report.agent_dir / ".agent.json")
    status_json, status_err = read_json(report.agent_dir / ".status.json")

    if agent_err:
        sec.add("FAIL", ".agent.json unavailable", f"Could not read .agent.json: {agent_err}")
    else:
        fields = {}
        assert isinstance(agent_json, dict)
        for key in (
            "name",
            "id",
            "state",
            "status",
            "model",
            "provider",
            "started_at",
            "woken_at",
            "current_time",
            "context_usage",
        ):
            if key in agent_json:
                fields[key] = agent_json[key]
        sec.add("OK", ".agent.json readable", "Agent identity/lifecycle file is present.", fields=fields)

    if status_err:
        sec.add("WARN", ".status.json unavailable", f"Could not read .status.json: {status_err}")
    else:
        assert isinstance(status_json, dict)
        fields = {
            k: status_json.get(k)
            for k in ("state", "status", "current_time", "no_progress_seconds", "context", "context_usage")
            if k in status_json
        }
        sec.add("OK", ".status.json readable", "Runtime status file is present.", fields=fields)
        status_age = age_seconds(report.agent_dir / ".status.json")
        if status_age is not None and status_age > 300:
            sec.add("WARN", "status file is stale", f".status.json has not changed for {status_age:.0f}s.", age_seconds=round(status_age, 1))

        # Issue #178 — read nested runtime fingerprint for drift visibility
        runtime_block = status_json.get("runtime")
        if isinstance(runtime_block, dict):
            fp = runtime_block.get("fingerprint")
            if isinstance(fp, dict):
                fp_fields = {
                    "git_rev": fp.get("git_rev"),
                    "source_digest": fp.get("source_digest"),
                    "captured_at": fp.get("captured_at"),
                }
                python_ver = runtime_block.get("python_version")
                plat = runtime_block.get("platform")
                if python_ver:
                    fp_fields["python_version"] = python_ver
                if plat:
                    fp_fields["platform"] = plat
                sec.add(
                    "OK",
                    "runtime fingerprint available",
                    f"Fingerprint captured at {fp.get('captured_at', 'unknown')}; "
                    f"git={fp.get('git_rev', 'n/a')}, digest={fp.get('source_digest', 'n/a')}.",
                    fingerprint=fp_fields,
                )
            else:
                sec.add(
                    "WARN",
                    "runtime fingerprint missing",
                    "The .status.json runtime block does not contain a fingerprint; "
                    "the agent may be running an older kernel version that predates issue #178.",
                )
        else:
            sec.add(
                "WARN",
                "runtime fingerprint missing",
                "The .status.json does not contain a runtime block; "
                "the agent may be running an older kernel version that predates issue #178.",
            )

    heartbeat = report.agent_dir / ".agent.heartbeat"
    hb = parse_heartbeat(heartbeat)
    if not hb.get("exists"):
        sec.add("FAIL", "heartbeat missing", ".agent.heartbeat is absent; the agent may be suspended/dead or using an old runtime.")
    else:
        hb_age = float(hb.get("timestamp_age_seconds", hb.get("age_seconds", 0.0)))
        sev = "OK" if hb_age <= 120 else "WARN" if hb_age <= 600 else "FAIL"
        detail = f".agent.heartbeat age is {hb_age:.0f}s."
        if hb.get("parse_error"):
            sev = "WARN" if sev == "OK" else sev
            detail += f" Timestamp parse warning: {hb['parse_error']}."
        sec.add(sev, "heartbeat freshness", detail, file=hb)

    return {"agent_json": agent_json, "status_json": status_json, "heartbeat": hb}


def collect_process(report: Report) -> None:
    sec = report.section("process")
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        sec.add("WARN", "process scan unavailable", f"Could not run ps: {type(exc).__name__}: {exc}")
        return
    if proc.returncode != 0:
        sec.add("WARN", "process scan failed", proc.stderr.strip() or f"ps exited {proc.returncode}")
        return

    agent_str = str(report.agent_dir)
    matches = []
    for line in proc.stdout.splitlines():
        trimmed = line.strip()
        parts = trimmed.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            command = parts[1]
        else:
            command = trimmed
        if match_agent_run(command, agent_str) is not None:
            matches.append(line.strip())
    if matches:
        sec.add("OK", "lingtai process found", f"Found {len(matches)} process line(s) referencing this agent.", processes=matches[:5])
    else:
        sec.add("WARN", "no lingtai process found", "No `lingtai-agent run <agent-dir>` or `python -m lingtai run <agent-dir>` process was found. A fresh heartbeat would make this inconsistent.")


def collect_notifications_logs_mail(report: Report) -> None:
    sec = report.section("notifications/logs/mail")
    notif_dir = report.agent_dir / ".notification"
    if notif_dir.is_dir():
        entries = []
        for path in sorted(notif_dir.iterdir()):
            if path.is_file():
                info = summarize_file(path)
                info["channel"] = path.name
                entries.append(info)
        sec.add("OK", "notification directory scanned", f"Found {len(entries)} notification file(s).", notifications=entries)
    else:
        sec.add("OK", "notification directory absent", "No .notification directory is present.")

    log_paths = [
        report.agent_dir / "logs" / "events.jsonl",
        report.agent_dir / "logs" / "agent.log",
        report.agent_dir / "logs" / "token_ledger.jsonl",
        report.agent_dir / "logs" / "tokens.jsonl",
    ]
    logs = []
    for path in log_paths:
        info = summarize_file(path)
        info["path"] = str(path.relative_to(report.agent_dir))
        logs.append(info)
    sec.add("OK", "log files summarized", "Log mtimes/sizes collected without reading message bodies.", logs=logs)
    heartbeat_age = age_seconds(report.agent_dir / ".agent.heartbeat")
    newest_log = newest_mtime(path for path in log_paths if path.exists())
    if heartbeat_age is not None and heartbeat_age <= 120 and newest_log is not None:
        log_age = max(0.0, datetime.now(timezone.utc).timestamp() - newest_log)
        if log_age > 3600:
            sec.add(
                "WARN",
                "logs stale while heartbeat fresh",
                f"Heartbeat is fresh but newest known log is {log_age:.0f}s old; logging may be disabled/stalled or the agent has been idle.",
                newest_log_age_seconds=round(log_age, 1),
            )

    for box_name in ("inbox", "outbox", "mailbox", "sent"):
        box = report.agent_dir / box_name
        if box.is_dir():
            try:
                count = sum(1 for p in box.rglob("*") if p.is_file())
            except OSError as exc:
                sec.add("WARN", f"{box_name} count failed", str(exc))
            else:
                sec.add("OK", f"{box_name} footprint", f"{count} file(s) under {box_name}; bodies not read.", count=count)


def iter_mcp_entries(agent_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    errors: list[str] = []

    init, init_err = read_json(agent_dir / "init.json")
    if init_err is None and isinstance(init, dict):
        mcp = init.get("mcp")
        if isinstance(mcp, dict):
            for name, cfg in mcp.items():
                if isinstance(cfg, dict):
                    entry = dict(cfg)
                    entry.setdefault("name", name)
                    entry["source"] = "init.json:mcp"
                    entries.append(entry)
        elif mcp is not None:
            errors.append("init.json:mcp is not an object")
    elif init_err != "missing":
        errors.append(f"init.json: {init_err}")

    reg_path = agent_dir / "mcp_registry.jsonl"
    if reg_path.exists():
        seen_registry_names: set[str] = set()
        try:
            for idx, line in enumerate(reg_path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"mcp_registry.jsonl:{idx}: {exc}")
                    continue
                if not isinstance(obj, dict):
                    errors.append(f"mcp_registry.jsonl:{idx}: not an object")
                    continue
                for field_name in ("name", "summary", "transport", "source"):
                    if not isinstance(obj.get(field_name), str) or not obj.get(field_name):
                        errors.append(f"mcp_registry.jsonl:{idx}: missing/invalid {field_name!r}")
                if obj.get("transport") == "stdio" and not isinstance(obj.get("command"), str):
                    errors.append(f"mcp_registry.jsonl:{idx}: stdio entry missing string 'command'")
                if obj.get("transport") == "stdio" and not isinstance(obj.get("args", []), list):
                    errors.append(f"mcp_registry.jsonl:{idx}: stdio entry has non-list 'args'")
                name = obj.get("name")
                if isinstance(name, str):
                    if name in seen_registry_names:
                        errors.append(f"mcp_registry.jsonl:{idx}: duplicate name {name!r}")
                    seen_registry_names.add(name)
                obj = dict(obj)
                obj["source"] = f"mcp_registry.jsonl:{idx}"
                entries.append(obj)
        except OSError as exc:
            errors.append(f"mcp_registry.jsonl: {exc}")

    return entries, errors


def command_from_entry(entry: dict[str, Any]) -> str | None:
    for key in ("command", "cmd", "executable"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    config = entry.get("config")
    if isinstance(config, dict):
        for key in ("command", "cmd", "executable"):
            value = config.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def env_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = {}
    for key in ("env", "environment"):
        value = entry.get(key)
        if isinstance(value, dict):
            env.update(value)
    config = entry.get("config")
    if isinstance(config, dict):
        for key in ("env", "environment"):
            value = config.get(key)
            if isinstance(value, dict):
                env.update(value)
    return env


def args_from_entry(entry: dict[str, Any]) -> list[str]:
    for key in ("args", "arguments"):
        value = entry.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
    config = entry.get("config")
    if isinstance(config, dict):
        for key in ("args", "arguments"):
            value = config.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]
    return []


def module_from_args(args: list[str]) -> str | None:
    for idx, item in enumerate(args[:-1]):
        if item == "-m" and args[idx + 1]:
            return args[idx + 1]
    return None


def validate_command(command: str) -> dict[str, Any]:
    expanded = os.path.expandvars(os.path.expanduser(command))
    has_sep = os.sep in expanded or (os.altsep and os.altsep in expanded)
    if has_sep:
        path = Path(expanded)
        exists = path.exists()
        executable = os.access(path, os.X_OK) if exists else False
        return {
            "command": command,
            "mode": "path",
            "resolved": str(path),
            "exists": exists,
            "executable": executable,
            "severity": "OK" if exists and executable else "FAIL" if not exists else "WARN",
        }
    found = shutil.which(expanded)
    return {
        "command": command,
        "mode": "PATH",
        "resolved": found,
        "exists": bool(found),
        "executable": bool(found),
        "severity": "OK" if found else "FAIL",
    }


def migration_hints(command: str) -> list[str]:
    hints: list[str] = []
    home_candidate = Path.home() / ".lingtai-tui" / "runtime" / "venv" / "bin" / "python"
    user = getpass.getuser()
    users_candidate = Path("/Users") / user / ".lingtai-tui" / "runtime" / "venv" / "bin" / "python"
    linux_candidate = Path("/home") / user / ".lingtai-tui" / "runtime" / "venv" / "bin" / "python"
    system = platform.system().lower()

    if "/home/" in command and (home_candidate.exists() or users_candidate.exists()):
        hints.append(
            "command contains a Linux /home path, but a local ~/.lingtai-tui runtime Python exists; this often means a stale MCP command after migration"
        )
    if "/Users/" in command and system == "linux" and linux_candidate.exists():
        hints.append(
            "command contains a macOS /Users path on Linux, but a /home runtime Python exists; this often means a stale MCP command after migration"
        )
    return hints


def summarize_env(
    env: dict[str, Any], agent_dir: Path, project_dir: Path | None
) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    bases = [agent_dir]
    if project_dir is not None:
        bases.append(project_dir)
    for key, value in sorted(env.items()):
        if is_secret_key(key):
            summarized.append({"key": key, "redacted": True})
        elif looks_path_like(key, value):
            raw = str(value)
            expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
            candidates = [expanded] if expanded.is_absolute() else [base / expanded for base in bases]
            summarized.append(
                {
                    "key": key,
                    "path_like": True,
                    "exists": any(candidate.exists() for candidate in candidates),
                    "checked": [str(candidate) for candidate in candidates[:3]],
                }
            )
        else:
            summarized.append({"key": key, "present": True})
    return summarized


def collect_mcp(report: Report) -> list[str]:
    sec = report.section("mcp/addons")
    entries, errors = iter_mcp_entries(report.agent_dir)
    for err in errors:
        sec.add("WARN", "MCP config parse warning", err)
    if not entries:
        sec.add("OK", "no MCP entries", "No MCP entries found in init.json or mcp_registry.jsonl.")
        return []

    python_commands: list[str] = []
    addon_modules_to_try: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or entry.get("id") or entry.get("alias") or "<unnamed>")
        transport = str(entry.get("transport") or entry.get("type") or "stdio")
        command = command_from_entry(entry)
        env_summary = summarize_env(env_from_entry(entry), report.agent_dir, report.project_dir)
        args = args_from_entry(entry)
        module = module_from_args(args)
        if module:
            addon_modules_to_try.add(module)
        elif name in ADDON_MODULES:
            addon_modules_to_try.add(ADDON_MODULES[name])

        if transport != "stdio" and not command:
            sec.add("OK", f"{name}: non-stdio entry", f"Transport is {transport}; no stdio command validation needed.", source=entry.get("source"), env=env_summary)
            continue
        if not command:
            sec.add("WARN", f"{name}: missing stdio command", "Entry appears to be stdio but has no command field.", source=entry.get("source"), env=env_summary)
            continue

        validation = validate_command(command)
        hints = migration_hints(command)
        severity = validation["severity"]
        if hints and severity == "OK":
            severity = "WARN"
        detail = "Command resolves and is executable."
        if validation["severity"] == "FAIL":
            detail = "Command path/executable could not be found."
        elif validation["severity"] == "WARN":
            detail = "Command exists but is not executable."
        if hints:
            detail += " Migration hint: " + "; ".join(hints)
        sec.add(
            severity,
            f"{name}: stdio command",
            detail,
            source=entry.get("source"),
            command=validation,
            args=args,
            module=module,
            migration_hints=hints,
            env=env_summary,
        )

        resolved = validation.get("resolved")
        if isinstance(resolved, str) and resolved and Path(resolved).name.startswith("python"):
            python_commands.append(resolved)
        elif Path(os.path.expanduser(os.path.expandvars(command))).name.startswith("python"):
            python_commands.append(os.path.expanduser(os.path.expandvars(command)))

    return try_addon_imports(sec, python_commands, addon_modules_to_try)


def try_addon_imports(sec: Section, python_commands: list[str], modules: set[str]) -> list[str]:
    if not modules:
        return []
    python = next((cmd for cmd in python_commands if Path(cmd).exists() and os.access(cmd, os.X_OK)), sys.executable)
    results: list[dict[str, Any]] = []
    for module in sorted(modules):
        proc = subprocess.run(
            [python, "-c", f"import {module}; print('ok')"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        ok = proc.returncode == 0
        results.append({"module": module, "ok": ok, "python": python, "error": "" if ok else proc.stderr.strip().splitlines()[-1:]})
    severity = "OK" if all(item["ok"] for item in results) else "WARN"
    sec.add(severity, "first-party addon import check", "Tried importing configured first-party addon modules without reading credentials.", imports=results)
    return [item["module"] for item in results if not item["ok"]]


def add_next_steps(report: Report) -> None:
    report.next_steps = [
        "If heartbeat is fresh, try internal email first; mail wakes ACTIVE/IDLE/STUCK/ASLEEP agents even when external addons are broken.",
        "Use CPR only when heartbeat/process evidence says the agent is suspended or dead; avoid CPR for a merely broken MCP addon.",
        "If an MCP stdio command is missing after migration, back up init.json and mcp_registry.jsonl, update the stale command path, then refresh the agent.",
        "Handle producer notifications with their producer tools when possible; generic dismiss only clears the notification mirror.",
    ]


def diagnose(agent_dir: Path, project_dir: Path | None = None) -> Report:
    agent_dir = agent_dir.expanduser().resolve()
    project_dir = project_dir.expanduser().resolve() if project_dir else default_project_dir(agent_dir)
    report = Report(agent_dir, project_dir)
    if not agent_dir.exists():
        sec = report.section("agent-dir")
        sec.add("FAIL", "agent directory missing", f"{agent_dir} does not exist.")
        add_next_steps(report)
        return report
    collect_marker_files(report)
    collect_lifecycle(report)
    collect_process(report)
    collect_notifications_logs_mail(report)
    collect_mcp(report)
    add_next_steps(report)
    return report


def render_text(report: Report) -> str:
    lines = [
        f"LingTai doctor report: {report.severity}",
        f"Generated: {report.generated_at}",
        f"Agent dir: {report.agent_dir}",
    ]
    if report.project_dir:
        lines.append(f"Project dir: {report.project_dir}")
    lines.append("")
    for section in report.sections:
        lines.append(f"## {section.name} [{section.severity}]")
        for finding in section.findings:
            lines.append(f"- [{finding.severity}] {finding.title}: {finding.detail}")
            if finding.data:
                rendered = json.dumps(finding.data, ensure_ascii=False, sort_keys=True)
                lines.append(f"  data: {rendered}")
        lines.append("")
    lines.append("## next steps")
    for step in report.next_steps:
        lines.append(f"- {step}")
    return "\n".join(lines)


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="lingtai-doctor-") as tmp:
        agent = Path(tmp) / "project" / ".lingtai" / "mimo-test"
        agent.mkdir(parents=True)
        (agent / ".agent.json").write_text(json.dumps({"name": "mimo-test", "state": "idle"}), encoding="utf-8")
        (agent / ".status.json").write_text(json.dumps({"state": "idle"}), encoding="utf-8")
        (agent / ".agent.heartbeat").write_text("ok", encoding="utf-8")
        (agent / "init.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "telegram": {
                            "transport": "stdio",
                            "command": "/home/example/.lingtai-tui/runtime/venv/bin/python",
                            "env": {"BOT_TOKEN": "should-not-print", "CONFIG_PATH": "/missing/config.json"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        report = diagnose(agent)
        text = render_text(report)
        if report.severity not in {"WARN", "FAIL"}:
            print("self-test failed: expected WARN/FAIL", file=sys.stderr)
            print(text, file=sys.stderr)
            return 1
        if "should-not-print" in text:
            print("self-test failed: secret value leaked", file=sys.stderr)
            return 1
        if "/home/example/.lingtai-tui/runtime/venv/bin/python" not in text:
            print("self-test failed: missing stale command evidence", file=sys.stderr)
            print(text, file=sys.stderr)
            return 1
    print("lingtai-doctor self-test OK")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only LingTai agent doctor diagnostics")
    parser.add_argument("--agent-dir", help="Path to an agent workdir. Defaults to LINGTAI_AGENT_DIR.")
    parser.add_argument("--project-dir", help="Optional project root path for display/context.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--self-test", action="store_true", help="Run an internal fixture test and exit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()
    agent_arg = args.agent_dir or os.environ.get("LINGTAI_AGENT_DIR")
    if not agent_arg:
        print("error: --agent-dir is required unless LINGTAI_AGENT_DIR is set", file=sys.stderr)
        return 2
    report = diagnose(Path(agent_arg), Path(args.project_dir) if args.project_dir else None)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report.severity == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
