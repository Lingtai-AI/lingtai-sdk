"""Regression tests for issue #145 — internal email reply route preservation.

When two `.lingtai/` networks both contain an agent named ``mimo-1``,
``email(action="send", mode="abs", address=...)`` from A to B followed by
``email(action="reply", ...)`` on the receiving side must route back to the
original sender's absolute path — not collapse to the ambiguous bare name and
self-deliver inside the responder's own network.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _seed_agent_dir(path: Path, *, agent_name: str = "mimo-1") -> None:
    """Materialize the .agent.json + heartbeat that FilesystemMailService needs."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".agent.json").write_text(json.dumps({"agent_name": agent_name, "admin": {}}))
    (path / ".agent.heartbeat").write_text(str(time.time()))
    (path / "mailbox" / "inbox").mkdir(parents=True, exist_ok=True)


def _make_inbox_email(working_dir: Path, *, sender: str, subject: str = "hello",
                       message: str = "body", identity: dict | None = None,
                       return_route: dict | None = None,
                       to: list[str] | None = None) -> str:
    """Write a fake inbound message into working_dir/mailbox/inbox/{uuid}/."""
    eid = str(uuid4())
    msg_dir = working_dir / "mailbox" / "inbox" / eid
    msg_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "_mailbox_id": eid,
        "from": sender,
        "to": to or ["mimo-1"],
        "subject": subject,
        "message": message,
        "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if identity is not None:
        data["identity"] = identity
    if return_route is not None:
        data["_return_route"] = return_route
    (msg_dir / "message.json").write_text(json.dumps(data, indent=2))
    return eid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_abs_send_embeds_return_route_in_dispatched_payload(tmp_path):
    """``_send(mode="abs")`` must persist a concrete return route so the
    recipient's later ``reply`` can address the original sender exactly,
    even when ``from`` is later trimmed/normalized."""
    sender_dir = tmp_path / "dev-1" / ".lingtai" / "mimo-1"
    sender_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=sender_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    recipient_path = str(tmp_path / "dev-2" / ".lingtai" / "mimo-1")
    result = agent._email_manager.handle({
        "action": "send",
        "mode": "abs",
        "address": recipient_path,
        "subject": "hi",
        "message": "ping",
    })
    assert result["status"] == "sent"

    # Wait briefly for the daemon dispatch thread to run mail_service.send.
    deadline = time.time() + 2.0
    while time.time() < deadline and not mock_svc.send.called:
        time.sleep(0.05)
    assert mock_svc.send.called, "mail service was never invoked"

    dispatched = mock_svc.send.call_args[0][1]
    rr = dispatched.get("_return_route")
    assert rr is not None, "abs send must embed _return_route"
    assert rr.get("mode") == "abs"
    assert rr.get("address") == str(sender_dir)
    # sender_agent_id is mandatory for the ambiguity guard on reply.
    assert rr.get("sender_agent_id") == agent._agent_id

    agent.stop(timeout=1.0)


def test_peer_send_does_not_embed_return_route(tmp_path):
    """Peer-mode (intra-network) sends keep the lean payload — no route needed."""
    sender_dir = tmp_path / "dev-1" / ".lingtai" / "mimo-1"
    sender_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=sender_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    agent._email_manager.handle({
        "action": "send",
        "address": "peer-2",
        "subject": "hi",
        "message": "ping",
    })

    deadline = time.time() + 2.0
    while time.time() < deadline and not mock_svc.send.called:
        time.sleep(0.05)
    assert mock_svc.send.called

    dispatched = mock_svc.send.call_args[0][1]
    assert dispatched.get("_return_route") in (None,)

    agent.stop(timeout=1.0)


def test_reply_uses_return_route_address_in_abs_mode(tmp_path):
    """When an inbound message carries ``_return_route`` (mode=abs), the
    reply must be dispatched in abs mode to the route's address — not to
    the bare ``from`` alias."""
    # dev-2 side: where we live and reply *from*.
    responder_dir = tmp_path / "dev-2" / ".lingtai" / "mimo-1"
    responder_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=responder_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    original_sender_abs = str(tmp_path / "dev-1" / ".lingtai" / "mimo-1")
    eid = _make_inbox_email(
        responder_dir,
        sender="mimo-1",  # bare; this is what made the original bug bite
        subject="please reply",
        message="original",
        identity={"agent_name": "mimo-1", "agent_id": "AGENT-DEV-1",
                  "admin": {}},
        return_route={"mode": "abs", "address": original_sender_abs,
                      "sender_agent_id": "AGENT-DEV-1"},
    )
    result = agent._email_manager.handle({
        "action": "reply",
        "email_id": [eid],
        "message": "reply body",
    })
    assert result["status"] == "sent"

    deadline = time.time() + 2.0
    while time.time() < deadline and not mock_svc.send.called:
        time.sleep(0.05)
    assert mock_svc.send.called, "reply was never dispatched"

    call = mock_svc.send.call_args
    address_arg = call[0][0]
    kwargs = call[1]
    assert address_arg == original_sender_abs, (
        f"reply went to {address_arg!r} not the abs return route"
    )
    assert kwargs.get("mode") == "abs"

    agent.stop(timeout=1.0)


