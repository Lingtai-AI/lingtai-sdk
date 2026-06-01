"""Tests for the skill-structure validator (validate.py).

Covers the multiline YAML description parsing fix: block scalars (>, |),
quoted strings, template placeholders, and nested-reference validation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The validator lives outside the package tree; import it directly.
_SCRIPTS = Path(__file__).resolve().parents[1] / "src" / "lingtai" / "core" / "skills" / "manual" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from validate import _parse_frontmatter, validate_frontmatter  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_frontmatter unit tests
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    """Low-level frontmatter parser."""

    def test_folded_scalar_expands(self):
        content = (
            "---\nname: test\ndescription: >\n  First line\n  second line\n---\n\nBody.\n"
        )
        fm = _parse_frontmatter(content)
        assert fm is not None
        assert "First line second line" in fm["description"]

    def test_literal_scalar_preserves_newlines(self):
        content = (
            "---\nname: test\ndescription: |\n  Line one\n  Line two\n---\n\nBody.\n"
        )
        fm = _parse_frontmatter(content)
        assert fm is not None
        assert "Line one\nLine two" in fm["description"]

    def test_quoted_string(self):
        content = '---\nname: test\ndescription: "Hello world"\n---\n\nBody.\n'
        fm = _parse_frontmatter(content)
        assert fm is not None
        assert fm["description"] == "Hello world"

    def test_inline_string(self):
        content = "---\nname: test\ndescription: A plain description\n---\n\nBody.\n"
        fm = _parse_frontmatter(content)
        assert fm is not None
        assert fm["description"] == "A plain description"

    def test_template_placeholder_returns_list(self):
        content = "---\nname: test\ndescription: [ONE_LINE_DESCRIPTION]\n---\n\nBody.\n"
        fm = _parse_frontmatter(content)
        assert fm is not None
        # YAML parses [FOO] as a flow-sequence
        assert isinstance(fm["description"], list)

    def test_missing_description(self):
        content = "---\nname: test\n---\n\nBody.\n"
        fm = _parse_frontmatter(content)
        assert fm is not None
        assert "description" not in fm

    def test_no_frontmatter(self):
        content = "# Just a heading\nSome body.\n"
        fm = _parse_frontmatter(content)
        assert fm is None

    def test_broken_yaml_returns_none(self):
        content = "---\n: invalid: yaml: ::::\n---\n"
        fm = _parse_frontmatter(content)
        assert fm is None

    def test_empty_frontmatter(self):
        content = "---\n---\n\nBody.\n"
        fm = _parse_frontmatter(content)
        # safe_load returns None for empty YAML; not a dict → returns None
        assert fm is None


# ---------------------------------------------------------------------------
# validate_frontmatter — multiline description regression
# ---------------------------------------------------------------------------

def _write_skill(tmp_path: Path, name: str, desc_text: str) -> Path:
    """Write a minimal SKILL.md with the given raw description value."""
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc_text}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_multiline_skill(tmp_path: Path, name: str, desc_lines: list[str]) -> Path:
    """Write a SKILL.md with a YAML block-scalar description."""
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    block = "\n".join(f"  {line}" for line in desc_lines)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n{block}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


class TestValidateFrontmatterMultiline:
    """Regression: multiline description must not trigger false warnings."""

    def test_folded_scalar_not_flagged_as_short(self, tmp_path):
        skill = _write_multiline_skill(
            tmp_path,
            "my-skill",
            [
                "This skill handles multi-step workflows",
                "that require domain knowledge and careful orchestration.",
            ],
        )
        passed, msgs = validate_frontmatter(skill)
        assert passed
        short_warnings = [m for m in msgs if "very short" in m]
        assert short_warnings == [], f"Got false short warning: {short_warnings}"

    def test_literal_scalar_not_flagged_as_short(self, tmp_path):
        skill_dir = tmp_path / "skills" / "lit-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: lit-skill\ndescription: |\n  A detailed literal block\n  description with multiple lines.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        passed, msgs = validate_frontmatter(skill_dir)
        assert passed
        short_warnings = [m for m in msgs if "very short" in m]
        assert short_warnings == []

    def test_actual_short_description_still_warns(self, tmp_path):
        skill = _write_skill(tmp_path, "tiny", "short")
        passed, msgs = validate_frontmatter(skill)
        assert passed  # short description is a warning, not an error
        short_warnings = [m for m in msgs if "very short" in m]
        assert len(short_warnings) == 1

    def test_actual_missing_description_errors(self, tmp_path):
        skill_dir = tmp_path / "skills" / "no-desc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: no-desc\n---\n\nBody.\n",
            encoding="utf-8",
        )
        passed, msgs = validate_frontmatter(skill_dir)
        assert not passed
        assert any("Missing 'description'" in m for m in msgs)

    def test_name_as_number_does_not_crash(self, tmp_path):
        """Non-string YAML scalar names should be handled gracefully."""
        skill_dir = tmp_path / "skills" / "num-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: 42\ndescription: A valid description with enough words.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        passed, msgs = validate_frontmatter(skill_dir)
        assert passed, msgs

    def test_name_as_zero_is_not_treated_as_missing(self, tmp_path):
        """Falsy non-None YAML scalars should not be collapsed to missing."""
        skill_dir = tmp_path / "skills" / "zero-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: 0\ndescription: A valid description with enough words.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        passed, msgs = validate_frontmatter(skill_dir)
        assert passed, msgs

    def test_template_placeholder_detected(self, tmp_path):
        skill = _write_skill(tmp_path, "tpl", "[ONE_LINE_DESCRIPTION]")
        passed, msgs = validate_frontmatter(skill)
        # Placeholder in description is an error (not a warning) —
        # the validator blocks publishing with unfilled template slots.
        assert not passed
        placeholder_msgs = [m for m in msgs if "Unfilled placeholder" in m]
        assert len(placeholder_msgs) == 1

    def test_long_description_char_count_uses_full_text(self, tmp_path):
        long_desc = "word " * 150  # ~750 chars
        skill = _write_skill(tmp_path, "verbose", long_desc.strip())
        passed, msgs = validate_frontmatter(skill)
        assert passed
        long_warnings = [m for m in msgs if "chars (> 500)" in m]
        assert len(long_warnings) == 1


# ---------------------------------------------------------------------------
# Nested reference semantics — validator runs on child SKILL.md
# ---------------------------------------------------------------------------

class TestNestedReferenceValidation:
    """Verify the validator can be pointed at a nested reference folder."""

    def test_validate_nested_child_directory(self, tmp_path):
        parent_dir = tmp_path / "parent-skill"
        parent_dir.mkdir()
        (parent_dir / "SKILL.md").write_text(
            "---\nname: parent-skill\ndescription: A parent router.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        child_dir = parent_dir / "reference" / "topic-a"
        child_dir.mkdir(parents=True)
        (child_dir / "SKILL.md").write_text(
            "---\nname: topic-a\ndescription: Nested parent-skill reference for topic A.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        # Parent should validate
        passed_parent, _ = validate_frontmatter(parent_dir)
        assert passed_parent
        # Child should also validate independently
        passed_child, _ = validate_frontmatter(child_dir)
        assert passed_child

    def test_nested_child_short_description_warns(self, tmp_path):
        child_dir = tmp_path / "reference" / "tiny"
        child_dir.mkdir(parents=True)
        (child_dir / "SKILL.md").write_text(
            "---\nname: tiny\ndescription: short\n---\n\nBody.\n",
            encoding="utf-8",
        )
        passed, msgs = validate_frontmatter(child_dir)
        assert passed
        short_warnings = [m for m in msgs if "very short" in m]
        assert len(short_warnings) == 1
