"""Tests for check-caps CLI subcommand."""
from __future__ import annotations

import json
import subprocess
import sys

from lingtai.capabilities import get_all_providers


def test_get_all_providers_returns_all_capabilities():
    result = get_all_providers()
    expected = {
        "file", "bash", "web_search", "knowledge",
        "skills", "vision", "avatar", "daemon",
    }
    assert expected == set(result.keys())


def test_get_all_providers_structure():
    result = get_all_providers()
    for name, info in result.items():
        assert "providers" in info, f"{name} missing 'providers'"
        assert "default" in info, f"{name} missing 'default'"
        assert isinstance(info["providers"], list), f"{name} providers not a list"


def test_builtin_capabilities_have_empty_providers():
    result = get_all_providers()
    builtins = ["file", "bash", "knowledge", "skills", "avatar", "daemon"]
    for name in builtins:
        assert result[name]["providers"] == [], f"{name} should have empty providers"
        assert result[name]["default"] == "builtin", f"{name} should default to builtin"


def test_provider_dependent_capabilities():
    result = get_all_providers()
    assert result["vision"]["default"] is None
    assert "minimax" in result["vision"]["providers"]
    assert "gemini" in result["vision"]["providers"]
    # "local" is intentionally NOT advertised in PROVIDERS yet — it works
    # via explicit opt-in (add_capability(..., provider="local")) but
    # should not appear in check-caps output. See capabilities/vision.py.
    assert "local" not in result["vision"]["providers"]


def test_check_caps_cli_output():
    """Test the CLI subcommand outputs valid JSON."""
    proc = subprocess.run(
        [sys.executable, "-m", "lingtai", "check-caps"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"check-caps failed: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "file" in data
    assert "vision" in data
    assert isinstance(data["vision"]["providers"], list)
