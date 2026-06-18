"""Pure SDK ``BundleManifest`` → kernel ``GuardCheck`` bridge (stage 17).

A :class:`~lingtai_sdk.capabilities.BundleManifest` already declares a per-bundle
:class:`~lingtai_sdk.capabilities.SecurityDanger` posture (``safe`` / ``caution``
/ ``destructive``) over its named tools. This module is the thin, import-light
adapter that turns one or more manifests into kernel
:mod:`lingtai_kernel.tool_call_guard` primitives — a :data:`GuardCheck` callable
and a ready :class:`ToolCallGuard` chain — so the existing ``ToolExecutor`` guard
seam (stage 16) can consult declared bundle posture *before* a tool is
dispatched, without any executor / ``BaseAgent`` / wrapper ``Agent`` change.

Dependency direction is one-way: the SDK imports the dependency-light kernel
``tool_call_guard`` types; the kernel never imports the SDK. This stage wires
**nothing** into a live wrapper/Agent (no C3 behavior-visible runtime change) —
it only builds the callable a later stage can choose to install.

Policy (deliberately narrow, deterministic, fail-open)
------------------------------------------------------
The map from a manifest's declared danger posture to a guard decision is:

* ``safe``        → **allow**, clean pass-through (the check returns ``None`` so
  the chain collapses to the unchanged ``default_allow`` pass-through — no
  advisory noise on read-only/pure tools);
* ``caution``     → **allow but warn** (a ``warn``/``warning`` advisory carrying
  the bundle name + declared danger, never blocking);
* ``destructive`` → policy-mode dependent:
    * :attr:`GuardPolicyMode.BLOCKING` (the default) → **deny**;
    * :attr:`GuardPolicyMode.ADVISORY` → **allow but warn**;
* an **unknown** tool (declared by no supplied manifest) → **allow** (fail open,
  clean pass-through). The bridge never fails closed: a tool it cannot place is
  always allowed, so installing it can only ever *add* denials for explicitly
  declared destructive surfaces, never break an undeclared tool.

When two manifests declare the *same* tool name with different danger postures,
the **most dangerous** posture wins (``destructive`` > ``caution`` > ``safe``),
so a benign alias can never silently downgrade a destructive tool.
"""
from __future__ import annotations

import enum
from typing import Iterable

from lingtai_kernel.tool_call_guard import (
    GuardCheck,
    GuardDecision,
    ToolCallGuard,
    ToolProposal,
)

from ..bundles.contracts import BundleManifest, SecurityDanger

# Total order over the danger postures, most-dangerous last, so a duplicate tool
# declaration keeps the strongest (highest) posture.
_DANGER_RANK: dict[SecurityDanger, int] = {
    SecurityDanger.SAFE: 0,
    SecurityDanger.CAUTION: 1,
    SecurityDanger.DESTRUCTIVE: 2,
}


class GuardPolicyMode(str, enum.Enum):
    """How the bridge treats a declared ``destructive`` tool.

    ``str``-valued (mirrors :class:`SecurityDanger`) so a member compares equal
    to its wire string and serializes transparently. Only the *destructive*
    posture is mode-sensitive; ``safe`` always passes through and ``caution``
    always warns, in both modes.
    """

    ADVISORY = "advisory"  # destructive tools allowed, surfaced as a warning
    BLOCKING = "blocking"  # destructive tools denied before dispatch


def tool_danger_index(
    manifests: Iterable[BundleManifest],
) -> dict[str, SecurityDanger]:
    """Build a ``{tool_name: SecurityDanger}`` index from bundle manifests.

    Every name in each manifest's ``surfaces.tools`` is mapped to that bundle's
    declared ``security.danger``. When the same tool name appears in more than
    one manifest, the **most dangerous** posture is kept (see module docstring).
    An unrecognized ``security.danger`` string is treated conservatively as
    ``destructive`` rather than silently dropped — a manifest that passed
    ``validate()`` can only carry a known value, but the bridge stays robust if
    handed an unvalidated one.
    """
    return {
        tool: danger for tool, (danger, _bundle) in _danger_origin_index(manifests).items()
    }


