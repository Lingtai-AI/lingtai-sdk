"""Nudge: warn the agent when a newer lingtai wheel is installed on disk
than the version this Python process started with.

``lingtai.__version__`` is computed from ``importlib.metadata`` at import
time and frozen in the module dict for the life of the process. Calling
``importlib.metadata.version("lingtai")`` again rescans dist-info on
disk — so a wheel installed mid-run by the TUI auto-upgrader, or a
manual ``pip install -U lingtai``, is visible immediately. When the
two disagree, the running interpreter is stale: only a full process
relaunch (``system(action='refresh')``) picks up the new code, because
Python caches imported modules in ``sys.modules`` and there is no
reliable way to reload them mid-process.

Throttled to one filesystem probe per 60 seconds and deduped per
installed version, so a long-running agent gets exactly one nudge per
upgrade rather than a flood every heartbeat tick. If the on-disk
version returns to match running (downgrade, or stale state from a
prior process), the previously-emitted entry is cleared so the agent
isn't told to refresh into the version it's already on.
"""
from __future__ import annotations
import time


_INTERVAL_SECONDS = 60.0
_KIND = "kernel_version"


def check(agent) -> None:
    state = _state(agent)
    now = time.time()
    if now - state.get("last_probe_ts", 0.0) < _INTERVAL_SECONDS:
        return
    state["last_probe_ts"] = now

    from importlib.metadata import version as pkg_version, PackageNotFoundError
    from . import upsert, remove
    import lingtai

    running = getattr(lingtai, "__version__", None)
    if not running:
        return
    try:
        installed = pkg_version("lingtai")
    except PackageNotFoundError:
        return

    if installed == running:
        if state.get("emitted_for_version") is not None:
            remove(agent, _KIND)
            state["emitted_for_version"] = None
        return

    if state.get("emitted_for_version") == installed:
        return

    body = {
        "title": f"Kernel upgrade available: {running} → {installed}",
        "running": running,
        "installed": installed,
        "detail": (
            f"This process is running lingtai {running}, but lingtai "
            f"{installed} is now installed on disk. Call "
            f"system(action='refresh', reason='pick up lingtai "
            f"{installed}') when convenient to relaunch into the new "
            f"version. No urgency — finish the current task first."
        ),
        "suggested_action": "system(action='refresh')",
    }
    try:
        upsert(agent, _KIND, body)
        state["emitted_for_version"] = installed
        agent._log(
            "nudge_emitted",
            kind=_KIND,
            running=running,
            installed=installed,
        )
    except Exception as e:
        agent._log("nudge_emit_error", kind=_KIND, error=str(e)[:200])


def _state(agent) -> dict:
    s = getattr(agent, "_nudge_kernel_version_state", None)
    if s is None:
        s = {}
        agent._nudge_kernel_version_state = s
    return s
