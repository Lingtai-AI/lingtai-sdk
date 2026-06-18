"""Committed, read-only SDK assets reachable via :mod:`importlib.resources`.

This package exists so the shipped asset trees (currently
``lingtai-sdk-skill/``) are importable resources — ``importlib.resources`` can
locate ``lingtai_sdk.assets`` and traverse into the skill directory without any
filesystem-path guessing and without importing the ``lingtai`` wrapper.

It carries **no code and no runtime** — only the package marker so the asset
files are packaged and resource-addressable. See ``lingtai_sdk.sdk_skill`` for
the loaders that read these assets.
"""
from __future__ import annotations

__all__: list[str] = []
