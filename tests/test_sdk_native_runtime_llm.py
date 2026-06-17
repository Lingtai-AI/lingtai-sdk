"""Stage 2 — LLM-service translation / default-factory DX fix.

Stage 1 left the default ``agent_factory`` path calling ``Agent(**kwargs)``
without a ``service``, so a real ``start()`` raised an opaque missing-``service``
``TypeError`` (GLM review nit N2). Stage 2 closes that gap: the default factory
lazily builds an ``LLMService`` from ``RuntimeOptions.provider/model/base_url/
api_key`` and passes it to ``Agent``; a partial/absent LLM config raises a
clear SDK-scoped error *before* any agent is constructed.

These tests run with **no real model, no API key, no network**: the
``LLMService`` builder is monkeypatched with a fake, and the default factory is
exercised through that fake. Import purity is preserved — accessing/constructing
a ``NativeRuntime`` must not import ``lingtai`` or any provider SDK.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import native
from lingtai_sdk import runtime as rt
from lingtai_sdk.errors import LingTaiSDKError, NativeRuntimeConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_SECRET = "sk-do-not-leak-12345"


# --------------------------------------------------------------------------
# Fakes — a fake LLMService builder and a fake Agent that requires a service,
# mirroring the real Agent's contract (service is required) without booting it.
# --------------------------------------------------------------------------
class _FakeService:
    def __init__(self, provider, model, api_key=None, base_url=None, **kwargs):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.kwargs = kwargs


class _ServiceRequiringAgent:
    """Stand-in for the wrapper Agent: service is required (like BaseAgent)."""

    last_kwargs: dict | None = None

    def __init__(self, service=None, **kwargs):
        if service is None:
            raise TypeError("missing required argument: 'service'")
        type(self).last_kwargs = {"service": service, **kwargs}
        self.service = service
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.sent: list[tuple] = []
        self.working_dir = Path(kwargs["working_dir"])

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def send(self, content, sender: str = "user") -> None:
        self.sent.append((content, sender))


@pytest.fixture
def fake_service_builder(monkeypatch):
    """Monkeypatch the SDK's service builder to a network-free fake.

    Records every call so tests can assert provider/model/base_url/api_key
    were translated correctly without ever importing lingtai.llm.
    """
    calls: list[dict] = []

    def _build(options):
        svc = _FakeService(
            provider=options.provider,
            model=options.model,
            api_key=options.api_key,
            base_url=options.base_url,
        )
        calls.append(
            {
                "provider": options.provider,
                "model": options.model,
                "base_url": options.base_url,
                "api_key": options.api_key,
            }
        )
        return svc

    monkeypatch.setattr(native, "_llm_service_from_options", _build)
    return calls


# --------------------------------------------------------------------------
# Default factory: build + pass a service
# --------------------------------------------------------------------------
def test_default_start_with_provider_and_model_builds_and_passes_service(
    tmp_path, monkeypatch, fake_service_builder
):
    monkeypatch.setattr(native, "_default_agent_factory", _ServiceRequiringAgent)
    rtm = native.NativeRuntime()  # default factory path
    session = rtm.create_session(
        rt.RuntimeOptions(
            working_dir=tmp_path,
            provider="anthropic",
            model="claude-opus-4-8",
            base_url="https://example",
            api_key=_SECRET,
        )
    )
    session.start()

    assert session.state is rt.RuntimeState.ACTIVE
    agent = session.agent
    assert isinstance(agent, _ServiceRequiringAgent)
    assert agent.started is True
    # The built service was passed through and carries the translated config.
    assert isinstance(agent.service, _FakeService)
    assert agent.service.provider == "anthropic"
    assert agent.service.model == "claude-opus-4-8"
    assert agent.service.base_url == "https://example"
    assert agent.service.api_key == _SECRET
    # The builder was invoked exactly once with the translated fields.
    assert fake_service_builder == [
        {
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "base_url": "https://example",
            "api_key": _SECRET,
        }
    ]


def test_config_error_exported_from_package_root():
    import lingtai_sdk

    assert lingtai_sdk.NativeRuntimeConfigurationError is NativeRuntimeConfigurationError


def test_default_start_without_provider_or_model_raises_clear_sdk_error(tmp_path):
    rtm = native.NativeRuntime()  # default factory path
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    with pytest.raises(NativeRuntimeConfigurationError) as excinfo:
        session.start()
    # Subclass of the SDK base error, NOT a raw TypeError.
    assert isinstance(excinfo.value, LingTaiSDKError)
    assert not isinstance(excinfo.value, TypeError)
    # No partial ACTIVE state, no agent constructed.
    assert session.state is rt.RuntimeState.PENDING
    assert session.agent is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "anthropic"},  # model missing
        {"model": "claude-opus-4-8"},  # provider missing
        {"provider": "anthropic", "model": ""},  # empty model
        {"provider": "", "model": "claude-opus-4-8"},  # empty provider
    ],
)
def test_default_start_with_partial_llm_config_raises(tmp_path, kwargs):
    rtm = native.NativeRuntime()
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path, **kwargs))
    with pytest.raises(NativeRuntimeConfigurationError):
        session.start()
    assert session.state is rt.RuntimeState.PENDING
    assert session.agent is None


# --------------------------------------------------------------------------
# Secret hygiene: api_key must not leak through public surfaces
# --------------------------------------------------------------------------
def test_api_key_not_leaked_in_error_string(tmp_path):
    rtm = native.NativeRuntime()
    # Provider present but model missing -> error path. Even if api_key is set,
    # it must never appear in the message.
    session = rtm.create_session(
        rt.RuntimeOptions(working_dir=tmp_path, provider="anthropic", api_key=_SECRET)
    )
    with pytest.raises(NativeRuntimeConfigurationError) as excinfo:
        session.start()
    assert _SECRET not in str(excinfo.value)


def test_api_key_not_leaked_in_events_or_deferred(
    tmp_path, monkeypatch, fake_service_builder
):
    monkeypatch.setattr(native, "_default_agent_factory", _ServiceRequiringAgent)
    rtm = native.NativeRuntime()
    session = rtm.create_session(
        rt.RuntimeOptions(
            working_dir=tmp_path,
            provider="anthropic",
            model="claude-opus-4-8",
            api_key=_SECRET,
        )
    )
    # Even before start(), the public deferred surface must not retain api_key.
    assert _SECRET not in repr(session.deferred)
    session.start()
    session.send("hello")

    # No event payload (state/notification/error) carries the secret.
    for event in session.events():
        assert _SECRET not in repr(event.data)
    # Applied LLM fields are recorded WITHOUT api_key.
    applied_llm = session.applied.get("llm", {})
    assert applied_llm.get("provider") == "anthropic"
    assert applied_llm.get("model") == "claude-opus-4-8"
    assert "api_key" not in applied_llm
    # Once applied, the LLM fields are no longer listed as deferred.
    assert session.deferred.get("llm", {}) == {}


# --------------------------------------------------------------------------
# Injected agent_factory bypasses service building (stays network-free)
# --------------------------------------------------------------------------
def test_injected_factory_bypasses_service_building(tmp_path):
    """A custom agent_factory must NOT require provider/model and must not
    invoke the service builder — fakes boot without a real service."""

    class _NoServiceFake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.working_dir = Path(kwargs["working_dir"])
            # service must NOT have been injected by the runtime.
            assert "service" not in kwargs

        def start(self):
            self.started = True

        def stop(self, timeout: float = 5.0):
            pass

        def send(self, content, sender="user"):
            pass

    rtm = native.NativeRuntime(agent_factory=_NoServiceFake)
    # No provider/model at all — would be an error on the default path, but the
    # injected factory bypasses service building entirely.
    session = rtm.create_session(rt.RuntimeOptions(working_dir=tmp_path))
    session.start()
    assert session.state is rt.RuntimeState.ACTIVE
    assert session.agent.started is True
    # Nothing was applied (no service built for injected factory).
    assert session.applied.get("llm", {}) == {}


# --------------------------------------------------------------------------
# Import purity still holds with the stage-2 builder present
# --------------------------------------------------------------------------
def test_constructing_runtime_with_llm_options_stays_pure():
    code = (
        "import sys\n"
        "import lingtai_sdk\n"
        "from lingtai_sdk import runtime as rt\n"
        "rtm = lingtai_sdk.NativeRuntime()\n"
        # Creating a session with LLM options must NOT import lingtai/providers;
        # service building is deferred to start().
        "rtm.create_session(rt.RuntimeOptions(working_dir='/tmp/x',"
        " provider='anthropic', model='claude-opus-4-8'))\n"
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
