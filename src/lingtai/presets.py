"""Compatibility shim — preset library moved into the kernel.

The implementation now lives at ``lingtai_kernel.presets``. New code should
import from there directly. This shim re-exports the public surface so older
wrapper-side callers keep working unchanged.
"""
from __future__ import annotations

from lingtai_kernel.presets import *  # noqa: F401,F403
from lingtai_kernel.presets import (  # noqa: F401
    default_presets_path,
    home_shortened,
    resolve_preset_name,
    resolve_allowed_presets,
    discover_presets_in_dirs,
    load_preset,
    materialize_active_preset,
    preset_tier,
    preset_context_limit,
    expand_inherit,
)
