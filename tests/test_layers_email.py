"""Tests for the email capability (filesystem-based mailbox)."""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _make_inbox_email(working_dir, *, sender="sender", to=None, subject="test",
                       message="body", cc=None, attachments=None):
    """Create an email on disk in mailbox/inbox/{uuid}/message.json.
    Returns the email_id (directory name)."""
    email_id = str(uuid4())
    msg_dir = working_dir / "mailbox" / "inbox" / email_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "_mailbox_id": email_id,
        "from": sender,
        "to": to or ["test"],
        "subject": subject,
        "message": message,
        "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if cc:
        data["cc"] = cc
    if attachments:
        data["attachments"] = attachments
    (msg_dir / "message.json").write_text(json.dumps(data, indent=2))
    return email_id


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def test_email_intrinsic_registers_tool(tmp_path):
    """Email is now an intrinsic; it appears in _intrinsics with a manager."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    assert "email" in agent._intrinsics
    assert mgr is not None


# ---------------------------------------------------------------------------
# Receive interception
# ---------------------------------------------------------------------------

def test_email_receive_notification(tmp_path):
    """Incoming mail should publish ``.notification/email.json`` with the
    current unread digest.  Under the .notification/ filesystem redesign,
    arrivals no longer enqueue on tc_inbox — the kernel's notification
    sync mechanism reads the file on its next heartbeat tick and injects
    the wire pair.
    """
    from lingtai_kernel.notifications import collect_notifications

    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    # Mail must be on disk before the digest renderer runs.
    _make_inbox_email(agent.working_dir, sender="sender", subject="hi", message="body")
    agent._on_mail_received({
        "_mailbox_id": "abc123",
        "from": "sender",
        "to": ["test"],
        "subject": "hi",
        "message": "body",
    })
    # tc_inbox should be empty under the new path.
    assert len(agent._tc_inbox.drain()) == 0
    # The notification file carries the digest.
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1
    digest = out["email"]["data"]["digest"]
    assert "hi" in digest
    assert "sender" in digest


def test_email_receive_fallback_id(tmp_path):
    """Digest should still publish even when arrival payload omits _mailbox_id."""
    from lingtai_kernel.notifications import collect_notifications

    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    _make_inbox_email(agent.working_dir, sender="sender", subject="(no subj)", message="body")
    agent._on_mail_received({"from": "sender", "message": "body"})
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1


def test_email_receive_via_agent(tmp_path):
    """After add_capability('email'), agent._on_mail_received publishes
    the unread digest to ``.notification/email.json``."""
    from lingtai_kernel.notifications import collect_notifications

    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    _make_inbox_email(agent.working_dir, sender="sender", subject="hi", message="body")
    agent._on_mail_received({
        "_mailbox_id": "xyz",
        "from": "sender",
        "to": ["test"],
        "subject": "hi",
        "message": "body",
    })
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1


# ---------------------------------------------------------------------------
# Mailbox: check, read
# ---------------------------------------------------------------------------

def test_email_check_inbox(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    _make_inbox_email(agent.working_dir, sender="a", subject="s1", message="m1")
    _make_inbox_email(agent.working_dir, sender="b", subject="s2", message="m2")
    result = mgr.handle({"action": "check"})
    assert result["status"] == "ok"
    assert result["total"] == 2
    assert all("id" in e for e in result["emails"])


def test_email_check_sent(tmp_path):
    """check with folder=sent should show sent emails."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    mgr.handle({"action": "send", "address": "someone", "message": "hello", "subject": "test"})
    result = mgr.handle({"action": "check", "folder": "sent"})
    assert result["total"] == 1
    assert result["emails"][0]["from"] == "me"


def test_email_check_empty_mailbox(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"action": "check"})
    assert result["status"] == "ok"
    assert result["total"] == 0


def test_email_read_by_id(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, sender="sender", subject="topic", message="full body")
    result = mgr.handle({"action": "read", "email_id": eid})
    assert result["status"] == "ok"
    assert len(result["emails"]) == 1
    assert result["emails"][0]["message"] == "full body"
    assert result["emails"][0]["subject"] == "topic"


