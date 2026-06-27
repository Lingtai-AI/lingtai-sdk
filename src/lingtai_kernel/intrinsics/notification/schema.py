"""Schema — tool registration for the standalone ``notification`` tool.

The notification tool exposes only the notification-facing verbs: ``check``
plus the three atomic dismiss verbs (``dismiss_channel``, ``dismiss_event``,
``dismiss_ref``).  ``summarize`` is *not* here — it remains a ``system`` action.

Where a parameter is shared in spirit with the ``system`` tool (``channel``,
``force``, ``event_id``, ``ref_id``, ``reason``), a notification-owned i18n
string is used so the notification tool documents its own behavior.
"""
from __future__ import annotations

LARGE_RESULT_DISMISS_ACTION_NOTE = (
    "large_tool_result reminders can be dismissed as an escape hatch "
    "(for example, stale pre-molt refs). Prefer system(action=summarize) "
    "when the result is still accessible: summarize records a compact "
    "runtime-history replacement and auto-clears the reminder. Dismissal only "
    "clears the notification surface; the original result stays in chat "
    "history and events.jsonl. See notification-manual."
)

LARGE_RESULT_FORCE_NOTE = (
    "Does not affect large_tool_result reminder dismissal; that escape hatch "
    "is always allowed and clears only the reminder surface."
)


def get_description(lang: str = "en") -> str:
    from ...i18n import t
    return t(lang, "notification_tool.description")


def get_schema(lang: str = "en") -> dict:
    from ...i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["check", "dismiss_channel", "dismiss_event", "dismiss_ref"],
                "description": t(lang, "notification_tool.action_description") + "\n\n" + LARGE_RESULT_DISMISS_ACTION_NOTE,
            },
            "channel": {
                "type": "string",
                "description": t(lang, "notification_tool.channel_description"),
            },
            "force": {
                "type": "boolean",
                "description": t(lang, "notification_tool.force_description") + " " + LARGE_RESULT_FORCE_NOTE,
            },
            "event_id": {
                "type": "string",
                "description": t(lang, "notification_tool.event_id_description"),
            },
            "ref_id": {
                "type": "string",
                "description": t(lang, "notification_tool.ref_id_description"),
            },
            "reason": {
                "type": "string",
                "description": t(lang, "notification_tool.reason_description"),
            },
        },
        "required": ["action"],
    }
