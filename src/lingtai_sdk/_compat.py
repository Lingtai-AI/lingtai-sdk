"""Migration map from legacy import paths to the SDK public surface.

The machine-readable contract behind the compatibility strategy: each entry
says "the name you used to import from *here* is now canonically reachable from
*there*, and both still work." It powers the migration table in the docs and a
round-trip test that asserts every legacy path resolves to the *same object*
the SDK exports — compatibility by re-export, never by a parallel fork.

No name is removed here. Repo policy is that the kernel public API is additive
within a major; this map records the recommended path without breaking the old
one. A name graduates from alias to removed only across a major bump, at which
point ``removed_in`` is filled.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Deprecation:
    legacy: str
    current: str
    symbol: str
    since: str
    removed_in: str | None = None
    note: str = ""

    @property
    def is_active_alias(self) -> bool:
        """True while the legacy path is still importable (not yet removed)."""
        return self.removed_in is None


_SDK_INTRODUCED = "0.12.3"

DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        legacy="lingtai_kernel.BaseAgent",
        current="lingtai_sdk.BaseAgent",
        symbol="BaseAgent",
        since=_SDK_INTRODUCED,
        note="Kernel coordinator. Still exported by lingtai_kernel and lingtai.",
    ),
    Deprecation(
        legacy="lingtai.Agent",
        current="lingtai_sdk.Agent",
        symbol="Agent",
        since=_SDK_INTRODUCED,
        note="Batteries-included agent. Lives in the wrapper; SDK re-exports lazily.",
    ),
    Deprecation(
        legacy="lingtai_kernel.config.AgentConfig",
        current="lingtai_sdk.types.AgentConfig",
        symbol="AgentConfig",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.state.AgentState",
        current="lingtai_sdk.types.AgentState",
        symbol="AgentState",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.message.Message",
        current="lingtai_sdk.types.Message",
        symbol="Message",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.types.UnknownToolError",
        current="lingtai_sdk.errors.UnknownToolError",
        symbol="UnknownToolError",
        since=_SDK_INTRODUCED,
    ),
)


def active_aliases() -> tuple[Deprecation, ...]:
    """Legacy paths that still import successfully (the common case today)."""
    return tuple(d for d in DEPRECATIONS if d.is_active_alias)


def migration_for(legacy_path: str) -> Deprecation | None:
    """Look up the recommended move for a legacy import path, if any."""
    for d in DEPRECATIONS:
        if d.legacy == legacy_path:
            return d
    return None


__all__ = ["Deprecation", "DEPRECATIONS", "active_aliases", "migration_for"]