def test_email_read_marks_as_read(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, message="m")
    # First check — should be unread
    result = mgr.handle({"action": "check"})
    assert result["emails"][0]["unread"] is True
    # Read it
    mgr.handle({"action": "read", "email_id": eid})
    # Now should be read
    result = mgr.handle({"action": "check"})
    assert result["emails"][0]["unread"] is False


def test_email_read_shows_attachments(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, subject="photo", message="look",
                            attachments=["/path/to/photo.png"])
    result = mgr.handle({"action": "read", "email_id": eid})
    assert result["status"] == "ok"
    assert "attachments" in result["emails"][0]
    assert any("photo.png" in p for p in result["emails"][0]["attachments"])


# ---------------------------------------------------------------------------
# Send — outbox → mailman pipeline
# ---------------------------------------------------------------------------

def test_email_send_through_mailman(tmp_path):
    """Email send goes through outbox → mailman → sent."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send", "address": "someone",
        "message": "hello", "subject": "test",
    })
    assert result["status"] == "sent"
    assert result["delay"] == 0
    time.sleep(0.5)
    sent_dir = agent.working_dir / "mailbox" / "sent"
    assert sent_dir.is_dir()
    sent_items = list(sent_dir.iterdir())
    assert len(sent_items) == 1
    msg = json.loads((sent_items[0] / "message.json").read_text())
    assert msg["message"] == "hello"
    assert msg["sent_at"]


def test_email_send_with_delay(tmp_path):
    """Email send with delay dispatches after waiting."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send", "address": "someone",
        "message": "delayed", "delay": 1,
    })
    assert result["status"] == "sent"
    assert result["delay"] == 1
    mail_svc.send.assert_not_called()
    time.sleep(1.5)
    mail_svc.send.assert_called_once()


def test_email_send_cc_one_sent_record(tmp_path):
    """CC/BCC email produces one sent record, not one per recipient."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send", "address": ["a", "b"],
        "cc": ["c"], "bcc": ["d"],
        "message": "broadcast", "subject": "multi",
    })
    assert result["status"] == "sent"
    time.sleep(0.5)
    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_items = list(sent_dir.iterdir())
    assert len(sent_items) == 1  # ONE sent record
    msg = json.loads((sent_items[0] / "message.json").read_text())
    assert msg["bcc"] == ["d"]


# ---------------------------------------------------------------------------
# Send — saves to sent/
# ---------------------------------------------------------------------------

def test_email_send_saves_to_sent(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send", "address": "someone",
        "message": "hello", "subject": "test",
    })
    assert result["status"] == "sent"
    sent_dir = agent.working_dir / "mailbox" / "sent"
    assert sent_dir.is_dir()
    sent_emails = list(sent_dir.iterdir())
    assert len(sent_emails) == 1
    msg = json.loads((sent_emails[0] / "message.json").read_text())
    assert msg["message"] == "hello"
    assert msg["sent_at"]


def test_email_send_saves_bcc_in_sent(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    mgr.handle({
        "action": "send", "address": "someone",
        "message": "secret", "bcc": ["hidden"],
    })
    sent_dir = agent.working_dir / "mailbox" / "sent"
    msg = json.loads(list(sent_dir.iterdir())[0].joinpath("message.json").read_text())
    assert msg["bcc"] == ["hidden"]


def test_email_blocks_identical_consecutive_send(tmp_path):
    """Sending the exact same message twice to the same recipient is blocked."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "127.0.0.1:9999"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    mgr._dup_free_passes = 1

    # First send — should work
    result = mgr.handle({
        "action": "send", "address": "127.0.0.1:8888",
        "subject": "hi", "message": "thumbs up",
    })
    assert result["status"] == "sent"

    # Identical send — should be blocked
    result = mgr.handle({
        "action": "send", "address": "127.0.0.1:8888",
        "subject": "hi", "message": "thumbs up",
    })
    assert result["status"] == "blocked"
    assert "warning" in result

    # Different message — should work
    result = mgr.handle({
        "action": "send", "address": "127.0.0.1:8888",
        "subject": "hi", "message": "Got it, thanks!",
    })
    assert result["status"] == "sent"


