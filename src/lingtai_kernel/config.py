"""Agent configuration — injected at construction, not read from files."""
from __future__ import annotations

from dataclasses import dataclass, field


THINKING_LEVELS = ("low", "medium", "high", "xhigh")


@dataclass
class AgentConfig:
    """Configuration for a BaseAgent instance.

    The host app reads its own config files and passes resolved values here.
    No file-based config reading inside lingtai.
    """
    max_turns: int = 50
    provider: str | None = None  # None = use LLMService's provider
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    retry_timeout: float = 300.0  # LLM call watchdog (seconds). Bumped from 120s — modern thinking models (GLM-5.1, DeepSeek V4 thinking, Anthropic extended-thinking) routinely take 60–180s for high-context turns; 120s spuriously fired on slow-but-successful calls and triggered AED cascades. 300s catches truly-hung connections without false positives on normal responses.
    aed_timeout: float = 360.0   # max seconds in STUCK before ASLEEP
    max_aed_attempts: int = 10   # max AED retry attempts per inbox message turn
    max_rpm: int = 60  # API requests-per-minute cap for this agent's provider; 0 = no gating. Shared across all agents in the same process that use the same (provider, base_url) pair (adapter cache key).
    thinking_budget: int | None = None
    thinking: str = "high"  # reasoning/thinking tier passed to the main persistent LLM session
    data_dir: str | None = None  # for cache files (e.g., model context windows)
    soul_delay: float = 99999.0  # seconds idle before soul whispers; large value (> stamina) = effectively off
    language: str = "en"  # agent language ("en", "zh", "wen"); controls kernel-injected prose
    activeness: str | None = "balanced"  # responsiveness posture: quiet, balanced, or responsive
    stamina: float = 3600.0  # agent stamina in seconds; set at birth, not changeable by the agent
    time_awareness: bool = True  # experimental: False strips LLM-visible timestamps (perception nerf)
    timezone_awareness: bool = True  # when True, now_iso emits OS local time; when False, UTC
    context_limit: int | None = None  # max context tokens; None = use model default
    molt_notice: float = 0.5  # context usage fraction at/above which agent_meta.context.molt suggests considering molt (0.0–1.0)
    molt_pressure: float = 0.7  # context usage fraction at/above which agent_meta.context.molt becomes firm (0.0–1.0)
    molt_urgency: float = 0.9  # context usage fraction at/above which agent_meta.context.molt requires immediate molt (0.0–1.0)
    molt_prompt: str = ""  # optional override for the short molt message inside agent_meta.context.molt
    ensure_ascii: bool = False  # JSON output: False = readable unicode, True = \uXXXX escapes
    insights_interval: int = 0  # turns between auto-insights; 0 = off
    consultation_past_count: int = 0  # K random past-snapshot consultations per fire; default 0 = current-context soul flow only
    soul_voice: str = "inner"  # consultation prompt profile — "inner" (terse, "you are the soul, speak as inner voice"), "observer" (structured stepped-back hook framing), or "custom" (use soul_voice_prompt). One unified prompt per profile; the per-fire cue text differentiates insights (current diary) vs past (future-self diary).
    soul_voice_prompt: str = ""  # custom voice prompt — only used when soul_voice == "custom". Set/cleared by the agent via soul(action="voice", set="custom", prompt="..."). Length-capped at SOUL_VOICE_PROMPT_MAX in soul.py.
    snapshot_interval: float | None = None  # seconds between git snapshots; None = off
