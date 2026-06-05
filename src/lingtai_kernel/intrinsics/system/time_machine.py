"""Filesystem Time Machine actions — snapshot, snapshots, rollback_preview, rollback.

These wrap the git primitives on ``WorkingDir`` (``workdir.py``) and form the
agent-callable rollback surface. They are the *filesystem* counterpart to the
psyche conversation snapshots — this is "roll the working directory back to a
prior commit", not "roll the conversation back".

Safety is enforced in ``WorkingDir`` (refuse dirty tree without force, refuse
invalid refs, tag a safety ref before reset, never delete ``.git``); these
handlers just translate the action surface and log.
"""
from __future__ import annotations


def _wd(agent):
    """Return the agent's WorkingDir, constructing one if not already attached."""
    wd = getattr(agent, "_workdir", None)
    if wd is not None:
        return wd
    from ...workdir import WorkingDir
    return WorkingDir(agent._working_dir)


def _snapshot(agent, args: dict) -> dict:
    """Capture the whole working directory as a git commit (no-op if clean)."""
    commit = _wd(agent).snapshot()
    agent._log("system_snapshot", hash=commit)
    if commit is None:
        return {"status": "ok", "hash": None, "message": "nothing changed — no snapshot taken"}
    return {"status": "ok", "hash": commit}


def _snapshots(agent, args: dict) -> dict:
    """List recent snapshots (commits), newest first."""
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    snaps = _wd(agent).snapshot_list(limit=limit)
    return {"status": "ok", "snapshots": snaps}


def _rollback_preview(agent, args: dict) -> dict:
    """Describe what rolling back to ``ref`` would change. Read-only."""
    ref = (args.get("ref") or "").strip()
    if not ref:
        return {"status": "error", "reason": "missing_ref",
                "message": "rollback_preview requires a `ref` (snapshot hash)."}
    preview = _wd(agent).rollback_preview(ref)
    if preview.get("error"):
        return {"status": "error", "reason": "invalid_ref", "message": preview["message"]}
    agent._log("system_rollback_preview", ref=ref, dirty=preview["dirty"])
    return {"status": "ok", **{k: v for k, v in preview.items() if k != "error"}}


def _rollback(agent, args: dict) -> dict:
    """Apply a rollback to ``ref`` via ``WorkingDir.rollback_apply`` (guarded)."""
    ref = (args.get("ref") or "").strip()
    if not ref:
        return {"status": "error", "reason": "missing_ref",
                "message": "rollback requires a `ref` (snapshot hash). "
                           "Preview first with action='rollback_preview'."}
    force = bool(args.get("force", False))
    result = _wd(agent).rollback_apply(ref, force=force)
    agent._log(
        "system_rollback",
        ref=ref,
        force=force,
        status=result.get("status"),
        reason=result.get("reason"),
        safety_ref=result.get("safety_ref"),
    )
    return result
