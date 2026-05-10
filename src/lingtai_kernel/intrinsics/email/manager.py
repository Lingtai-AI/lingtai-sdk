"""EmailManager — filesystem-based email manager with search, contacts, schedules.

Moved from the former monolithic email.py.  Imports mailbox primitives from
the sibling primitives module.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ...i18n import t
from ...message import _make_message, MSG_REQUEST
from ...time_veil import scrub_time_fields
from ...token_counter import count_tokens

from .primitives import (
    _coerce_address_list,
    _email_time,
    _is_self_send,
    _list_inbox,
    _load_message,
    _mailbox_dir,
    _mailman,
    _mark_read,
    _message_summary,
    _new_mailbox_id,
    _outbox_dir,
    _persist_to_inbox,
    _persist_to_outbox,
    _preview,
    _read_ids,
    _save_read_ids,
    _sent_dir,
)

if TYPE_CHECKING:
    from ...base_agent import BaseAgent


class EmailManager:
    """Filesystem-based email manager — reads/writes mailbox/ directory."""

    def __init__(self, agent: "BaseAgent"):
        self._agent = agent
        # Track consecutive identical sends per recipient to block loops.
        self._last_sent: dict[str, tuple[str, int]] = {}
        self._dup_free_passes = 2  # allow this many identical sends
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    def start_scheduler(self) -> None:
        """Start the background scheduler thread."""
        if self._scheduler_thread is not None:
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name=f"scheduler-{self._agent._working_dir.name}",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        """Stop the scheduler thread cleanly."""
        self._stop_event.set()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=5.0)
            self._scheduler_thread = None

    @property
    def _mailbox_path(self) -> Path:
        return _mailbox_dir(self._agent)

    @property
    def _schedules_dir(self) -> Path:
        return self._mailbox_path / "schedules"

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _load_email(self, email_id: str) -> dict | None:
        """Load a single email by ID. Checks inbox, then sent/, then archive/."""
        msg = _load_message(self._agent, email_id)
        if msg is not None:
            msg["_folder"] = "inbox"
            msg.setdefault("_mailbox_id", email_id)
            return msg
        path = self._mailbox_path / "sent" / email_id / "message.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
            data["_folder"] = "sent"
            data.setdefault("_mailbox_id", email_id)
            return data
        path = self._mailbox_path / "archive" / email_id / "message.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
            data["_folder"] = "archive"
            data.setdefault("_mailbox_id", email_id)
            return data
        return None

    def _list_emails(self, folder: str) -> list[dict]:
        """Load all emails from a folder, sorted by time (newest first)."""
        if folder == "inbox":
            messages = _list_inbox(self._agent)
            for m in messages:
                m["_folder"] = "inbox"
                m.setdefault("_mailbox_id", m.get("_mailbox_id", ""))
            return messages
        folder_dir = self._mailbox_path / folder
        if not folder_dir.is_dir():
            return []
        emails = []
        for msg_dir in folder_dir.iterdir():
            msg_file = msg_dir / "message.json"
            if msg_dir.is_dir() and msg_file.is_file():
                try:
                    data = json.loads(msg_file.read_text())
                    data["_folder"] = folder
                    data.setdefault("_mailbox_id", msg_dir.name)
                    emails.append(data)
                except (json.JSONDecodeError, OSError):
                    continue
        emails.sort(key=_email_time, reverse=True)
        return emails

    def _rerender_unread_digest(self) -> None:
        """Republish ``.notification/email.json`` per current unread state.

        Called after any read-state mutation (``_read``, ``_reply``,
        ``_reply_all``, ``_archive``, ``_delete``) so the agent's
        notification reflects the new state on the next heartbeat
        sync.  Lazy-imports the function from ``base_agent.messaging``
        to avoid circular import at module load (intrinsics →
        base_agent crosses the layering boundary).
        """
        from ...base_agent.messaging import _rerender_unread_digest
        _rerender_unread_digest(self._agent)

    def _email_summary(self, e: dict, read_set: set[str] | None = None, truncate: int = 500) -> dict:
        """Build a summary dict from a raw email dict."""
        if read_set is None:
            read_set = _read_ids(self._agent)
        recipient_id = getattr(self._agent, "_agent_id", "")
        if e.get("_folder") == "inbox":
            summary = _message_summary(e, read_set, truncate=truncate,
                                       recipient_agent_id=recipient_id)
            summary["folder"] = "inbox"
            if e.get("cc"):
                summary["cc"] = e["cc"]
            self._inject_identity(summary, e)
            return summary
        if e.get("_folder") == "archive":
            summary = _message_summary(e, read_set, truncate=truncate,
                                       recipient_agent_id=recipient_id)
            summary["folder"] = "archive"
            if e.get("cc"):
                summary["cc"] = e["cc"]
            self._inject_identity(summary, e)
            return summary
        eid = e.get("_mailbox_id", "")
        entry = {
            "id": eid,
            "from": e.get("from", ""),
            "to": e.get("to", []),
            "subject": e.get("subject", "(no subject)"),
            "preview": _preview(e.get("message", ""), limit=truncate),
            "time": e.get("received_at") or e.get("sent_at") or e.get("time") or "",
            "folder": e.get("_folder", ""),
        }
        if e.get("cc"):
            entry["cc"] = e["cc"]
        return entry

    @staticmethod
    def _inject_identity(summary: dict, raw: dict) -> None:
        """Surface identity card fields in check/read results."""
        identity = raw.get("identity")
        if not identity or not isinstance(identity, dict):
            return
        summary["is_human"] = identity.get("admin") is None
        summary["sender_name"] = identity.get("agent_name", "")
        summary["sender_nickname"] = identity.get("nickname", "")
        summary["sender_agent_id"] = identity.get("agent_id", "")
        summary["sender_language"] = identity.get("language", "")
        loc = identity.get("location")
        if isinstance(loc, dict) and loc.get("timezone"):
            summary["sender_location"] = {
                "city": loc.get("city", ""),
                "region": loc.get("region", ""),
                "timezone": loc.get("timezone", ""),
            }

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def handle(self, args: dict) -> dict:
        schedule = args.get("schedule")
        if schedule is not None:
            return self._handle_schedule(args, schedule)
        action = args.get("action")
        if not action:
            return {"error": "action is required (or pass a schedule object)"}
        if action == "send":
            return self._send(args)
        elif action == "check":
            return self._check(args)
        elif action == "read":
            return self._read(args)
        elif action == "dismiss":
            return self._dismiss(args)
        elif action == "reply":
            return self._reply(args)
        elif action == "reply_all":
            return self._reply_all(args)
        elif action == "search":
            return self._search(args)
        elif action == "archive":
            return self._archive(args)
        elif action == "delete":
            return self._delete(args)
        elif action == "contacts":
            return self._contacts()
        elif action == "add_contact":
            return self._add_contact(args)
        elif action == "remove_contact":
            return self._remove_contact(args)
        elif action == "edit_contact":
            return self._edit_contact(args)
        else:
            return {"error": f"Unknown email action: {action}"}

    # ------------------------------------------------------------------
    # Schedule dispatch
    # ------------------------------------------------------------------

    def _handle_schedule(self, args: dict, schedule: dict) -> dict:
        action = schedule.get("action")
        if action == "create":
            return self._schedule_create(args, schedule)
        elif action == "cancel":
            return self._schedule_cancel(schedule)
        elif action == "list":
            return self._schedule_list()
        elif action == "reactivate":
            return self._schedule_reactivate(schedule)
        else:
            return {"error": f"Unknown schedule action: {action}"}

    def _schedule_create(self, args: dict, schedule: dict) -> dict:
        interval = schedule.get("interval")
        count = schedule.get("count")
        if interval is None or count is None:
            return {"error": "schedule.interval and schedule.count are required"}
        if interval <= 0 or count <= 0:
            return {"error": "schedule.interval and schedule.count must be positive"}

        raw_address = args.get("address", "")
        to_list = _coerce_address_list(raw_address)
        if not to_list:
            return {"error": "address is required"}

        send_payload = {
            "address": args.get("address"),
            "subject": args.get("subject", ""),
            "message": args.get("message", ""),
            "cc": args.get("cc") or [],
            "bcc": args.get("bcc") or [],
            "type": args.get("type", "normal"),
        }
        if args.get("attachments"):
            send_payload["attachments"] = args["attachments"]

        schedule_id = uuid4().hex[:12]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "schedule_id": schedule_id,
            "send_payload": send_payload,
            "interval": interval,
            "count": count,
            "sent": 0,
            "created_at": now,
            "last_sent_at": None,
            "status": "active",
        }

        sched_dir = self._schedules_dir / schedule_id
        sched_dir.mkdir(parents=True, exist_ok=True)
        self._write_schedule(sched_dir / "schedule.json", record)

        return {"status": "scheduled", "schedule_id": schedule_id, "interval": interval, "count": count}

    def _schedule_cancel(self, schedule: dict) -> dict:
        schedule_id = schedule.get("schedule_id")

        if not schedule_id:
            schedules_dir = self._schedules_dir
            if not schedules_dir.is_dir():
                return {"status": "paused", "message": "No schedules to cancel"}
            for sched_dir in schedules_dir.iterdir():
                if not sched_dir.is_dir():
                    continue
                sched_file = sched_dir / "schedule.json"
                if not sched_file.is_file():
                    continue
                try:
                    record = json.loads(sched_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                status = record.get("status", "active")
                if status in ("inactive", "completed"):
                    continue
                record["status"] = "inactive"
                try:
                    self._write_schedule(sched_file, record)
                except OSError:
                    continue
            return {"status": "paused", "message": "All active schedules paused"}

        record = self._read_schedule(schedule_id)
        if record is None:
            return {"error": f"Schedule not found: {schedule_id}"}
        status = record.get("status", "active")
        if status == "inactive":
            return {"status": "already_inactive", "schedule_id": schedule_id}
        if status == "completed":
            return {"status": "already_completed", "schedule_id": schedule_id}
        self._set_schedule_status(schedule_id, "inactive")
        return {"status": "paused", "schedule_id": schedule_id}

    def _schedule_reactivate(self, schedule: dict) -> dict:
        schedule_id = schedule.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required for reactivate"}
        record = self._read_schedule(schedule_id)
        if record is None:
            return {"error": f"Schedule not found: {schedule_id}"}
        status = record.get("status", "active")
        if status == "completed":
            return {"error": "Cannot reactivate a completed schedule"}
        if status == "active":
            return {"status": "already_active", "schedule_id": schedule_id}
        sent = record.get("sent", 0)
        count = record.get("count", 0)
        if sent >= count:
            record["status"] = "completed"
            sched_file = self._schedules_dir / schedule_id / "schedule.json"
            self._write_schedule(sched_file, record)
            return {"error": "Cannot reactivate a completed schedule"}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["status"] = "active"
        record["last_sent_at"] = now
        sched_file = self._schedules_dir / schedule_id / "schedule.json"
        self._write_schedule(sched_file, record)
        return {"status": "reactivated", "schedule_id": schedule_id}

    def _schedule_list(self) -> dict:
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return {"status": "ok", "schedules": []}

        entries = []
        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue
            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue
            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            payload = record.get("send_payload", {})
            address = payload.get("address", "")
            if isinstance(address, list):
                address = ", ".join(address)

            sent = record.get("sent", 0)
            count = record.get("count", 0)

            entries.append(scrub_time_fields(self._agent, {
                "schedule_id": record.get("schedule_id", sched_dir.name),
                "to": address,
                "subject": payload.get("subject", ""),
                "interval": record.get("interval", 0),
                "count": count,
                "sent": sent,
                "status": record.get("status", "active"),
                "created_at": record.get("created_at", ""),
                "last_sent_at": record.get("last_sent_at"),
            }))

        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return {"status": "ok", "schedules": entries}

    # ------------------------------------------------------------------
    # Schedule helpers
    # ------------------------------------------------------------------

    def _write_schedule(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(record, indent=2, default=str).encode())
            os.close(fd)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _read_schedule(self, schedule_id: str) -> dict | None:
        path = self._schedules_dir / schedule_id / "schedule.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _set_schedule_status(self, schedule_id: str, status: str) -> bool:
        record = self._read_schedule(schedule_id)
        if record is None:
            return False
        record["status"] = status
        sched_file = self._schedules_dir / schedule_id / "schedule.json"
        self._write_schedule(sched_file, record)
        return True

    def _reconcile_schedules_on_startup(self) -> None:
        """Flip every non-completed schedule to inactive on agent startup."""
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return
        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue
            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue
            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            status = record.get("status", "active")
            if status == "completed":
                continue
            if status == "inactive":
                continue
            record["status"] = "inactive"
            try:
                self._write_schedule(sched_file, record)
            except OSError:
                continue

    def _scheduler_loop(self) -> None:
        """Single polling loop that drives all schedules from disk state."""
        while not self._stop_event.is_set():
            try:
                self._scheduler_tick()
            except Exception:
                pass
            self._stop_event.wait(timeout=1.0)

    def _scheduler_tick(self) -> None:
        """One scan of all schedule folders."""
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return

        now = datetime.now(timezone.utc)

        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue

            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue

            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            status = record.get("status", "active")
            if status != "active":
                continue

            sent = record.get("sent", 0)
            count = record.get("count", 0)
            if sent >= count:
                continue

            last_sent_at = record.get("last_sent_at")
            if last_sent_at is not None:
                try:
                    last_dt = datetime.strptime(last_sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                interval = record.get("interval", 0)
                due_at = last_dt + timedelta(seconds=interval)
                if now < due_at:
                    continue

            seq = sent + 1
            record["sent"] = seq
            self._write_schedule(sched_file, record)

            send_payload = record.get("send_payload", {})
            remaining = count - seq
            interval = record.get("interval", 0)
            estimated_finish = (now + timedelta(seconds=remaining * interval)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            schedule_meta = {
                "schedule_id": record.get("schedule_id", sched_dir.name),
                "seq": seq,
                "total": count,
                "interval": interval,
                "scheduled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "estimated_finish": estimated_finish,
            }
            send_args = {**send_payload, "_schedule": schedule_meta}
            result = self._send(send_args)

            if result.get("error") or result.get("status") == "blocked":
                record["last_sent_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                self._write_schedule(sched_file, record)
                continue

            to_label = send_payload.get("address", "")
            subj_label = send_payload.get("subject", "(no subject)")
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if seq < count:
                next_at = (now + timedelta(seconds=interval)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                note = (
                    f"[schedule {seq}/{count}] sent to {to_label} "
                    f"| subject: {subj_label} "
                    f"| sent at {ts} "
                    f"| next at {next_at} "
                    f"| ends ~{estimated_finish}"
                )
            else:
                note = (
                    f"[schedule {seq}/{count}] sent to {to_label} "
                    f"| subject: {subj_label} "
                    f"| sent at {ts} "
                    f"| schedule complete"
                )
            self._agent._log(
                "schedule_send", schedule_id=schedule_meta["schedule_id"],
                seq=seq, total=count, to=to_label, subject=subj_label,
            )
            msg = _make_message(MSG_REQUEST, "system", note)
            self._agent.inbox.put(msg)

            record["last_sent_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if seq >= count:
                record["status"] = "completed"
            self._write_schedule(sched_file, record)

    # ------------------------------------------------------------------
    # Send — deliver + save to sent/
    # ------------------------------------------------------------------

    def _send(self, args: dict) -> dict:
        raw_address = args.get("address", "")
        subject = args.get("subject", "")
        message_text = args.get("message", "")
        mail_type = args.get("type", "normal")
        cc = args.get("cc") or []
        bcc = args.get("bcc") or []
        delay = args.get("delay", 0)
        mode = args.get("mode", "peer")

        to_list = _coerce_address_list(raw_address)

        if not to_list:
            return {"error": "address is required"}
        if mode not in ("peer", "abs"):
            return {"error": f"invalid mode: {mode!r} (must be peer or abs)"}

        all_targets = to_list + cc + bcc
        if args.get("_schedule"):
            duplicates = []
        else:
            duplicates = [
                addr for addr in all_targets
                if (prev := self._last_sent.get(addr)) is not None
                and prev[0] == message_text
                and prev[1] >= self._dup_free_passes
            ]
        if duplicates:
            return {
                "status": "blocked",
                "warning": (
                    "Identical message already sent to: "
                    f"{', '.join(duplicates)}. "
                    "This looks like a repetitive loop — "
                    "think twice before sending."
                ),
            }

        sender = (self._agent._mail_service.address
                  if self._agent._mail_service is not None and self._agent._mail_service.address
                  else self._agent._working_dir.name)

        base_payload = {
            "from": sender,
            "to": to_list,
            "subject": subject,
            "message": message_text,
            "type": mail_type,
            "mode": mode,
            "identity": self._agent._build_manifest(),
        }
        if cc:
            base_payload["cc"] = cc
        attachments = args.get("attachments", [])
        if attachments:
            base_payload["attachments"] = attachments

        deliver_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        all_recipients = to_list + cc + bcc

        # For cross-project (abs) emails, use full path as sender
        # so the recipient can reply to the correct address.
        abs_sender = str(self._agent._working_dir) if mode == "abs" else None

        for addr in all_recipients:
            dispatch_payload = dict(base_payload)
            dispatch_payload["_dispatch_to"] = addr
            dispatch_payload["_mode"] = mode
            if abs_sender is not None:
                dispatch_payload["from"] = abs_sender
            msg_id = _persist_to_outbox(self._agent, dispatch_payload, deliver_at)
            tt = threading.Thread(
                target=_mailman,
                args=(self._agent, msg_id, dispatch_payload, deliver_at),
                kwargs={"skip_sent": True},
                name=f"mailman-{msg_id[:8]}",
                daemon=True,
            )
            tt.start()

        sent_id = _new_mailbox_id()
        sent_dir = self._mailbox_path / "sent" / sent_id
        sent_dir.mkdir(parents=True, exist_ok=True)
        sent_payload = dict(base_payload)
        if abs_sender is not None:
            sent_payload["from"] = abs_sender
        sent_record = {
            **sent_payload,
            "_mailbox_id": sent_id,
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delay": delay,
        }
        if bcc:
            sent_record["bcc"] = bcc
        if args.get("_schedule"):
            sent_record["_schedule"] = args["_schedule"]
        (sent_dir / "message.json").write_text(
            json.dumps(sent_record, indent=2, default=str)
        )

        for addr in all_recipients:
            prev = self._last_sent.get(addr)
            if prev is not None and prev[0] == message_text:
                self._last_sent[addr] = (message_text, prev[1] + 1)
            else:
                self._last_sent[addr] = (message_text, 1)

        self._agent._log(
            "email_sent", to=to_list, cc=cc, bcc=bcc,
            subject=subject, message=message_text, delay=delay,
        )

        return {"status": "sent", "to": to_list, "cc": cc, "bcc": bcc, "delay": delay}

    # ------------------------------------------------------------------
    # Check / Read / Reply / Search / Archive / Delete
    # ------------------------------------------------------------------

    def _check(self, args: dict) -> dict:
        folder = args.get("folder", "inbox")
        n = args.get("n", 10)
        f = args.get("filter") or {}
        sort = f.get("sort", "newest")
        truncate = f.get("truncate", 500)

        emails = self._list_emails(folder)
        read_set = _read_ids(self._agent)

        if f.get("from"):
            ff = f["from"].lower()
            emails = [e for e in emails if ff in (e.get("from") or "").lower()]
        if f.get("subject"):
            sf = f["subject"].lower()
            emails = [e for e in emails if sf in (e.get("subject") or "").lower()]
        if f.get("contains"):
            cf = f["contains"].lower()
            emails = [e for e in emails if cf in (e.get("message") or "").lower()]
        if f.get("after"):
            emails = [e for e in emails if _email_time(e) >= f["after"]]
        if f.get("before"):
            emails = [e for e in emails if _email_time(e) <= f["before"]]
        if f.get("unread_only"):
            emails = [e for e in emails if e.get("_mailbox_id", "") not in read_set]
        if f.get("has_attachments"):
            emails = [e for e in emails if e.get("attachments")]

        if sort == "oldest":
            emails = list(reversed(emails))

        total = len(emails)
        recent = emails[:n] if n > 0 else emails
        summaries = [scrub_time_fields(self._agent, self._email_summary(e, read_set, truncate=truncate)) for e in recent]

        result = {"status": "ok", "total": total, "showing": len(summaries), "emails": summaries}
        tokens = count_tokens(json.dumps(result, ensure_ascii=False))
        if tokens > 10_000:
            while summaries and count_tokens(json.dumps(result, ensure_ascii=False)) > 10_000:
                summaries.pop()
                result["emails"] = summaries
                result["showing"] = len(summaries)
            result["truncated_by_budget"] = total - len(summaries)

        return result

    def _read(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        folder = args.get("folder")

        results = []
        errors = []
        read_state_changed = False
        for eid in ids:
            if folder:
                path = self._mailbox_path / folder / eid / "message.json"
                if path.is_file():
                    try:
                        data = json.loads(path.read_text())
                        data["_folder"] = folder
                        data.setdefault("_mailbox_id", eid)
                    except (json.JSONDecodeError, OSError):
                        errors.append(eid)
                        continue
                else:
                    errors.append(eid)
                    continue
            else:
                data = self._load_email(eid)
                if data is None:
                    errors.append(eid)
                    continue
            if data.get("_folder") == "inbox":
                _mark_read(self._agent, eid)
                read_state_changed = True
            entry = {
                "id": eid,
                "from": data.get("from", ""),
                "to": data.get("to", []),
                "subject": data.get("subject", "(no subject)"),
                "message": data.get("message", ""),
                "time": data.get("received_at") or data.get("sent_at") or data.get("time") or "",
                "folder": data.get("_folder", ""),
            }
            if data.get("cc"):
                entry["cc"] = data["cc"]
            if data.get("attachments"):
                entry["attachments"] = data["attachments"]
            self._inject_identity(entry, data)
            results.append(scrub_time_fields(self._agent, entry))

        if read_state_changed:
            self._rerender_unread_digest()

        result = {"status": "ok", "emails": results}
        if errors:
            result["not_found"] = errors

        return result

    def _dismiss(self, args: dict) -> dict:
        """Mark inbox emails as read without returning their content.

        ``dismiss`` is the lightweight cousin of ``read``: same effect
        on the unread set and the notification, but no email bodies
        come back in the result.  Use it when the agent has already
        seen the content (e.g. via the unread digest) and just wants
        to clear the notification entry for a list of IDs.

        Returns ``{"status": "ok", "dismissed": [...]}`` with the IDs
        actually marked.  Non-inbox or missing IDs go into
        ``not_found`` so the caller can detect partial failures.
        """
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        dismissed: list[str] = []
        not_found: list[str] = []
        for eid in ids:
            data = self._load_email(eid)
            if data is None or data.get("_folder") != "inbox":
                not_found.append(eid)
                continue
            _mark_read(self._agent, eid)
            dismissed.append(eid)

        if dismissed:
            self._rerender_unread_digest()

        result: dict = {"status": "ok", "dismissed": dismissed}
        if not_found:
            result["not_found"] = not_found
        return result

    def _lookup(self, email_id: str) -> dict | None:
        return self._load_email(email_id)

    @staticmethod
    def _reply_subject(original: dict, override: str | None) -> str:
        """Derive a reply subject. If the original has no subject (every
        "naked" message), synthesize one from the first body line so the
        reply doesn't ship as literal "Re: " — bad for self-recall and
        rejected by some IMAP relays.
        """
        if override:
            return override
        orig_subject = (original.get("subject") or "").strip()
        if not orig_subject:
            body_first = (original.get("message") or "").strip().split("\n", 1)[0]
            orig_subject = body_first[:30] if body_first else "(no subject)"
        return orig_subject if orig_subject.startswith("Re: ") else f"Re: {orig_subject}"

    def _reply(self, args: dict) -> dict:
        email_id = args.get("email_id", "")
        if isinstance(email_id, list):
            email_id = email_id[0] if email_id else ""
        if not email_id:
            return {"error": "email_id is required for reply"}
        message_text = args.get("message", "")
        if not message_text:
            return {"error": "message is required for reply"}

        original = self._lookup(email_id)
        if original is None:
            return {"error": f"Email not found: {email_id}"}

        return self._send({
            "address": original["from"],
            "subject": self._reply_subject(original, args.get("subject")),
            "message": message_text,
            "cc": args.get("cc") or [],
            "bcc": args.get("bcc") or [],
        })

    def _reply_all(self, args: dict) -> dict:
        email_id = args.get("email_id", "")
        if isinstance(email_id, list):
            email_id = email_id[0] if email_id else ""
        if not email_id:
            return {"error": "email_id is required for reply_all"}
        message_text = args.get("message", "")
        if not message_text:
            return {"error": "message is required for reply_all"}

        original = self._lookup(email_id)
        if original is None:
            return {"error": f"Email not found: {email_id}"}

        my_address = (
            self._agent._mail_service.address
            if self._agent._mail_service
            else self._agent._working_dir.name
        )

        reply_to = original["from"]
        orig_to = original.get("to") or []
        if isinstance(orig_to, str):
            orig_to = [orig_to]
        orig_cc = original.get("cc") or []
        other_recipients = [
            addr for addr in orig_to + orig_cc
            if addr != my_address and addr != reply_to
        ]

        extra_cc = args.get("cc") or []
        extra_bcc = args.get("bcc") or []

        return self._send({
            "address": reply_to,
            "subject": self._reply_subject(original, args.get("subject")),
            "message": message_text,
            "cc": other_recipients + extra_cc,
            "bcc": extra_bcc,
        })

    def _search(self, args: dict) -> dict:
        query = args.get("query", "")
        if not query:
            return {"error": "query is required for search"}

        folder = args.get("folder")
        folders = [folder] if folder else ["inbox", "sent"]

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        matches = []
        read_set = _read_ids(self._agent)
        for f in folders:
            for email in self._list_emails(f):
                searchable = " ".join([
                    email.get("from", ""),
                    email.get("subject", ""),
                    email.get("message", ""),
                ])
                if pattern.search(searchable):
                    matches.append(self._email_summary(email, read_set))

        return {"status": "ok", "total": len(matches), "emails": matches}

    def _archive(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        archived = []
        not_found = []
        archive_dir = self._mailbox_path / "archive"
        inbox_dir = self._mailbox_path / "inbox"

        for eid in ids:
            src = inbox_dir / eid
            if not src.is_dir():
                not_found.append(eid)
                continue
            dst = archive_dir / eid
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            archived.append(eid)

        if archived:
            read_set = _read_ids(self._agent)
            read_set -= set(archived)
            _save_read_ids(self._agent, read_set)
            # Archive removes mail from inbox — the unread set shrinks
            # too.  Rerender so .notification/email.json reflects the
            # new state on the next heartbeat sync.
            self._rerender_unread_digest()

        result: dict = {"status": "ok", "archived": archived}
        if not_found:
            result["not_found"] = not_found
        return result

    def _delete(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        folder = args.get("folder", "inbox")
        if folder not in ("inbox", "archive"):
            return {"error": f"Cannot delete from folder: {folder}"}

        folder_dir = self._mailbox_path / folder
        deleted = []
        not_found = []

        for eid in ids:
            target = folder_dir / eid
            if target.is_dir():
                shutil.rmtree(target)
                deleted.append(eid)
            else:
                not_found.append(eid)

        if deleted:
            read_set = _read_ids(self._agent)
            read_set -= set(deleted)
            _save_read_ids(self._agent, read_set)
            # Delete from inbox shrinks the unread set the same way
            # archive does.  Rerender so the wire's notification
            # updates on the next heartbeat sync.
            if folder == "inbox":
                self._rerender_unread_digest()

        result: dict = {"status": "ok", "deleted": deleted}
        if not_found:
            result["not_found"] = not_found
        return result

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    @property
    def _contacts_path(self) -> Path:
        return self._mailbox_path / "contacts.json"

    def _load_contacts(self) -> list[dict]:
        if self._contacts_path.is_file():
            try:
                return json.loads(self._contacts_path.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_contacts(self, contacts: list[dict]) -> None:
        self._mailbox_path.mkdir(parents=True, exist_ok=True)
        target = self._contacts_path
        fd, tmp = tempfile.mkstemp(dir=str(self._mailbox_path), suffix=".tmp")
        try:
            os.write(fd, json.dumps(contacts, indent=2).encode())
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _contacts(self) -> dict:
        return {"status": "ok", "contacts": self._load_contacts()}

    def _add_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        name = args.get("name", "")
        if not address:
            return {"error": "address is required"}
        if not name:
            return {"error": "name is required"}
        note = args.get("note", "")
        contacts = self._load_contacts()
        for c in contacts:
            if c["address"] == address:
                c["name"] = name
                c["note"] = note
                self._save_contacts(contacts)
                return {"status": "updated", "contact": c}
        entry: dict = {"address": address, "name": name, "note": note}
        contacts.append(entry)
        self._save_contacts(contacts)
        return {"status": "added", "contact": entry}

    def _remove_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        if not address:
            return {"error": "address is required"}
        contacts = self._load_contacts()
        new_contacts = [c for c in contacts if c["address"] != address]
        if len(new_contacts) == len(contacts):
            return {"error": f"Contact not found: {address}"}
        self._save_contacts(new_contacts)
        return {"status": "removed", "address": address}

    def _edit_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        if not address:
            return {"error": "address is required"}
        contacts = self._load_contacts()
        for c in contacts:
            if c["address"] == address:
                if "name" in args:
                    c["name"] = args["name"]
                if "note" in args:
                    c["note"] = args["note"]
                self._save_contacts(contacts)
                return {"status": "updated", "contact": c}
        return {"error": f"Contact not found: {address}"}
