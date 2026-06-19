"""Tests for FilesystemMailService — filesystem-based mail delivery."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _make_agent_dir(base: Path, name: str) -> Path:
    """Create a minimal agent working dir with .agent.json and fresh heartbeat."""
    d = base / name
    d.mkdir()
    (d / ".agent.json").write_text(json.dumps({
        "agent_name": "test",
        "admin": {},
    }))
    (d / ".agent.heartbeat").write_text(str(time.time()))
    return d


class TestSend:

    def test_send_creates_message(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello", "subject": "test"})
        assert result is None  # success

        inbox = recip_dir / "mailbox" / "inbox"
        msgs = list(inbox.iterdir())
        assert len(msgs) == 1
        data = json.loads((msgs[0] / "message.json").read_text())
        assert data["message"] == "hello"
        assert data["subject"] == "test"

    def test_send_injects_mailbox_metadata(self, tmp_path):
        """send() must inject _mailbox_id and received_at for mail intrinsic."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is None

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        data = json.loads((msg_dir / "message.json").read_text())
        assert "_mailbox_id" in data
        assert data["_mailbox_id"] == msg_dir.name  # UUID matches dir name
        assert "received_at" in data
        assert data["received_at"].endswith("Z")  # UTC format

    def test_send_copies_attachments(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        # Create a file to attach
        att = sender_dir / "report.txt"
        att.write_text("data")

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {
            "message": "see attached",
            "attachments": [str(att)],
        })
        assert result is None

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        att_dir = msg_dir / "attachments"
        assert att_dir.exists()
        assert (att_dir / "report.txt").read_text() == "data"

        # The message.json should reference the recipient-local copy
        data = json.loads((msg_dir / "message.json").read_text())
        assert len(data["attachments"]) == 1
        assert "report.txt" in data["attachments"][0]

    def test_send_fails_no_agent_json(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        bad_dir = tmp_path / "noagent"
        bad_dir.mkdir()

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(bad_dir), {"message": "hello"})
        assert result is not None  # error string
        assert "no agent" in result.lower()

    def test_send_fails_stale_heartbeat(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)
        # Write stale heartbeat
        (recip_dir / ".agent.heartbeat").write_text(str(time.time() - 10))

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is not None
        assert "not running" in result.lower()

    def test_send_self(self, tmp_path):
        """Send to own address should work (self-send)."""
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        result = svc.send(str(agent_dir), {"message": "note to self"})
        assert result is None

        inbox = agent_dir / "mailbox" / "inbox"
        msgs = list(inbox.iterdir())
        assert len(msgs) == 1
        data = json.loads((msgs[0] / "message.json").read_text())
        assert data["message"] == "note to self"

    def test_send_fails_missing_attachment(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {
            "message": "see attached",
            "attachments": ["/nonexistent/file.txt"],
        })
        assert result is not None
        assert "attachment" in result.lower()

    def test_send_to_human_skips_heartbeat(self, tmp_path):
        """Human recipients (admin=null) don't need a heartbeat."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        human_dir = tmp_path / "human01"
        human_dir.mkdir()
        (human_dir / ".agent.json").write_text(json.dumps({
            "agent_name": "human",
            "admin": None,
        }))
        (human_dir / "mailbox" / "inbox").mkdir(parents=True)
        # No .agent.heartbeat file — should still deliver

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(human_dir), {"message": "hello human"})
        assert result is None  # success

        inbox = human_dir / "mailbox" / "inbox"
        entries = list(inbox.iterdir())
        assert len(entries) == 1
        msg = json.loads((entries[0] / "message.json").read_text())
        assert msg["message"] == "hello human"

    def test_send_fails_no_heartbeat_file(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = tmp_path / "recip01"
        recip_dir.mkdir()
        (recip_dir / ".agent.json").write_text(json.dumps({
            "agent_name": "test",
            "admin": {},
        }))
        # No .agent.heartbeat file

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is not None
        assert "not running" in result.lower()

    def test_send_atomic_write(self, tmp_path):
        """Verify no .tmp file is left behind after successful send."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        svc.send(str(recip_dir), {"message": "hello"})

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        assert (msg_dir / "message.json").exists()
        assert not (msg_dir / "message.json.tmp").exists()