def test_email_blocks_identical_reply(tmp_path):
    """Replying with the same message twice is blocked."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "127.0.0.1:9999"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    mgr._dup_free_passes = 1

    # Create an inbox email to reply to
    _make_inbox_email(agent.working_dir, sender="127.0.0.1:8888", subject="hello", message="hi there")
    check = mgr.handle({"action": "check"})
    email_id = check["emails"][0]["id"]

    # First reply
    result = mgr.handle({"action": "reply", "email_id": email_id, "message": "thumbs up"})
    assert result["status"] == "sent"

    # Identical reply — blocked
    result = mgr.handle({"action": "reply", "email_id": email_id, "message": "thumbs up"})
    assert result["status"] == "blocked"


def test_email_send_with_attachments(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "127.0.0.1:9999"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send",
        "address": "127.0.0.1:8888",
        "subject": "file for you",
        "message": "see attached",
        "attachments": ["/path/to/file.png"],
    })
    assert result["status"] == "sent"
    time.sleep(0.5)
    sent = mail_svc.send.call_args[0][1]
    assert sent.get("attachments") == ["/path/to/file.png"]


# ---------------------------------------------------------------------------
# Send — Filesystem integration tests
# ---------------------------------------------------------------------------

def _setup_receiver(tmp_path, name, stop_event):
    """Create a receiver agent dir with heartbeat and FilesystemMailService."""
    from lingtai_kernel.services.mail import FilesystemMailService
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ".agent.json").write_text(json.dumps({"agent_name": name}))
    (d / ".agent.heartbeat").write_text(str(time.time()))
    def _hb():
        while not stop_event.is_set():
            try:
                (d / ".agent.heartbeat").write_text(str(time.time()))
            except OSError:
                pass
            stop_event.wait(0.5)
    threading.Thread(target=_hb, daemon=True).start()
    svc = FilesystemMailService(working_dir=d)
    return d, svc


def test_email_send_multi_to(tmp_path):
    """email send should deliver to multiple addresses."""
    from lingtai_kernel.services.mail import FilesystemMailService

    stop = threading.Event()
    received = {0: [], 1: []}
    events = [threading.Event(), threading.Event()]
    services = []
    addrs = []
    for i in range(2):
        d, svc = _setup_receiver(tmp_path, f"recv{i}", stop)
        svc.listen(on_message=lambda msg, idx=i: (received[idx].append(msg), events[idx].set()))
        services.append(svc)
        addrs.append(str(d))

    try:
        sender_dir = tmp_path / "sender"
        sender_dir.mkdir()
        sender_svc = FilesystemMailService(working_dir=sender_dir)
        agent = Agent(service=make_mock_service(), agent_name="sender", working_dir=tmp_path / "test", mail_service=sender_svc)
        mgr = agent._email_manager
        result = mgr.handle({"action": "send", "address": addrs, "message": "multi-to"})
        assert result["status"] == "sent"
        for ev in events:
            assert ev.wait(timeout=5.0)
        for i in range(2):
            assert received[i][0]["message"] == "multi-to"
    finally:
        for svc in services:
            svc.stop()
        stop.set()


def test_email_send_cc_visible(tmp_path):
    """CC addresses should receive the email with cc field visible."""
    from lingtai_kernel.services.mail import FilesystemMailService

    stop = threading.Event()
    received = {0: [], 1: []}
    events = [threading.Event(), threading.Event()]
    services = []
    addrs = []
    for i in range(2):
        d, svc = _setup_receiver(tmp_path, f"recv{i}", stop)
        svc.listen(on_message=lambda msg, idx=i: (received[idx].append(msg), events[idx].set()))
        services.append(svc)
        addrs.append(str(d))

    try:
        sender_dir = tmp_path / "sender"
        sender_dir.mkdir()
        sender_svc = FilesystemMailService(working_dir=sender_dir)
        agent = Agent(service=make_mock_service(), agent_name="sender", working_dir=tmp_path / "test", mail_service=sender_svc)
        mgr = agent._email_manager
        to_addr = addrs[0]
        cc_addr = addrs[1]
        result = mgr.handle({"action": "send", "address": to_addr, "message": "cc test", "cc": [cc_addr]})
        assert result["status"] == "sent"
        for ev in events:
            assert ev.wait(timeout=5.0)
        assert received[0][0]["cc"] == [cc_addr]
        assert received[1][0]["cc"] == [cc_addr]
    finally:
        for svc in services:
            svc.stop()
        stop.set()


def test_email_send_bcc_hidden(tmp_path):
    """BCC addresses should receive the email but bcc field should NOT be in payload."""
    from lingtai_kernel.services.mail import FilesystemMailService

    stop = threading.Event()
    received = {0: [], 1: []}
    events = [threading.Event(), threading.Event()]
    services = []
    addrs = []
    for i in range(2):
        d, svc = _setup_receiver(tmp_path, f"recv{i}", stop)
        svc.listen(on_message=lambda msg, idx=i: (received[idx].append(msg), events[idx].set()))
        services.append(svc)
        addrs.append(str(d))

    try:
        sender_dir = tmp_path / "sender"
        sender_dir.mkdir()
        sender_svc = FilesystemMailService(working_dir=sender_dir)
        agent = Agent(service=make_mock_service(), agent_name="sender", working_dir=tmp_path / "test", mail_service=sender_svc)
        mgr = agent._email_manager
        to_addr = addrs[0]
        bcc_addr = addrs[1]
        result = mgr.handle({"action": "send", "address": to_addr, "message": "bcc test", "bcc": [bcc_addr]})
        assert result["status"] == "sent"
        for ev in events:
            assert ev.wait(timeout=5.0)
        assert received[0][0]["message"] == "bcc test"
        assert received[1][0]["message"] == "bcc test"
        assert "bcc" not in received[0][0]
        assert "bcc" not in received[1][0]
    finally:
        for svc in services:
            svc.stop()
        stop.set()


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------

def test_email_reply(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="replier", working_dir=tmp_path / "test")
    mock_svc = MagicMock()
    mock_svc.address = "me"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, sender="alice", subject="Original topic", message="Please respond")
    result = mgr.handle({"action": "reply", "email_id": eid, "message": "Here is my reply"})
    assert result["status"] == "sent"
    time.sleep(0.5)
    sent_payload = mock_svc.send.call_args[0][1]
    assert sent_payload["subject"] == "Re: Original topic"
    assert sent_payload["message"] == "Here is my reply"


def test_email_reply_no_double_re(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="replier", working_dir=tmp_path / "test")
    mock_svc = MagicMock()
    mock_svc.address = "me"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, sender="other", subject="Re: Already replied", message="text")
    result = mgr.handle({"action": "reply", "email_id": eid, "message": "follow up"})
    time.sleep(0.5)
    sent_payload = mock_svc.send.call_args[0][1]
    assert sent_payload["subject"] == "Re: Already replied"


# ---------------------------------------------------------------------------
# Reply All
# ---------------------------------------------------------------------------

def test_email_reply_all(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="replier", working_dir=tmp_path / "test")
    mock_svc = MagicMock()
    mock_svc.address = "me"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, sender="alice", to=["me", "bob"],
                            cc=["charlie"], subject="Group thread", message="discussion")
    result = mgr.handle({"action": "reply_all", "email_id": eid, "message": "my thoughts"})
    assert result["status"] == "sent"
    time.sleep(0.5)
    sent_addresses = [call[0][0] for call in mock_svc.send.call_args_list]
    assert "alice" in sent_addresses
    assert "bob" in sent_addresses
    assert "charlie" in sent_addresses
    assert "me" not in sent_addresses


def test_email_reply_all_excludes_self(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="replier", working_dir=tmp_path / "test")
    mock_svc = MagicMock()
    mock_svc.address = "me"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, sender="alice", to=["me", "alice"],
                            subject="Self-cc", message="text")
    result = mgr.handle({"action": "reply_all", "email_id": eid, "message": "reply"})
    assert result["status"] == "sent"
    time.sleep(0.5)
    sent_addresses = [call[0][0] for call in mock_svc.send.call_args_list]
    assert sent_addresses.count("alice") == 1
    assert "me" not in sent_addresses


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_email_search_by_subject(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    _make_inbox_email(agent.working_dir, subject="important meeting", message="body1")
    _make_inbox_email(agent.working_dir, subject="casual chat", message="body2")
    result = mgr.handle({"action": "search", "query": "important"})
    assert result["total"] == 1
    assert "important" in result["emails"][0]["subject"]


def test_email_search_by_sender(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    _make_inbox_email(agent.working_dir, sender="alice@test", message="hello")
    _make_inbox_email(agent.working_dir, sender="bob@test", message="world")
    result = mgr.handle({"action": "search", "query": "alice"})
    assert result["total"] == 1


def test_email_search_by_message_body(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    _make_inbox_email(agent.working_dir, message="the secret code is 42")
    _make_inbox_email(agent.working_dir, message="nothing interesting")
    result = mgr.handle({"action": "search", "query": "secret.*42"})
    assert result["total"] == 1


def test_email_search_folder_filter(tmp_path):
    """Search with folder param should only search that folder."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    _make_inbox_email(agent.working_dir, message="keyword in inbox")
    mgr.handle({"action": "send", "address": "someone", "message": "keyword in sent"})
    # Search both — should find 2
    result = mgr.handle({"action": "search", "query": "keyword"})
    assert result["total"] == 2
    # Search inbox only — should find 1
    result = mgr.handle({"action": "search", "query": "keyword", "folder": "inbox"})
    assert result["total"] == 1


