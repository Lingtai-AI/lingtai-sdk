"""Schema and description for the email intrinsic tool."""
from __future__ import annotations

from ...i18n import t
from .primitives import mode_field


def get_description(lang: str = "en") -> str:
    return t(lang, "email.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "send", "check", "read", "dismiss", "reply", "reply_all",
                    "search", "archive", "delete",
                    "contacts", "add_contact", "remove_contact", "edit_contact",
                ],
                "description": t(lang, "email.action"),
            },
            "address": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": t(lang, "email.address"),
            },
            "cc": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.cc"),
            },
            "bcc": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.bcc"),
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.attachments"),
            },
            "subject": {"type": "string", "description": t(lang, "email.subject")},
            "message": {"type": "string", "description": t(lang, "email.message")},
            "email_id": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.email_id"),
            },
            "n": {
                "type": "integer",
                "description": t(lang, "email.n"),
                "default": 10,
            },
            "query": {
                "type": "string",
                "description": t(lang, "email.query"),
            },
            "folder": {
                "type": "string",
                "enum": ["inbox", "sent", "archive"],
                "description": t(lang, "email.folder"),
            },
            "delay": {
                "type": "integer",
                "description": t(lang, "email.delay"),
            },
            "mode": mode_field(lang),
            "type": {
                "type": "string",
                "enum": ["normal"],
                "description": t(lang, "email.type"),
            },
            "name": {
                "type": "string",
                "description": t(lang, "email.name"),
            },
            "note": {
                "type": "string",
                "description": t(lang, "email.note"),
            },
            "filter": {
                "type": "object",
                "description": t(lang, "email.filter"),
                "properties": {
                    "sort": {
                        "type": "string",
                        "enum": ["newest", "oldest"],
                        "description": t(lang, "email.filter_sort"),
                    },
                    "from": {
                        "type": "string",
                        "description": t(lang, "email.filter_from"),
                    },
                    "subject": {
                        "type": "string",
                        "description": t(lang, "email.filter_subject"),
                    },
                    "contains": {
                        "type": "string",
                        "description": t(lang, "email.filter_contains"),
                    },
                    "after": {
                        "type": "string",
                        "description": t(lang, "email.filter_after"),
                    },
                    "before": {
                        "type": "string",
                        "description": t(lang, "email.filter_before"),
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": t(lang, "email.filter_unread_only"),
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": t(lang, "email.filter_has_attachments"),
                    },
                    "truncate": {
                        "type": "integer",
                        "description": t(lang, "email.filter_truncate"),
                        "default": 500,
                    },
                },
            },
        },
        "required": [],
    }