class TestListen:

    def test_listen_detects_new_message(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))

        # Simulate incoming mail (another agent writes to our inbox)
        msg_dir = agent_dir / "mailbox" / "inbox" / "test-uuid-1"
        msg_dir.mkdir()
        (msg_dir / "message.json").write_text(json.dumps({
            "from": "/tmp/other",
            "message": "hi",
        }))

        time.sleep(1.0)
        svc.stop()
        assert len(received) == 1
        assert received[0]["message"] == "hi"

    def test_listen_ignores_existing_messages(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        inbox = agent_dir / "mailbox" / "inbox"
        inbox.mkdir(parents=True)

        # Pre-existing message
        old = inbox / "old-uuid"
        old.mkdir()
        (old / "message.json").write_text(json.dumps({"message": "old"}))

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))
        time.sleep(1.0)
        svc.stop()
        assert len(received) == 0

    def test_listen_detects_multiple_messages(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))

        for i in range(3):
            msg_dir = agent_dir / "mailbox" / "inbox" / f"uuid-{i}"
            msg_dir.mkdir()
            (msg_dir / "message.json").write_text(json.dumps({
                "message": f"msg-{i}",
            }))

        time.sleep(1.5)
        svc.stop()
        assert len(received) == 3
        messages = sorted(r["message"] for r in received)
        assert messages == ["msg-0", "msg-1", "msg-2"]

    def test_stop_is_idempotent(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        # stop without listen should not raise
        svc.stop()
        svc.stop()


class TestAddress:

    def test_address_returns_working_dir_name(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        assert svc.address == agent_dir.name

    def test_address_is_str(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        assert isinstance(svc.address, str)


def test_pseudo_agent_outbox_pickup(tmp_path):
    """Subscribed outbox messages addressed to self are claimed, written to own
    inbox, archived in the pseudo-agent's sent/, and dispatched exactly once."""
    import json
    import threading
    import time
    from lingtai_kernel.services.mail import FilesystemMailService

    # Two sibling folders under a shared parent.
    base = tmp_path
    pseudo_dir = base / "human"
    my_dir = base / "本我"
    pseudo_dir.mkdir()
    my_dir.mkdir()

    # Seed the pseudo-agent's outbox with a message addressed to "本我".
    outbox_dir = pseudo_dir / "mailbox" / "outbox"
    msg_dir = outbox_dir / "msg-001"
    msg_dir.mkdir(parents=True)
    msg = {
        "id": "msg-001",
        "_mailbox_id": "msg-001",
        "from": "human",
        "to": ["本我"],
        "subject": "hi",
        "message": "hello from human",
        "type": "normal",
        "received_at": "2026-04-21T10:00:00.000Z",
    }
    (msg_dir / "message.json").write_text(json.dumps(msg))

    # Start the service for "本我" subscribed to "../human".
    received: list[dict] = []
    received_event = threading.Event()

    def on_message(payload: dict) -> None:
        received.append(payload)
        received_event.set()

    svc = FilesystemMailService(
        working_dir=my_dir,
        pseudo_agent_subscriptions=["../human"],
    )
    svc.listen(on_message=on_message)
    try:
        # Wait up to 3s for the poller to pick up the message.
        assert received_event.wait(timeout=3.0), "on_message never fired"
        # Give the poller a couple more ticks so any Phase-1 re-dispatch
        # due to the newly written own-inbox copy would manifest.
        time.sleep(1.5)
    finally:
        svc.stop()

    # The message is gone from the pseudo-agent's outbox and now in its sent/.
    assert not msg_dir.exists(), "message should have been renamed out of outbox"
    sent_dir = pseudo_dir / "mailbox" / "sent" / "msg-001"
    assert sent_dir.is_dir(), "message should now be in pseudo-agent sent/"

    # The message must ALSO be present in the subscriber's own inbox — this is
    # the core invariant of the subscription claim flow: wake-corresponds-to-file.
    own_inbox_msg = my_dir / "mailbox" / "inbox" / "msg-001" / "message.json"
    assert own_inbox_msg.is_file(), (
        "claimed message must be written to the subscriber's own inbox; "
        "otherwise email(action='check') sees an empty inbox after wake"
    )
    own_payload = json.loads(own_inbox_msg.read_text())
    assert own_payload["message"] == "hello from human"
    assert own_payload["_mailbox_id"] == "msg-001"
    assert own_payload["to"] == ["本我"]

    # The payload we got matches and is delivered exactly once (no duplicate
    # dispatch from Phase 1 re-scanning the newly written own-inbox copy).
    assert len(received) == 1, (
        f"expected exactly one dispatch, got {len(received)}: "
        "Phase 1 own-inbox scan must not re-dispatch messages that Phase 2 "
        "just wrote into the inbox"
    )
    assert received[0]["message"] == "hello from human"


def test_pseudo_agent_outbox_lost_race_rollback(tmp_path):
    """When two subscribers race for the same message, exactly one claims it:
    the loser leaves no speculative inbox copy and does not fire on_message."""
    import json
    import threading
    import time
    from lingtai_kernel.services.mail import FilesystemMailService

    base = tmp_path
    pseudo_dir = base / "human"
    a_dir = base / "agent_a"
    b_dir = base / "agent_b"
    pseudo_dir.mkdir()
    a_dir.mkdir()
    b_dir.mkdir()

    # Seed one message addressed to BOTH agents.
    outbox_dir = pseudo_dir / "mailbox" / "outbox"
    msg_dir = outbox_dir / "msg-race"
    msg_dir.mkdir(parents=True)
    msg = {
        "id": "msg-race",
        "_mailbox_id": "msg-race",
        "from": "human",
        "to": ["agent_a", "agent_b"],
        "subject": "shared",
        "message": "both are listed but only one may claim",
        "type": "normal",
        "received_at": "2026-04-22T00:00:00.000Z",
    }
    (msg_dir / "message.json").write_text(json.dumps(msg))

    received_a: list[dict] = []
    received_b: list[dict] = []
    a_evt = threading.Event()
    b_evt = threading.Event()

    def on_a(payload: dict) -> None:
        received_a.append(payload)
        a_evt.set()

    def on_b(payload: dict) -> None:
        received_b.append(payload)
        b_evt.set()

    svc_a = FilesystemMailService(
        working_dir=a_dir, pseudo_agent_subscriptions=["../human"]
    )
    svc_b = FilesystemMailService(
        working_dir=b_dir, pseudo_agent_subscriptions=["../human"]
    )
    svc_a.listen(on_message=on_a)
    svc_b.listen(on_message=on_b)
    try:
        # Wait long enough for whichever service wins the race to dispatch,
        # plus several more ticks so the loser would have had ample opportunity
        # to incorrectly also dispatch.
        deadline = time.time() + 3.0
        while time.time() < deadline and not (a_evt.is_set() or b_evt.is_set()):
            time.sleep(0.1)
        time.sleep(1.5)
    finally:
        svc_a.stop()
        svc_b.stop()

    # Exactly one agent claimed: one inbox has the message, the other is empty.
    a_inbox_msg = a_dir / "mailbox" / "inbox" / "msg-race" / "message.json"
    b_inbox_msg = b_dir / "mailbox" / "inbox" / "msg-race" / "message.json"
    claimed_count = int(a_inbox_msg.is_file()) + int(b_inbox_msg.is_file())
    assert claimed_count == 1, (
        f"exactly one subscriber must have the inbox copy; "
        f"found {claimed_count} (a={a_inbox_msg.is_file()}, b={b_inbox_msg.is_file()})"
    )

    # And the loser must have no leftover directory at all — no orphan
    # speculative inbox copy.
    if a_inbox_msg.is_file():
        loser_inbox_entry = b_dir / "mailbox" / "inbox" / "msg-race"
        loser_received = received_b
        winner_received = received_a
    else:
        loser_inbox_entry = a_dir / "mailbox" / "inbox" / "msg-race"
        loser_received = received_a
        winner_received = received_b
    assert not loser_inbox_entry.exists(), (
        "loser must have no leftover speculative inbox directory after rollback"
    )

    # on_message fires exactly once for the winner, never for the loser.
    assert len(winner_received) == 1
    assert len(loser_received) == 0

    # The message is archived in pseudo-agent sent/ exactly once.
    sent_dir = pseudo_dir / "mailbox" / "sent" / "msg-race"
    assert sent_dir.is_dir()
    assert not msg_dir.exists(), "message must be moved out of the pseudo outbox"


def test_pseudo_agent_outbox_skips_non_matching_to(tmp_path):
    """Messages addressed to a different agent are not claimed."""
    import json
    import time
    from lingtai_kernel.services.mail import FilesystemMailService

    base = tmp_path
    pseudo_dir = base / "human"
    my_dir = base / "本我"
    pseudo_dir.mkdir()
    my_dir.mkdir()

    # Message addressed to someone else.
    outbox_dir = pseudo_dir / "mailbox" / "outbox"
    msg_dir = outbox_dir / "msg-002"
    msg_dir.mkdir(parents=True)
    msg = {
        "id": "msg-002",
        "_mailbox_id": "msg-002",
        "from": "human",
        "to": ["stranger"],
        "message": "not for me",
        "received_at": "2026-04-21T10:00:00.000Z",
    }
    (msg_dir / "message.json").write_text(json.dumps(msg))

    received: list[dict] = []
    svc = FilesystemMailService(
        working_dir=my_dir,
        pseudo_agent_subscriptions=["../human"],
    )
    svc.listen(on_message=lambda p: received.append(p))
    try:
        # Give the poller more than one tick to make sure it saw the file.
        time.sleep(1.5)
    finally:
        svc.stop()

    # Message must still be in outbox; not in sent; no dispatch.
    assert msg_dir.exists(), "message must remain in outbox — it isn't addressed to us"
    sent_dir = pseudo_dir / "mailbox" / "sent" / "msg-002"
    assert not sent_dir.exists(), "message must not be moved to sent"
    # And the subscriber's own inbox must be empty: a non-addressed message
    # must not leave a speculative inbox copy behind.
    own_inbox_entry = my_dir / "mailbox" / "inbox" / "msg-002"
    assert not own_inbox_entry.exists(), (
        "non-addressed message must not produce an own-inbox entry"
    )
    assert received == []
    assert received == [], f"on_message must not fire for non-matching To: got {received}"
