"""Context-pressure / molt reminder — the single owned abstraction.

This module unifies the molt/context-pressure reminder that used to be spread
across ``SessionManager`` (raw streak counters) and ``meta_block`` (warning
decision + prose).  It is deliberately scoped to *this one* reminder — there is
no global reminder registry yet.

The abstraction owns:

  * the sustained-pressure **state machine** (channel B): per-provider-round
    input (usage + round id), the transient streak state, and the
    warn-after-N-consecutive-high-rounds decision;
  * the current-state reminder **rendering** for
    ``_meta.agent_meta.context.molt`` (:meth:`current_molt_context`);
  * the reconstruction-event **annotation** (channel A) for
    ``_meta.tool_meta.reconstruction.molt`` (:meth:`annotate_reconstruction`);
  * a :meth:`snapshot` / :meth:`to_debug_dict` view for tests / logs /
    debugging (thresholds, streak, active, last usage/round, and why).

Behavior is unchanged from the pre-abstraction code: same 0.75 high-round ratio,
same 3-round warn count, same 0.60 recovery target, same one-shot reconstruction
event, same natural-language strings, same ``_meta`` shapes.  See
``config.py`` for the constants' rationale and ``meta_block.py`` /
``session.py`` for the callers.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import (
    CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
    CONTEXT_PRESSURE_WARN_AFTER_ROUNDS,
)


def _format_ratio_percent(value: float | int | str | None) -> str:
    """Render a 0..1 ratio as a compact percent string ('75%', '60.5%').

    Same behavior as the former ``meta_block._format_ratio_percent`` (which this
    replaces), so the reminder prose reads identically to the pre-abstraction
    output. Kept local to avoid a meta_block import (meta_block imports this
    module, not the reverse). Negative / unparseable input renders as "an
    unknown amount".
    """
    try:
        pct = float(value) * 100
    except Exception:
        return "an unknown amount"
    if pct < 0:
        return "an unknown amount"
    if abs(pct - round(pct)) < 0.05:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


# Transition reasons recorded on the reminder for debugging (``why`` in the
# debug dict). These describe what the LAST ``note_round`` observation did.
TRANSITION_INITIAL = "initial"
TRANSITION_HIGH_ROUND = "high_round"          # advanced the streak (below warn count)
TRANSITION_WARNING_ACTIVE = "warning_active"  # advanced the streak to/above warn count
TRANSITION_RELIEVED = "relieved"              # dropped below ratio; streak reset to 0
TRANSITION_DUPLICATE = "duplicate_round"      # same round_id re-observed; no-op
TRANSITION_UNKNOWN_USAGE = "unknown_usage"    # sentinel/unparseable usage; left untouched


@dataclass
class ContextPressureReminder:
    """Owns the molt/context-pressure reminder state machine and rendering.

    One instance lives per ``SessionManager``.  It is transient runtime state —
    a fresh or restored session starts with a fresh reminder (context pressure
    does not survive a restart), so nothing here is persisted.

    Thresholds default to the kernel-fixed ``CONTEXT_PRESSURE_*`` constants and
    are stored on the instance so :meth:`to_debug_dict` can report exactly which
    values drove a decision (and so tests can inject variants).
    """

    reconstruction_ratio: float = CONTEXT_PRESSURE_RECONSTRUCTION_RATIO
    warn_after_rounds: int = CONTEXT_PRESSURE_WARN_AFTER_ROUNDS
    recovery_target: float = CONTEXT_PRESSURE_RECOVERY_TARGET

    # Transient streak state.
    streak: int = 0
    last_round_id: int | None = None
    last_usage: float | None = None
    last_transition_reason: str = TRANSITION_INITIAL

    # ------------------------------------------------------------------
    # Channel B — sustained-pressure streak state machine
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True once context has been high for >= ``warn_after_rounds`` rounds.

        Current state, not a one-shot event: it stays True while the streak
        remains at/above the warn count and drops the moment context relaxes
        below the ratio (which resets the streak).
        """
        return self.streak >= self.warn_after_rounds

    def note_round(self, usage: float, *, round_id: int) -> None:
        """Record one *fresh provider round*'s context usage for the streak.

        ``round_id`` identifies the provider round (the kernel passes the
        ``_api_calls`` counter, which increments exactly once per real provider
        response).  Re-observing the same ``round_id`` is a no-op so duplicate
        ``build_meta`` / tool-result stamps in a single batch cannot advance the
        streak — only genuinely new provider rounds do.

        ``usage`` is the fraction of the context window in use:
          * ``>= reconstruction_ratio`` → high round, advance the streak.
          * ``0 <= usage < ratio``      → relieved, reset to 0.
          * ``< 0`` (sentinel, decomposition not ready) → leave streak untouched.
        """
        if round_id == self.last_round_id:
            self.last_transition_reason = TRANSITION_DUPLICATE
            return
        try:
            pressure = float(usage)
        except (TypeError, ValueError):
            self.last_transition_reason = TRANSITION_UNKNOWN_USAGE
            return
        if pressure < 0:
            # Unknown/sentinel usage: neither a high round nor a real relief.
            # Don't advance and don't spuriously reset an existing streak.
            self.last_transition_reason = TRANSITION_UNKNOWN_USAGE
            return
        self.last_round_id = round_id
        self.last_usage = pressure
        if pressure >= self.reconstruction_ratio:
            self.streak += 1
            self.last_transition_reason = (
                TRANSITION_WARNING_ACTIVE if self.active else TRANSITION_HIGH_ROUND
            )
        else:
            self.streak = 0
            self.last_transition_reason = TRANSITION_RELIEVED

    # ------------------------------------------------------------------
    # Channel B — current-state reminder rendering
    # ------------------------------------------------------------------

    def current_molt_context(self, usage: float) -> str | None:
        """Return the ``_meta.agent_meta.context.molt`` reminder, or ``None``.

        Returns ``None`` unless the sustained-pressure warning is :attr:`active`.
        ``usage`` is the current context-window fraction to name in the prose
        (the streak count comes from this instance's own state).
        """
        if not self.active:
            return None
        return render_current_molt_context(
            streak=self.streak,
            usage=usage,
            recovery_target=self.recovery_target,
        )

    # ------------------------------------------------------------------
    # Channel A — reconstruction-event annotation
    # ------------------------------------------------------------------

    def annotate_reconstruction(
        self, after_usage: float, *, recovery_target: float | None = None
    ) -> str | None:
        """Return the reconstruction-event ``molt`` reminder, or ``None``.

        Channel A is a permanent one-shot event assembled by
        ``meta_block.build_reconstruction_tool_meta`` (which owns the
        provider-vs-local after-context resolution and event shape).  This method
        owns only the *warning decision + prose*: when the rebuilt after-context
        ``after_usage`` is still at/above the recovery target, return the
        natural-language reminder; otherwise return ``None``.

        ``recovery_target`` lets the caller pass the event's own recovery target
        (which may come from the adapter's pending event) so the decision uses
        the same value stamped into the event; it defaults to this instance's.
        """
        target = self.recovery_target if recovery_target is None else recovery_target
        return render_reconstruction_molt(
            after_usage=after_usage,
            recovery_target=target,
            reconstruction_ratio=self.reconstruction_ratio,
        )

    # ------------------------------------------------------------------
    # Debug / snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Alias for :meth:`to_debug_dict` (test/log-friendly name)."""
        return self.to_debug_dict()

    def to_debug_dict(self) -> dict:
        """Return a flat, JSON-friendly view for tests / logs / debugging.

        Carries the thresholds that drove decisions, the current streak/active
        state, the last observed usage/round, and *why* the last observation
        transitioned the way it did.
        """
        return {
            "reconstruction_ratio": self.reconstruction_ratio,
            "warn_after_rounds": self.warn_after_rounds,
            "recovery_target": self.recovery_target,
            "streak": self.streak,
            "active": self.active,
            "last_round_id": self.last_round_id,
            "last_usage": self.last_usage,
            "last_transition_reason": self.last_transition_reason,
        }


