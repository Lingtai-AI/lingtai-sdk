"""Tests for the email capability (filesystem-based mailbox)."""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai.agent import Agent


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


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
# Schedule — schema and routing
# ---------------------------------------------------------------------------

def test_email_schedule_in_schema(tmp_path):
    """Email intrinsic schema should include schedule property."""
    from lingtai_kernel.intrinsics.email import get_schema
    schema = get_schema("en")
    props = schema["properties"]
    assert "schedule" in props
    actions = props["schedule"]["properties"]["action"]["enum"]
    assert "create" in actions
    assert "cancel" in actions
    assert "list" in actions
    assert "reactivate" in actions  # NEW


def test_email_handle_without_action_or_schedule(tmp_path):
    """Missing both action and schedule should return error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({})
    assert "action is required" in result["error"]


def test_email_schedule_unknown_action(tmp_path):
    """Unknown schedule action should return error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "bogus"}})
    assert "error" in result


def test_email_schedule_reactivate_routes_to_handler(tmp_path):
    """reactivate action should be dispatched (not return 'Unknown schedule action')."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": "nonexistent"}})
    # Should NOT return the dispatch fallback error
    assert "error" in result
    assert "Unknown schedule action" not in result["error"]
    # Should route to reactivate handler, which errors on missing record
    assert "Schedule not found" in result["error"]


# ---------------------------------------------------------------------------
# Schedule — create
# ---------------------------------------------------------------------------

def test_email_schedule_create_basic(tmp_path):
    """schedule.create should persist schedule.json and return schedule_id."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "subject": "Heartbeat",
        "message": "alive",
        "schedule": {"action": "create", "interval": 1, "count": 3},
    })
    assert result["status"] == "scheduled"
    assert "schedule_id" in result
    assert result["interval"] == 1
    assert result["count"] == 3
    # schedule.json should exist on disk
    sched_dir = agent.working_dir / "mailbox" / "schedules" / result["schedule_id"]
    assert (sched_dir / "schedule.json").is_file()
    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["count"] == 3
    assert sched["sent"] == 0


def test_email_schedule_create_writes_status_active(tmp_path):
    """Newly created schedules should have status='active' on disk."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })
    sid = result["schedule_id"]
    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["status"] == "active"


def test_set_schedule_status_helper_updates_record(tmp_path):
    """_set_schedule_status should update the on-disk status field."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone", "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })
    sid = result["schedule_id"]
    # Use the helper directly
    ok = mgr._set_schedule_status(sid, "inactive")
    assert ok is True
    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["status"] == "inactive"
    # Returns False on missing record
    assert mgr._set_schedule_status("nonexistent", "inactive") is False


def test_email_schedule_create_sends_messages(tmp_path):
    """schedule.create should send count messages with interval between them."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "subject": "Beat",
        "message": "ping",
        "schedule": {"action": "create", "interval": 1, "count": 3},
    })
    sid = result["schedule_id"]
    # Wait for all 3 sends (3 sends * 1s interval + buffer)
    time.sleep(4.0)
    # Should have sent 3 times
    sched = json.loads((agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text())
    assert sched["sent"] == 3
    # Sent folder should have 3 records
    sent_dir = agent.working_dir / "mailbox" / "sent"
    assert len(list(sent_dir.iterdir())) == 3


def test_email_schedule_create_includes_metadata(tmp_path):
    """Each scheduled send should include _schedule metadata."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "message": "beat",
        "schedule": {"action": "create", "interval": 1, "count": 2},
    })
    time.sleep(3.0)
    # Check sent records for _schedule metadata
    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_msgs = []
    for d in sent_dir.iterdir():
        msg = json.loads((d / "message.json").read_text())
        sent_msgs.append(msg)
    # Sort by seq
    sent_msgs.sort(key=lambda m: m.get("_schedule", {}).get("seq", 0))
    assert len(sent_msgs) == 2
    assert sent_msgs[0]["_schedule"]["seq"] == 1
    assert sent_msgs[0]["_schedule"]["total"] == 2
    assert sent_msgs[1]["_schedule"]["seq"] == 2
    assert "estimated_finish" in sent_msgs[1]["_schedule"]
    assert "schedule_id" in sent_msgs[0]["_schedule"]


