"""CloudMailManager — omnibus ``cloud_mail`` tool + polling coordinator.

One MCP tool, five actions:
  * ``check``    — recent public emails (optional filters).
  * ``search``   — public emailList filters (toEmail/sendEmail/subject/...).
  * ``read``     — one email by compound id ``<account>:<emailId>``.
  * ``send``     — send via user /login + /email/send (needs user creds).
  * ``accounts`` — redacted per-account status (no tokens/passwords).

Inbound mail is discovered by a per-account polling thread and pushed into
the host agent's inbox via LICC. Watermarks (highest delivered ``emailId``)
persist under ``<working_dir>/cloud_mail/<alias>/watermark.json`` so restarts
never re-notify old mail.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from .client import CloudMailClient, CloudMailError
from ._watermark import WatermarkStore
from .. import _skill

log = logging.getLogger("lingtai_cloud_mail")

# Bundled usage manual (skill format) — SKILL.md ships in this package folder.
# action='manual' reads the full body; the YAML frontmatter name/description are
# injected into the tool schema as a progressive-disclosure catalog entry.
_SKILL_NAME = "cloud-mail-mcp-manual"
_SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH = _skill.load_skill(__package__)

DESCRIPTION = (
    "Cloud Mail REST email client for a self-hosted Cloud Mail deployment "
    "(Cloudflare Workers, https://github.com/maillab/cloud-mail). Actions: "
    "check (recent inbound mail), search (filter by sender/recipient/subject/"
    "content), read (full content by compound id '<account>:<emailId>'), send "
    "(requires user credentials in config), accounts (redacted status). "
    "Inbound mail also arrives automatically in your inbox via polling."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["check", "search", "read", "send", "accounts", "add_user", "manual"],
            "description": (
                "Which Cloud Mail operation to perform. "
                + _skill.manual_action_description(_SKILL_FRONTMATTER, _SKILL_NAME)
            ),
        },
        "account": {
            "type": "string",
            "description": "Account alias (or admin email). Defaults to the first configured account.",
        },
        "limit": {"type": "integer", "description": "check: max rows to return (default 10)."},
        "n": {"type": "integer", "description": "Alias for limit."},
        # search filters (public emailList)
        "to_email": {"type": "string", "description": "search/check: filter by recipient address (LIKE)."},
        "send_email": {"type": "string", "description": "search/check: filter by sender address (LIKE)."},
        "send_name": {"type": "string", "description": "search: filter by sender display name (LIKE)."},
        "subject": {"type": "string", "description": "search/check: filter by subject (LIKE); send: subject line."},
        "content": {"type": "string", "description": "search: filter by body content (LIKE)."},
        "time_sort": {"type": "string", "enum": ["asc", "desc"], "description": "search/check: sort order (default desc)."},
        "num": {"type": "integer", "description": "search: page number (default 1)."},
        "size": {"type": "integer", "description": "search: page size (default 20)."},
        "type": {"type": "integer", "description": "search/check: Cloud Mail email type filter."},
        "is_del": {"type": "integer", "description": "search: include deleted rows."},
        # read
        "id": {"type": "string", "description": "read: compound id '<account>:<emailId>'."},
        "email_id": {"description": "read: numeric email id (used with 'account')."},
        # send
        "address": {"description": "send: recipient address or list of addresses."},
        "message": {"type": "string", "description": "send: plain-text body (maps to text)."},
        "text": {"type": "string", "description": "send: plain-text body (alias for message)."},
        "html": {"type": "string", "description": "send: HTML body (maps to content)."},
        "content_html": {"type": "string", "description": "send: HTML body (alias for html)."},
        "name": {"type": "string", "description": "send: sender display name."},
        "send_account_id": {"type": "integer", "description": "send: override the configured sender account id."},
        "attachments": {"type": "array", "description": "send: NOT SUPPORTED in this first pass."},
        # add_user (optional)
        "email": {"type": "string", "description": "add_user: new user email."},
        "password": {"type": "string", "description": "add_user: new user password."},
        "role_name": {"type": "string", "description": "add_user: optional Cloud Mail role name."},
    },
    "required": ["action"],
}


class CloudMailAccount:
    """One configured Cloud Mail account: client + watermark + poll state."""

    def __init__(self, cfg: dict, *, working_dir: Path | None, transport=None) -> None:
        self.base_url = cfg["base_url"]
        self.alias = cfg.get("alias") or cfg.get("admin_email") or self.base_url
        self.admin_email = cfg.get("admin_email")
        self.user_email = cfg.get("user_email")
        self._user_password = cfg.get("user_password")
        self.send_account_id = cfg.get("send_account_id")
        self.poll_interval = float(cfg.get("poll_interval", 30) or 30)
        self.notify_existing = bool(cfg.get("notify_existing", False))
        allowed = cfg.get("allowed_senders") or []
        self.allowed_senders = {str(a).strip().lower() for a in allowed if a}

        self.client = CloudMailClient(
            base_url=self.base_url,
            admin_email=cfg.get("admin_email"),
            admin_password=cfg.get("admin_password"),
            user_email=cfg.get("user_email"),
            user_password=cfg.get("user_password"),
            transport=transport,
        )

        self._wm_path = None
        if working_dir is not None:
            self._wm_path = Path(working_dir) / "cloud_mail" / _safe_segment(self.alias) / "watermark.json"
        self.watermark = WatermarkStore(self._wm_path) if self._wm_path else WatermarkStore(Path("/dev/null"))

    @property
    def has_user_creds(self) -> bool:
        return bool(self.user_email and self._user_password)

    def sender_allowed(self, sender: str | None) -> bool:
        if not self.allowed_senders:
            return True
        return (sender or "").strip().lower() in self.allowed_senders

    def status(self) -> dict:
        """Redacted status — never includes tokens/passwords."""
        return {
            "alias": self.alias,
            "base_url": self.base_url,
            "admin_email": self.admin_email,
            "user_email": self.user_email,
            "can_send": self.has_user_creds and self.send_account_id is not None,
            "send_account_id": self.send_account_id,
            "allowed_senders": sorted(self.allowed_senders) or None,
            "poll_interval": self.poll_interval,
            "watermark_email_id": self.watermark.last_email_id,
            "seeded": self.watermark.seeded,
        }


class CloudMailManager:
    """Coordinates accounts, the omnibus tool, and the polling threads."""

    def __init__(
        self,
        accounts: list[dict],
        *,
        working_dir: Path | str | None = None,
        on_inbound: Callable[[dict], None] | None = None,
        transport=None,
    ) -> None:
        self._working_dir = Path(working_dir) if working_dir else None
        self._on_inbound = on_inbound
        self._accounts: list[CloudMailAccount] = [
            CloudMailAccount(cfg, working_dir=self._working_dir, transport=transport)
            for cfg in accounts
        ]
        if not self._accounts:
            raise ValueError("cloud_mail config has no accounts")
        self._by_alias: dict[str, CloudMailAccount] = {}
        for acct in self._accounts:
            self._by_alias[acct.alias.lower()] = acct
            if acct.admin_email:
                self._by_alias.setdefault(acct.admin_email.lower(), acct)
        self._poll_stop = threading.Event()
        self._poll_threads: list[threading.Thread] = []

    # -- account resolution --

    @property
    def default_account(self) -> CloudMailAccount:
        return self._accounts[0]

    def _resolve(self, account: str | None) -> CloudMailAccount:
        if not account:
            return self.default_account
        acct = self._by_alias.get(str(account).strip().lower())
        if acct is None:
            raise CloudMailError(f"unknown account {account!r}")
        return acct

    # -- tool dispatch --

    def handle(self, args: dict) -> dict:
        action = (args or {}).get("action")
        try:
            if action == "check":
                return self._handle_check(args)
            if action == "search":
                return self._handle_search(args)
            if action == "read":
                return self._handle_read(args)
            if action == "send":
                return self._handle_send(args)
            if action == "accounts":
                return {"status": "ok", "accounts": [a.status() for a in self._accounts]}
            if action == "add_user":
                return self._handle_add_user(args)
            if action == "manual":
                return self._handle_manual()
            return {"status": "error", "error": f"unknown action: {action!r}"}
        except CloudMailError as exc:
            return {"status": "error", "error": str(exc), "error_type": "CloudMailError"}
        except Exception as exc:  # defensive — never leak a traceback to the model
            log.exception("cloud_mail action %r failed", action)
            return {"status": "error", "error": str(exc), "error_type": type(exc).__name__}

    def _handle_manual(self) -> dict:
        # The manual lives in this package's bundled SKILL.md (standard skill
        # format: YAML frontmatter + markdown body), loaded at import time.
        # action='manual' returns the full skill markdown plus parsed metadata
        # and the resolved path; the frontmatter is also injected into the
        # schema's 'manual' action description as a catalog entry. Bundled
        # asset/reference sidecars, if any, are documented inside SKILL.md and
        # are not returned as structured tool fields.
        return _skill.manual_payload(
            _SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH, _SKILL_NAME
        )

    def _handle_check(self, args: dict) -> dict:
        acct = self._resolve(args.get("account"))
        limit = args.get("limit") or args.get("n") or 10
        try:
            size = max(1, int(limit))
        except (TypeError, ValueError):
            size = 10
        rows = acct.client.email_list(
            toEmail=args.get("to_email"),
            sendEmail=args.get("send_email"),
            subject=args.get("subject"),
            timeSort=args.get("time_sort"),
            type=args.get("type"),
            num=1,
            size=size,
        )
        return {
            "status": "ok",
            "account": acct.alias,
            "count": len(rows),
            "emails": [_summarize_row(acct.alias, r) for r in rows],
        }

    def _handle_search(self, args: dict) -> dict:
        acct = self._resolve(args.get("account"))
        rows = acct.client.email_list(
            toEmail=args.get("to_email"),
            sendEmail=args.get("send_email"),
            sendName=args.get("send_name"),
            subject=args.get("subject"),
            content=args.get("content"),
            timeSort=args.get("time_sort"),
            num=args.get("num"),
            size=args.get("size"),
            type=args.get("type"),
            isDel=args.get("is_del"),
        )
        return {
            "status": "ok",
            "account": acct.alias,
            "count": len(rows),
            "emails": [_summarize_row(acct.alias, r) for r in rows],
        }

    def _handle_read(self, args: dict) -> dict:
        alias, email_id = self._parse_read_target(args)
        acct = self._resolve(alias)
        if email_id is None:
            return {"status": "error", "error": "read requires 'id' as '<account>:<emailId>' or 'email_id'"}
        # Public emailList has no by-id filter; page through recent rows and
        # match the emailId. Bounded scan keeps this cheap and predictable.
        for page in range(1, 6):
            rows = acct.client.email_list(num=page, size=50, timeSort="desc")
            if not rows:
                break
            for r in rows:
                if str(r.get("emailId")) == str(email_id):
                    return {
                        "status": "ok",
                        "account": acct.alias,
                        "email": _full_row(acct.alias, r),
                    }
        return {
            "status": "error",
            "error": f"email {email_id} not found in account {acct.alias!r} "
            "(searched recent pages; it may be older than the scan window)",
        }

    def _handle_send(self, args: dict) -> dict:
        acct = self._resolve(args.get("account"))
        if not acct.has_user_creds:
            return {
                "status": "error",
                "error": (
                    f"send needs user credentials for account {acct.alias!r}. "
                    "Add 'user_email' and 'user_password' (and 'send_account_id') "
                    "to that account in the LINGTAI_CLOUD_MAIL_CONFIG file."
                ),
            }
        send_account_id = args.get("send_account_id")
        if send_account_id is None:
            send_account_id = acct.send_account_id
        if send_account_id is None:
            return {
                "status": "error",
                "error": (
                    f"send needs 'send_account_id' for account {acct.alias!r} "
                    "(set it in config or pass send_account_id)."
                ),
            }

        recipients = _as_recipient_list(args.get("address"))
        if not recipients:
            return {"status": "error", "error": "send requires 'address' (a string or list of addresses)"}

        if args.get("attachments"):
            return {
                "status": "error",
                "error": "attachments are not supported by the cloud_mail addon (first pass); omit them",
            }

        text = args.get("message") or args.get("text") or ""
        html = args.get("html") or args.get("content_html")
        # Cloud Mail's send service derives the HTML/preview from `content`;
        # fall back to the plain text wrapped so a text-only send still works.
        content = html if html else text
        payload = {
            "accountId": send_account_id,
            "name": args.get("name") or "",
            "sendType": "count",
            "receiveEmail": recipients,
            "text": text,
            "content": content,
            "subject": args.get("subject") or "",
            "attachments": [],
        }
        data = acct.client.email_send(payload)
        return {
            "status": "ok",
            "account": acct.alias,
            "sent_to": recipients,
            "result": data,
        }

    def _handle_add_user(self, args: dict) -> dict:
        acct = self._resolve(args.get("account"))
        email = args.get("email")
        password = args.get("password")
        if not email or not password:
            return {"status": "error", "error": "add_user requires 'email' and 'password'"}
        role_name = args.get("role_name") or args.get("roleName")
        extra = {"roleName": role_name} if role_name else {}
        data = acct.client.add_user(email, password, **extra)
        # Never echo the password back.
        return {"status": "ok", "account": acct.alias, "added": email, "result": data}

    @staticmethod
    def _parse_read_target(args: dict) -> tuple[str | None, Any]:
        raw = args.get("id")
        if raw and ":" in str(raw):
            alias, _, email_id = str(raw).rpartition(":")
            return (alias or None), email_id
        return args.get("account"), args.get("email_id")

    # -- polling / LICC --

    def start(self) -> None:
        """Begin per-account polling threads."""
        if self._poll_threads:
            return
        self._poll_stop.clear()
        for acct in self._accounts:
            t = threading.Thread(
                target=self._poll_loop,
                args=(acct,),
                daemon=True,
                name=f"cloud-mail-poll-{acct.alias}",
            )
            t.start()
            self._poll_threads.append(t)
        log.info("cloud_mail polling %d account(s)", len(self._accounts))

    def stop(self) -> None:
        self._poll_stop.set()
        for t in self._poll_threads:
            t.join(timeout=2.0)
        self._poll_threads = []
        for acct in self._accounts:
            acct.client.close()

    def _poll_loop(self, acct: CloudMailAccount) -> None:
        # First tick runs immediately; subsequent ticks wait poll_interval.
        first = True
        while not self._poll_stop.is_set():
            if not first:
                if self._poll_stop.wait(acct.poll_interval):
                    break
            first = False
            try:
                self.poll_once(acct)
            except CloudMailError as exc:
                log.warning("cloud_mail poll failed for %s: %s", acct.alias, exc)
            except Exception:
                log.exception("cloud_mail poll crashed for %s", acct.alias)

    def poll_once(self, acct: CloudMailAccount) -> int:
        """One poll cycle. Returns the number of LICC events pushed.

        Seeds the watermark silently on first run (unless notify_existing).
        Otherwise pushes one LICC event per new row with emailId >
        watermark, honoring allowed_senders.
        """
        rows = acct.client.email_list(num=1, size=50, timeSort="desc")
        if not rows:
            return 0
        # Highest emailId seen this cycle.
        def _eid(r: dict) -> int:
            try:
                return int(r.get("emailId") or 0)
            except (TypeError, ValueError):
                return 0

        max_id = max(_eid(r) for r in rows)
        last_id = acct.watermark.last_email_id
        seeded = acct.watermark.seeded

        if not seeded and not acct.notify_existing:
            # Fresh first run — record high-water mark without flooding.
            acct.watermark.set_last_email_id(max_id, seeded=True)
            log.info("cloud_mail seeded %s watermark at emailId=%s", acct.alias, max_id)
            return 0

        # New rows (ascending so we deliver oldest-first), above the watermark.
        new_rows = sorted(
            (r for r in rows if _eid(r) > last_id),
            key=_eid,
        )
        pushed = 0
        for r in new_rows:
            if not acct.sender_allowed(r.get("sendEmail")):
                continue
            if self._push_licc(acct, r):
                pushed += 1
        # Advance watermark to the max we observed regardless of allow-list,
        # so filtered-out senders don't replay forever.
        if max_id > last_id:
            acct.watermark.set_last_email_id(max_id, seeded=True)
        return pushed

    def _push_licc(self, acct: CloudMailAccount, row: dict) -> bool:
        if self._on_inbound is None:
            return False
        email_id = row.get("emailId")
        compound = f"{acct.alias}:{email_id}"
        sender = row.get("sendEmail") or row.get("sendName") or "unknown"
        subject = row.get("subject") or "(no subject)"
        body = row.get("text") or _strip_to_text(row.get("content")) or ""
        event = {
            "from": sender,
            "subject": subject,
            "body": body,
            "wake": True,
            "metadata": {
                "source": "cloud_mail",
                "event_type": "email",
                "account": acct.alias,
                "email_id": email_id,
                "compound_id": compound,
                "from": sender,
                "from_name": row.get("sendName"),
                "to": row.get("toEmail"),
                "created_at": row.get("createTime"),
            },
        }
        try:
            self._on_inbound(event)
            return True
        except Exception:
            log.exception("cloud_mail LICC push failed for %s", compound)
            return False


# ---------------------------------------------------------------------------
# Row helpers (shared by tool results + LICC)
# ---------------------------------------------------------------------------

def _summarize_row(alias: str, row: dict) -> dict:
    """Compact, list-friendly view of one email row."""
    email_id = row.get("emailId")
    return {
        "compound_id": f"{alias}:{email_id}",
        "email_id": email_id,
        "from": row.get("sendEmail"),
        "from_name": row.get("sendName"),
        "to": row.get("toEmail"),
        "subject": row.get("subject"),
        "created_at": row.get("createTime"),
        "type": row.get("type"),
        "preview": _preview(row),
    }


def _full_row(alias: str, row: dict) -> dict:
    """Full view of one email row including content/text."""
    out = _summarize_row(alias, row)
    out["text"] = row.get("text")
    out["content"] = row.get("content")
    out["is_del"] = row.get("isDel")
    out.pop("preview", None)
    return out


def _preview(row: dict, n: int = 200) -> str:
    text = row.get("text") or _strip_to_text(row.get("content")) or ""
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def _strip_to_text(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    # Cheap tag strip — we do NOT pull in an HTML parser for a preview.
    import re
    return re.sub(r"<[^>]+>", " ", content)


def _as_recipient_list(address: Any) -> list[str]:
    if address is None:
        return []
    if isinstance(address, str):
        return [address] if address.strip() else []
    if isinstance(address, (list, tuple)):
        return [str(a) for a in address if str(a).strip()]
    return []


def _safe_segment(name: str) -> str:
    """Make an alias safe for a filesystem path segment."""
    import re
    seg = re.sub(r"[^A-Za-z0-9._@-]+", "_", str(name)).strip("_")
    return seg or "account"
