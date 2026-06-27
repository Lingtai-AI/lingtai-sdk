"""System prompt — section manager + builder.

SystemPromptManager manages named sections of an agent's system prompt.
Sections are rendered in a configurable order. The default order groups
sections by mutation frequency so cache breakpoints can be placed between
batches:

    Batch 1 — immovable after init (ideal cache-read prefix):
        dynamic principle preface + principle (no header) → covenant → tools → substrate → procedures → comment
    Batch 2 — rarely mutated (most stable first):
        rules → brief → skills → knowledge → identity → character → pad →
        meta_guidance (always the final, resident kernel-runtime-guidance section)

`substrate` sits **right after tools** so it functions as the long-form
companion to the schemas above it: tool schemas carry mechanical
reference (parameter names, types, one-line action descriptions),
substrate carries the operational wisdom (tool tiers, data-flow
topology, life states, channel discipline, attention model — patterns
that span multiple tools). The kernel ships `lingtai/prompts/substrate.md`
as the packaged default (v1); the `Agent` subclass copies it to
`system/substrate.md` on first boot, where the agent (or human) can
edit it freely.

build_system_prompt() assembles the kernel-owned principle layer +
base_prompt + rendered sections. The principle layer includes the dynamic
runtime preface (language, activeness, progressive-disclosure hook, and
token-efficiency action rules) immediately adjacent to the packaged raw
`principle` section, then renders wrapper-level base_prompt material after
that combined principle block.
"""
from __future__ import annotations

from typing import Optional

# Kernel-injected dynamic runtime principle, keyed by language code. Each
# entry is written in the target language itself. Unknown codes fall back
# to English wording with the raw code embedded (see _language_principle).
_LANGUAGE_PRINCIPLES: dict[str, str] = {
    "en": (
        "Agent language: English. Write all ordinary prose and human-facing "
        "replies in English, unless an explicit instruction from a human or "
        "the task at hand asks otherwise. Do not switch languages just "
        "because nearby prompt or source material is in another language. "
        "Quoting source text, and using code or identifiers as-is, is fine."
    ),
    "zh": (
        "智能体语言：简体中文。所有日常行文与面向人类的回复一律使用简体中文，"
        "除非人类或当前任务明确要求使用其他语言。不要仅因周围的提示词或资料"
        "是其他语言就切换语言。引用原文、保留代码与标识符的原样不受此限。"
    ),
    "wen": (
        "器灵之言：文言。凡寻常行文、对人之复，皆以文言书之；"
        "非人或当下之务明命改易，毋得擅换。毋因旁近提示、材料为他语而随之。"
        "引录原文、代码、名号，仍其旧貌可也。"
    ),
}


def _language_principle(language: str) -> str:
    """Return the dynamic runtime language principle for `language`.

    Known codes (en/zh/wen) get a principle written in the target language.
    Unknown codes fall back to English wording carrying the raw code.
    """
    principle = _LANGUAGE_PRINCIPLES.get(language)
    if principle is not None:
        return principle
    return (
        f"Agent language: {language}. Write all ordinary prose and "
        f"human-facing replies in that language ({language}), unless an "
        "explicit instruction from a human or the task at hand asks "
        "otherwise. Do not switch languages just because nearby prompt or "
        "source material is in another language. Quoting source text, and "
        "using code or identifiers as-is, is fine."
    )


# Kernel-injected dynamic runtime principle for agent activeness /
# responsiveness posture. Values are intentionally small and stable because
# they are operator-facing manifest knobs. Unknown values fall back to English
# wording that names the raw value rather than silently choosing a posture.
_ACTIVENESS_PRINCIPLES: dict[str, dict[str, str]] = {
    "quiet": {
        "en": (
            "Agent activeness: quiet. Be swift in action and sparing in words: "
            "do the useful work first, avoid unnecessary chatter, and report "
            "only decisions, blockers, errors, deliverables, or facts the human "
            "needs to proceed."
        ),
        "zh": (
            "智能体主动程度：克制。君子敏于行而讷于言：先做有用之事，少作无谓寒暄；"
            "仅在有决策、阻碍、错误、交付物或人类推进所需事实时回报。"
        ),
        "wen": (
            "器灵主动之度：克制。君子敏于行而讷于言：先行其事，慎其辞令；"
            "非有决断、阻碍、差误、交付，或人所必需之事实，毋多言也。"
        ),
    },
    "balanced": {
        "en": (
            "Agent activeness: balanced. Act without delay, acknowledge human "
            "instructions promptly, give progress for long work or blockers, "
            "and keep routine reports concise and evidence-based."
        ),
        "zh": (
            "智能体主动程度：均衡。能做则先做；收到人类指令要及时确认；长任务或遇阻要交代进展；"
            "日常回报保持简洁，并以事实为据。"
        ),
        "wen": (
            "器灵主动之度：中和。可行则亟行；受人之命则速应；事久或有阻则告其进；"
            "凡常报，务简而有据。"
        ),
    },
    "responsive": {
        "en": (
            "Agent activeness: responsive. Make every human-facing exchange feel "
            "answered: acknowledge each request, state what you will do next, "
            "surface errors and uncertainty plainly, and close the loop with "
            "specific facts, paths, or outcomes."
        ),
        "zh": (
            "智能体主动程度：高响应。句句有回应，事事有交代：每个人类请求都要确认；说明下一步；"
            "如有错误或不确定性要明说；结束时以具体事实、路径或结果闭环。"
        ),
        "wen": (
            "器灵主动之度：勤应。句句有应，事事有交代：凡人有所请，必先承之；明示次第；"
            "有差误疑滞则直陈；事毕以实、以径、以果结之。"
        ),
    },
}

