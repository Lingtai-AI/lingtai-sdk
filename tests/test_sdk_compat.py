"""Compatibility is by re-export, not re-implementation: every active legacy
import path must resolve to the SAME object the SDK exports (identity, not just
name equality), so there is no forked parallel hierarchy.
"""
from __future__ import annotations

import importlib

import pytest

from lingtai_sdk import _compat


def _resolve(dotted: str):
    module_path, _, attr = dotted.rpartition(".")
    mod = importlib.import_module(module_path)
    return getattr(mod, attr)


def test_migration_map_nonempty():
    assert _compat.active_aliases(), "expected at least one active alias"


@pytest.mark.parametrize("dep", _compat.active_aliases(), ids=lambda d: d.legacy)
def test_legacy_path_resolves_to_same_object(dep):
    assert _resolve(dep.legacy) is _resolve(dep.current), (
        f"{dep.legacy} and {dep.current} resolved to different objects; "
        "the compatibility re-export has forked."
    )


def test_migration_for_lookup():
    first = _compat.active_aliases()[0]
    assert _compat.migration_for(first.legacy) is first
    assert _compat.migration_for("does.not.exist") is None


def test_deprecation_is_active_alias_flag():
    active = _compat.Deprecation(
        legacy="a", current="b", symbol="s", since="0.0.0"
    )
    retired = _compat.Deprecation(
        legacy="a", current="b", symbol="s", since="0.0.0", removed_in="1.0.0"
    )
    assert active.is_active_alias is True
    assert retired.is_active_alias is False