# ---------------------------------------------------------------------------
# Pure prose renderers — shared by the class methods above and by the
# meta_block compatibility path (which may see a session stand-in without a
# real ContextPressureReminder, e.g. a bare test SimpleNamespace).  Keeping the
# strings here is the single source of truth for the reminder wording.
# ---------------------------------------------------------------------------


def render_current_molt_context(
    *,
    streak: int,
    usage: float,
    recovery_target: float = CONTEXT_PRESSURE_RECOVERY_TARGET,
) -> str:
    """Render the channel-B sustained-pressure reminder string.

    Kept agent-facing and sentence-like: the agent needs a clear reminder about
    why it appeared and what to do, not a tag soup of stage/threshold/action
    fields.
    """
    try:
        pressure = float(usage)
    except (TypeError, ValueError):
        pressure = -1.0
    usage_text = _format_ratio_percent(pressure)
    recovery_text = _format_ratio_percent(recovery_target)
    return (
        f"Context has stayed high across {int(streak)} consecutive fresh model calls "
        f"(currently {usage_text} of the context window). This is a context-pressure "
        "reminder, not an immediate command: when continuing, batch tool results "
        "you have already digested before summarizing. Repeated summarize calls "
        "while context stays above 75% substantially hurt token efficiency. "
        f"The recovery target is {recovery_text}, but if a batched summarize/"
        "reconstruction pass still leaves context above 75%, stop repeating "
        "summarize, tend durable stores, and molt deliberately. See psyche-manual."
    )


def render_reconstruction_molt(
    *,
    after_usage: float,
    recovery_target: float = CONTEXT_PRESSURE_RECOVERY_TARGET,
    reconstruction_ratio: float = CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
) -> str | None:
    """Render the channel-A reconstruction ``molt`` reminder, or ``None``.

    Returns ``None`` when the rebuilt after-context is below the recovery target
    (a successful reconstruction — no reminder needed).  Above the recovery
    target there are two branches: still ``>= reconstruction_ratio`` (stop
    looping summarize, molt) vs merely above the recovery target (one more
    batched summarize is fine, otherwise molt).
    """
    try:
        above_recovery = after_usage >= float(recovery_target)
    except (TypeError, ValueError):
        return None
    if not above_recovery:
        return None
    after_text = _format_ratio_percent(after_usage)
    recovery_text = _format_ratio_percent(recovery_target)
    if after_usage >= reconstruction_ratio:
        return (
            "The runtime already rebuilt the provider context after summarization, "
            f"but the rebuilt context is still at {after_text} of the context "
            "window, above the 75% high-context threshold. Repeated summarize "
            "calls while context stays above 75% substantially hurt token "
            "efficiency; stop repeating summarize, tend durable stores, and molt "
            "deliberately. See psyche-manual."
        )
    return (
        "The runtime already rebuilt the provider context after summarization, "
        f"but the rebuilt context is still at {after_text} of the context "
        f"window, at or above the {recovery_text} recovery target. "
        "If more digested tool results can be summarized, do that as one "
        "batch; otherwise tend durable stores and molt deliberately. See "
        "psyche-manual."
    )
