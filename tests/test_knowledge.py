"""Tests for the filesystem-backed knowledge capability.

Knowledge is structurally isomorphic to skills but physically separate:
entries live at ``<agent>/knowledge/<name>/KNOWLEDGE.md`` (not ``SKILL.md``).
The catalog injects only ``name``/``description``/``location`` from frontmatter;
bodies and supporting files are loaded on demand via the regular ``read`` tool.
"""
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


def _mk_agent(tmp_path: Path, knowledge_cfg: dict | None = None):
    caps = {"knowledge": knowledge_cfg or {}}
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=caps,
    )
    return agent, workdir


def _write_entry(folder: Path, name: str, desc: str = "test entry", body: str = "Body text.") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "KNOWLEDGE.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n"
    )
    return path


# ---------------------------------------------------------------------------
# Setup & registration
# ---------------------------------------------------------------------------


def test_knowledge_setup_registers_only_knowledge_tool(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        assert "knowledge" in agent._tool_handlers
        assert "library" not in agent._tool_handlers
        assert "codex" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_former_alias_capabilities_do_not_register_knowledge(tmp_path):
    for cap in ("library", "codex"):
        agent = Agent(
            service=make_mock_service(),
            agent_name=f"test-{cap}",
            working_dir=tmp_path / cap,
            capabilities=[cap],
        )
        try:
            assert agent.get_capability("knowledge") is None
            assert "knowledge" not in agent._tool_handlers
            assert cap not in agent._tool_handlers
        finally:
            agent.stop(timeout=1.0)


def test_knowledge_independent_of_psyche(tmp_path):
    """Knowledge is a separate capability; psyche is always-on as intrinsic."""
    agent, _ = _mk_agent(tmp_path)
    try:
        assert "psyche" in agent._intrinsics
        assert "knowledge" in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_legacy_knowledge_limit_kwarg_is_ignored(tmp_path):
    """Old presets may still carry knowledge_limit — must not error."""
    agent, _ = _mk_agent(tmp_path, {"knowledge_limit": 50})
    try:
        assert "knowledge" in agent._tool_handlers
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["status"] == "ok"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Tool surface — info action
# ---------------------------------------------------------------------------


def test_info_returns_runtime_snapshot(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["knowledge_dir"] == str(workdir / "knowledge")
        assert result["catalog_size"] == 0
        assert result["problems"] == []
    finally:
        agent.stop(timeout=1.0)


def test_info_picks_up_authored_entry(tmp_path):
    workdir = tmp_path / "agent"
    _write_entry(
        workdir / "knowledge" / "tcp-retry",
        "tcp-retry",
        "How the mail service retries TCP — exponential backoff and failure modes.",
    )
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["catalog_size"] == 1
        assert result["problems"] == []
    finally:
        agent.stop(timeout=1.0)


def test_unknown_action_returns_error(tmp_path):
    """Removed JSON-store actions (submit/view/etc.) must be rejected."""
    agent, _ = _mk_agent(tmp_path)
    try:
        for action in ("submit", "view", "consolidate", "delete", "filter", "export"):
            result = agent._tool_handlers["knowledge"]({"action": action})
            assert result["status"] == "error", f"{action!r} should be rejected"
            assert "unknown action" in result["message"].lower()
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_has_only_info_action():
    from lingtai.core.knowledge import get_schema
    SCHEMA = get_schema("en")
    actions = SCHEMA["properties"]["action"]["enum"]
    assert actions == ["info"]
    # Old JSON-store properties are gone — these fields no longer have any code path.
    props = SCHEMA["properties"]
    for removed in ("title", "summary", "content", "supplementary", "ids", "include_supplementary"):
        assert removed not in props, f"{removed!r} must be removed from schema"


# ---------------------------------------------------------------------------
# Catalog metadata only — no body, no supporting-file content
# ---------------------------------------------------------------------------


def test_prompt_catalog_only_metadata_not_body(tmp_path):
    """Bodies and supplementary material must never enter the prompt section."""
    workdir = tmp_path / "agent"
    body_sentinel = "BODY_SENTINEL_should_never_appear_in_prompt"
    _write_entry(
        workdir / "knowledge" / "important-finding",
        "important-finding",
        "Short prompt-visible description.",
        body=f"## Notes\n\n{body_sentinel}\n\nLong reasoning paragraph here.\n",
    )
    # Add a supporting file — must never enter the prompt either.
    support_sentinel = "SUPPORT_SENTINEL_also_must_not_appear"
    (workdir / "knowledge" / "important-finding" / "raw-log.txt").write_text(
        support_sentinel
    )

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("knowledge") or ""
        # name + description + location are present.
        assert "important-finding" in prompt
        assert "Short prompt-visible description." in prompt
        assert "<knowledge>" in prompt
        # Body and supporting file content are absent.
        assert body_sentinel not in prompt
        assert support_sentinel not in prompt
    finally:
        agent.stop(timeout=1.0)


def test_catalog_clears_when_no_entries(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        prompt = agent._prompt_manager.read_section("knowledge") or ""
        assert prompt == ""
    finally:
        agent.stop(timeout=1.0)


def test_catalog_refreshes_on_info(tmp_path):
    """info() re-scans so newly authored entries appear without restart."""
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert (agent._prompt_manager.read_section("knowledge") or "") == ""

        _write_entry(
            workdir / "knowledge" / "late-arrival",
            "late-arrival",
            "Added after agent boot.",
        )
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["catalog_size"] == 1

        prompt = agent._prompt_manager.read_section("knowledge") or ""
        assert "late-arrival" in prompt
        assert "Added after agent boot." in prompt
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Convention boundary: KNOWLEDGE.md vs SKILL.md
# ---------------------------------------------------------------------------


def test_knowledge_md_convention_distinct_from_skill_md(tmp_path):
    """The knowledge tool only picks up KNOWLEDGE.md files, not SKILL.md."""
    workdir = tmp_path / "agent"
    # Valid knowledge entry.
    _write_entry(
        workdir / "knowledge" / "real-entry",
        "real-entry",
        "Picked up because it has KNOWLEDGE.md.",
    )
    # A SKILL.md sibling inside knowledge/ must NOT be cataloged.
    skill_folder = workdir / "knowledge" / "skill-shaped-thing"
    skill_folder.mkdir(parents=True, exist_ok=True)
    (skill_folder / "SKILL.md").write_text(
        "---\nname: skill-shaped-thing\ndescription: would-be-skill\n---\nBody.\n"
    )

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["catalog_size"] == 1
        prompt = agent._prompt_manager.read_section("knowledge") or ""
        assert "real-entry" in prompt
        assert "skill-shaped-thing" not in prompt
        # The corrupted folder is reported as a problem (loose file, no KNOWLEDGE.md).
        problem_folders = [p["folder"] for p in result["problems"]]
        assert any("skill-shaped-thing" in f for f in problem_folders)
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Entries may carry references, scripts, assets
# ---------------------------------------------------------------------------


def test_entries_may_have_scripts_and_assets(tmp_path):
    """Knowledge entries can carry supporting files like skills do."""
    workdir = tmp_path / "agent"
    entry_dir = workdir / "knowledge" / "rich-entry"
    _write_entry(
        entry_dir,
        "rich-entry",
        "An entry with scripts and assets.",
        body="See scripts/repro.sh and assets/diagram.png.\n",
    )
    (entry_dir / "scripts").mkdir()
    (entry_dir / "scripts" / "repro.sh").write_text("#!/bin/sh\necho hi\n")
    (entry_dir / "assets").mkdir()
    (entry_dir / "assets" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["catalog_size"] == 1
        assert result["problems"] == []
    finally:
        agent.stop(timeout=1.0)


def test_entry_may_reference_local_paths_in_body(tmp_path):
    """Knowledge bodies may mention local paths, mail ids, logs — unlike skills.

    The capability does not parse the body; this test asserts that nothing
    blocks an agent from authoring such an entry and that the catalog still
    only injects the public-shaped frontmatter.
    """
    workdir = tmp_path / "agent"
    body = (
        "Saw this in mailbox/inbox/20260512T081132-fdb2/ and logs/agent.log.\n"
        "Cross-reference with /Users/me/private/notes.md.\n"
    )
    _write_entry(
        workdir / "knowledge" / "private-refs",
        "private-refs",
        "Notes citing local-only context — fine for knowledge.",
        body=body,
    )
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["catalog_size"] == 1
        prompt = agent._prompt_manager.read_section("knowledge") or ""
        # Body (and its private references) stays out of the prompt catalog.
        assert "mailbox/inbox" not in prompt
        assert "/Users/me/private" not in prompt
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Health / problem reporting
# ---------------------------------------------------------------------------


def test_info_surfaces_missing_frontmatter(tmp_path):
    workdir = tmp_path / "agent"
    bad = workdir / "knowledge" / "missing-desc" / "KNOWLEDGE.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nname: missing-desc\n---\nno description!\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        problem_folders = [p["folder"] for p in result["problems"]]
        assert any("missing-desc" in f for f in problem_folders)
        assert result["catalog_size"] == 0
    finally:
        agent.stop(timeout=1.0)


def test_legacy_knowledge_json_is_ignored(tmp_path):
    """Old `knowledge/knowledge.json` is not consulted; it does not become an entry."""
    workdir = tmp_path / "agent"
    legacy_dir = workdir / "knowledge"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "knowledge.json").write_text(
        '{"version": 1, "entries": [{"id": "abc", "title": "Old", "summary": "Stale"}]}'
    )
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}},
    )
    try:
        result = agent._tool_handlers["knowledge"]({"action": "info"})
        assert result["catalog_size"] == 0
        prompt = agent._prompt_manager.read_section("knowledge") or ""
        assert "Old" not in prompt
        assert "Stale" not in prompt
    finally:
        agent.stop(timeout=1.0)
