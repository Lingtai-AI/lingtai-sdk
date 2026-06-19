"""End-to-end plumbing test: init.json max_rpm reaches the adapter's gate.

Verifies:
1. AgentConfig has the field with default 0 (no gating).
2. init_schema accepts max_rpm without warning.
3. cli._build_agent passes max_rpm into LLMService.provider_defaults.
4. LLMService threads max_rpm into the adapter via factory kwargs.
5. Every adapter, after construction, exposes a working _gate when max_rpm > 0.
6. _wrap_with_gate wraps sessions when a gate is configured.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig
from lingtai.llm.service import LLMService
from lingtai.llm.base import LLMAdapter, _GatedSession
from lingtai.llm.api_gate import APICallGate


def test_agent_config_default_is_60():
    """Default max_rpm = 60 — conservative cap that fixes the network
    cascade scenario (multiple agents simultaneously hitting one provider)
    while being well under any paid-tier provider's actual limit. Solo
    agents on high-tier providers can bump it via init.json."""
    assert AgentConfig().max_rpm == 60


def test_agent_config_overridable():
    assert AgentConfig(max_rpm=120).max_rpm == 120
    assert AgentConfig(max_rpm=0).max_rpm == 0  # 0 disables gating


def test_llm_service_threads_max_rpm_via_provider_defaults():
    # Build a service with provider_defaults set the way cli.py / agent.py do
    svc = LLMService(
        provider="openai",
        model="gpt-4",
        api_key="sk-test",
        provider_defaults={"openai": {"max_rpm": 30}},
    )
    adapter = svc.get_adapter("openai")
    # OpenAIAdapter._setup_gate must have created a gate
    assert adapter._gate is not None
    assert isinstance(adapter._gate, APICallGate)
    adapter._gate.shutdown()


def test_llm_service_no_gate_when_max_rpm_zero():
    svc = LLMService(
        provider="openai",
        model="gpt-4",
        api_key="sk-test",
        # No provider_defaults: defaults.get("max_rpm", 0) returns 0
    )
    adapter = svc.get_adapter("openai")
    assert adapter._gate is None


def test_wrap_with_gate_returns_proxy_when_gate_present():
    class _StubAdapter(LLMAdapter):
        def create_chat(self, *a, **kw): pass
        def generate(self, *a, **kw): pass
        def make_tool_result_message(self, *a, **kw): pass
        def is_quota_error(self, exc): return False

    a = _StubAdapter()
    a._setup_gate(60)
    inner = MagicMock(spec=["interface", "send", "send_stream"])
    wrapped = a._wrap_with_gate(inner)
    assert isinstance(wrapped, _GatedSession)
    a._gate.shutdown()


def test_wrap_with_gate_returns_inner_when_no_gate():
    class _StubAdapter(LLMAdapter):
        def create_chat(self, *a, **kw): pass
        def generate(self, *a, **kw): pass
        def make_tool_result_message(self, *a, **kw): pass
        def is_quota_error(self, exc): return False

    a = _StubAdapter()
    inner = MagicMock(spec=["interface"])
    wrapped = a._wrap_with_gate(inner)
    assert wrapped is inner


def test_gated_session_routes_send_through_gate():
    class _StubAdapter(LLMAdapter):
        def create_chat(self, *a, **kw): pass
        def generate(self, *a, **kw): pass
        def make_tool_result_message(self, *a, **kw): pass
        def is_quota_error(self, exc): return False

    a = _StubAdapter()
    a._setup_gate(60)
    inner = MagicMock()
    inner.send.return_value = "response"
    wrapped = a._wrap_with_gate(inner)
    result = wrapped.send("hi")
    assert result == "response"
    inner.send.assert_called_once_with("hi")
    a._gate.shutdown()


def test_gated_session_attribute_passthrough():
    """Read-only attribute access falls through to inner."""
    class _StubAdapter(LLMAdapter):
        def create_chat(self, *a, **kw): pass
        def generate(self, *a, **kw): pass
        def make_tool_result_message(self, *a, **kw): pass
        def is_quota_error(self, exc): return False

    a = _StubAdapter()
    a._setup_gate(60)
    inner = MagicMock()
    inner.session_id = "sess-123"
    inner.total_usage.return_value = {"input_tokens": 100}
    wrapped = a._wrap_with_gate(inner)
    assert wrapped.session_id == "sess-123"  # via __getattr__
    assert wrapped.total_usage()["input_tokens"] == 100  # method passthrough
    a._gate.shutdown()