_ACTIVENESS_ALIASES: dict[str, str] = {
    "low": "quiet",
    "restrained": "quiet",
    "concise": "quiet",
    "medium": "balanced",
    "normal": "balanced",
    "default": "balanced",
    "high": "responsive",
    "active": "responsive",
    "verbose": "responsive",
}


# Localized progressive-disclosure hook for the dynamic principle preface. The
# detailed layer contract is kernel-owned in the packaged raw `principle`
# section (`prompts/principle.md`); build_system_prompt_batches() renders this
# preface adjacent to that raw section so they form one principle layer.
_PROGRESSIVE_DISCLOSURE_PRINCIPLES: dict[str, str] = {
    "en": (
        "Progressive disclosure principle: each prompt layer has one job and "
        "points to the next. The kernel-owned `principle` section defines the "
        "role of meta_guidance, procedures, substrate, and references. Keep each "
        "rule in its owning layer; other layers should route instead of copying."
    ),
    "zh": (
        "渐进式披露原则：每个 prompt 层各司一职，并指向下一层。kernel-owned `principle` "
        "section 定义 meta_guidance、procedures、substrate、references 的职责。"
        "每条规则应留在拥有它的层里；其他层只指路，不复制。"
    ),
    "wen": (
        "渐披之则：诸 prompt 层各有其职，递相指路。kernel-owned `principle` section "
        "定 meta_guidance、procedures、substrate、references 之职。"
        "凡规则当居其主层；余层但指路，勿复写。"
    ),
}


# Kernel-injected token-efficiency operating principle. Detailed provider/cache
# rationale stays in manuals; the resident principle states when to act.
_TOKEN_EFFICIENCY_PRINCIPLES: dict[str, str] = {
    "en": (
        "Token efficiency principle: the current session's active context is "
        "carried into every provider request. When continuing, summarize consumed "
        "tool results whose raw text is no longer needed. At completed task "
        "boundaries, after reporting and durable stores are tended, do not molt "
        "automatically; default to a proactive task-boundary molt only once "
        "current-session API calls exceed 100, or when context pressure, explicit "
        "human request, or conversation confusion makes the molt worth its cost. "
        "Use daemons to keep bulky or noisy work out of the main context."
    ),
    "zh": (
        "Token efficiency 原则：当前会话的活跃上下文会随每次 provider 请求一起发送。"
        "继续同一任务时，已消化且不再需要原文的工具结果要摘要。任务完成并完成必要汇报与"
        "durable stores 后，不要自动凝蜕；默认只在当前 session API calls 超过 100 时才做"
        "proactive task-boundary molt，或在上下文压力、人类显式要求、对话已混乱且值得支付 molt 成本时才做。"
        "嘈杂或大批量工作应先用 daemon 隔离。"
    ),
    "wen": (
        "省筹之则：当会话之活跃上下文随每次 provider 请求而行。续作时，凡工具结果已消化且"
        "无须原文者，当摘要。事毕、已报且诸藏已理，毋自动凝蜕；默认须当会话 API calls 逾 100，"
        "方行 proactive task-boundary molt；或遇上下文之压、人明令重启、对话混乱而其费可偿，乃行之。"
        "繁重嘈杂之工，先遣 daemon 以隔主上下文。"
    ),
}


def _progressive_disclosure_principle(language: str) -> str:
    """Return the resident-vs-reference layering principle for `language`."""
    return _PROGRESSIVE_DISCLOSURE_PRINCIPLES.get(language) or _PROGRESSIVE_DISCLOSURE_PRINCIPLES["en"]


def _token_efficiency_principle(language: str) -> str:
    """Return the token-efficiency action principle for `language`."""
    return _TOKEN_EFFICIENCY_PRINCIPLES.get(language) or _TOKEN_EFFICIENCY_PRINCIPLES["en"]



