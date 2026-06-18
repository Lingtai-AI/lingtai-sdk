"""Stage 3 — manifest.llm / init.json-level translation for the default factory.

Stage 2 built the default-factory ``LLMService`` only from explicit
``RuntimeOptions.provider/model/base_url/api_key``. Stage 3 lets the runtime
*also* derive that config (and recognized provider defaults / context window)
from ``RuntimeOptions.manifest`` — especially ``manifest['llm']`` — when the
explicit fields are absent. Explicit fields still win; manifest only fills gaps.

These tests run with **no real model, no API key, no network**: the wrapper
``LLMService`` and ``build_provider_defaults_from_manifest_llm`` are
monkeypatched with fakes that capture their arguments, and the default factory
is exercised through a service-requiring fake ``Agent``. Import purity and
secret hygiene are preserved — ``api_key`` must never reach a public surface.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Canonical home of the native runtime adapter (legacy ``lingtai_sdk.native`` is a
# thin re-export shim; private translation helpers live on the canonical module).
from lingtai_sdk.bundles import native
from lingtai_sdk import runtime as rt
from lingtai_sdk.errors import NativeRuntimeConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_SECRET = "sk-manifest-do-not-leak-98765"


# --------------------------------------------------------------------------
# Fakes — a fake LLMService that captures its construction kwargs, a fake
# provider-defaults builder, and a service-requiring fake Agent. None of these
# import lingtai.llm or any provider SDK.
# --------------------------------------------------------------------------
class _FakeLLMService:
    last: "_FakeLLMService | None" = None

    def __init__(
        self,
        provider,
        model,
        api_key=None,
        base_url=None,
        provider_defaults=None,
        context_window=None,
        **kwargs,
    ):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.provider_defaults = provider_defaults
        self.context_window = context_window
        self.kwargs = kwargs
        type(self).last = self


class _ServiceRequiringAgent:
    """Stand-in for the wrapper Agent: service is required (like BaseAgent)."""

    def __init__(self, service=None, **kwargs):
        if service is None:
            raise TypeError("missing required argument: 'service'")
        self.service = service
        self.kwargs = kwargs
        self.started = False
        self.working_dir = Path(kwargs["working_dir"])

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def send(self, content, sender: str = "user") -> None:
        pass


@pytest.fixture
def patched_llm(monkeypatch):
    """Patch the lazy LLMService import target and the defaults builder.

    Returns a dict with a ``builder_calls`` list recording every call to the
    provider-defaults builder, so tests can assert exactly what manifest.llm
    block was handed to it and with which ``max_rpm``.
    """
    builder_calls: list[dict] = []

    def _fake_builder(llm, *, max_rpm):
        builder_calls.append({"llm": dict(llm), "max_rpm": max_rpm})
        # Mimic the real builder's scoped shape; return None when empty.
        per_provider = {}
        if max_rpm > 0:
            per_provider["max_rpm"] = max_rpm
        for key in ("api_compat", "default_headers", "compact_threshold"):
            if key in llm and llm.get(key) is not None:
                per_provider[key] = llm[key]
        return {llm["provider"].lower(): per_provider} if per_provider else None

    # The SDK lazily imports these names from lingtai.llm.service inside the
    # helper; patch them on that module so no real provider SDK loads.
    import lingtai.llm.service as svc_mod

    monkeypatch.setattr(svc_mod, "LLMService", _FakeLLMService)
    monkeypatch.setattr(
        svc_mod, "build_provider_defaults_from_manifest_llm", _fake_builder
    )
    monkeypatch.setattr(native, "_default_agent_factory", _ServiceRequiringAgent)
    _FakeLLMService.last = None
    return {"builder_calls": builder_calls, "service_cls": _FakeLLMService}


def _start(tmp_path, **opt_kwargs):
    rtm = native.NativeRuntime()
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path, **opt_kwargs))
    session.start()
    return session


# --------------------------------------------------------------------------
# Manifest-only config builds the service from manifest.llm
# --------------------------------------------------------------------------
def test_manifest_only_provider_model_builds_service(tmp_path, patched_llm):
    session = _start(
        tmp_path,
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "base_url": "https://manifest.example",
                "api_key": _SECRET,
            }
        },
    )
    svc = patched_llm["service_cls"].last
    assert svc is not None
    assert svc.provider == "anthropic"
    assert svc.model == "claude-opus-4-8"
    assert svc.base_url == "https://manifest.example"
    assert svc.api_key == _SECRET
    assert session.state is rt.RuntimeState.ACTIVE


def test_manifest_only_config_does_not_leak_secret(tmp_path, patched_llm):
    session = _start(
        tmp_path,
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "api_key": _SECRET,
            }
        },
    )
    session.send("hi")
    # No public surface carries the secret.
    assert _SECRET not in repr(session.deferred)
    assert _SECRET not in repr(session.applied)
    for event in session.events():
        assert _SECRET not in repr(event.data)
    applied_llm = session.applied.get("llm", {})
    assert applied_llm.get("provider") == "anthropic"
    assert applied_llm.get("model") == "claude-opus-4-8"
    assert "api_key" not in applied_llm


def test_manifest_llm_api_key_sanitized_in_deferred(tmp_path, patched_llm):
    """A raw api_key inside manifest['llm'] must not survive into the public
    ``session.deferred['manifest']`` unchanged."""
    session = _start(
        tmp_path,
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "api_key": _SECRET,
            }
        },
    )
    assert _SECRET not in repr(session.deferred)


# --------------------------------------------------------------------------
# Explicit RuntimeOptions fields override manifest.llm fields
# --------------------------------------------------------------------------
def test_explicit_options_override_manifest(tmp_path, patched_llm):
    session = _start(
        tmp_path,
        provider="openai",
        model="gpt-explicit",
        base_url="https://explicit.example",
        api_key="sk-explicit",
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-manifest",
                "base_url": "https://manifest.example",
                "api_key": "sk-manifest",
            }
        },
    )
    svc = patched_llm["service_cls"].last
    assert svc.provider == "openai"
    assert svc.model == "gpt-explicit"
    assert svc.base_url == "https://explicit.example"
    assert svc.api_key == "sk-explicit"
    assert session.applied["llm"]["provider"] == "openai"
    assert session.applied["llm"]["model"] == "gpt-explicit"


def test_manifest_fills_only_absent_explicit_fields(tmp_path, patched_llm):
    # provider explicit, model from manifest; base_url explicit, api_key manifest
    _start(
        tmp_path,
        provider="openai",
        base_url="https://explicit.example",
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-manifest",
                "base_url": "https://manifest.example",
                "api_key": _SECRET,
            }
        },
    )
    svc = patched_llm["service_cls"].last
    assert svc.provider == "openai"  # explicit wins
    assert svc.model == "claude-manifest"  # manifest fills
    assert svc.base_url == "https://explicit.example"  # explicit wins
    assert svc.api_key == _SECRET  # manifest fills


# --------------------------------------------------------------------------
# Provider defaults / context_window / max_rpm pass-through
# --------------------------------------------------------------------------
def test_provider_defaults_passed_through(tmp_path, patched_llm):
    session = _start(
        tmp_path,
        manifest={
            "max_rpm": 30,
            "llm": {
                "provider": "custom",
                "model": "glm-4",
                "api_compat": "anthropic",
                "default_headers": {"X-Title": "lingtai"},
                "compact_threshold": 0.8,
            },
        },
    )
    # The builder saw the manifest.llm block and the resolved max_rpm.
    assert patched_llm["builder_calls"], "builder was not called"
    call = patched_llm["builder_calls"][-1]
    assert call["max_rpm"] == 30
    assert call["llm"]["api_compat"] == "anthropic"
    # The built service received the scoped provider_defaults.
    svc = patched_llm["service_cls"].last
    assert svc.provider_defaults is not None
    assert svc.provider_defaults["custom"]["api_compat"] == "anthropic"
    assert svc.provider_defaults["custom"]["max_rpm"] == 30
    # Headers flow to provider_defaults, but are not mirrored on the public
    # deferred manifest because they may carry auth-like secrets.
    assert "default_headers" in svc.provider_defaults["custom"]
    assert "default_headers" not in session.deferred["manifest"]["llm"]


def test_max_rpm_from_options_extra_native_without_manifest_llm(tmp_path, patched_llm):
    # No manifest['llm'] block, so the manifest defaults builder is not invoked;
    # the SDK still scopes the opted-in max_rpm onto provider_defaults directly.
    _start(
        tmp_path,
        provider="anthropic",
        model="claude-opus-4-8",
        extra={"native": {"max_rpm": 12}},
    )
    assert patched_llm["builder_calls"] == []
    svc = patched_llm["service_cls"].last
    assert svc.provider_defaults == {"anthropic": {"max_rpm": 12}}


def test_max_rpm_from_options_extra_native_with_manifest_llm(tmp_path, patched_llm):
    # With a manifest['llm'] block, max_rpm from extra['native'] flows through
    # the manifest-defaults builder (extra wins over manifest['max_rpm']).
    _start(
        tmp_path,
        manifest={
            "max_rpm": 99,
            "llm": {"provider": "anthropic", "model": "claude-opus-4-8"},
        },
        extra={"native": {"max_rpm": 12}},
    )
    call = patched_llm["builder_calls"][-1]
    assert call["max_rpm"] == 12


def test_context_window_from_manifest_llm(tmp_path, patched_llm):
    _start(
        tmp_path,
        manifest={
            "llm": {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "context_window": 500_000,
            }
        },
    )
    svc = patched_llm["service_cls"].last
    assert svc.context_window == 500_000


def test_context_window_from_manifest_context_limit(tmp_path, patched_llm):
    _start(
        tmp_path,
        manifest={
            "context_limit": 250_000,
            "llm": {"provider": "anthropic", "model": "claude-opus-4-8"},
        },
    )
    svc = patched_llm["service_cls"].last
    assert svc.context_window == 250_000


def test_context_window_from_options_extra(tmp_path, patched_llm):
    _start(
        tmp_path,
        provider="anthropic",
        model="claude-opus-4-8",
        extra={"context_window": 333_000},
    )
    svc = patched_llm["service_cls"].last
    assert svc.context_window == 333_000


# --------------------------------------------------------------------------
# Still-missing config raises a clear, secret-free SDK error
# --------------------------------------------------------------------------
def test_partial_manifest_missing_model_raises(tmp_path, patched_llm):
    rtm = native.NativeRuntime()
    session = rtm.create_session(
        rt.RuntimeOptions(
            working_dir=tmp_path,
            api_key=_SECRET,
            manifest={"llm": {"provider": "anthropic", "api_key": _SECRET}},
        )
    )
    with pytest.raises(NativeRuntimeConfigurationError) as excinfo:
        session.start()
    assert _SECRET not in str(excinfo.value)
    assert session.state is rt.RuntimeState.PENDING
    assert session.agent is None


def test_empty_manifest_still_raises(tmp_path, patched_llm):
    rtm = native.NativeRuntime()
    session = rtm.create_session(
        rt.RuntimeOptions(working_dir=tmp_path, manifest={})
    )
    with pytest.raises(NativeRuntimeConfigurationError):
        session.start()
    assert session.state is rt.RuntimeState.PENDING


# --------------------------------------------------------------------------
# Pure helpers (no monkeypatch needed — they never import lingtai)
# --------------------------------------------------------------------------
def test_llm_config_merge_helper_is_pure(tmp_path):
    options = rt.RuntimeOptions(
        working_dir=tmp_path,
        provider="openai",
        manifest={"llm": {"provider": "anthropic", "model": "m", "api_key": "k"}},
    )
    cfg = native._llm_config_from_options(options)
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "m"
    assert cfg["api_key"] == "k"


# --------------------------------------------------------------------------
# Import purity still holds with the stage-3 manifest translation present
# --------------------------------------------------------------------------
def test_manifest_options_construction_stays_pure():
    code = (
        "import sys\n"
        "import lingtai_sdk\n"
        "from lingtai_sdk import runtime as rt\n"
        "rtm = lingtai_sdk.NativeRuntime()\n"
        "rtm.create_session(rt.RuntimeOptions(working_dir='/tmp/x',"
        " manifest={'llm': {'provider': 'anthropic', 'model': 'claude-opus-4-8',"
        " 'context_window': 500000}, 'max_rpm': 30}))\n"
        "providers = ('anthropic','openai','google.genai',"
        "'google.generativeai','mcp','trafilatura','ddgs')\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "bad += [m for m in sys.modules "
        "if any(m == p or m.startswith(p + '.') for p in providers)]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