def test_email_search_invalid_regex(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"action": "search", "query": "[invalid"})
    assert "error" in result


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_email_without_mail_service(tmp_path):
    """Send without mail service succeeds at send-time."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent._mail_service = None
    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send", "address": "someone",
        "message": "hello",
    })
    assert result["status"] == "sent"
    sent_dir = agent.working_dir / "mailbox" / "sent"
    assert sent_dir.is_dir()


def test_email_read_not_found(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"action": "read", "email_id": "nonexistent"})
    assert result["status"] == "ok"
    assert result["not_found"] == ["nonexistent"]


def test_email_intrinsic_no_mail_intrinsic(tmp_path):
    """After collapse, mail intrinsic is gone — email replaced it."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    assert "mail" not in agent._intrinsics
    assert "email" in agent._intrinsics
    agent.stop(timeout=1.0)


# Private mode tests removed — feature was deleted in lingtai-kernel 0.7.5.
# private_mode was an outbound contact-allowlist gate on EmailManager._send;
# it had zero live callers (only dated planning docs). Contacts machinery
# (add/remove/edit/list) stays — it's still a useful contact book without
# the enforcement.


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def test_email_archive_moves_to_archive(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="keep this")
    mgr = agent._email_manager
    result = mgr.handle({"action": "archive", "email_id": [email_id]})
    assert result["status"] == "ok"
    assert email_id in result["archived"]
    inbox = agent.working_dir / "mailbox" / "inbox" / email_id
    assert not inbox.exists()
    archive = agent.working_dir / "mailbox" / "archive" / email_id
    assert archive.is_dir()
    msg = json.loads((archive / "message.json").read_text())
    assert msg["subject"] == "keep this"


