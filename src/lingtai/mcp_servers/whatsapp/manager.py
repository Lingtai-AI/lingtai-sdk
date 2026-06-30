"""WhatsAppManager — tool dispatch + local filesystem persistence."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .client import WhatsAppClient
from .redaction import redact_account
from .webhook import extract_events
from .. import _identity, _skill


def _load_notification_header_template() -> str:
    return resources.files(__package__).joinpath("notification_header.md").read_text(encoding="utf-8")


_NOTIFICATION_HEADER_TEMPLATE = _load_notification_header_template()

# Bundled usage manual (skill format) — SKILL.md ships in this package folder.
# action='manual' reads the full body; the YAML frontmatter name/description are
# injected into the tool schema as a progressive-disclosure catalog entry.
_SKILL_NAME = "whatsapp-mcp-manual"
_SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH = _skill.load_skill(__package__)

_CS_WINDOW_NOTE = (
    "WhatsApp Cloud API allows free-form business replies only inside the "
    "24-hour customer-service window. Outside that window use an approved "
    "message template."
)

ACTIONS = [
    "send", "check", "read", "reply", "search", "react", "contacts", "add_contact",
    "remove_contact", "templates", "accounts", "status", "manual",
]

DESCRIPTION = "WhatsApp Cloud API client for LingTai. Official Meta API only; no WhatsApp Web bridge."

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ACTIONS,
            "description": _skill.manual_action_description(_SKILL_FRONTMATTER, _SKILL_NAME),
        },
        "account": {"type": "string"},
        "to": {"type": "string", "description": "WhatsApp wa_id recipient"},
        "wa_id": {"type": "string"},
        "message_id": {"type": "string", "description": "compound account:wa_id:wamid id"},
        "text": {"type": "string"},
        "template": {"type": "object"},
        "media": {"type": "object"},
        "emoji": {"type": "string"},
        "query": {"type": "string"},
        "limit": {"type": "integer", "default": 10},
        "name": {"type": "string"},
        "mark_read": {"type": "boolean", "default": True},
        "preview_url": {"type": "boolean"},
    },
    "required": ["action"],
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WhatsAppManager:
    def __init__(self, *, accounts_config: list[dict[str, Any]], working_dir: Path, on_inbound: Callable[[dict[str, Any]], None] | None = None, config_source: str | None = None) -> None:
        self.accounts = {a.get("alias") or "default": dict(a) for a in accounts_config}
        if not self.accounts:
            raise ValueError("config must contain at least one WhatsApp account")
        self.working_dir = Path(working_dir)
        self.root = self.working_dir / "whatsapp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.on_inbound = on_inbound
        self.config_source = config_source
        self._last_verified_at = _utcnow()
        self._contacts_lock = threading.Lock()

    def _account_alias(self, alias: str | None) -> str:
        if alias:
            if alias not in self.accounts:
                raise ValueError(f"unknown WhatsApp account: {alias}")
            return alias
        return next(iter(self.accounts))

    def default_account(self) -> dict[str, Any]:
        return self.accounts[self._account_alias(None)]

    def account_alias_for_phone_number_id(self, phone_number_id: str | None) -> str:
        if phone_number_id:
            for alias, account in self.accounts.items():
                if str(account.get("phone_number_id") or "") == str(phone_number_id):
                    return alias
        return self._account_alias(None)

    def match_account_alias_for_phone_number_id(self, phone_number_id: str | None) -> str | None:
        if not phone_number_id:
            return None
        for alias, account in self.accounts.items():
            if str(account.get("phone_number_id") or "") == str(phone_number_id):
                return alias
        return None

    def _account_dir(self, alias: str) -> Path:
        d = self.root / alias
        for sub in ("inbox", "sent"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def _contacts_path(self, alias: str) -> Path:
        return self._account_dir(alias) / "contacts.json"

    def _load_contacts(self, alias: str) -> dict[str, Any]:
        p = self._contacts_path(alias)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save_contacts(self, alias: str, contacts: dict[str, Any]) -> None:
        path = self._contacts_path(alias)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _compound(self, alias: str, wa_id: str, wamid: str) -> str:
        return f"{alias}:{wa_id}:{wamid}"

    def _split_compound(self, compound: str) -> tuple[str, str, str]:
        parts = compound.split(":", 2)
        if len(parts) != 3:
            raise ValueError("message_id must be account:wa_id:wamid")
        return parts[0], parts[1], parts[2]

    def _store_message(self, alias: str, folder: str, msg: dict[str, Any]) -> dict[str, Any]:
        d = self._account_dir(alias) / folder / str(uuid4())
        d.mkdir(parents=True, exist_ok=True)
        msg = dict(msg)
        msg.setdefault("stored_at", _utcnow())
        (d / "message.json").write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
        return msg

    def _iter_messages(self, alias: str, folder: str | None = None) -> list[dict[str, Any]]:
        base = self._account_dir(alias)
        folders = [folder] if folder else ["inbox", "sent"]
        out: list[dict[str, Any]] = []
        for f in folders:
            for p in sorted((base / f).glob("*/message.json")):
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                    m.setdefault("_folder", f)
                    out.append(m)
                except Exception:
                    continue
        out.sort(key=lambda m: m.get("stored_at", ""), reverse=True)
        return out

    def handle(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action not in ACTIONS:
            return {"status": "error", "error": f"unknown action: {action}"}
        try:
            return getattr(self, f"_{action}")(args)
        except Exception as e:
            return {"status": "error", "error": str(e), "error_type": type(e).__name__}

    def _manual(self, args: dict[str, Any]) -> dict[str, Any]:
        # The manual lives in this package's bundled SKILL.md (standard skill
        # format: YAML frontmatter + markdown body), loaded at import time.
        # action='manual' returns the full skill markdown plus parsed metadata
        # and the resolved path; the frontmatter is also injected into the
        # schema's 'action' description as a catalog entry. Bundled
        # asset/reference sidecars, if any, are documented inside SKILL.md and
        # are not returned as structured tool fields.
        return _skill.manual_payload(
            _SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH, _SKILL_NAME
        )

    def _client(self, alias: str) -> WhatsAppClient:
        a = self.accounts[alias]
        return WhatsAppClient(access_token=a.get("access_token", ""), phone_number_id=a.get("phone_number_id", ""), api_version=a.get("api_version", "v23.0"))

    def _message_payload(self, args: dict[str, Any], to: str) -> dict[str, Any]:
        if args.get("template"):
            t = dict(args["template"])
            if not t.get("name") or not ((t.get("language") or {}).get("code")):
                raise ValueError("template requires name and language.code")
            return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": t}
        if args.get("media"):
            media = dict(args["media"])
            mtype = media.pop("type", None)
            if not mtype:
                raise ValueError("media requires type")
            return {"messaging_product": "whatsapp", "to": to, "type": mtype, mtype: media}
        text = args.get("text")
        if not text:
            raise ValueError("send/reply requires text, media, or template")
        body = {"body": text}
        if "preview_url" in args:
            body["preview_url"] = bool(args["preview_url"])
        return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": body}

    def _send(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        to = args.get("to") or args.get("wa_id")
        if not to:
            return {"status": "error", "error": "send requires to or wa_id"}
        payload = self._message_payload(args, to)
        try:
            response = self._client(alias).post_message(payload)
            wamid = (((response.get("messages") or [{}])[0]).get("id") or f"local-{uuid4()}")
            stored = self._store_message(alias, "sent", {"id": self._compound(alias, to, wamid), "wa_id": to, "message_id": wamid, "text": args.get("text"), "payload": payload, "response": response, "direction": "outgoing"})
            return {"status": "sent", "message_id": stored["id"], "response": response}
        except Exception as e:
            return {"status": "error", "error": str(e), "note": _CS_WINDOW_NOTE}

    def _reply(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("message_id"):
            return {"status": "error", "error": "reply requires message_id"}
        alias, wa_id, wamid = self._split_compound(args["message_id"])
        send_args = dict(args)
        send_args["account"] = alias
        send_args["to"] = wa_id
        payload = self._message_payload(send_args, wa_id)
        payload["context"] = {"message_id": wamid}
        # Inline send to preserve context override.
        try:
            response = self._client(alias).post_message(payload)
            new_id = (((response.get("messages") or [{}])[0]).get("id") or f"local-{uuid4()}")
            stored = self._store_message(alias, "sent", {"id": self._compound(alias, wa_id, new_id), "wa_id": wa_id, "message_id": new_id, "reply_to": wamid, "text": args.get("text"), "payload": payload, "response": response, "direction": "outgoing"})
            return {"status": "sent", "message_id": stored["id"], "response": response}
        except Exception as e:
            return {"status": "error", "error": str(e), "note": _CS_WINDOW_NOTE}

    def _react(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("message_id") or not args.get("emoji"):
            return {"status": "error", "error": "react requires message_id and emoji"}
        alias, wa_id, wamid = self._split_compound(args["message_id"])
        payload = {"messaging_product": "whatsapp", "to": wa_id, "type": "reaction", "reaction": {"message_id": wamid, "emoji": args["emoji"]}}
        try:
            response = self._client(alias).post_message(payload)
            return {"status": "sent", "response": response}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _check(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        limit = int(args.get("limit") or 10)
        return {"status": "ok", "messages": self._iter_messages(alias, "inbox")[:limit]}

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account")) if not args.get("message_id") else self._split_compound(args["message_id"])[0]
        wa_id = args.get("wa_id") or (self._split_compound(args["message_id"])[1] if args.get("message_id") else None)
        msgs = [m for m in self._iter_messages(alias) if not wa_id or m.get("wa_id") == wa_id]
        selected = msgs[: int(args.get("limit") or 20)]
        result: dict[str, Any] = {"status": "ok", "messages": selected}
        if args.get("mark_read", True):
            marked: list[str] = []
            errors: list[dict[str, str]] = []
            client = self._client(alias)
            for msg in selected:
                if msg.get("_folder") != "inbox" or not msg.get("message_id"):
                    continue
                try:
                    client.mark_message_read(str(msg["message_id"]))
                    marked.append(str(msg["message_id"]))
                except Exception as e:
                    errors.append({"message_id": str(msg.get("message_id")), "error": str(e), "error_type": type(e).__name__})
            result["marked_read"] = marked
            if errors:
                result["mark_read_errors"] = errors
        return result

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        q = args.get("query") or ""
        try:
            rx = re.compile(q, re.I)
        except re.error as e:
            return {"status": "error", "error": f"invalid regex: {e}"}
        matches = [m for m in self._iter_messages(alias) if rx.search(json.dumps(m, ensure_ascii=False))]
        return {"status": "ok", "messages": matches[: int(args.get("limit") or 20)]}

    def _contacts(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        return {"status": "ok", "contacts": self._load_contacts(alias)}

    def _add_contact(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        wa_id = args.get("wa_id") or args.get("to")
        if not wa_id:
            return {"status": "error", "error": "add_contact requires wa_id"}
        with self._contacts_lock:
            contacts = self._load_contacts(alias)
            contacts[wa_id] = {"wa_id": wa_id, "name": args.get("name") or wa_id}
            self._save_contacts(alias, contacts)
            contact = contacts[wa_id]
        return {"status": "ok", "contact": contact}

    def _remove_contact(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        wa_id = args.get("wa_id") or args.get("to")
        if not wa_id:
            return {"status": "error", "error": "remove_contact requires wa_id"}
        with self._contacts_lock:
            contacts = self._load_contacts(alias)
            removed = contacts.pop(wa_id, None)
            self._save_contacts(alias, contacts)
        return {"status": "ok", "removed": removed}

    def _templates(self, args: dict[str, Any]) -> dict[str, Any]:
        alias = self._account_alias(args.get("account"))
        return {"status": "ok", "templates": self.accounts[alias].get("templates", [])}

    def _accounts(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "accounts": [redact_account(a) for a in self.accounts.values()],
            "details": self.account_details(),
            "identity_path": str(self.identity_path()),
        }

    def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        accounts = [redact_account(a) for a in self.accounts.values()]
        return {
            "status": "ok",
            "transport": "official_meta_cloud_api",
            "webhook": "required_for_inbound",
            "customer_service_window_hours": 24,
            "accounts": accounts,
            "identity_path": str(self.identity_path()),
        }

    def account_details(self) -> list[dict[str, Any]]:
        """Return non-secret public identity details for each account."""
        details: list[dict[str, Any]] = []
        for alias, account in self.accounts.items():
            contacts = self._load_contacts(alias)
            item: dict[str, Any] = {
                "alias": alias,
                "phone_number_id": account.get("phone_number_id"),
                "waba_id": account.get("waba_id") or account.get("business_account_id"),
                "business_account_id": account.get("business_account_id"),
                "display_phone_number": account.get("display_phone_number"),
                "api_version": account.get("api_version"),
                "last_verified_at": self._last_verified_at,
                "template_count": len(account.get("templates") or []),
                "contact_count": len(contacts),
            }
            if self.config_source:
                item["config_source"] = self.config_source
            details.append({k: v for k, v in item.items() if v is not None})
        return details

    def identity_payload(self) -> dict[str, Any]:
        """Build the non-secret MCP identity document for this service."""
        return _identity.identity_payload(
            "whatsapp", self.account_details(), generated_at=_utcnow()
        )

    def identity_path(self) -> Path:
        return _identity.identity_path(self.working_dir, "whatsapp")

    def write_identity_file(self) -> Path:
        """Atomically write public, non-secret MCP identity metadata."""
        return _identity.write_identity_file(
            self.identity_path(), self.identity_payload()
        )

    def ingest_webhook(self, alias: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        events = extract_events(payload)
        for ev in events:
            if ev.get("kind") == "message":
                wa_id = ev.get("wa_id") or "unknown"
                wamid = ev.get("message_id") or f"local-{uuid4()}"
                msg = {"id": self._compound(alias, wa_id, wamid), "wa_id": wa_id, "message_id": wamid, "text": ev.get("text"), "type": ev.get("type"), "direction": "incoming", "metadata": ev.get("metadata"), "timestamp": ev.get("timestamp"), "stored_at": _utcnow()}
                self._store_message(alias, "inbox", msg)
                if self.on_inbound:
                    header = _NOTIFICATION_HEADER_TEMPLATE.format(channel="WhatsApp").rstrip("\n")
                    message_body = ev.get("text") or f"[{ev.get('type')}]"
                    body = f"{header}\n\n**Newest WhatsApp message**\n{message_body}"
                    self.on_inbound({"from": f"whatsapp:{wa_id}", "subject": "WhatsApp message", "body": body, "metadata": {"mcp": "whatsapp", "account": alias, "wa_id": wa_id, "message_id": msg["id"]}, "wake": True})
        return events