def test_reply_falls_back_to_abs_when_from_is_absolute_path(tmp_path):
    """Older messages without ``_return_route`` but with an absolute ``from``
    should still be replied to via abs mode — graceful upgrade path."""
    responder_dir = tmp_path / "dev-2" / ".lingtai" / "mimo-1"
    responder_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=responder_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    original_sender_abs = str(tmp_path / "dev-1" / ".lingtai" / "mimo-1")
    eid = _make_inbox_email(
        responder_dir,
        sender=original_sender_abs,
        subject="please reply",
        message="original",
        identity={"agent_name": "mimo-1", "agent_id": "AGENT-DEV-1",
                  "admin": {}},
    )
    agent._email_manager.handle({
        "action": "reply",
        "email_id": [eid],
        "message": "reply body",
    })

    deadline = time.time() + 2.0
    while time.time() < deadline and not mock_svc.send.called:
        time.sleep(0.05)
    assert mock_svc.send.called

    address_arg = mock_svc.send.call_args[0][0]
    kwargs = mock_svc.send.call_args[1]
    assert address_arg == original_sender_abs
    assert kwargs.get("mode") == "abs"

    agent.stop(timeout=1.0)


def test_reply_to_same_network_bare_address_still_uses_peer_mode(tmp_path):
    """Same-network replies must not be perturbed: bare ``from`` →
    peer-mode dispatch to the same bare address."""
    responder_dir = tmp_path / "dev-2" / ".lingtai" / "mimo-1"
    responder_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=responder_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    eid = _make_inbox_email(
        responder_dir,
        sender="peer-7",
        subject="hey",
        message="howdy",
        identity={"agent_name": "peer-7", "agent_id": "AGENT-PEER-7",
                  "admin": {}},
    )
    agent._email_manager.handle({
        "action": "reply",
        "email_id": [eid],
        "message": "back at you",
    })

    deadline = time.time() + 2.0
    while time.time() < deadline and not mock_svc.send.called:
        time.sleep(0.05)
    assert mock_svc.send.called

    address_arg = mock_svc.send.call_args[0][0]
    kwargs = mock_svc.send.call_args[1]
    assert address_arg == "peer-7"
    # Peer is the default; allow either explicit "peer" or absent.
    assert kwargs.get("mode", "peer") == "peer"

    agent.stop(timeout=1.0)


def test_reply_self_route_with_different_agent_id_is_refused(tmp_path):
    """Ambiguity guard: if the reply would target the responder's own
    workdir while ``sender_agent_id`` says the original came from a
    different agent, the reply must fail loudly rather than silently
    self-deliver (the exact bug observed in issue #145)."""
    responder_dir = tmp_path / "dev-2" / ".lingtai" / "mimo-1"
    responder_dir.mkdir(parents=True)
    agent = Agent(service=make_mock_service(), agent_name="mimo-1",
                  working_dir=responder_dir)
    mock_svc = MagicMock()
    mock_svc.address = "mimo-1"
    mock_svc.send.return_value = None
    agent._mail_service = mock_svc

    # No ``_return_route`` and bare ``from`` that resolves to self under
    # the peer-resolution rule; identity says the sender is a different
    # agent. This is the exact shape of the dev-1→dev-2 reply that bit
    # us live.
    eid = _make_inbox_email(
        responder_dir,
        sender="mimo-1",
        subject="cross-project ping",
        message="please reply",
        identity={"agent_name": "mimo-1", "agent_id": "AGENT-DEV-1",
                  "admin": {}},
    )
    result = agent._email_manager.handle({
        "action": "reply",
        "email_id": [eid],
        "message": "should be refused",
    })
    assert "error" in result, f"expected ambiguity error, got {result!r}"
    err = result["error"].lower()
    assert "ambig" in err or "abs" in err or "self" in err, (
        f"error should explain the ambiguity, got: {result['error']!r}"
    )
    # No outbound dispatch.
    time.sleep(0.3)
    assert not mock_svc.send.called

    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# End-to-end: two real .lingtai/ roots, both with an agent named "mimo-1".
# ---------------------------------------------------------------------------


