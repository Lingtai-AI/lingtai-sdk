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
                "enum": ["refresh", "sleep", "lull", "interrupt", "suspend", "cpr", "clear", "nirvana", "presets", "summarize"],
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
            "rebuild_context": {
                "type": "boolean",
                "description": t(lang, "system_tool.rebuild_context_description"),
            },
            "rebuild_only": {
                "type": "boolean",
                "description": "For action='summarize': request a one-shot provider-context rebuild without summarizing any items. Use with no items.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Alias for rebuild_only for action='summarize'; performs no compression and only requests rebuild.",
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
