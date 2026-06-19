"""Composable guard layer for proposed tool calls.

The first version is deliberately thin: an empty guard chain preserves the
existing default-allow behavior while giving future policy checks a structured
place to return denial or warning decisions before a tool is dispatched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class ToolProposal:
    """Normalized proposal presented to tool-call guard checks."""

    tool_name: str
    tool_args: dict[str, Any]
    tool_call_id: str | None = None
    tool_trace_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_call_id": self.tool_call_id,
            "tool_trace_id": self.tool_trace_id,
            "context": self.context,
        }


@dataclass(frozen=True)
class GuardDecision:
    """Structured decision returned by a tool-call guard check.

    ``allowed`` is the execution gate.  The other fields are intentionally
    provider/UI friendly so a denied call can be turned into a synthesized tool
    result rejection pair without guessing which check fired or why.
    """

    allowed: bool = True
    check_name: str = "default_allow"
    reason: str = ""
    action: str = "allow"
    severity: str = "info"
    metadata: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def allow(
        cls,
        *,
        check_name: str = "default_allow",
        reason: str = "",
        action: str = "allow",
        severity: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> "GuardDecision":
        return cls(
            allowed=True,
            check_name=check_name,
            reason=reason,
            action=action,
            severity=severity,
            metadata=metadata or {},
        )

    @classmethod
    def deny(
        cls,
        *,
        check_name: str,
        reason: str,
        action: str = "deny",
        severity: str = "error",
        metadata: dict[str, Any] | None = None,
    ) -> "GuardDecision":
        return cls(
            allowed=False,
            check_name=check_name,
            reason=reason,
            action=action,
            severity=severity,
            metadata=metadata or {},
        )

    @property
    def approval_mode(self) -> str:
        if self.check_name == "default_allow" and self.allowed and self.action == "allow":
            return "pass_through"
        return "guard"

    @property
    def is_structured(self) -> bool:
        return not (
            self.allowed
            and self.check_name == "default_allow"
            and self.action == "allow"
            and not self.reason
            and not self.metadata
            and not self.checks
        )

    def to_payload(self, proposal: ToolProposal | None = None) -> dict[str, Any]:
        payload = {
            "allowed": self.allowed,
            "check_name": self.check_name,
            "reason": self.reason,
            "action": self.action,
            "severity": self.severity,
            "metadata": self.metadata,
            "checks": self.checks,
        }
        if proposal is not None:
            payload["proposal"] = proposal.to_payload()
        return payload

    def advisory_metadata(self, proposal: ToolProposal | None = None) -> dict[str, Any] | None:
        if self.allowed and self.action == "allow" and self.severity not in {"warning", "error"}:
            return None
        payload = self.to_payload(proposal)
        payload["type"] = "tool_call_guard"
        payload["message"] = self.reason
        return payload


GuardCheck = Callable[[ToolProposal], GuardDecision | bool | None]


class ToolCallGuard:
    """Evaluate a chain of functional checks for a proposed tool call."""

    def __init__(self, checks: Iterable[GuardCheck] | None = None) -> None:
        self._checks = list(checks or [])

    def evaluate(self, proposal: ToolProposal) -> GuardDecision:
        checks_payload: list[dict[str, Any]] = []
        strongest = GuardDecision.allow()
        for index, check in enumerate(self._checks):
            try:
                raw = check(proposal)
            except Exception as exc:
                decision = self._check_exception_decision(check=check, index=index, exc=exc)
            else:
                decision = self._coerce_decision(raw, check=check, index=index)
            checks_payload.append(decision.to_payload())
            if not decision.allowed:
                return GuardDecision(
                    allowed=False,
                    check_name=decision.check_name,
                    reason=decision.reason,
                    action=decision.action,
                    severity=decision.severity,
                    metadata=decision.metadata,
                    checks=checks_payload,
                )
            if decision.action != "allow" or decision.severity in {"warning", "error"}:
                strongest = decision
        if checks_payload and strongest.check_name != "default_allow":
            return GuardDecision(
                allowed=True,
                check_name=strongest.check_name,
                reason=strongest.reason,
                action=strongest.action,
                severity=strongest.severity,
                metadata=strongest.metadata,
                checks=checks_payload,
            )
        return GuardDecision.allow()

    @staticmethod
    def _check_name(check: GuardCheck, index: int) -> str:
        return getattr(check, "__name__", None) or f"check_{index}"

    @classmethod
    def _check_exception_decision(
        cls,
        *,
        check: GuardCheck,
        index: int,
        exc: Exception,
    ) -> GuardDecision:
        name = cls._check_name(check, index)
        return GuardDecision.deny(
            check_name=name,
            reason=f"Tool-call guard check {name} raised {type(exc).__name__}: {exc}",
            metadata={
                "exception_type": type(exc).__name__,
            },
        )

    @staticmethod
    def _coerce_decision(
        raw: GuardDecision | bool | None,
        *,
        check: GuardCheck,
        index: int,
    ) -> GuardDecision:
        if isinstance(raw, GuardDecision):
            return raw
        name = ToolCallGuard._check_name(check, index)
        if raw is False:
            return GuardDecision.deny(
                check_name=name,
                reason=f"Tool call denied by guard check {name}",
            )
        return GuardDecision.allow(check_name=name)
