"""Per-account poller lockfile for the WeChat addon.

iLink's getUpdates is a single-consumer long-poll: when two processes hold
the same bot_token and both call getUpdates, each call may receive a
different subset of messages, and there is no way for either consumer to
know it is racing. The practical symptom is "inbound messages appear flaky"
— see GH issue #83. This module prevents that by taking an exclusive
fcntl.flock on a per-account lockfile in the user's runtime directory.

The lock key hashes the bot_token (which is the only stable identifier of
the iLink account from the addon's perspective). The lockfile path is
deterministic across processes/working-dirs on the same machine, so a
second poller for the same account on the same host is reliably refused.

Platform note: this module is POSIX-only. ``acquire()`` raises
``UnsupportedPlatformError`` if ``fcntl`` is unavailable (e.g. on Windows)
rather than silently no-opping, since a silent no-op would leave issue #83
unresolved while pretending the lock had been taken.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class PollerLockBusy(RuntimeError):
    """Raised when another lingtai-wechat poller already holds this account."""


class UnsupportedPlatformError(RuntimeError):
    """Raised when the poller lock cannot be implemented on this OS."""


def _lock_dir() -> Path:
    """Where lockfiles live. ~/.lingtai-wechat/locks/ on POSIX."""
    base = Path.home() / ".lingtai-wechat" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _account_key(bot_token: str) -> str:
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]


def lock_path(bot_token: str) -> Path:
    return _lock_dir() / f"poller-{_account_key(bot_token)}.lock"


class AccountLock:
    """fcntl-based exclusive lock per iLink account.

    Held for the lifetime of the poller. Releases automatically when the
    process exits (kernel drops the flock), so a hard kill leaves no stale
    state requiring cleanup.
    """

    def __init__(self, bot_token: str) -> None:
        self._path = lock_path(bot_token)
        self._fh: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        """Take the exclusive lock.

        Raises:
            PollerLockBusy: if another process already holds the lock.
            UnsupportedPlatformError: if ``fcntl`` is not available (Windows).
        """
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover — non-POSIX (Windows)
            raise UnsupportedPlatformError(
                "lingtai-wechat's poller lock requires fcntl (POSIX). "
                "Running on this platform without a lock would silently "
                "re-introduce the duplicate-poller race (GH #83). If you "
                "need Windows support, please open an issue."
            ) from exc

        # Open without truncating: a losing contender used to wipe the
        # holder's PID entry between holder-write and contender-read, which
        # made the PollerLockBusy diagnostic unreliable. Create-if-missing
        # via os.open with O_RDWR|O_CREAT, then wrap in a Python file.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            existing_pid = _read_existing_pid_fh(fh)
            fh.close()
            pid_str = existing_pid or "unknown"
            # Concrete remediation hints — after an upgrade, the most common
            # reason this fires is that another LingTai project still has
            # its old lingtai-wechat MCP running and we can't tell the user
            # which project it belongs to from this side of the lock.
            remediation: list[str] = []
            if existing_pid and existing_pid.isdigit():
                remediation.append(
                    f"  Inspect the holder:  ps -p {existing_pid} -o pid,command"
                )
                remediation.append(
                    f"  Find its workdir:    lsof -p {existing_pid} 2>/dev/null | grep cwd"
                )
                remediation.append(
                    f"  Stop it gracefully:  kill -TERM {existing_pid}"
                )
            else:
                remediation.append(
                    "  Find pollers:   pgrep -af 'python.*lingtai_wechat'"
                )
                remediation.append(
                    "  Lockfile is held but no PID recorded — most likely a "
                    "pre-upgrade poller that predates the lockfile. Stop it "
                    "from the project that launched it."
                )
            raise PollerLockBusy(
                f"Another lingtai-wechat poller is already running for this "
                f"iLink account.\n"
                f"  Lockfile:    {self._path}\n"
                f"  Holder PID:  {pid_str}\n"
                f"Stop the other poller before starting this one:\n"
                + "\n".join(remediation)
                + "\n(See lingtai-wechat README → Troubleshooting → "
                "\"multiple pollers after upgrade\".)"
            ) from exc

        # Only write the PID *after* the lock is acquired, so contenders
        # never observe a half-truncated empty file.
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        log.info("Acquired WeChat poller lock for account at %s", self._path)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        # Leave the lockfile on disk (its presence + flock state is what
        # matters); removing it would race with a concurrent acquire().


def _read_existing_pid_fh(fh: IO[str]) -> str | None:
    """Read PID from an already-open lockfile handle (no re-open race)."""
    try:
        fh.seek(0)
        return fh.read().strip() or None
    except OSError:
        return None