def test_abs_reply_lands_in_original_sender_inbox_not_self(tmp_path):
    """Full integration: dev-1 and dev-2 both run an agent named ``mimo-1``.
    The live regression from #145 surfaces on the *second* reply: dev-2
    sends abs to dev-1, dev-1 replies (this hop works because peer-mode
    resolution still honours the abs ``from`` address), then dev-2 replies
    to dev-1's reply. That third hop is where the bare-alias ``from`` made
    the message self-deliver inside dev-2's own network.
    """
    from lingtai_kernel.services.mail import FilesystemMailService

    stop = threading.Event()

    # Two .lingtai/ roots, each with an agent dir named "mimo-1".
    dev1_dir = tmp_path / "dev-1" / ".lingtai" / "mimo-1"
    dev2_dir = tmp_path / "dev-2" / ".lingtai" / "mimo-1"
    _seed_agent_dir(dev1_dir)
    _seed_agent_dir(dev2_dir)

    # Keep heartbeats fresh on both dirs (FilesystemMailService refuses
    # delivery when heartbeat is stale).
    def _hb(target: Path) -> None:
        while not stop.is_set():
            try:
                (target / ".agent.heartbeat").write_text(str(time.time()))
            except OSError:
                pass
            stop.wait(0.3)
    threading.Thread(target=_hb, args=(dev1_dir,), daemon=True).start()
    threading.Thread(target=_hb, args=(dev2_dir,), daemon=True).start()

    dev1_received: list[dict] = []
    dev2_received: list[dict] = []
    dev1_ev = threading.Event()
    dev2_ev = threading.Event()

    dev1_svc = FilesystemMailService(working_dir=dev1_dir)
    dev2_svc = FilesystemMailService(working_dir=dev2_dir)
    dev1_svc.listen(on_message=lambda m: (dev1_received.append(m), dev1_ev.set()))
    dev2_svc.listen(on_message=lambda m: (dev2_received.append(m), dev2_ev.set()))

    try:
        # Two Agents — one per network root, each backed by its own
        # FilesystemMailService.
        dev1 = Agent(
            service=make_mock_service(), agent_name="mimo-1",
            working_dir=dev1_dir, mail_service=dev1_svc,
        )
        dev2 = Agent(
            service=make_mock_service(), agent_name="mimo-1",
            working_dir=dev2_dir, mail_service=dev2_svc,
        )

        # Hop 1: dev-2 sends abs to dev-1.
        send_result = dev2._email_manager.handle({
            "action": "send",
            "mode": "abs",
            "address": str(dev1_dir),
            "subject": "ping from dev-2",
            "message": "are you there?",
        })
        assert send_result["status"] == "sent"
        assert dev1_ev.wait(timeout=5.0), "dev-1 never received dev-2's mail"

        # Hop 2: dev-1 replies to dev-2.
        check_dev1 = dev1._email_manager.handle({"action": "check"})
        hop1_candidates = [e for e in check_dev1["emails"]
                           if "are you there" in (e.get("preview") or "")]
        assert hop1_candidates, f"dev-1 inbox unexpected shape: {check_dev1}"
        hop1_eid = hop1_candidates[0]["id"]
        dev2_ev.clear()
        reply1 = dev1._email_manager.handle({
            "action": "reply",
            "email_id": [hop1_eid],
            "message": "yes, hi back",
        })
        assert reply1.get("status") == "sent", f"reply1 failed: {reply1!r}"
        assert dev2_ev.wait(timeout=5.0), "dev-2 never received dev-1's reply"

        # Hop 3: dev-2 replies to dev-1's reply. This is the hop that
        # silently self-delivered in #145.
        check_dev2 = dev2._email_manager.handle({"action": "check"})
        hop2_candidates = [e for e in check_dev2["emails"]
                           if (e.get("subject") or "").startswith("Re: ping from dev-2")]
        assert hop2_candidates, f"dev-2 inbox unexpected shape: {check_dev2}"
        hop2_eid = hop2_candidates[0]["id"]

        dev1_ev.clear()
        before_dev2_inbox = len(dev2._email_manager.handle({"action": "check"})["emails"])
        reply2 = dev2._email_manager.handle({
            "action": "reply",
            "email_id": [hop2_eid],
            "message": "round 3, dev-2 → dev-1",
        })
        assert reply2.get("status") == "sent", (
            f"reply2 did not dispatch: {reply2!r}"
        )
        assert dev1_ev.wait(timeout=5.0), (
            "dev-1 never received dev-2's second-hop reply — likely "
            "self-delivered inside dev-2's own network, which is exactly "
            "bug #145."
        )

        # dev-2 must NOT have received its own reply back.
        time.sleep(0.5)
        after_dev2_inbox = dev2._email_manager.handle({"action": "check"})["emails"]
        # The only delta should be from genuine arrivals — not the
        # second-hop reply self-bouncing into dev-2's inbox.
        new_in_dev2 = [
            e for e in after_dev2_inbox
            if "round 3" in (e.get("preview") or "")
        ]
        assert not new_in_dev2, (
            "dev-2 self-received its own reply — this is exactly the "
            "misroute described in issue #145.\n"
            f"dev-2 inbox (count {before_dev2_inbox} → {len(after_dev2_inbox)}): "
            f"{after_dev2_inbox}"
        )

        # dev-1 must have the second-hop reply.
        dev1_msgs = [m.get("message", "") for m in dev1_received]
        assert any("round 3" in m for m in dev1_msgs), (
            f"dev-1 missed the second-hop reply: {dev1_msgs}"
        )

        dev1.stop(timeout=1.0)
        dev2.stop(timeout=1.0)
    finally:
        dev1_svc.stop()
        dev2_svc.stop()
        stop.set()
