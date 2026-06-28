from pathlib import Path
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


def test_default_order_and_batches_cover_the_same_sections_once():
    order = list(SystemPromptManager._DEFAULT_ORDER)
    flat_batches = [section for batch in SystemPromptManager._BATCHES for section in batch]
    assert set(flat_batches) == set(order)
    assert len(flat_batches) == len(set(flat_batches)) == len(order)


def test_meta_guidance_renders_between_procedures_and_comment():
    """`meta_guidance` is resident kernel-runtime guidance and renders after
    `procedures` but before `comment`, so operator/project comments can follow
    the static runtime guidance instead of being separated from procedures."""
    mgr = SystemPromptManager()
    mgr.write_section("procedures", "How to act.", protected=True)
    mgr.write_section("meta_guidance", "Resident runtime guidance.", protected=True)
    mgr.write_section("comment", "Project-specific adaptation.", protected=True)
    mgr.write_section("pad", "Working notes.")
    prompt = mgr.render()
    procedures_pos = prompt.index("How to act.")
    mg_pos = prompt.index("Resident runtime guidance.")
    comment_pos = prompt.index("Project-specific adaptation.")
    pad_pos = prompt.index("Working notes.")
    assert procedures_pos < mg_pos < comment_pos < pad_pos
    assert "## meta_guidance" in prompt
    batches = mgr.render_batches()
    assert "Resident runtime guidance." in batches[0]
    assert "Resident runtime guidance." not in batches[-1]


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


def test_base_prompt_follows_kernel_owned_principle_without_dynamic_injection():
    """Runtime prompt building must not synthesize principle text.

    All kernel-owned principle prose lives in the raw ``principle`` section. When
    wrapper/framework guidance exists, it follows that section rather than being
    injected before it.
    """
    mgr = SystemPromptManager()
    mgr.write_section("principle", "Kernel-owned principle body.", protected=True)
    mgr.write_section("covenant", "Be good.", protected=True)
    prompt = build_system_prompt(
        mgr,
        base_prompt="Framework guidance.",
        language="zh",
        activeness="responsive",
    )

    assert prompt.startswith("Kernel-owned principle body.")
    assert "Agent " + "language:" not in prompt
    assert "智能体语言" not in prompt
    assert "器灵之言" not in prompt
    assert "Agent " + "activeness:" not in prompt
    assert "智能体主动程度" not in prompt
    assert "器灵主动" not in prompt
    assert prompt.index("Kernel-owned principle body.") < prompt.index("Framework guidance.")
    assert prompt.index("Framework guidance.") < prompt.index("Be good.")


def test_batch_form_keeps_principle_then_base_prompt_in_first_batch():
    """The cached-batch path must preserve the same principle/base ordering."""
    mgr = SystemPromptManager()
    mgr.write_section("principle", "Kernel-owned principle body.", protected=True)
    mgr.write_section("covenant", "Be good.", protected=True)

    batches = build_system_prompt_batches(
        mgr,
        base_prompt="Framework guidance.",
        language="wen",
        activeness="quiet",
    )

    assert len(batches) == 2
    first_blocks = batches[0].split("\n\n---\n\n")
    assert first_blocks[0] == "Kernel-owned principle body."
    assert first_blocks[1] == "Framework guidance."
    assert first_blocks[2].startswith("## covenant\nBe good.")
    assert "Agent " + "language:" not in batches[0]
    assert "Agent " + "activeness:" not in batches[0]


def test_base_prompt_renders_first_when_no_principle_without_dynamic_fallback():
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    prompt = build_system_prompt(
        mgr,
        base_prompt="Framework guidance.",
        language="fr",
        activeness="quiet",
    )

    assert prompt.startswith("Framework guidance.")
    assert "Agent " + "language:" not in prompt
    assert "Agent " + "activeness:" not in prompt
    assert "Progressive disclosure principle:" not in prompt
    assert "Token efficiency principle:" not in prompt
    assert prompt.index("Framework guidance.") < prompt.index("Be good.")


def test_packaged_principle_owns_static_progressive_and_token_efficiency_rules():
    mgr = SystemPromptManager()
    principle = Path("src/lingtai/prompts/principle.md").read_text()
    mgr.write_section("principle", principle, protected=True)
    prompt = build_system_prompt(mgr, language="en", activeness="quiet")

    assert "Progressive disclosure principle: each resident prompt layer" in prompt
    assert "`meta_guidance` is immediate runtime guidance" in prompt
    assert "`procedures` is how to act" in prompt
    assert "`substrate` is the working model" in prompt
    assert "Reference manuals are why" in prompt
    assert "Token efficiency principle:" in prompt
    assert "the current session's active context is carried into every provider request" in prompt
    assert "summarize consumed tool results" in prompt
    assert "do not molt automatically" in prompt
    assert "current-session API calls exceed 100" in prompt
    assert "Agent " + "language:" not in prompt
    assert "Agent " + "activeness:" not in prompt


def test_task_boundary_molt_guidance_is_cost_thresholded():
    """Resident and manual guidance should agree that task-boundary molt is costed."""
    from lingtai_kernel._frontmatter import strip_frontmatter

    # Skill-style section/manual files carry developer-facing YAML frontmatter;
    # strip it so the corpus is the rendered body only (not metadata text).
    md_paths = [
        Path("src/lingtai/prompts/principle.md"),
        Path("src/lingtai/prompts/procedures.md"),
        Path("src/lingtai/prompts/substrate.md"),
        Path("src/lingtai/intrinsic_skills/system-manual/reference/procedures-manual/SKILL.md"),
        Path("src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md"),
        Path("src/lingtai/intrinsic_skills/system-manual/reference/summarize-manual/SKILL.md"),
    ]
    parts = [strip_frontmatter(path.read_text()) for path in md_paths]
    # Guidance is now a skill-style Markdown catalog; fold each section body in.
    from lingtai_kernel.meta_block import build_runtime_guidance

    for section in build_runtime_guidance().get("sections", []):
        parts.append(section.get("body", ""))
    corpus = "\n".join(parts)

    assert "molt regardless" + " of context size" not in corpus
    assert "do not molt automatically" in corpus
    assert "api_calls > 100" in corpus
    assert "current-session API calls exceed 100" in corpus
    assert "proactive task-boundary molt" in corpus


def test_batches_byte_identical_to_string():
    """Joining build_system_prompt_batches() with '\\n\\n' (empty batches
    filtered) must reproduce build_system_prompt() byte-for-byte."""
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    mgr.write_section("rules", "No deleting files.", protected=True)
    mgr.write_section("pad", "Working notes.")
    for language in ("en", "zh", "wen", "fr"):
        for activeness in (None, "", "quiet", "balanced", "responsive"):
            full = build_system_prompt(
                mgr,
                base_prompt="Framework guidance.",
                language=language,
                activeness=activeness,
            )
            batches = build_system_prompt_batches(
                mgr,
                base_prompt="Framework guidance.",
                language=language,
                activeness=activeness,
            )
            assert "\n\n".join(seg for seg in batches if seg) == full


def test_batches_byte_identical_when_empty():
    """Even with no sections and no base_prompt, no dynamic fallback text is injected."""
    mgr = SystemPromptManager()
    full = build_system_prompt(mgr, language="zh")
    batches = build_system_prompt_batches(mgr, language="zh")
    assert "\n\n".join(seg for seg in batches if seg) == full
    # Batch shape is preserved: still one string per batch.
    assert len(batches) == 2