def test_email_archive_not_found(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"action": "archive", "email_id": ["nonexistent"]})
    assert result["not_found"] == ["nonexistent"]


def test_email_check_archive_folder(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="archived msg")
    mgr = agent._email_manager
    mgr.handle({"action": "archive", "email_id": [email_id]})
    result = mgr.handle({"action": "check", "folder": "archive"})
    assert result["total"] == 1
    assert result["emails"][0]["id"] == email_id


def test_email_read_archive_folder(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="archived")
    mgr = agent._email_manager
    mgr.handle({"action": "archive", "email_id": [email_id]})
    result = mgr.handle({"action": "read", "email_id": [email_id], "folder": "archive"})
    assert len(result["emails"]) == 1
    assert result["emails"][0]["subject"] == "archived"


def test_email_search_archive_folder(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="unique archived topic")
    mgr = agent._email_manager
    mgr.handle({"action": "archive", "email_id": [email_id]})
    result = mgr.handle({"action": "search", "query": "unique archived", "folder": "archive"})
    assert result["total"] == 1


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_email_delete_from_inbox(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="delete me")
    mgr = agent._email_manager
    result = mgr.handle({"action": "delete", "email_id": [email_id]})
    assert email_id in result["deleted"]
    inbox = agent.working_dir / "mailbox" / "inbox" / email_id
    assert not inbox.exists()


def test_email_delete_from_archive(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="archive then delete")
    mgr = agent._email_manager
    mgr.handle({"action": "archive", "email_id": [email_id]})
    result = mgr.handle({"action": "delete", "email_id": [email_id], "folder": "archive"})
    assert email_id in result["deleted"]
    archive = agent.working_dir / "mailbox" / "archive" / email_id
    assert not archive.exists()


