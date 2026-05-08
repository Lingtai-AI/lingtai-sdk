"""Avatar capability — spawn independent peer agents (分身).

Shallow (初生): Copy init.json to a new working dir, strip name, launch.
    The avatar gets the same LLM config + capabilities but no identity,
    no pad, no history.  A fresh life — but its own, not yours.

Deep (二重身): Copy the entire working dir (system/, codex/, exports/)
    plus init.json to a new dir, strip name + history, launch.
    The avatar is a doppelgänger — same character, pad, knowledge —
    but starts a fresh conversation.

Both modes launch `lingtai run <dir>` as a fully detached process.
The avatar is an independent life — its existence does not depend on yours.

Maintains an append-only ledger (delegates/ledger.jsonl) that records
every spawn event.

Usage:
    Agent(capabilities=["avatar"])
    # avatar(name="researcher")                    — shallow (初生)
    # avatar(name="clone", type="deep")            — deep (二重身)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t

# Avatar name doubles as its working-directory basename. Letters (any script,
# including CJK), digits, underscore, and hyphen — no path separators, no
# control chars, no dots. The structural chars are what make this dangerous;
# the script itself is the agent's choice.
_AVATAR_NAME_RE = re.compile(r"^[\w-]+$")  # \w is Unicode-aware in Py3 re
_AVATAR_NAME_MAX_LEN = 64

# Mission quality gate — minimum length below which we treat the mission as a
# probable accidental spawn unless the caller explicitly confirms.
_MISSION_MIN_CHARS = 20

# Suspicious tokens that indicate a debug/test placeholder mission. Compared
# case-insensitively against the trimmed mission (full match) and against the
# first whitespace-delimited token (prefix match like "test something").
_MISSION_SUSPICIOUS = {"test", "debug", "check", "tmp", "temp", "foo", "bar"}


def _mission_looks_unsafe(mission: str) -> tuple[bool, str]:
    """Heuristic mission-quality gate.

    Returns ``(unsafe, reason)``. Used to refuse accidental spawns where the
    mission field is empty, far too short, or matches a debug/test placeholder
    pattern. Caller can override with ``confirm=True``.
    """
    trimmed = (mission or "").strip()
    if not trimmed:
        return True, "mission is empty"
    if len(trimmed) < _MISSION_MIN_CHARS:
        return True, f"mission is very short ({len(trimmed)} chars)"
    lower = trimmed.lower()
    if lower in _MISSION_SUSPICIOUS or lower.startswith(
        tuple(f"{w} " for w in _MISSION_SUSPICIOUS)
    ):
        return True, "mission looks like a debug/test placeholder"
    return False, ""


if TYPE_CHECKING:
    from ...agent import Agent

PROVIDERS = {"providers": [], "default": "builtin"}

def get_description(lang: str = "en") -> str:
    return t(lang, "avatar.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["spawn", "rules"],
                "description": t(lang, "avatar.action"),
            },
            "name": {
                "type": "string",
                "description": t(lang, "avatar.name"),
            },
            "type": {
                "type": "string",
                "enum": ["shallow", "deep"],
                "description": t(lang, "avatar.type"),
            },
            "comment": {
                "type": "string",
                "description": t(lang, "avatar.comment"),
            },
            "rules_content": {
                "type": "string",
                "description": t(lang, "avatar.rules_content"),
            },
            "dry_run": {
                "type": "boolean",
                "description": t(lang, "avatar.dry_run"),
            },
            "confirm": {
                "type": "boolean",
                "description": t(lang, "avatar.confirm"),
            },
        },
        "allOf": [
            {
                "if": {
                    "not": {
                        "properties": {"action": {"const": "rules"}},
                        "required": ["action"],
                    },
                },
                "then": {"required": ["name"]},
            },
            {
                "if": {
                    "properties": {"action": {"const": "rules"}},
                    "required": ["action"],
                },
                "then": {"required": ["rules_content"]},
            },
        ],
    }



class AvatarManager:
    """Spawns avatar (分身) peer agents as detached processes.

    Each avatar gets its own working directory with init.json and is
    launched via `lingtai run`.  No in-process references — liveness
    is checked via the filesystem (handshake.is_alive).
    """

    def __init__(self, agent: "Agent"):
        self._agent = agent

    # ------------------------------------------------------------------
    # Handler
    # ------------------------------------------------------------------

    def handle(self, args: dict) -> dict:
        action = args.get("action", "spawn")
        if action == "rules":
            return self._rules(args)
        return self._spawn(args)

    # ------------------------------------------------------------------
    # Ledger (append-only JSONL log of avatar spawn events)
    # ------------------------------------------------------------------

    @property
    def _ledger_path(self) -> Path:
        return self._agent._working_dir / "delegates" / "ledger.jsonl"

    def _append_ledger(self, event: str, name: str, **fields) -> None:
        """Append a single event record to the ledger."""
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, "name": name, **fields}
        with open(self._ledger_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Core spawn
    # ------------------------------------------------------------------

    def _spawn(self, args: dict) -> dict:
        parent = self._agent
        reasoning = args.get("_reasoning")
        peer_name = args.get("name")
        avatar_type = args.get("type", "shallow")
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        if peer_name is None:
            return {"error": "name is required — pick a true name (真名) for the 他我 (e.g. 'researcher', '学者')"}

        if avatar_type not in ("shallow", "deep"):
            return {"error": "type must be 'shallow' or 'deep'"}

        # Name doubles as working-dir basename. Enforce a safe, single-segment
        # name so an LLM-chosen string cannot traverse, target absolute paths,
        # or nest avatars inside subfolders (which would desync path-identity
        # from the ledger and mail-routing layer).
        if (
            not isinstance(peer_name, str)
            or not peer_name
            or peer_name in (".", "..")
            or peer_name.startswith(".")
            or len(peer_name) > _AVATAR_NAME_MAX_LEN
            or not _AVATAR_NAME_RE.match(peer_name)
        ):
            return {
                "error": (
                    f"Invalid avatar name '{peer_name}': must be a bare directory "
                    f"name — letters (any script), digits, underscore, or hyphen; "
                    f"no slashes, dots, spaces, or leading '.'; 1-{_AVATAR_NAME_MAX_LEN} chars."
                )
            }

        # Mission-quality gate. The reasoning field becomes the avatar's first
        # prompt, so an empty / very-short / debug-placeholder mission almost
        # always means an accidental spawn (a real incident: an agent batched
        # avatar(spawn) into a parallel call with mission "test" and a process
        # was created). Refuse unless the caller explicitly passes confirm=True.
        # The dry-run path is exempt — its whole purpose is preview without
        # commitment, and forcing confirm=True there would defeat that.
        if not dry_run and not confirm:
            unsafe, reason = _mission_looks_unsafe(reasoning or "")
            if unsafe:
                preview_mission = (reasoning or "").strip()
                return {
                    "status": "confirmation_needed",
                    "warning": (
                        f"Mission appears short/test-like ({reason}). "
                        f"Pass confirm=true to proceed, or dry_run=true to preview. "
                        f"Each avatar(spawn) creates an independent process — "
                        f"double-check your reasoning field before retrying."
                    ),
                    "reason": reason,
                    "preview": {
                        "name": peer_name,
                        "type": avatar_type,
                        "mission": preview_mission,
                        "mission_chars": len(preview_mission),
                    },
                }

        # Check if this peer already exists and is live
        from lingtai_kernel.handshake import is_alive
        for record in self._read_ledger():
            if record.get("name") == peer_name:
                wd = record.get("working_dir", "")
                if wd and is_alive(wd):
                    return {
                        "status": "already_active",
                        "working_dir": wd,
                        "message": (
                            f"'{peer_name}' is already running. "
                            f"Use mail to communicate, or system intrinsic to manage lifecycle."
                        ),
                    }

        # Parent must have init.json
        parent_init_path = parent._working_dir / "init.json"
        if not parent_init_path.is_file():
            return {"error": "parent has no init.json — cannot spawn avatar"}

        try:
            parent_init = json.loads(parent_init_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"failed to read parent init.json: {e}"}

        # Dry-run short-circuit. Returns a preview of what would be created,
        # but performs NO filesystem mutation and NO process launch. We've
        # already validated name/type and confirmed parent has a usable
        # init.json, so the preview reflects what a real spawn would do.
        if dry_run:
            avatar_working_dir = parent._working_dir.parent / peer_name
            preview_mission = (reasoning or "").strip()
            unsafe, reason = _mission_looks_unsafe(reasoning or "")
            return {
                "status": "dry_run",
                "preview": {
                    "name": peer_name,
                    "type": avatar_type,
                    "working_dir": str(avatar_working_dir),
                    "address": avatar_working_dir.name,
                    "mission": preview_mission,
                    "mission_chars": len(preview_mission),
                    "mission_unsafe": unsafe,
                    "mission_reason": reason if unsafe else "",
                    "comment": args.get("comment", ""),
                },
                "message": "Dry run — no process spawned, no files written.",
            }

        # Working dir: sibling of parent, named after the avatar. Defense-in-depth
        # scope check — resolve and assert the target's parent equals the network
        # root, so even if peer_name validation is ever loosened, this still
        # prevents writing outside .lingtai/<siblings>/.
        avatar_working_dir = parent._working_dir.parent / peer_name
        network_root = parent._working_dir.parent.resolve()
        try:
            resolved = avatar_working_dir.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            return {"error": f"Cannot resolve avatar path: {e}"}
        if resolved.parent != network_root:
            return {
                "error": (
                    f"Avatar path '{avatar_working_dir}' escapes the network root "
                    f"'{network_root}' — rejected."
                )
            }
        if avatar_working_dir.exists():
            return {"error": f"Directory '{peer_name}' already exists. Choose another name."}

        # Prepare the avatar's working directory
        if avatar_type == "deep":
            self._prepare_deep(parent._working_dir, avatar_working_dir)
        else:
            avatar_working_dir.mkdir(parents=True, exist_ok=True)

        # Resolve relative file paths to absolute so avatar can find them
        for key in ("env_file", "covenant_file", "principle_file",
                    "substrate_file", "procedures_file", "comment_file"):
            val = parent_init.get(key)
            if val and not os.path.isabs(val):
                resolved = parent._working_dir / val
                if resolved.is_file():
                    parent_init[key] = str(resolved)

        # Inherit parent's venv_path so avatar can find the runtime
        if hasattr(parent, "_venv_path") and parent._venv_path:
            parent_init["venv_path"] = parent._venv_path

        # Clean stale signal files before launch
        for sig in (".suspend", ".sleep", ".interrupt"):
            sig_file = avatar_working_dir / sig
            if sig_file.is_file():
                sig_file.unlink(missing_ok=True)

        # Seed the avatar's first turn with a parent-identity prompt + the
        # caller's reasoning (task brief). Written to the avatar's `.prompt`
        # file — picked up by the kernel's signal-file watcher on first poll
        # and delivered as a one-shot system message (consumed-once via unlink).
        parent_name = parent.agent_name or parent._working_dir.name
        parent_address = parent._working_dir.name
        avatar_lang = parent_init.get("manifest", {}).get("language", "en")
        parent_prompt = t(
            avatar_lang, "avatar.parent_prompt",
            parent_name=parent_name,
            parent_address=parent_address,
        )
        first_prompt = parent_prompt
        if reasoning and reasoning.strip():
            first_prompt = f"{parent_prompt}\n\n{reasoning.strip()}"

        # Write avatar's init.json (modified copy of parent's).
        avatar_comment = args.get("comment", "")
        avatar_init = self._make_avatar_init(
            parent_init, peer_name, comment=avatar_comment,
            parent_working_dir=parent._working_dir,
        )
        (avatar_working_dir / "init.json").write_text(
            json.dumps(avatar_init, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        # Drop the spawn prompt as a `.prompt` signal file — the avatar's
        # kernel watcher consumes it on first poll and delivers it once.
        (avatar_working_dir / ".prompt").write_text(first_prompt, encoding="utf-8")

        # Launch as detached process and wait briefly for the child to either
        # write its handshake (.agent.heartbeat) or exit. If the child exits
        # before handshaking, the spawn failed — capture stderr, ledger the
        # failure, and return an error to the caller. Without this check the
        # avatar capability returns "ok" the instant Popen forks, even if the
        # child crashes 50ms later (e.g. invalid init.json), and the parent's
        # LLM has no idea anything went wrong.
        proc, stderr_path = self._launch(avatar_working_dir)
        pid = proc.pid

        boot_status, boot_error = self._wait_for_boot(
            avatar_working_dir, proc, stderr_path,
        )

        # Record in ledger — include boot status so post-mortem can distinguish
        # successful spawns from failed ones without re-checking the filesystem.
        ledger_extra = {"boot_status": boot_status}
        if boot_error:
            ledger_extra["boot_error"] = boot_error
        self._append_ledger(
            "avatar", peer_name,
            working_dir=avatar_working_dir.name,
            mission=reasoning or "",
            type=avatar_type,
            pid=pid,
            **ledger_extra,
        )

        if boot_status == "failed":
            return {
                "error": (
                    f"avatar {peer_name!r} failed to boot: {boot_error}. "
                    f"See {stderr_path} for details."
                ),
                "address": avatar_working_dir.name,
                "agent_name": peer_name,
                "pid": pid,
            }

        # Auto-distribute rules to all descendants (including newborn) — read from canonical system/rules.md
        parent_rules_md = parent._working_dir / "system" / "rules.md"
        if parent_rules_md.is_file():
            try:
                rules_content = parent_rules_md.read_text()
            except OSError:
                rules_content = ""
            if rules_content.strip():
                self._distribute_rules_to_descendants(rules_content, parent._working_dir)

        result = {
            "status": "ok",
            "address": avatar_working_dir.name,
            "agent_name": peer_name,
            "type": avatar_type,
            "pid": pid,
        }
        if boot_status == "slow":
            # Process is still alive but didn't finish handshaking in the
            # window — surface a warning so the caller knows to monitor it.
            result["warning"] = (
                f"avatar still booting after {self._BOOT_WAIT_SECS}s — "
                f"check .agent.heartbeat freshness before relying on it"
            )
        return result

    @classmethod
    def _wait_for_boot(
        cls, working_dir: Path, proc: subprocess.Popen, stderr_path: Path,
    ) -> tuple[str, str | None]:
        """Wait for the avatar to write .agent.heartbeat or exit.

        Returns (status, error_message):
            - ("ok", None)     — heartbeat appeared before timeout
            - ("failed", msg)  — process exited before handshaking
            - ("slow", None)   — neither happened in BOOT_WAIT_SECS; process
                                 is still alive, caller should monitor
        """
        heartbeat = working_dir / ".agent.heartbeat"
        deadline = time.monotonic() + cls._BOOT_WAIT_SECS
        while time.monotonic() < deadline:
            if heartbeat.is_file():
                return ("ok", None)
            rc = proc.poll()
            if rc is not None:
                # Child exited before writing heartbeat. Tail stderr (capped)
                # so the parent's LLM gets a useful, bounded error string.
                stderr_tail = ""
                try:
                    raw = stderr_path.read_bytes()
                    if len(raw) > 2000:
                        raw = b"...[truncated]...\n" + raw[-2000:]
                    stderr_tail = raw.decode("utf-8", errors="replace").strip()
                except OSError:
                    pass
                msg = f"process exited with code {rc}"
                if stderr_tail:
                    msg = f"{msg}: {stderr_tail}"
                return ("failed", msg)
            time.sleep(cls._BOOT_POLL_INTERVAL)
        return ("slow", None)

    # ------------------------------------------------------------------
    # Init.json construction
    # ------------------------------------------------------------------

    @staticmethod
    def _make_avatar_init(
        parent_init: dict, name: str, *,
        comment: str = "",
        parent_working_dir: "Path | None" = None,
    ) -> dict:
        """Build avatar's init.json from parent's, setting name.

        The spawn brief (parent identity + reasoning) is delivered out-of-band
        via a `.prompt` signal file dropped in the avatar's working dir by the
        caller — see ``_spawn``. Here we only blank the inherited prompt so the
        schema sees a present-but-empty field (no stale prompt carried over).

        Avatars inherit the parent's `manifest.preset.allowed` list verbatim.
        Entries are stored as path strings; if any are relative, they are
        re-rooted against ``parent_working_dir`` (if given) so the avatar's
        own working dir doesn't change their meaning.
        """
        init = json.loads(json.dumps(parent_init))  # deep copy
        init["manifest"]["agent_name"] = name
        # Blank inherited prompt — schema requires the field to exist, but the
        # avatar's actual first prompt arrives via the `.prompt` signal file.
        init["prompt"] = ""
        init.pop("prompt_file", None)
        # Avatar has no admin privileges
        init["manifest"]["admin"] = {}
        # Comment is not inherited — parent can set one explicitly for the avatar
        init["comment"] = comment
        init.pop("comment_file", None)
        # Brief is not inherited — avatars don't need life context
        init.pop("brief", None)
        init.pop("brief_file", None)
        # Addons (IMAP, Telegram) are not inherited — each agent must be
        # explicitly configured to avoid multiple agents polling the same account
        init.pop("addons", None)
        # Re-root any relative paths in preset.{default,active,allowed}
        # against the parent's working dir so they remain valid from the
        # avatar's different working directory. Absolute and ~-prefixed
        # entries pass through unchanged.
        if parent_working_dir is not None:
            preset_block = init["manifest"].get("preset")
            if isinstance(preset_block, dict):
                def _reroot(s: object) -> object:
                    if not isinstance(s, str) or not s:
                        return s
                    p = Path(s).expanduser()
                    if p.is_absolute():
                        return s
                    return str((Path(parent_working_dir) / p).resolve())
                for key in ("default", "active"):
                    if isinstance(preset_block.get(key), str):
                        preset_block[key] = _reroot(preset_block[key])
                allowed = preset_block.get("allowed")
                if isinstance(allowed, list):
                    preset_block["allowed"] = [_reroot(x) for x in allowed]

        # Avatars always spawn on the parent's DEFAULT preset, not its
        # currently-active one. This keeps the avatar's notion of 'default'
        # well-defined as a peer in the network — auto-fallback targets a
        # stable home base, not whatever transient preset the parent happened
        # to be on at spawn time.
        #
        # Strip materialized llm + capabilities unconditionally so the avatar's
        # _read_init re-materializes from the (possibly-rewritten) active on
        # first boot. Letting the existing materialization path do its job
        # is cleaner than manually re-substituting here.
        preset_block = init["manifest"].get("preset")
        if isinstance(preset_block, dict) and preset_block.get("default"):
            preset_block["active"] = preset_block["default"]
            init["manifest"].pop("llm", None)
            init["manifest"].pop("capabilities", None)

        return init

    # ------------------------------------------------------------------
    # Deep copy — 二重身
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_deep(src: Path, dst: Path) -> None:
        """Copy identity + knowledge from parent, excluding runtime state.

        Guarded: dst must be a direct sibling of src (same parent). This mirrors
        the path-scope assertion in _spawn so the rmtree() calls below cannot
        reach outside the network root even if _prepare_deep is ever called
        from a future, less-validated path.
        """
        src_resolved = src.resolve(strict=False)
        dst_resolved = dst.resolve(strict=False)
        if dst_resolved.parent != src_resolved.parent:
            raise ValueError(
                f"_prepare_deep refused: dst '{dst}' is not a sibling of src '{src}' "
                f"(parents differ: {dst_resolved.parent} vs {src_resolved.parent})"
            )
        dst.mkdir(parents=True, exist_ok=True)

        # system/ (character, pad, covenant, etc.)
        src_system = src / "system"
        if src_system.is_dir():
            dst_system = dst / "system"
            if dst_system.exists():
                shutil.rmtree(dst_system)
            shutil.copytree(src_system, dst_system)

        # codex/
        src_lib = src / "codex"
        if src_lib.is_dir():
            dst_lib = dst / "codex"
            if dst_lib.exists():
                shutil.rmtree(dst_lib)
            shutil.copytree(src_lib, dst_lib)

        # exports/
        src_exports = src / "exports"
        if src_exports.is_dir():
            dst_exports = dst / "exports"
            if dst_exports.exists():
                shutil.rmtree(dst_exports)
            shutil.copytree(src_exports, dst_exports)

        # combo.json
        src_combo = src / "combo.json"
        if src_combo.is_file():
            shutil.copy2(src_combo, dst / "combo.json")

        # Explicitly do NOT copy: history/, mailbox/, delegates/,
        # .agent.json, .agent.heartbeat, logs/

    # ------------------------------------------------------------------
    # Process launch
    # ------------------------------------------------------------------

    # Boot verification — how long to wait for the child to write .agent.heartbeat
    # before we conclude it crashed. Healthy boots finish well under 2s on local
    # disk; 5s is generous enough for slow systems to still pass.
    _BOOT_WAIT_SECS = 5.0
    _BOOT_POLL_INTERVAL = 0.1

    @staticmethod
    def _launch(working_dir: Path) -> tuple[subprocess.Popen, Path]:
        """Launch `lingtai run <dir>` as a fully detached process.

        Captures stderr to ``logs/spawn.stderr`` so a child that exits before
        writing its handshake leaves a usable diagnostic behind. Returns the
        Popen handle (so callers can poll for early exit) plus the stderr path.
        """
        from lingtai.venv_resolve import resolve_venv, venv_python

        # Resolve Python from avatar's init.json → global runtime
        init_path = working_dir / "init.json"
        init_data = None
        if init_path.is_file():
            try:
                init_data = json.loads(init_path.read_text())
            except (ValueError, OSError):
                pass
        venv_dir = resolve_venv(init_data)
        python = venv_python(venv_dir)
        cmd = [python, "-m", "lingtai", "run", str(working_dir)]

        # Ensure logs/ exists for stderr capture; the kernel also creates this
        # on boot, but we need it before the child has run.
        logs_dir = working_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = logs_dir / "spawn.stderr"
        stderr_fh = stderr_path.open("wb")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
                start_new_session=True,
            )
        finally:
            # Popen dups the fd; we can close ours immediately. The child
            # keeps writing through its inherited copy.
            stderr_fh.close()
        return proc, stderr_path

    # ------------------------------------------------------------------
    # Ledger reading
    # ------------------------------------------------------------------

    def _read_ledger(self) -> list[dict]:
        """Read all ledger records."""
        if not self._ledger_path.is_file():
            return []
        records = []
        for line in self._ledger_path.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    # ------------------------------------------------------------------
    # Rules distribution
    # ------------------------------------------------------------------

    def _rules(self, args: dict) -> dict:
        """Set rules and distribute via .rules signal files to self + descendants.

        Self and descendants are handled uniformly: a `.rules` signal file is
        written to every agent directory in the subtree (including the caller's
        own). Each agent's heartbeat loop (`_check_rules_file`) then consumes
        the signal, diffs it against `system/rules.md`, and refreshes its own
        system prompt if the content changed. The caller's own prompt refresh
        happens on its next heartbeat tick (within ~1s).
        """
        parent = self._agent
        content = args.get("rules_content", "").strip()
        if not content:
            return {"error": "rules_content is required"}

        # Admin check: at least one admin privilege must be truthy
        admin = getattr(parent, "_admin", {}) or {}
        if not any(admin.values()):
            return {"error": "Not authorized — admin privilege required to set rules"}

        # Write .rules signal to self — heartbeat will consume and persist
        try:
            (parent._working_dir / ".rules").write_text(content)
        except OSError as e:
            return {"error": f"failed to write .rules signal: {e}"}

        # Write .rules signal file to all descendants
        distributed = self._distribute_rules_to_descendants(content, parent._working_dir)

        # Include self in the reported distribution for transparency
        return {
            "status": "ok",
            "message": f"Rules set; signal written to self and {len(distributed)} descendant(s).",
            "distributed_to": [parent._working_dir.name] + distributed,
        }

    @staticmethod
    def _walk_avatar_tree(root: Path) -> list[Path]:
        """Recursively collect all descendant working-dir Paths from ledger files.

        Ledger entries store relative names (e.g. 'researcher'); we resolve each
        against the *parent agent's parent directory* since avatars live as
        siblings in .lingtai/. Returns absolute Paths of live descendant dirs.
        """
        from lingtai_kernel.handshake import resolve_address

        visited: set[str] = {str(Path(root))}
        queue: list[Path] = [Path(root)]
        result: list[Path] = []

        while queue:
            current = queue.pop(0)
            ledger_path = current / "delegates" / "ledger.jsonl"
            if not ledger_path.is_file():
                continue
            try:
                lines = ledger_path.read_text().splitlines()
            except OSError:
                continue
            # Siblings of `current` live in current.parent
            base_dir = current.parent
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") != "avatar":
                    continue
                wd = record.get("working_dir", "")
                if not wd:
                    continue
                # Resolve relative name to absolute Path
                child_dir = resolve_address(wd, base_dir)
                key = str(child_dir)
                if key in visited:
                    continue
                if not child_dir.is_dir():
                    continue  # dead avatar, directory gone
                visited.add(key)
                result.append(child_dir)
                queue.append(child_dir)

        return result

    def _distribute_rules_to_descendants(self, content: str, root: Path) -> list[str]:
        """Write `.rules` signal file to every descendant in the avatar tree.

        Returns the list of descendant directory names that were successfully written.
        Failures are silently swallowed (caller has no visibility), consistent with
        the best-effort, idempotent design of signal files.
        """
        distributed: list[str] = []
        for child_dir in self._walk_avatar_tree(root):
            try:
                (child_dir / ".rules").write_text(content)
                distributed.append(child_dir.name)
            except OSError:
                pass
        return distributed


def setup(agent: "Agent", **kwargs) -> AvatarManager:
    """Set up the avatar capability on an agent."""
    lang = agent._config.language
    mgr = AvatarManager(agent)
    schema = get_schema(lang)
    agent.add_tool("avatar", schema=schema, handler=mgr.handle, description=get_description(lang))
    return mgr