def test_email_schedule_create_missing_params(tmp_path):
    """schedule.create without interval or count should error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone", "message": "hi",
        "schedule": {"action": "create", "count": 3},
    })
    assert "error" in result
    result = mgr.handle({
        "address": "someone", "message": "hi",
        "schedule": {"action": "create", "interval": 10},
    })
    assert "error" in result


def test_email_schedule_create_invalid_params(tmp_path):
    """schedule.create with non-positive interval or count should error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone", "message": "hi",
        "schedule": {"action": "create", "interval": 0, "count": 3},
    })
    assert "error" in result
    result = mgr.handle({
        "address": "someone", "message": "hi",
        "schedule": {"action": "create", "interval": 10, "count": -1},
    })
    assert "error" in result


def test_email_schedule_create_missing_address(tmp_path):
    """schedule.create without address should error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({
        "message": "hi",
        "schedule": {"action": "create", "interval": 10, "count": 3},
    })
    assert "error" in result


# ---------------------------------------------------------------------------
# Schedule — cancel
# ---------------------------------------------------------------------------

def test_email_schedule_cancel_not_found(tmp_path):
    """cancel on non-existent schedule should error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "cancel", "schedule_id": "nonexistent"}})
    assert "error" in result


# ---------------------------------------------------------------------------
# Schedule — list
# ---------------------------------------------------------------------------

def test_email_schedule_list_empty(tmp_path):
    """list with no schedules should return empty list."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "list"}})
    assert result["status"] == "ok"
    assert result["schedules"] == []


def test_email_schedule_list_shows_active(tmp_path):
    """list should show active schedules with progress."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "subject": "Status",
        "message": "ok",
        "schedule": {"action": "create", "interval": 60, "count": 10},
    })
    sid = result["schedule_id"]
    time.sleep(0.5)  # let first send happen
    listing = mgr.handle({"schedule": {"action": "list"}})
    assert listing["status"] == "ok"
    assert len(listing["schedules"]) == 1
    entry = listing["schedules"][0]
    assert entry["schedule_id"] == sid
    assert entry["interval"] == 60
    assert entry["count"] == 10
    assert entry["to"] == "someone"
    assert entry["subject"] == "Status"
    assert entry["status"] == "active"
    # Cleanup
    mgr.handle({"schedule": {"action": "cancel", "schedule_id": sid}})


