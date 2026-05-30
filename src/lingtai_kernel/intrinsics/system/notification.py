"""Notification management for ``system(action='dismiss')``.

Under the `.notification/` filesystem model, notification producers write
one file per channel.  This module provides the agent-facing generic
dismiss verb: clear one channel's notification surface, while preserving
producer-specific verbs (for example email read/dismiss) for stateful
producers.
"""
from __future__ import annotations


def _dismiss(agent, args: dict) -> dict:
    """Dismiss a single notification channel, with legacy ``ids`` soak."""
    channel = args.get("channel")
    legacy_ids = args.get("ids")

    if channel is None:
        if legacy_ids is not None:
            agent._log("system_dismiss_legacy_ids_ignored", ids=legacy_ids)
            return {
                "status": "ok",
                "cleared": False,
                "note": "legacy ids ignored; pass channel=<name> instead",
            }
        agent._log("system_dismiss_missing_channel")
        return {
            "status": "error",
            "reason": "missing_channel",
            "message": "system(action='dismiss') requires channel=<name>.",
        }

    from ...notifications import dismiss_channel

    return dismiss_channel(
        agent,
        channel,
        invoked_by=args.get("_invoked_by", "system"),
        force=bool(args.get("force", False)),
        reason=args.get("reason"),
    )
