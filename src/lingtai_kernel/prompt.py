"""System prompt — section manager + builder.

SystemPromptManager manages named sections of an agent's system prompt.
Sections are rendered in a configurable order. The default order groups
sections by mutation frequency so cache breakpoints can be placed between
batches:

    Batch 1 — immovable after init (ideal cache-read prefix):
        principle (no header) → covenant → tools → substrate → procedures → comment
    Batch 2 — rarely mutated (most stable first):
        rules → brief → skills → knowledge → identity → character → pad

`substrate` sits **right after tools** so it functions as the long-form
companion to the schemas above it: tool schemas carry mechanical
reference (parameter names, types, one-line action descriptions),
substrate carries the operational wisdom (tool tiers, data-flow
topology, life states, channel discipline, attention model — patterns
that span multiple tools). The kernel ships `lingtai/prompts/substrate.md`
as the packaged default (v1); the `Agent` subclass copies it to
`system/substrate.md` on first boot, where the agent (or human) can
edit it freely.

build_system_prompt() assembles language principle + base_prompt + rendered
sections. The language principle is a kernel-injected runtime constraint
derived from the agent's configured language (en/zh/wen) — it always renders
first, before base_prompt, so the agent's working language is the very first
thing in the prompt.
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
        ("rules", "brief", "skills", "knowledge", "identity", "character", "pad"),
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
) -> str:
    """Build the full system prompt from components.

    Order: language principle → base prompt → section batches.
    The language principle is kernel-injected from `language` and always
    comes first. base_prompt is framework-level guidance injected by the
    wrapper package (lingtai).

    This delegates to build_system_prompt_batches() and joins non-empty
    batches with ``\\n\\n``. That matches LLMChatSession.update_system_prompt_batches()
    so cached-batch and single-string callers see byte-identical text.
    """
    return "\n\n".join(
        seg
        for seg in build_system_prompt_batches(
            prompt_manager, base_prompt=base_prompt, language=language
        )
        if seg
    )


def build_system_prompt_batches(
    prompt_manager: SystemPromptManager,
    base_prompt: str = "",
    language: str = "en",
) -> list[str]:
    """Build the system prompt as a list of mutation-frequency batches.

    Same ordering as build_system_prompt, but returned as segments so
    adapters that support per-block prompt caching (e.g. Anthropic's
    `cache_control`) can place breakpoints at batch boundaries. Callers
    that want a string can do ``"\\n\\n".join(filter(None, batches))``
    — and build_system_prompt() does exactly that composition.

    The language principle (and ``base_prompt``, if any) is prepended to
    Batch 1, the cache-stable prefix batch, using the same
    ``\\n\\n---\\n\\n`` prefix separator that the historical single-string
    builder used between framework-level guidance and sections. Empty
    non-prefix batches stay empty so caller indexing remains stable.
    """
    batches = prompt_manager.render_batches()

    prefix = _language_principle(language)
    if base_prompt:
        prefix = f"{prefix}\n\n---\n\n{base_prompt}"

    if batches[0]:
        batches[0] = f"{prefix}\n\n---\n\n{batches[0]}"
    else:
        # Keep the dynamic principle in the first/cache-stable batch even when
        # an otherwise-minimal prompt only has tail sections (or no sections).
        batches[0] = prefix

    return batches
