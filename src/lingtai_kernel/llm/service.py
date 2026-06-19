"""LLMService ABC — protocol for LLM access.

The kernel depends only on this interface. Concrete implementations
(adapter-based, local model, mock) live outside the kernel.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ChatSession, FunctionSchema, LLMResponse
    from .interface import ChatInterface, ToolResultBlock


class LLMService(ABC):
    """Protocol for LLM access. Kernel depends only on this."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Default model identifier."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Provider name."""

    @abstractmethod
    def create_session(
        self,
        system_prompt: str,
        tools: "list[FunctionSchema] | None" = None,
        *,
        model: str | None = None,
        thinking: str = "default",
        agent_type: str = "",
        tracked: bool = True,
        interaction_id: str | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        provider: str | None = None,
        interface: "ChatInterface | None" = None,
    ) -> "ChatSession":
        """Start a new multi-turn conversation."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
        provider: str | None = None,
    ) -> "LLMResponse":
        """Single-turn generation."""

    @abstractmethod
    def make_tool_result(
        self,
        tool_name: str,
        result: dict,
        *,
        tool_call_id: str | None = None,
        provider: str | None = None,
    ) -> "ToolResultBlock":
        """Build a canonical ToolResultBlock."""