def _activeness_principle(activeness: str | None, language: str) -> str:
    """Return the dynamic runtime activeness principle.

    ``None`` / empty string uses the operator default (balanced). Known levels
    are quiet, balanced, and responsive, with a few permissive
    aliases for operator ergonomics. Unknown values render an explicit English
    warning carrying the raw value instead of silently picking a level.
    """
    raw = "balanced" if activeness is None else str(activeness).strip()
    if not raw:
        raw = "balanced"
    key = raw.lower().replace("_", "-")
    key = _ACTIVENESS_ALIASES.get(key, key)
    entries = _ACTIVENESS_PRINCIPLES.get(key)
    if entries is None:
        return (
            f"Agent activeness: {raw}. Unknown activeness level; known levels "
            "are quiet, balanced, and responsive. Follow any explicit human or "
            "operator instruction about responsiveness, and otherwise use "
            "balanced responsiveness."
        )
    return entries.get(language) or entries["en"]


class SystemPromptManager:
    """Manages named sections of an agent's system prompt.

    Sections can be marked as protected (host-written, not overwritable by the LLM)
    or unprotected (LLM-writable at runtime).

    Render order is configurable via set_order(). Sections not in the order
    list are rendered between the ordered sections and the tail. The last
    name in the order list is always rendered last (typically 'context').
    """

    # Default render order — grouped by mutation frequency. Sections in
    # the same batch are adjacent so batch-boundary cache breakpoints in
    # the adapter can cover the whole stable prefix. Within each batch,
    # sections are ordered most-stable-first so later mutations invalidate
    # as little prior content as possible.
    #   Batch 1 (immovable):         principle, covenant, tools, substrate, procedures, comment
    #   Batch 2 (rarely-mutated):    rules, brief, skills, knowledge, identity, character, pad
    # First entry (principle) is rendered without ## header (raw text).
    # `identity` is the mechanical section (name/nickname/manifest, written by
    # BaseAgent); `character` is the agent's self-authored identity from
    # system/lingtai.md (灵台) — distinct sections, character right after identity.
    _DEFAULT_ORDER = [
        # Batch 1 — immovable
        "principle",
        "covenant",
        "tools",
        "substrate",
        "procedures",
        "comment",
        # Batch 2 — rarely mutated (most stable first)
        "rules",
        "brief",
        "skills",
        "knowledge",
        "identity",
        "character",
        "pad",
        # Resident kernel runtime guidance — always last so it is the final
        # system-prompt section (see meta_block.build_meta_guidance).
        "meta_guidance",
    ]

    def __init__(self) -> None:
        self._sections: dict[str, dict] = {}
        self._order: list[str] = list(self._DEFAULT_ORDER)
        # First entry in order is rendered without ## header (raw text)
        self._raw_sections: set[str] = {"principle"}

    def write_section(self, name: str, content: str, protected: bool = False) -> None:
        """Write a section (host API — bypasses protection checks)."""
        self._sections[name] = {"content": content, "protected": protected}

    def read_section(self, name: str) -> Optional[str]:
        """Read a section's content, or None if not found."""
        entry = self._sections.get(name)
        return entry["content"] if entry else None

    def delete_section(self, name: str) -> bool:
        """Delete a section. Returns True if it existed."""
        return self._sections.pop(name, None) is not None

    def list_sections(self) -> list[dict]:
        """Return a list of section metadata dicts."""
        return [
            {"name": name, "protected": entry["protected"], "length": len(entry["content"])}
            for name, entry in self._sections.items()
        ]

    def set_order(self, names: list[str]) -> None:
        """Set the render order. Last name is always rendered last."""
        self._order = list(names)

    def set_raw(self, name: str) -> None:
        """Mark a section as raw — rendered without ## header."""
        self._raw_sections.add(name)

    # Cache-breakpoint batches — must cover the same names as _DEFAULT_ORDER.
    # Each tuple is one batch; batch boundaries are where the adapter can
    # place cache_control markers. Sections not listed here fall into the
    # "unordered" bucket rendered just before the tail batch.
    _BATCHES: tuple[tuple[str, ...], ...] = (
        ("principle", "covenant", "tools", "substrate", "procedures", "comment"),
        (
            "rules", "brief", "skills", "knowledge", "identity", "character",
            "pad", "meta_guidance",
        ),
    )

    def render(self) -> str:
        """Render all sections into a single string following the configured order.

        See render_batches() for the batched form used for cache breakpoints.
        """
        return "\n\n".join(seg for seg in self.render_batches() if seg)

    def render_batches(self) -> list[str]:
        """Render sections grouped into cache-breakpoint batches.

        Returns one string per batch in `_BATCHES`, in order. Empty batches
        are returned as empty strings (not skipped) so caller indexing is
        stable. Unordered sections (not in any batch) are appended to the
        penultimate batch — never to the final tail batch, because cache
        breakpoints land between batches and the tail must stay the most
        volatile chunk.
        """
        batches: list[list[str]] = [[] for _ in self._BATCHES]

        def _render_entry(name: str) -> str | None:
            entry = self._sections.get(name)
            if not entry:
                return None
            if name in self._raw_sections:
                return entry["content"]
            return f"## {name}\n{entry['content']}"

        # Fill each batch with its named sections (in batch order).
        for i, batch_names in enumerate(self._BATCHES):
            for name in batch_names:
                rendered = _render_entry(name)
                if rendered:
                    batches[i].append(rendered)

        # Unordered sections → penultimate batch (or first batch if only one).
        all_batched = {n for batch in self._BATCHES for n in batch}
        unordered_target = max(0, len(batches) - 2)
        for name, entry in self._sections.items():
            if name in all_batched:
                continue
            if name in self._raw_sections:
                batches[unordered_target].append(entry["content"])
            else:
                batches[unordered_target].append(f"## {name}\n{entry['content']}")

        return ["\n\n".join(b) for b in batches]