def test_email_delete_from_sent_rejected(tmp_path):
    """Cannot delete from sent folder."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"action": "delete", "email_id": ["x"], "folder": "sent"})
    assert "error" in result


def test_email_archive_already_archived(tmp_path):
    """Archiving a message that's already in archive returns not_found."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    email_id = _make_inbox_email(agent.working_dir, subject="move me")
    mgr = agent._email_manager
    mgr.handle({"action": "archive", "email_id": [email_id]})
    result = mgr.handle({"action": "archive", "email_id": [email_id]})
    assert result["not_found"] == [email_id]


# ---------------------------------------------------------------------------
# Removed recurring scheduler surface
# ---------------------------------------------------------------------------

def test_email_schedule_removed_from_schema(tmp_path):
    """The built-in recurring-send scheduler was removed in favor of host cron."""
    from lingtai_kernel.intrinsics.email import get_schema
    schema = get_schema("en")
    assert "schedule" not in schema["properties"]


def test_email_schedule_payload_is_not_routed(tmp_path):
    """A schedule-only payload should no longer dispatch to scheduler handlers."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "list"}})
    assert "action is required" in result["error"]


# ---------------------------------------------------------------------------
# _coerce_address_list — normalize LLM-quirky address args to list[str]
# ---------------------------------------------------------------------------

from lingtai_kernel.intrinsics.email import _coerce_address_list


def test_coerce_address_list_empty_string():
    assert _coerce_address_list("") == []


def test_coerce_address_list_none():
    assert _coerce_address_list(None) == []


def test_coerce_address_list_plain_string():
    assert _coerce_address_list("alice@host") == ["alice@host"]


def test_coerce_address_list_real_list():
    assert _coerce_address_list(["alice", "bob"]) == ["alice", "bob"]


def test_coerce_address_list_json_string_list():
    # LLM sometimes serializes a list arg as a JSON string
    assert _coerce_address_list('["alice","bob"]') == ["alice", "bob"]


def test_coerce_address_list_json_single():
    assert _coerce_address_list('["alice"]') == ["alice"]


def test_coerce_address_list_malformed_json_falls_back():
    # Starts with '[' but isn't valid JSON — keep as raw string
    # (reader-side defense catches nothing actionable; this matches today's behavior)
    assert _coerce_address_list("[not valid json") == ["[not valid json"]


def test_coerce_address_list_empty_list():
    assert _coerce_address_list([]) == []


def test_coerce_address_list_drops_empty_items():
    assert _coerce_address_list(["alice", "", "bob"]) == ["alice", "bob"]


def test_coerce_address_list_coerces_non_str_items():
    # Defensive — tool-call arg drift could give us numbers, etc.
    assert _coerce_address_list(["alice", 123]) == ["alice", "123"]


# ---------------------------------------------------------------------------
# Integration: capability _send unwraps JSON-string address on disk
# ---------------------------------------------------------------------------

def test_email_send_unwraps_json_string_address(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="sender",
                  working_dir=tmp_path / "sender")
    mail_svc = MagicMock()
    mail_svc.address = "sender"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    # Simulate an LLM that serialized its list arg as a JSON string
    result = mgr.handle({
        "action": "send",
        "address": '["alice","bob"]',
        "subject": "hi",
        "message": "test body",
    })
    assert result.get("status") == "sent" or "sent" in str(result), f"send failed: {result}"

    # Read the persisted sent record
    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_files = list(sent_dir.glob("*/message.json"))
    assert len(sent_files) == 1, f"expected 1 sent record, got {len(sent_files)}"

    record = json.loads(sent_files[0].read_text())
    assert record["to"] == ["alice", "bob"], \
        f"to field not unwrapped: {record['to']!r}"


def test_email_send_plain_string_address_becomes_list(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="sender",
                  working_dir=tmp_path / "sender")
    mail_svc = MagicMock()
    mail_svc.address = "sender"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    result = mgr.handle({
        "action": "send",
        "address": "alice",
        "subject": "hi",
        "message": "test body",
    })
    assert result.get("status") == "sent" or "sent" in str(result), f"send failed: {result}"

    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_files = list(sent_dir.glob("*/message.json"))
    assert len(sent_files) == 1

    record = json.loads(sent_files[0].read_text())
    assert record["to"] == ["alice"], f"to field not normalized: {record['to']!r}"


# ---------------------------------------------------------------------------
# dismiss action — mark read without returning content; refreshes the
# unread digest so .notification/email.json reflects the new state.
# ---------------------------------------------------------------------------


def test_email_dismiss_marks_read_and_returns_no_bodies(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid_a = _make_inbox_email(agent.working_dir, message="a")
    eid_b = _make_inbox_email(agent.working_dir, message="b")

    result = mgr.handle({"action": "dismiss", "email_id": [eid_a, eid_b]})

    assert result["status"] == "ok"
    assert set(result["dismissed"]) == {eid_a, eid_b}
    # No bodies returned.
    assert "emails" not in result
    # Both are now marked read.
    check = mgr.handle({"action": "check"})
    unread_flags = {e["id"]: e["unread"] for e in check["emails"]}
    assert unread_flags[eid_a] is False
    assert unread_flags[eid_b] is False


def test_email_dismiss_accepts_single_string(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, message="m")

    result = mgr.handle({"action": "dismiss", "email_id": eid})

    assert result["dismissed"] == [eid]


def test_email_dismiss_unknown_id_goes_to_not_found(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid_real = _make_inbox_email(agent.working_dir, message="real")

    result = mgr.handle({"action": "dismiss", "email_id": [eid_real, "nope-xxx"]})

    assert result["dismissed"] == [eid_real]
    assert result["not_found"] == ["nope-xxx"]


def test_email_dismiss_requires_email_id(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager

    result = mgr.handle({"action": "dismiss", "email_id": []})

    assert "error" in result


def test_email_dismiss_rerenders_notification(tmp_path):
    """After dismiss, .notification/email.json reflects the new
    unread count (or is cleared when count drops to zero)."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid_a = _make_inbox_email(agent.working_dir, message="a")
    eid_b = _make_inbox_email(agent.working_dir, message="b")

    from lingtai_kernel.base_agent.messaging import _rerender_unread_digest
    _rerender_unread_digest(agent)

    notif_path = agent.working_dir / ".notification" / "email.json"
    assert notif_path.exists()
    payload = json.loads(notif_path.read_text())
    assert payload["data"]["count"] == 2

    mgr.handle({"action": "dismiss", "email_id": [eid_a]})
    payload = json.loads(notif_path.read_text())
    assert payload["data"]["count"] == 1

    mgr.handle({"action": "dismiss", "email_id": [eid_b]})
    assert not notif_path.exists()