def test_email_schedule_list_shows_completed(tmp_path):
    """list should show completed schedules with status='completed'."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager
    result = mgr.handle({
        "address": "someone",
        "message": "done",
        "schedule": {"action": "create", "interval": 1, "count": 1},
    })
    time.sleep(2.0)
    listing = mgr.handle({"schedule": {"action": "list"}})
    entry = listing["schedules"][0]
    assert entry["status"] == "completed"
    assert entry["sent"] == 1


# ---------------------------------------------------------------------------
# Schedule — startup reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_flips_active_to_inactive_on_startup(tmp_path):
    """A new EmailManager should flip all active schedules to inactive on startup."""
    agent1 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent1._mail_service = mail_svc

    # Manually write an active schedule.json
    sched_id = "active1234"
    sched_dir = agent1.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 5, "sent": 1,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:00:00Z",
        "status": "active",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))
    agent1.stop(timeout=1.0)

    # Create a new agent at the same dir — reconciliation should flip to inactive
    agent2 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                       mail_service=mail_svc)

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["status"] == "inactive"


def test_reconcile_flips_legacy_record_to_inactive(tmp_path):
    """A schedule.json with NO status field should be flipped to inactive on startup."""
    agent1 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"

    sched_id = "legacy12345"
    sched_dir = agent1.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {  # NO status field
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 5, "sent": 1,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:00:00Z",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))
    agent1.stop(timeout=1.0)

    agent2 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                       mail_service=mail_svc)

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["status"] == "inactive"


def test_reconcile_leaves_completed_records_alone(tmp_path):
    """Completed schedules should NOT be flipped — they stay completed."""
    agent1 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"

    sched_id = "completed5678"
    sched_dir = agent1.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 3, "sent": 3,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:02:00Z",
        "status": "completed",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))
    agent1.stop(timeout=1.0)

    agent2 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                       mail_service=mail_svc)

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["status"] == "completed"


# ---------------------------------------------------------------------------
# Schedule — recovery
# ---------------------------------------------------------------------------

def test_email_schedule_recovery_on_setup(tmp_path):
    """After agent restart, in-flight schedules should pause (status=inactive), not auto-resume."""
    agent1 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent1._mail_service = mail_svc

    # Manually write a schedule.json that looks like it was interrupted at sent=1 of count=3
    sched_id = "recover12345"
    sched_dir = agent1.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {
            "address": "someone",
            "subject": "Resume",
            "message": "continued",
            "cc": [],
            "bcc": [],
            "type": "normal",
        },
        "interval": 1,
        "count": 3,
        "sent": 1,
        "created_at": "2026-03-18T10:00:00Z",
        "last_sent_at": "2026-03-18T10:00:00Z",
        "status": "active",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record, indent=2))

    agent1.stop(timeout=1.0)

    # Create a NEW agent at the same base_dir — reconciliation flips to inactive
    agent2 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                        mail_service=mail_svc)
    mgr2 = agent2._email_manager

    # Wait — sends should NOT happen
    time.sleep(2.5)
    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["sent"] == 1, "schedule should not have auto-resumed"
    assert sched["status"] == "inactive"

    # Now reactivate — sends should resume after one full interval
    result = mgr2.handle({"schedule": {"action": "reactivate", "schedule_id": sched_id}})
    assert result["status"] == "reactivated"

    # Wait for the remaining 2 sends (interval=1, so ~2 more seconds)
    time.sleep(3.0)
    final = json.loads((sched_dir / "schedule.json").read_text())
    assert final["sent"] == 3


def test_email_schedule_recovery_skips_inactive(tmp_path):
    """Inactive schedules should not be resumed and should not be flipped back to active."""
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None

    agent1 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")

    sched_id = "inactive12345"
    sched_dir = agent1.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "someone", "message": "x", "subject": "", "cc": [], "bcc": [], "type": "normal"},
        "interval": 1, "count": 5, "sent": 2,
        "created_at": "2026-03-18T10:00:00Z",
        "last_sent_at": "2026-03-18T10:00:00Z",
        "status": "inactive",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record, indent=2))
    agent1.stop(timeout=1.0)

    agent2 = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                       mail_service=mail_svc)

    time.sleep(2.0)

    final = json.loads((sched_dir / "schedule.json").read_text())
    assert final["sent"] == 2, "inactive schedule should not have resumed"
    assert final["status"] == "inactive", "inactive should not be flipped back to active"


# ---------------------------------------------------------------------------
# Schedule — end-to-end
# ---------------------------------------------------------------------------

def test_email_schedule_end_to_end(tmp_path):
    """Full lifecycle: create → sends happen → list shows progress → cancel → record is inactive."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    # Create
    result = mgr.handle({
        "address": "peer",
        "subject": "Status",
        "message": "System OK",
        "schedule": {"action": "create", "interval": 1, "count": 5},
    })
    assert result["status"] == "scheduled"
    sid = result["schedule_id"]

    # Let 2 sends happen
    time.sleep(2.5)

    # List — should be active with some progress
    listing = mgr.handle({"schedule": {"action": "list"}})
    entry = [s for s in listing["schedules"] if s["schedule_id"] == sid][0]
    assert entry["status"] == "active"
    assert entry["sent"] >= 2

    # Cancel
    cancel = mgr.handle({"schedule": {"action": "cancel", "schedule_id": sid}})
    assert cancel["status"] == "paused"

    # Record on disk should be inactive (status-field cancel)
    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["status"] == "inactive"
    assert sched["sent"] < 5


# test_email_private_mode_receive_unrestricted removed — private_mode
# was deleted; receiving was never gated regardless.


# ---------------------------------------------------------------------------
# Disk-driven scheduler service
# ---------------------------------------------------------------------------

def test_scheduler_service_sends_due_messages(tmp_path):
    """The scheduler service thread should send messages when due."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    result = mgr.handle({
        "address": "someone",
        "subject": "ping",
        "message": "hello",
        "schedule": {"action": "create", "interval": 1, "count": 3},
    })
    sid = result["schedule_id"]
    time.sleep(4.0)

    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["sent"] == 3


def test_schedule_completion_sets_status_completed(tmp_path):
    """When sent reaches count, the record's status should become 'completed'."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    result = mgr.handle({
        "address": "someone", "subject": "done", "message": "bye",
        "schedule": {"action": "create", "interval": 1, "count": 2},
    })
    sid = result["schedule_id"]
    time.sleep(3.0)  # both sends should complete

    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["sent"] == 2
    assert sched["status"] == "completed"


