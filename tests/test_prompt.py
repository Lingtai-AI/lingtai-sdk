from lingtai_kernel.prompt import build_system_prompt
from lingtai_kernel.prompt import build_system_prompt_batches
from lingtai_kernel.prompt import SystemPromptManager


def test_build_system_prompt_minimal():
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr)
    assert isinstance(prompt, str)


def test_build_system_prompt_with_sections():
    mgr = SystemPromptManager()
    mgr.write_section("role", "You are a test agent")
    mgr.write_section("pad", "Remember: user likes concise")
    prompt = build_system_prompt(mgr)
    assert "You are a test agent" in prompt
    assert "Remember: user likes concise" in prompt


def test_rules_renders_after_covenant_and_tools():
    """Section order is grouped by mutation frequency for cache stability:
    Batch 1 (immovable, prefix-cacheable) — principle, covenant, tools, substrate, ...
    Batch 2 (rarely mutated)              — rules, brief, skills, library, ...

    So both ``covenant`` and ``tools`` precede ``rules`` in the rendered
    prompt, since adjusting rules at runtime should invalidate as little
    of the cached prefix as possible."""
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("rules", "No deleting files.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    prompt = mgr.render()
    cov_pos = prompt.index("Be good.")
    rules_pos = prompt.index("No deleting files.")
    tools_pos = prompt.index("Run commands.")
    assert cov_pos < tools_pos < rules_pos


def test_rules_section_absent_when_empty():
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    prompt = mgr.render()
    assert "## rules" not in prompt


def test_character_renders_after_identity_before_pad():
    """The self-authored `character` section (system/lingtai.md) sits in
    Batch 2 right after the mechanical `identity` (name/nickname/manifest)
    and before `pad`. It is a first-class section, distinct from both
    `covenant` (the operator contract, Batch 1 before tools) and the
    mechanical `identity`."""
    mgr = SystemPromptManager()
    mgr.write_section("identity", "name: alice", protected=True)
    mgr.write_section("character", "I am a meticulous archivist.", protected=True)
    mgr.write_section("pad", "Working notes.")
    prompt = mgr.render()
    identity_pos = prompt.index("name: alice")
    character_pos = prompt.index("I am a meticulous archivist.")
    pad_pos = prompt.index("Working notes.")
    assert identity_pos < character_pos < pad_pos


def test_character_section_separate_from_covenant():
    """`covenant` lives in immovable Batch 1 (before tools); `character`
    lives in Batch 2 (after the mechanical `identity`). Asserting character
    renders *after* identity proves it is a registered Batch-2 section rather
    than spilling into the unordered bucket that precedes Batch 2."""
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "The operator contract.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    mgr.write_section("identity", "name: alice", protected=True)
    mgr.write_section("character", "I am a meticulous archivist.", protected=True)
    prompt = mgr.render()
    cov_pos = prompt.index("The operator contract.")
    tools_pos = prompt.index("Run commands.")
    identity_pos = prompt.index("name: alice")
    char_pos = prompt.index("I am a meticulous archivist.")
    # covenant before tools (Batch 1); character after identity (Batch 2).
    assert cov_pos < tools_pos
    assert identity_pos < char_pos


def test_language_principle_en_renders_first():
    """The kernel-injected language principle is the very first thing in
    the prompt — before base_prompt and all sections."""
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    prompt = build_system_prompt(mgr, base_prompt="Framework guidance.", language="en")
    assert prompt.startswith("Agent language: English.")
    assert prompt.index("Agent language: English.") < prompt.index("Framework guidance.")
    assert prompt.index("Framework guidance.") < prompt.index("Be good.")


def test_language_principle_zh():
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr, language="zh")
    assert prompt.startswith("智能体语言：简体中文。")


def test_language_principle_wen():
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr, language="wen")
    assert prompt.startswith("器灵之言：文言。")


def test_language_principle_unknown_falls_back_to_english_with_code():
    """Unknown language codes get English wording carrying the raw code."""
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr, language="fr")
    assert prompt.startswith("Agent language: fr.")
    assert "explicit instruction" in prompt


def test_batches_byte_identical_to_string():
    """Joining build_system_prompt_batches() with '\\n\\n' (empty batches
    filtered) must reproduce build_system_prompt() byte-for-byte."""
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    mgr.write_section("rules", "No deleting files.", protected=True)
    mgr.write_section("pad", "Working notes.")
    for language in ("en", "zh", "wen", "fr"):
        full = build_system_prompt(mgr, base_prompt="Framework guidance.", language=language)
        batches = build_system_prompt_batches(
            mgr, base_prompt="Framework guidance.", language=language
        )
        assert "\n\n".join(seg for seg in batches if seg) == full


def test_batches_byte_identical_when_empty():
    """Even with no sections and no base_prompt, the joined batches equal
    the string form (the language principle is the only content)."""
    mgr = SystemPromptManager()
    full = build_system_prompt(mgr, language="zh")
    batches = build_system_prompt_batches(mgr, language="zh")
    assert "\n\n".join(seg for seg in batches if seg) == full
    # Batch shape is preserved: still one string per batch.
    assert len(batches) == 2