def test_email_dismiss_carries_instructions_in_envelope(tmp_path):
    """The unread-mail notification envelope includes an
    ``instructions`` field telling the agent to call read or dismiss
    to clear handled mails."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    _make_inbox_email(agent.working_dir, message="m")

    from lingtai_kernel.base_agent.messaging import _rerender_unread_digest
    _rerender_unread_digest(agent)

    payload = json.loads((agent.working_dir / ".notification" / "email.json").read_text())
    assert "instructions" in payload
    text = payload["instructions"]
    assert "dismiss" in text
    assert "read" in text
    assert "long work" in text
    assert "secondary" not in text
    assert "email(action=\"read\"" in text
    assert "email reply directly" in text


def test_email_read_rerenders_notification(tmp_path):
    """``read`` should also refresh the notification (it marks
    mail as read just like dismiss does)."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, message="m")

    from lingtai_kernel.base_agent.messaging import _rerender_unread_digest
    _rerender_unread_digest(agent)

    notif_path = agent.working_dir / ".notification" / "email.json"
    assert notif_path.exists()

    mgr.handle({"action": "read", "email_id": [eid]})
    assert not notif_path.exists()


def test_email_archive_rerenders_notification(tmp_path):
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    eid = _make_inbox_email(agent.working_dir, message="m")

    from lingtai_kernel.base_agent.messaging import _rerender_unread_digest
    _rerender_unread_digest(agent)

    notif_path = agent.working_dir / ".notification" / "email.json"
    assert notif_path.exists()

    mgr.handle({"action": "archive", "email_id": [eid]})
    assert not notif_path.exists()
