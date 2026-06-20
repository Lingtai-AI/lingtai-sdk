"""Schema — tool registration (get_description, get_schema)."""
from __future__ import annotations


def get_description(lang: str = "en") -> str:
    from ...i18n import t
    return t(lang, "system_tool.description")


def get_schema(lang: str = "en") -> dict:
    from ...i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["refresh", "sleep", "lull", "interrupt", "suspend", "cpr", "clear", "nirvana", "presets", "dismiss", "notification", "summarize"],
                "description": t(lang, "system_tool.action_description"),
            },
            "reason": {
                "type": "string",
                "description": t(lang, "system_tool.reason_description"),
            },
            "address": {
                "type": "string",
                "description": t(lang, "system_tool.address_description"),
            },
            "preset": {
                "type": "string",
                "description": t(lang, "system_tool.preset_description"),
            },
            "revert_preset": {
                "type": "boolean",
                "description": t(lang, "system_tool.revert_preset_description"),
            },
            "channel": {
                "type": "string",
                "description": t(lang, "system_tool.channel_description"),
            },
            "force": {
                "type": "boolean",
                "description": t(lang, "system_tool.force_description"),
            },
            "event_id": {
                "type": "string",
                "description": t(lang, "system_tool.event_id_description"),
            },
            "ref_id": {
                "type": "string",
                "description": t(lang, "system_tool.ref_id_description"),
            },
            "items": {
                "type": "array",
                "description": t(lang, "system_tool.items_description"),
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_call_id": {
                            "type": "string",
                            "description": "The id of the prior tool-result block to summarize.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Your agent-authored summary of that tool result.",
                        },
                    },
                    "required": ["tool_call_id", "summary"],
                },
            },
        },
        "required": ["action"],
    }
