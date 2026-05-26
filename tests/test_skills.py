"""Tests for the renamed skills capability."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from lingtai.agent import Agent


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _mk_agent(tmp_path: Path, skills_cfg: dict | None = None):
    """Create an agent with the skills capability, optionally passing kwargs."""
    caps = {"skills": skills_cfg or {}}
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=caps,
    )
    return agent, workdir


def _write_skill(folder: Path, name: str, desc: str = "test skill"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody of {name}.\n"
    )


# ---------------------------------------------------------------------------
# Structure & setup
# ---------------------------------------------------------------------------


def test_skills_setup_creates_per_agent_directories(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert (workdir / ".library").is_dir()
        assert (workdir / ".library" / "intrinsic").is_dir()
        assert (workdir / ".library" / "intrinsic" / "capabilities").is_dir()
        # Note: intrinsic/addons/ no longer created since lingtai.addons was
        # removed in v0.7.3 — addons are now MCP servers (curated catalog
        # decompressed into mcp_registry.jsonl by the `mcp` capability).
        assert (workdir / ".library" / "custom").is_dir()
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_hard_copies_intrinsics(tmp_path):
    # The Agent initializer installs each loaded capability's manual/ bundle
    # into intrinsic/capabilities/<cap>/. The skills capability documents
    # itself like every other capability.
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        )
        assert skill_md.is_file()
        assert "name: skills-manual" in skill_md.read_text()
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_hard_copies_standalone_intrinsic_skills(tmp_path):
    # Standalone always-included skills live in lingtai.intrinsic_skills and are
    # copied next to capability manuals under .library/intrinsic/capabilities/.
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "file-manual"
            / "SKILL.md"
        )
        assert skill_md.is_file()
        body = skill_md.read_text(encoding="utf-8")
        assert "name: file-manual" in body
        assert "encoding='gbk'" in body
        assert "iconv -f gbk -t utf-8" in body
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_overwrites_stale_intrinsic(tmp_path):
    # The Agent initializer wipes-and-rewrites intrinsic/ on construction.
    # A stale entry from a previous kernel version must be replaced.
    workdir = tmp_path / "agent"
    stale = (
        workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("---\nname: skills-manual\ndescription: STALE\n---\n")

    # Also leave a stale top-level dir to confirm wipe-and-rewrite scrubs old layouts.
    old_layout = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
    old_layout.parent.mkdir(parents=True, exist_ok=True)
    old_layout.write_text("---\nname: skill-for-skill\ndescription: ANCIENT\n---\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        body = stale.read_text()
        assert "STALE" not in body
        assert "The Skills Capability" in body or "skills-manual" in body
        # Old layout scrubbed.
        assert not old_layout.exists()
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_leaves_custom_untouched(tmp_path):
    workdir = tmp_path / "agent"
    user_skill = workdir / ".library" / "custom" / "my-tool" / "SKILL.md"
    user_skill.parent.mkdir(parents=True, exist_ok=True)
    user_skill.write_text("---\nname: my-tool\ndescription: Mine\n---\nUser content.\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        assert user_skill.read_text() == "---\nname: my-tool\ndescription: Mine\n---\nUser content.\n"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_skills_scans_absolute_path(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-skill", "shared-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["paths"][str(extra)]["skills"] == 1
        assert result["catalog_size"] >= 2  # skills-manual + shared-skill
    finally:
        agent.stop(timeout=1.0)


def test_skills_resolves_relative_path_from_working_dir(tmp_path):
    # Build a network-root layout: tmp_path is the network root.
    # The agent lives at tmp_path/agent, and .library_shared sits at tmp_path/.library_shared.
    shared = tmp_path / ".library_shared"
    _write_skill(shared / "net-skill", "net-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["../.library_shared"]})
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["paths"]["../.library_shared"]["exists"] is True
        assert result["paths"]["../.library_shared"]["skills"] == 1
    finally:
        agent.stop(timeout=1.0)


def test_skills_expands_tilde(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    utils = fake_home / "my-utils"
    _write_skill(utils / "util-skill", "util-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["~/my-utils"]})
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["paths"]["~/my-utils"]["exists"] is True
    finally:
        agent.stop(timeout=1.0)


def test_skills_reports_missing_path_as_not_existing(tmp_path):
    agent, _ = _mk_agent(tmp_path, {"paths": ["/does/not/exist"]})
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["paths"]["/does/not/exist"]["exists"] is False
        assert result["paths"]["/does/not/exist"]["skills"] == 0
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# info action
# ---------------------------------------------------------------------------


def test_info_returns_skills_manual_body(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert "skills_manual" in result
        assert "name: skills-manual" in result["skills_manual"]
    finally:
        agent.stop(timeout=1.0)


def test_info_reports_ok_when_healthy(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["status"] == "ok"
        assert "error" not in result
    finally:
        agent.stop(timeout=1.0)


def test_info_reports_degraded_when_intrinsic_missing(tmp_path):
    # The skills capability is pure presentation — it does NOT reinstall
    # manuals when info is called. So if the initializer-installed manual is
    # deleted out-of-band after setup, info must report degraded.
    agent, workdir = _mk_agent(tmp_path)
    try:
        manual_path = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        )
        assert manual_path.is_file(), "precondition: initializer installed manual"
        manual_path.unlink()

        result = agent._tool_handlers["skills"]({"action": "info"})
        assert result["status"] == "degraded"
        assert "error" in result
    finally:
        agent.stop(timeout=1.0)


def test_info_surfaces_problems(tmp_path):
    workdir = tmp_path / "agent"
    # Pre-create a broken custom skill (missing description frontmatter).
    bad = workdir / ".library" / "custom" / "broken" / "SKILL.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nname: broken\n---\nno description!\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        result = agent._tool_handlers["skills"]({"action": "info"})
        problem_folders = [p["folder"] for p in result["problems"]]
        assert any("broken" in f for f in problem_folders)
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_catalog_injected_into_skills_section(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-thing", "shared-thing")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        assert "- name: skills-manual" in prompt
        assert "- name: file-manual" in prompt
        assert "- name: shared-thing" in prompt
    finally:
        agent.stop(timeout=1.0)


def test_catalog_rendering_is_readable_without_xml_quote_noise(tmp_path):
    # The catalog goes straight into the system prompt; humans (and the model)
    # complained that the prior XML shape was escape soup. Pin the YAML shape:
    # per-skill block with a `description:` block scalar carrying raw quotes
    # and apostrophes, no `&quot;` / `&apos;` over-escaping noise.
    workdir = tmp_path / "agent"
    _write_skill(
        workdir / ".library" / "custom" / "fancy-tool",
        "fancy-tool",
        'Handles "quoted" args and \'apostrophes\' — keep them raw.',
    )

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        # No spurious escape entities for `"` and `'` in element text.
        assert "&quot;" not in prompt
        assert "&apos;" not in prompt
        # YAML shape: `- name:` entry with a `description: |` block scalar.
        assert "- name: fancy-tool" in prompt
        assert "  description: |" in prompt
        # Body sits one level deeper than the `description:` field.
        assert "    Handles \"quoted\" args" in prompt
    finally:
        agent.stop(timeout=1.0)


def test_custom_skills_appear_in_catalog(tmp_path):
    workdir = tmp_path / "agent"
    _write_skill(workdir / ".library" / "custom" / "my-tool", "my-tool", "my desc")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        assert "my-tool" in prompt
        assert "my desc" in prompt
    finally:
        agent.stop(timeout=1.0)



# NOTE: `knowledge` and `skills` are now default-on (the `lingtai.core.*` floor
# boots on every Agent). The tests below preserve the breaking-rename guarantee
# at its remaining surface: legacy `library` / `codex` capability NAMES must not
# themselves produce tool handlers. Whether `knowledge`/`skills` are present is
# governed by core defaults, not by alias normalization.


def test_former_library_config_does_not_register_library_tool(tmp_path):
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_former_library_list_config_does_not_register_library_tool(tmp_path):
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=["library"],
    )
    try:
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_former_library_paths_do_not_leak_into_skills_catalog(tmp_path):
    """Skills extra paths must come from the `skills` cap, not `library` alias."""
    extra = tmp_path / "extra"
    _write_skill(extra / "old-shared", "old-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {"paths": [str(extra)]}},
    )
    try:
        # `skills` is default-on, but the legacy `library.paths` must not be
        # picked up as an extra skill path by alias normalization.
        assert "old-shared" not in (agent._prompt_manager.read_section("skills") or "")
    finally:
        agent.stop(timeout=1.0)


def test_former_codex_library_pair_does_not_register_legacy_tools(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "paired-shared", "paired-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"codex": {}, "library": {"paths": [str(extra)]}},
    )
    try:
        assert "codex" not in agent._tool_handlers
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_new_knowledge_and_skills_config_registers_both(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "new-shared", "new-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}, "skills": {"paths": [str(extra)]}},
    )
    try:
        assert {"knowledge", "skills"}.issubset(agent._tool_handlers)
        assert "library" not in agent._tool_handlers
        assert "codex" not in agent._tool_handlers
        assert "new-shared" in (agent._prompt_manager.read_section("skills") or "")
        # Knowledge is now filesystem-backed and isomorphic to skills: author by
        # writing knowledge/<name>/KNOWLEDGE.md, then refresh via info.
        entry_dir = workdir / "knowledge" / "new-entry"
        entry_dir.mkdir(parents=True)
        (entry_dir / "KNOWLEDGE.md").write_text(
            "---\nname: new-entry\ndescription: A freshly authored knowledge entry.\n---\nBody.\n"
        )
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["catalog_size"] == 1
        assert "new-entry" in (agent._prompt_manager.read_section("knowledge") or "")
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# No git operations
# ---------------------------------------------------------------------------


def test_skills_does_not_create_git_repo(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert not (workdir / ".library" / ".git").exists()
    finally:
        agent.stop(timeout=1.0)