def build_system_prompt(
    prompt_manager: SystemPromptManager,
    base_prompt: str = "",
    language: str = "en",
    activeness: str | None = "balanced",
) -> str:
    """Build the full system prompt from components.

    Order: dynamic principle preface → packaged principle section → base
    prompt → remaining section batches. The dynamic preface is kernel-owned
    principle material (language, activeness, progressive-disclosure hook,
    token-efficiency) and renders adjacent to the raw `principle` section.
    base_prompt is framework-level guidance injected by the wrapper package
    (lingtai), so it follows the combined principle layer.

    This delegates to build_system_prompt_batches() and joins non-empty
    batches with ``\\n\\n``. That matches LLMChatSession.update_system_prompt_batches()
    so cached-batch and single-string callers see byte-identical text.
    """
    return "\n\n".join(
        seg
        for seg in build_system_prompt_batches(
            prompt_manager,
            base_prompt=base_prompt,
            language=language,
            activeness=activeness,
        )
        if seg
    )


def build_system_prompt_batches(
    prompt_manager: SystemPromptManager,
    base_prompt: str = "",
    language: str = "en",
    activeness: str | None = "balanced",
) -> list[str]:
    """Build the system prompt as a list of mutation-frequency batches.

    Same ordering as build_system_prompt, but returned as segments so
    adapters that support per-block prompt caching (e.g. Anthropic's
    `cache_control`) can place breakpoints at batch boundaries. Callers
    that want a string can do ``"\\n\\n".join(filter(None, batches))``
    — and build_system_prompt() does exactly that composition.

    The dynamic principle preface (language, activeness, progressive-disclosure
    hook, token efficiency) is folded into the first/cache-stable batch adjacent
    to the raw `principle` section. ``base_prompt`` follows that combined
    principle layer, then the remaining Batch-1 sections. Separators stay the
    historical ``\n\n---\n\n`` between major blocks. Empty non-prefix batches
    stay empty so caller indexing remains stable.
    """
    batches = prompt_manager.render_batches()

    prefix_parts = [_language_principle(language)]
    activeness_principle = _activeness_principle(activeness, language)
    if activeness_principle:
        prefix_parts.append(activeness_principle)
    prefix_parts.append(_progressive_disclosure_principle(language))
    prefix_parts.append(_token_efficiency_principle(language))
    dynamic_principle_preface = "\n\n".join(prefix_parts)

    first_batch = batches[0]
    principle = prompt_manager.read_section("principle")
    if principle and first_batch.startswith(principle):
        # `principle` is now kernel-owned, so the dynamic runtime preface is
        # part of that same principle layer. Keep them adjacent and put any
        # wrapper-level base_prompt after the combined principle block rather
        # than between the preface and packaged principle body.
        remaining_first_batch = first_batch[len(principle):]
        if remaining_first_batch.startswith("\n\n"):
            remaining_first_batch = remaining_first_batch[2:]
        blocks = [f"{dynamic_principle_preface}\n\n{principle}"]
        if base_prompt:
            blocks.append(base_prompt)
        if remaining_first_batch:
            blocks.append(remaining_first_batch)
        batches[0] = "\n\n---\n\n".join(blocks)
    else:
        # Minimal/test managers may not have a packaged principle section. Keep
        # the dynamic principle in the first/cache-stable batch, preserving the
        # historical order for that degenerate case.
        blocks = [dynamic_principle_preface]
        if base_prompt:
            blocks.append(base_prompt)
        if first_batch:
            blocks.append(first_batch)
        batches[0] = "\n\n---\n\n".join(blocks)

    return batches
