"""Agent configuration — injected at construction, not read from files."""
from __future__ import annotations

from dataclasses import dataclass, field


THINKING_LEVELS = ("low", "medium", "high", "xhigh")

# Molt context-pressure thresholds are kernel-fixed runtime constants — NOT
# agent-configurable. An agent must not be able to raise its own molt
# thresholds (or defeat them entirely) to avoid molting under pressure, so the
# stage boundaries are owned by the kernel. Legacy ``init.json`` /
# resolved-manifest ``molt_notice`` / ``molt_pressure`` / ``molt_urgency``
# fields are tolerated for backward compatibility (old agents still validate)
# but are ignored — they no longer override these values. See
# ``lingtai/agent.py`` (config reload) and ``lingtai/init_schema.py``
# (MANIFEST_LEGACY_IGNORED).
MOLT_NOTICE_THRESHOLD = 0.60  # legacy name; now the molt RECOVERY TARGET (see below)

# Sustained context-pressure / molt-warning constants (kernel-fixed).
#
# The molt warning surfaced in ``_meta.agent_meta.context.molt`` is no longer an
# immediate ``usage >= 0.60`` trip-wire.  It is a *sustained-pressure* signal:
#
#   * CONTEXT_PRESSURE_RECONSTRUCTION_RATIO (0.75) — a fresh provider round whose
#     context usage is at/above this fraction counts as a "high" round.  This is
#     the same ratio at which the codex adapter's delayed-summarize
#     reconstruction fires (``_CODEX_SUMMARIZE_DELAY_THRESHOLD_RATIO``).
#     Interpretation is INCLUSIVE (``usage >= 0.75``), matching the adapter's
#     release check (``usage >= ratio``).
#   * CONTEXT_PRESSURE_WARN_AFTER_ROUNDS (3) — the resident warning begins on the
#     THIRD consecutive high round; the first two are the window in which
#     summarize/reconstruction is expected to relieve pressure.
#   * CONTEXT_PRESSURE_RECOVERY_TARGET (0.60) — if summarize/reconstruction
#     cannot bring context below this fraction of the window, molt becomes the
#     recommended action.  This is the new meaning of the legacy 0.60 constant:
#     a recovery target, not an immediate trip-wire.
CONTEXT_PRESSURE_RECONSTRUCTION_RATIO = 0.75
CONTEXT_PRESSURE_WARN_AFTER_ROUNDS = 3
CONTEXT_PRESSURE_RECOVERY_TARGET = MOLT_NOTICE_THRESHOLD  # 0.60

MOLT_PRESSURE_THRESHOLD = MOLT_NOTICE_THRESHOLD  # legacy alias; not a separate stage
MOLT_URGENCY_THRESHOLD = MOLT_NOTICE_THRESHOLD  # legacy alias; not a separate stage
DEFAULT_SOUL_DELAY_SECONDS = 999999999.0


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
    max_aed_attempts: int = 3   # max AED retry attempts per inbox message turn
    max_rpm: int = 60  # API requests-per-minute cap for this agent's provider; 0 = no gating. Shared across all agents in the same process that use the same (provider, base_url) pair (adapter cache key).
    thinking_budget: int | None = None
    thinking: str = "high"  # reasoning/thinking tier passed to the main persistent LLM session
    data_dir: str | None = None  # for cache files (e.g., model context windows)
    soul_delay: float = DEFAULT_SOUL_DELAY_SECONDS  # seconds idle before soul whispers; large value (> stamina) = effectively off
    language: str = "en"  # legacy language field retained for compatibility; prompt.py no longer injects prose from it
    activeness: str | None = "balanced"  # legacy responsiveness posture field; prompt.py no longer injects text from it
    stamina: float = 86400.0  # agent stamina in seconds; set at birth, not changeable by the agent
    time_awareness: bool = True  # experimental: False strips LLM-visible timestamps (perception nerf)
    timezone_awareness: bool = True  # when True, now_iso emits OS local time; when False, UTC
    context_limit: int | None = None  # max context tokens; None = use model default
    # Legacy molt-threshold fields, retained ONLY for backward compatibility
    # (old AgentConfig constructions / serialized state still set them). They are
    # NOT the active warning threshold and are no longer read by the warning
    # path: the sustained-pressure warning (meta_block.build_molt_context) is
    # driven by the SessionManager streak and the kernel constants
    # CONTEXT_PRESSURE_* (see top of this module), not by these fields. Legacy
    # init.json molt_notice/molt_pressure/molt_urgency values remain ignored.
    # The 0.60 default here now corresponds to the molt RECOVERY TARGET
    # (CONTEXT_PRESSURE_RECOVERY_TARGET), not an immediate trip-wire.
    molt_notice: float = MOLT_NOTICE_THRESHOLD  # legacy/compat only; == recovery target (0.60), not a trip-wire
    molt_pressure: float = MOLT_PRESSURE_THRESHOLD  # legacy alias; unused by the warning path
    molt_urgency: float = MOLT_URGENCY_THRESHOLD  # legacy alias; unused by the warning path
    ensure_ascii: bool = False  # JSON output: False = readable unicode, True = \uXXXX escapes
    insights_interval: int = 0  # turns between auto-insights; 0 = off
    consultation_past_count: int = 0  # K random past-snapshot consultations per fire; default 0 = current-context soul flow only
    soul_voice: str = "inner"  # consultation prompt profile — "inner" (terse, "you are the soul, speak as inner voice"), "observer" (structured stepped-back hook framing), or "custom" (use soul_voice_prompt). One unified prompt per profile; the per-fire cue text differentiates insights (current diary) vs past (future-self diary).
    soul_voice_prompt: str = ""  # custom voice prompt — only used when soul_voice == "custom". Set/cleared by the agent via soul(action="voice", set="custom", prompt="..."). Length-capped at SOUL_VOICE_PROMPT_MAX in soul.py.
    snapshot_interval: float | None = None  # seconds between git snapshots; None = off