def test_scheduler_tick_skips_inactive_records(tmp_path):
    """The scheduler tick should NOT send for records with status='inactive'."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    # Create a schedule with status=inactive directly on disk (skipping reconciliation)
    sched_id = "inactivetick1"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 1, "count": 5, "sent": 0,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": None,  # would be due immediately if active
        "status": "inactive",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))

    time.sleep(2.0)

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["sent"] == 0, "scheduler should not tick inactive records"


def test_scheduler_respects_interval_on_resume(tmp_path):
    """On resume, scheduler should wait remaining interval, not send immediately."""
    from datetime import timedelta

    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    sched_id = "resume-test"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    record = {
        "schedule_id": sched_id,
        "send_payload": {
            "address": "someone", "subject": "Resume", "message": "continued",
            "cc": [], "bcc": [], "type": "normal",
        },
        "interval": 5,
        "count": 3,
        "sent": 1,
        "created_at": "2026-03-18T10:00:00Z",
        "last_sent_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record, indent=2))

    mgr = agent._email_manager
    time.sleep(2.0)

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["sent"] == 1  # should not have sent yet — 4s remaining


def test_schedule_sends_inbox_notification(tmp_path):
    """After a scheduled send fires, agent should get an inbox notification."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    result = mgr.handle({
        "address": "someone",
        "subject": "alarm",
        "message": "wake up",
        "schedule": {"action": "create", "interval": 1, "count": 2},
    })
    assert result["status"] == "scheduled"

    # Wait for both sends to fire
    time.sleep(3.5)

    # Drain inbox for schedule notifications
    notifications = []
    while not agent.inbox.empty():
        msg = agent.inbox.get_nowait()
        if hasattr(msg, "content") and "[schedule" in str(msg.content):
            notifications.append(str(msg.content))

    assert len(notifications) >= 1, f"Expected schedule notifications, got {notifications}"
    assert "[schedule 1/2]" in notifications[0]
    assert "alarm" in notifications[0]
    # Last notification should say "schedule complete"
    assert "schedule complete" in notifications[-1]


def test_schedule_reactivate_inactive_resumes(tmp_path):
    """reactivate on an inactive schedule should flip status and reset last_sent_at."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    # Manually create an inactive schedule
    sched_id = "reactivate1234"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "someone", "subject": "", "message": "x", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60,
        "count": 5,
        "sent": 1,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:00:00Z",
        "status": "inactive",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))

    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": sched_id}})
    assert result["status"] == "reactivated"
    assert result["schedule_id"] == sched_id

    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["status"] == "active"
    # last_sent_at should be ~now (not the old "10:00:00Z")
    assert sched["last_sent_at"] != "2026-04-06T10:00:00Z"


def test_schedule_reactivate_active_is_noop(tmp_path):
    """reactivate on an active schedule should return already_active without mutation."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    create_result = mgr.handle({
        "address": "someone", "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })
    sid = create_result["schedule_id"]
    original = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )

    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": sid}})
    assert result["status"] == "already_active"
    assert result["schedule_id"] == sid

    after = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    # last_sent_at should be unchanged
    assert after["last_sent_at"] == original["last_sent_at"]


def test_schedule_reactivate_completed_errors(tmp_path):
    """reactivate on a completed schedule should error."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager

    sched_id = "completed1234"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 3, "sent": 3,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:02:00Z",
        "status": "completed",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))

    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": sched_id}})
    assert "error" in result
    assert "completed" in result["error"].lower()


def test_schedule_reactivate_not_found_errors(tmp_path):
    """reactivate on a missing schedule should return Schedule not found."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager
    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": "nonexistent"}})
    assert "error" in result
    assert "Schedule not found" in result["error"]


