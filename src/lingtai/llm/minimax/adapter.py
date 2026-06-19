from lingtai_kernel.logging import get_logger
from ..anthropic.adapter import AnthropicAdapter

logger = get_logger()


class MiniMaxAdapter(AnthropicAdapter):
    """MiniMax via the Anthropic-compatible endpoint.

    Rate gating (default 120 rpm) is applied automatically by AnthropicAdapter's
    inherited _wrap_with_gate / _gated_call hooks — no per-method overrides
    needed here.
    """

    def __init__(
        self, api_key: str, *, base_url: str | None = None,
        max_rpm: int = 120, timeout_ms: int = 300_000,
    ):
        effective_url = base_url or "https://api.minimax.io/anthropic"
        super().__init__(api_key=api_key, base_url=effective_url, timeout_ms=timeout_ms)
        self._setup_gate(max_rpm)
