"""Intrinsic tools available to all agents.

Each intrinsic module exposes:
- get_schema(lang) -> dict: JSON Schema for tool parameters
- get_description(lang) -> str: human-readable description
- handle(agent, args) -> dict: handler function
"""
from . import email, system, psyche, soul

ALL_INTRINSICS = {
    "email": {"module": email},
    "system": {"module": system},
    "psyche": {"module": psyche},
    "soul": {"module": soul},
}
