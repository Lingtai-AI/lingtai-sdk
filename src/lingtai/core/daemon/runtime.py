"""Daemon backend runtime primitives.

Small, behavior-preserving helpers extracted from ``daemon/__init__.py`` so
the backend runners (LingTai in-process + the CLI backends) stop re-declaring
the same subprocess-cleanup, stdout-deadline, cancellation/timeout, and stderr
draining logic inline.

Everything here is package-internal by location. ``daemon/__init__.py`` imports
these under their historical private names (``_kill_process_group`` etc.) so
existing tests and local monkeypatches that target those names keep working.

This module depends only on the standard library; ``DaemonRunDir`` is referenced
under ``TYPE_CHECKING`` to avoid an import cycle.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .run_dir import DaemonRunDir


def kill_process_group(
    proc: subprocess.Popen,
    *,
    term_timeout: float = 5.0,
    kill_timeout: float = 3.0,
) -> None:
    """Terminate the entire process group for *proc*, then force-kill if needed.

    Requires *proc* to have been started with ``start_new_session=True`` so
    that its PGID equals its own PID.  Sends SIGTERM to the group, waits up
    to ``term_timeout`` seconds, then escalates to SIGKILL for any survivors.

    Uses ``proc.pid`` directly as the PGID (since ``start_new_session=True``
    guarantees PGID == PID) to avoid a ``getpgid`` round-trip that could
    race with PID recycling.

    Silently ignores ``ProcessLookupError`` (process already dead) and
    ``OSError`` (permission denied on already-dead group).
    """
    # start_new_session=True guarantees pgid == pid
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired:
            pass


# Sentinel placed on the stdout-reader queue when the background reader
# thread observes EOF on the subprocess pipe. The consumer treats this as
# "no more lines will ever arrive — stop draining."
_STDOUT_EOF = object()


def iter_stdout_with_deadline(
    proc: subprocess.Popen,
    deadline: float,
    thread_name: str,
):
    """Yield stdout lines from *proc* until EOF, deadline, or process exit.

    The fundamental problem this solves: ``for line in proc.stdout`` blocks
    the caller's thread until the subprocess writes a newline. If the
    resumed CLI hangs without producing output, the caller can never
    observe the deadline. We work around it by pushing the blocking
    read onto a small daemon thread that drops each line into a queue,
    while the caller pulls from the queue with ``timeout=remaining``.

    Yields raw lines (with trailing ``\\n`` preserved, matching the
    original iterator semantics). Stops iterating when:
      - the reader thread reports EOF (sentinel arrives), OR
      - ``time.monotonic() >= deadline`` (caller is expected to
        ``kill_process_group`` after handling timeout — we do NOT do
        it here so the worker can record timeout state first).

    The reader thread is a daemon thread (won't block process exit) and
    is left orphaned if the deadline fires — it will exit naturally once
    the subprocess is killed and its pipe closes.
    """
    q: "queue.Queue[object]" = queue.Queue(maxsize=1024)

    def _reader():
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                q.put(raw_line)
        except (ValueError, OSError):
            # Pipe closed mid-read (e.g. after kill_process_group). Treat
            # as EOF — the consumer either already noticed the timeout or
            # is about to.
            pass
        finally:
            q.put(_STDOUT_EOF)

    reader = threading.Thread(target=_reader, daemon=True, name=thread_name)
    reader.start()

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return  # caller handles timeout (kill + mark)
        try:
            item = q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue  # re-check deadline
        if item is _STDOUT_EOF:
            return
        yield item


def mark_cancelled_or_timeout(
    run_dir: "DaemonRunDir",
    timeout_event: threading.Event | None,
) -> str:
    """Mark *run_dir* terminal for a cancellation/timeout and return ``"[cancelled]"``.

    If *timeout_event* is set, the run was stopped by the watchdog, so mark
    it as a timeout; otherwise it was a manual reclaim/shutdown, so mark it
    cancelled. ``timeout_event`` may be ``None`` (direct-call tests), which
    defaults to cancelled semantics.

    Returns the literal ``"[cancelled]"`` sentinel string the backend runners
    hand back to the caller — model-visible, do not change.
    """
    if timeout_event is not None and timeout_event.is_set():
        run_dir.mark_timeout()
    else:
        run_dir.mark_cancelled()
    return "[cancelled]"


class StderrDrain:
    """Handle for a background stderr-draining thread.

    Holds the live ``lines`` list the drainer appends to (callers that need
    the full captured stderr read it directly), and exposes ``join`` to wait
    for the drainer in a ``finally`` block and ``tail`` to render the trailing
    diagnostic lines for failure messages.
    """

    def __init__(self, thread: threading.Thread, lines: list[str]) -> None:
        self._thread = thread
        self.lines = lines

    def join(self, timeout: float = 2.0) -> None:
        """Give the drainer a moment to finish reading before the pipe closes."""
        self._thread.join(timeout=timeout)

    def tail(self, n: int = 20) -> str:
        """Return the last *n* captured stderr lines joined by newlines (or "")."""
        return "\n".join(self.lines[-n:]) if self.lines else ""


def spawn_stderr_drainer(
    proc: subprocess.Popen,
    run_dir: "DaemonRunDir",
    *,
    thread_name: str,
) -> StderrDrain:
    """Start a daemon thread that drains *proc*'s stderr into a ``StderrDrain``.

    Preserves the existing per-backend behavior exactly: blank stripped lines
    are ignored, each non-blank stripped line is appended to the captured list
    and best-effort mirrored to ``run_dir.record_cli_output(..., stream="stderr")``
    with any recording exception swallowed. The thread is a daemon thread with
    the caller-supplied *thread_name* and is started immediately.
    """
    lines: list[str] = []

    def _drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            lines.append(stripped)
            try:
                run_dir.record_cli_output(stripped, stream="stderr")
            except Exception:
                pass

    thread = threading.Thread(target=_drain, daemon=True, name=thread_name)
    thread.start()
    return StderrDrain(thread, lines)
