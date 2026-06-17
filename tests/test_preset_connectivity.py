"""Tests for the preset connectivity check helper."""
import os
import time
from unittest.mock import patch

import pytest


def test_no_credentials_when_env_var_unset(monkeypatch):
    """If api_key_env is set but the env var is not, return no_credentials immediately."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    from lingtai_kernel.preset_connectivity import check_connectivity
    result = check_connectivity(
        provider="minimax",
        base_url="https://api.minimax.io",
        api_key_env="MISSING_KEY",
    )
    assert result["status"] == "no_credentials"
    assert "MISSING_KEY" in result.get("error", "")


def test_no_credentials_does_not_make_network_call(monkeypatch):
    """When env var is missing, no socket/network call is attempted."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    from lingtai_kernel import preset_connectivity
    with patch.object(preset_connectivity, "_probe_host") as probe:
        result = preset_connectivity.check_connectivity(
            provider="minimax",
            base_url="https://api.minimax.io",
            api_key_env="MISSING_KEY",
        )
        assert result["status"] == "no_credentials"
        probe.assert_not_called()


def test_ok_when_host_reachable(monkeypatch):
    """When env var is set and host is reachable, return ok with latency."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity
    with patch.object(preset_connectivity, "_probe_host", return_value=42):
        result = preset_connectivity.check_connectivity(
            provider="x",
            base_url="https://api.example.com",
            api_key_env="MOCK_KEY",
        )
        assert result["status"] == "ok"
        assert result["latency_ms"] == 42
        assert "checked_at" in result


def test_unreachable_when_host_probe_raises(monkeypatch):
    """When the probe raises, return unreachable with the error message."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity
    with patch.object(preset_connectivity, "_probe_host", side_effect=OSError("connection refused")):
        result = preset_connectivity.check_connectivity(
            provider="x",
            base_url="https://api.example.com",
            api_key_env="MOCK_KEY",
        )
        assert result["status"] == "unreachable"
        assert "connection refused" in result["error"]


def test_no_api_key_env_skips_credential_check(monkeypatch):
    """If api_key_env is None or empty, skip credential check and just probe."""
    from lingtai_kernel import preset_connectivity
    with patch.object(preset_connectivity, "_probe_host", return_value=10):
        result = preset_connectivity.check_connectivity(
            provider="x",
            base_url="https://api.example.com",
            api_key_env=None,
        )
        assert result["status"] == "ok"


def test_default_url_used_when_base_url_missing(monkeypatch):
    """When base_url is None, fall back to provider's default URL."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity
    captured = {}
    def fake_probe(host, port, timeout):
        captured["host"] = host
        captured["port"] = port
        return 5
    with patch.object(preset_connectivity, "_probe_host", side_effect=fake_probe):
        preset_connectivity.check_connectivity(
            provider="openai",
            base_url=None,
            api_key_env="MOCK_KEY",
        )
        assert captured["host"] == "api.openai.com"
        assert captured["port"] == 443


def test_unreachable_when_no_url_and_unknown_provider(monkeypatch):
    """No base_url + unknown provider → unreachable with explanatory error."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel.preset_connectivity import check_connectivity
    result = check_connectivity(
        provider="weird-provider",
        base_url=None,
        api_key_env="MOCK_KEY",
    )
    assert result["status"] == "unreachable"
    assert "weird-provider" in result["error"] or "no base_url" in result["error"]


def test_no_caching_every_call_reprobes(monkeypatch):
    """No cache: identical back-to-back calls each trigger a probe.

    The whole point of the check is freshness — caching would let the agent
    swap into a preset that just went down. Every call hits the network
    (or short-circuits on no_credentials, which is free anyway).
    """
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity
    probe_calls = []
    def counting_probe(host, port, timeout):
        probe_calls.append((host, port))
        return 1
    with patch.object(preset_connectivity, "_probe_host", side_effect=counting_probe):
        preset_connectivity.check_connectivity("x", "https://h.example.com", "MOCK_KEY")
        preset_connectivity.check_connectivity("x", "https://h.example.com", "MOCK_KEY")
        preset_connectivity.check_connectivity("x", "https://h.example.com", "MOCK_KEY")
    assert len(probe_calls) == 3  # every call probes; no shortcut


def test_check_many_runs_in_parallel(monkeypatch):
    """check_many() runs probes concurrently — total time is bounded by the
    slowest single probe, not the sum."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity

    def slow_probe(host, port, timeout):
        time.sleep(0.5)
        return 500

    with patch.object(preset_connectivity, "_probe_host", side_effect=slow_probe):
        start = time.time()
        results = preset_connectivity.check_many([
            {"provider": "a", "base_url": "https://a.example.com", "api_key_env": "MOCK_KEY"},
            {"provider": "b", "base_url": "https://b.example.com", "api_key_env": "MOCK_KEY"},
            {"provider": "c", "base_url": "https://c.example.com", "api_key_env": "MOCK_KEY"},
            {"provider": "d", "base_url": "https://d.example.com", "api_key_env": "MOCK_KEY"},
        ])
        elapsed = time.time() - start

    assert len(results) == 4
    assert all(r["status"] == "ok" for r in results)
    # 4 sequential probes would take 2.0s; parallel should be ~0.5s.
    # Allow generous slack for CI.
    assert elapsed < 1.5, f"check_many took {elapsed:.2f}s — should be parallel"


def test_check_many_preserves_input_order(monkeypatch):
    """check_many() returns results in the same order as specs, even though
    probes complete in arbitrary order in the thread pool."""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    from lingtai_kernel import preset_connectivity

    def variable_probe(host, port, timeout):
        # Probes complete in reverse order from input
        if "first" in host: time.sleep(0.3)
        elif "second" in host: time.sleep(0.2)
        else: time.sleep(0.1)
        return 1

    with patch.object(preset_connectivity, "_probe_host", side_effect=variable_probe):
        results = preset_connectivity.check_many([
            {"provider": "a", "base_url": "https://first.example.com", "api_key_env": "MOCK_KEY"},
            {"provider": "b", "base_url": "https://second.example.com", "api_key_env": "MOCK_KEY"},
            {"provider": "c", "base_url": "https://third.example.com", "api_key_env": "MOCK_KEY"},
        ])
    assert all(r["status"] == "ok" for r in results)


def test_check_many_empty_list_returns_empty():
    from lingtai_kernel.preset_connectivity import check_many
    assert check_many([]) == []