def test_schedule_reactivate_self_heals_crash_mid_completion(tmp_path):
    """If sent>=count but status==inactive (crash mid-completion), reactivate should self-heal to completed and refuse."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager

    sched_id = "crashed12345"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 3, "sent": 3,  # sent==count
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:02:00Z",
        "status": "inactive",  # but status was never updated to completed
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))

    result = mgr.handle({"schedule": {"action": "reactivate", "schedule_id": sched_id}})
    assert "error" in result
    assert "completed" in result["error"].lower()

    # The on-disk record should now be self-healed to completed
    sched = json.loads((sched_dir / "schedule.json").read_text())
    assert sched["status"] == "completed"


# ---------------------------------------------------------------------------
# Schedule — cancel (new status-field-based tests, Task 6)
# ---------------------------------------------------------------------------

def test_schedule_cancel_sets_status_inactive(tmp_path):
    """schedule.cancel should flip the record's status to inactive (no .cancel file)."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    create_result = mgr.handle({
        "address": "someone", "message": "beat",
        "schedule": {"action": "create", "interval": 60, "count": 100},
    })
    sid = create_result["schedule_id"]

    cancel_result = mgr.handle({"schedule": {"action": "cancel", "schedule_id": sid}})
    assert cancel_result["status"] == "paused"
    assert cancel_result["schedule_id"] == sid

    # On-disk record should be inactive
    sched = json.loads(
        (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
    )
    assert sched["status"] == "inactive"

    # No .cancel file should exist
    assert not (agent.working_dir / "mailbox" / "schedules" / sid / ".cancel").exists()


def test_schedule_cancel_all_sets_all_to_inactive(tmp_path):
    """schedule.cancel without schedule_id should flip all active records to inactive."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    r1 = mgr.handle({
        "address": "a", "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 50},
    })
    r2 = mgr.handle({
        "address": "b", "message": "y",
        "schedule": {"action": "create", "interval": 60, "count": 50},
    })

    cancel_result = mgr.handle({"schedule": {"action": "cancel"}})
    assert cancel_result["status"] == "paused"

    for sid in [r1["schedule_id"], r2["schedule_id"]]:
        sched = json.loads(
            (agent.working_dir / "mailbox" / "schedules" / sid / "schedule.json").read_text()
        )
        assert sched["status"] == "inactive"
        assert not (agent.working_dir / "mailbox" / "schedules" / sid / ".cancel").exists()

    # No agent-level .cancel file either
    assert not (agent.working_dir / "mailbox" / "schedules" / ".cancel").exists()


def test_schedule_cancel_already_inactive_returns_noop(tmp_path):
    """Cancelling an already-inactive schedule should return already_inactive."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    create_result = mgr.handle({
        "address": "someone", "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })
    sid = create_result["schedule_id"]

    # First cancel — succeeds
    mgr.handle({"schedule": {"action": "cancel", "schedule_id": sid}})

    # Second cancel — already inactive
    second = mgr.handle({"schedule": {"action": "cancel", "schedule_id": sid}})
    assert second["status"] == "already_inactive"
    assert second["schedule_id"] == sid


def test_schedule_cancel_already_completed_returns_noop(tmp_path):
    """Cancelling a completed schedule should return already_completed."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mgr = agent._email_manager

    sched_id = "completed1234"
    sched_dir = agent.working_dir / "mailbox" / "schedules" / sched_id
    sched_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schedule_id": sched_id,
        "send_payload": {"address": "x", "subject": "", "message": "y", "cc": [], "bcc": [], "type": "normal"},
        "interval": 60, "count": 3, "sent": 3,
        "created_at": "2026-04-06T10:00:00Z",
        "last_sent_at": "2026-04-06T10:02:00Z",
        "status": "completed",
    }
    (sched_dir / "schedule.json").write_text(json.dumps(record))

    result = agent._email_manager.handle({"schedule": {"action": "cancel", "schedule_id": sched_id}})
    assert result["status"] == "already_completed"


def test_schedule_list_returns_status_field(tmp_path):
    """list should return a status field on each entry, not the legacy active/cancelled booleans."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc
    mgr = agent._email_manager

    # Create one schedule and cancel it; create another and leave it active
    r1 = mgr.handle({
        "address": "a", "message": "x",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })
    mgr.handle({"schedule": {"action": "cancel", "schedule_id": r1["schedule_id"]}})

    r2 = mgr.handle({
        "address": "b", "message": "y",
        "schedule": {"action": "create", "interval": 60, "count": 5},
    })

    listing = mgr.handle({"schedule": {"action": "list"}})
    assert listing["status"] == "ok"
    assert len(listing["schedules"]) == 2

    by_id = {s["schedule_id"]: s for s in listing["schedules"]}
    # New status field present
    assert by_id[r1["schedule_id"]]["status"] == "inactive"
    assert by_id[r2["schedule_id"]]["status"] == "active"
    # Legacy boolean fields gone
    for entry in listing["schedules"]:
        assert "cancelled" not in entry


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


# ---------------------------------------------------------------------------
# Regression: issue #154 — refresh leaks scheduler threads, ticks double-send
# ---------------------------------------------------------------------------

def test_email_boot_stops_previous_scheduler(tmp_path):
    """Re-running email.boot on the same agent must stop the prior
    EmailManager's scheduler thread; otherwise two daemon threads
    race on schedule.json (issue #154).
    """
    from lingtai_kernel.intrinsics import email as _email

    agent = Agent(service=make_mock_service(), agent_name="test",
                  working_dir=tmp_path / "test")
    first_mgr = agent._email_manager
    first_thread = first_mgr._scheduler_thread
    assert first_thread is not None
    assert first_thread.is_alive()

    # Simulate the molt/refresh path that re-boots email on a live agent.
    _email.boot(agent)

    # The previous scheduler MUST have been stopped.
    first_thread.join(timeout=5.0)
    assert not first_thread.is_alive(), (
        "Old EmailManager scheduler thread still running after refresh — "
        "two threads will race on schedule.json (issue #154)."
    )
    assert agent._email_manager is not first_mgr


def test_scheduler_tick_is_idempotent_under_concurrent_threads(tmp_path):
    """Two scheduler ticks for the same due window must produce exactly
    one extra send (sent advances by 1, not 2). Regression for the
    intra-process race between leaked scheduler threads in issue #154.

    Reproduces the exact interleaving from the bug: thread A reads the
    schedule, claims seq=N by writing sent=N (but ``last_sent_at`` is
    still the previous tick's old stamp), then enters ``_send``. While
    A is inside ``_send``, thread B reads the schedule — sees stale
    ``last_sent_at`` and not-yet-finalised state, decides "still due",
    fires seq=N+1. After fix #2 (claim ``last_sent_at`` before
    ``_send``), B's read sees a fresh stamp and skips.
    """
    agent = Agent(service=make_mock_service(), agent_name="test",
                  working_dir=tmp_path / "test")
    mail_svc = MagicMock()
    mail_svc.address = "me"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    mgr = agent._email_manager
    # Drive _scheduler_tick by hand — stop the background loop so it
    # doesn't fire its own ticks concurrently.
    mgr.stop_scheduler()

    result = mgr.handle({
        "address": "me",
        "subject": "tick",
        "message": "x",
        "schedule": {"action": "create", "interval": 1, "count": 5},
    })
    sid = result["schedule_id"]
    sched_path = (agent.working_dir / "mailbox" / "schedules"
                  / sid / "schedule.json")

    # Force "due now" with last_sent_at well in the past.
    rec = json.loads(sched_path.read_text())
    rec["sent"] = 1
    rec["last_sent_at"] = "2020-01-01T00:00:00Z"
    sched_path.write_text(json.dumps(rec))

    # Force the exact race interleaving: pause thread A inside _send so
    # thread B can observe the in-flight state (sent advanced but
    # last_sent_at not yet updated by the post-send write at line 583).
    in_send = threading.Event()
    release_send = threading.Event()
    original_send = mgr._send
    send_call_count = {"n": 0}

    def slow_send(args):
        send_call_count["n"] += 1
        # Only block on the first _send so thread B can race in.
        if send_call_count["n"] == 1:
            in_send.set()
            release_send.wait(timeout=5.0)
        return original_send(args)

    mgr._send = slow_send

    errors: list[BaseException] = []

    def worker():
        try:
            mgr._scheduler_tick()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=worker, name="tick-A")
    t1.start()
    assert in_send.wait(timeout=5.0), "thread A never entered _send"

    t2 = threading.Thread(target=worker, name="tick-B")
    t2.start()
    # Give B time to run _scheduler_tick to completion — without fix #2,
    # B sees stale last_sent_at and fires; with fix #2, B sees the
    # tentative claim and skips.
    t2.join(timeout=10.0)

    release_send.set()
    t1.join(timeout=10.0)

    assert not errors, f"worker errors: {errors!r}"
    # The bug: thread B saw the stale ``last_sent_at`` (the post-_send
    # finalize hadn't happened yet) and fired a second ``_send`` for
    # the same due window. After fix #2 (claim ``last_sent_at`` before
    # _send), B's pre-tick read sees the fresh stamp and skips, so
    # ``_send`` is called exactly once.
    assert send_call_count["n"] == 1, (
        f"_send was called {send_call_count['n']} times for one due "
        f"tick — concurrent scheduler ticks double-fired (issue #154)"
    )
