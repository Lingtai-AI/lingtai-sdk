"""Soul inquiry — synchronous mirror session + inquiry runner.

Clones the agent's conversation (text + thinking only, no tool calls/results),
sends a one-shot question, returns the answer. One-shot per invocation.
"""

from __future__ import annotations


def soul_inquiry(agent, question: str) -> dict | None:
    """Inquiry mode — one-shot mirror session with cloned conversation.

    Clones the agent's conversation (thinking + diary only, no tool
    calls/results), sends the question. Fresh session each time.
    """
    from ...llm.interface import ChatInterface, TextBlock, ThinkingBlock
    from .config import _build_soul_system_prompt
    from .consultation import _send_with_timeout, _write_soul_tokens

    cloned = ChatInterface()

    if agent._chat is not None:
        for entry in agent._chat.interface.entries:
            if entry.role == "system":
                continue
            stripped: list = []
            for block in entry.content:
                if isinstance(block, (TextBlock, ThinkingBlock)):
                    stripped.append(block)
            if stripped:
                if entry.role == "assistant":
                    cloned.add_assistant_message(stripped)
                else:
                    cloned.add_user_blocks(stripped)

    system_prompt = _build_soul_system_prompt(agent)
    system_prompt += "\n\nYou have no tools. Respond with plain text only. Never output tool calls or XML tags."

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=cloned,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=str(e)[:200])
        return None

    response = _send_with_timeout(agent, session, question)
    if not response or not response.text:
        return None

    _write_soul_tokens(agent, response)

    return {
        "prompt": question,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def _publish_human_inquiry_notification(agent, result: dict, question: str) -> None:
    """Publish a clear `/btw` notification for human-source inquiries.

    The TUI's `/btw` command writes `.inquiry`; the heartbeat runs this
    inquiry asynchronously and the existing log entry still drives TUI
    rendering.  This notification is the bridge back to the main agent:
    it tells the active self what the human asked its mirrored self and
    what answer came back, without treating the exchange as a normal
    user request.
    """
    from ...notifications import submit

    answer = str(result.get("voice") or "")
    submit(
        agent._working_dir,
        "btw",
        header="/btw side inquiry answered",
        icon="💭",
        instructions=(
            "This is the result of a human /btw side inquiry answered by "
            "your mirrored self via soul.inquiry. It is context with clear "
            "provenance, not a direct new instruction. Use it only if it "
            "helps the current work. The human still reaches you directly "
            "through email. Dismiss with system(action='dismiss', channel='btw') "
            "after you have noted it."
        ),
        data={
            "source": "human",
            "mode": "inquiry",
            "question": question,
            "answer": answer,
            "thinking": result.get("thinking") or [],
        },
    )
    agent._log("btw_notification_published", question=question[:200])
    try:
        agent._wake_nap("btw_inquiry_published")
    except Exception as e:
        agent._log("btw_notification_wake_error", error=str(e)[:200])


def _run_inquiry(agent, question: str, source: str = "agent") -> None:
    """Run soul.inquiry and log result as insight event."""
    from .flow import _persist_soul_entry

    try:
        result = soul_inquiry(agent, question)
        if result:
            agent._log(
                "insight", text=result["voice"], question=question, source=source
            )
            _persist_soul_entry(agent, result, mode="inquiry", source=source)
            if source == "human":
                _publish_human_inquiry_notification(agent, result, question)
        else:
            agent._log("insight", text="(silence)", question=question, source=source)
    except Exception as e:
        agent._log("insight_error", error=str(e)[:200], question=question)