def _danger_origin_index(
    manifests: Iterable[BundleManifest],
) -> dict[str, tuple[SecurityDanger, str]]:
    """``{tool_name: (winning_danger, origin_bundle_name)}``.

    Like :func:`tool_danger_index` but also records the name of the bundle whose
    *winning* (strongest) posture decided the tool, so advisories can attribute
    the decision without a second pass. Most-dangerous posture wins ties.
    """
    index: dict[str, tuple[SecurityDanger, str]] = {}
    for manifest in manifests:
        try:
            danger = SecurityDanger(manifest.security.danger)
        except ValueError:
            danger = SecurityDanger.DESTRUCTIVE
        for tool_name in manifest.surfaces.tools:
            existing = index.get(tool_name)
            if existing is None or _DANGER_RANK[danger] > _DANGER_RANK[existing[0]]:
                index[tool_name] = (danger, manifest.name)
    return index


def guard_check_from_manifests(
    manifests: Iterable[BundleManifest],
    *,
    mode: GuardPolicyMode = GuardPolicyMode.BLOCKING,
) -> GuardCheck:
    """Return a single :data:`GuardCheck` deciding by declared bundle posture.

    The returned callable maps a :class:`ToolProposal` to a
    :class:`GuardDecision` per the module-level policy. It is pure and stateless
    over a frozen snapshot of ``manifests`` taken at build time — calling it
    never mutates the manifests and never imports the wrapper or a provider SDK.

    * unknown tool → ``None`` (clean pass-through, fail open);
    * ``safe``     → ``None`` (clean pass-through);
    * ``caution``  → allow-with-``warn`` advisory;
    * ``destructive`` → deny (``BLOCKING``) or allow-with-``warn`` (``ADVISORY``).
    """
    index = _danger_origin_index(manifests)

    def bundle_manifest_guard(proposal: ToolProposal) -> GuardDecision | None:
        entry = index.get(proposal.tool_name)
        if entry is None or entry[0] is SecurityDanger.SAFE:
            # Unknown or safe: clean pass-through (fail open), no advisory.
            return None
        danger, bundle = entry
        metadata = {"danger": danger.value, "bundle": bundle, "policy_mode": mode.value}
        if danger is SecurityDanger.CAUTION:
            return GuardDecision.allow(
                check_name="bundle_manifest_guard",
                reason=(
                    f"tool {proposal.tool_name!r} is declared "
                    f"{danger.value} by bundle {bundle!r}"
                ),
                action="warn",
                severity="warning",
                metadata=metadata,
            )
        # destructive
        if mode is GuardPolicyMode.BLOCKING:
            return GuardDecision.deny(
                check_name="bundle_manifest_guard",
                reason=(
                    f"tool {proposal.tool_name!r} is declared "
                    f"{danger.value} by bundle {bundle!r} and blocked by policy"
                ),
                metadata=metadata,
            )
        return GuardDecision.allow(
            check_name="bundle_manifest_guard",
            reason=(
                f"tool {proposal.tool_name!r} is declared "
                f"{danger.value} by bundle {bundle!r} (advisory mode)"
            ),
            action="warn",
            severity="warning",
            metadata=metadata,
        )

    return bundle_manifest_guard


def tool_call_guard_from_manifests(
    manifests: Iterable[BundleManifest],
    *,
    mode: GuardPolicyMode = GuardPolicyMode.BLOCKING,
) -> ToolCallGuard:
    """Return a ready :class:`ToolCallGuard` chain wrapping one bundle check.

    A convenience over :func:`guard_check_from_manifests` for the common case of
    a single-check chain. The empty-manifest case yields a guard whose only
    check always passes through, i.e. the unchanged ``default_allow`` behavior.
    """
    return ToolCallGuard([guard_check_from_manifests(manifests, mode=mode)])


__all__ = [
    "GuardPolicyMode",
    "tool_danger_index",
    "guard_check_from_manifests",
    "tool_call_guard_from_manifests",
]
