"""Kernel-owned runtime safety limits.

These constants are safety rails owned by the kernel.  They are not user,
preset, or agent manifest configuration and should not be read from or emitted
into ``init.json`` / resolved manifests as authoritative per-agent settings.
Changing them is a kernel behavior change.
"""

from __future__ import annotations


# Per-ACTIVE-turn tool-call progress meter.  This is intentionally a very large
# emergency fuse rather than a normal workflow boundary: agents should usually
# finish, delegate, molt, or go IDLE long before this many tool calls in a
# single ACTIVE turn.
ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT = 10_000

# Model-visible soft self-check cadence.  At every crossed interval, dict-shaped
# tool results carry a gentle notice asking the agent to notice whether it may be
# repeating a loop.
ACTIVE_TURN_TOOL_CALL_NOTICE_INTERVAL = 500
