"""Tests for the shared knowledge/skills catalog helpers (issue #513).

These lock the model-visible behavior that ``knowledge`` and ``skills`` both
depend on: exact problem strings, traversal rules, and byte-for-byte catalog
YAML rendering.
"""
from __future__ import annotations

from pathlib import Path

from lingtai.core._catalog import (
    build_catalog_yaml,
    parse_frontmatter,
    parse_markdown_catalog_file,
    scan_markdown_catalog,
)


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_normalizes_values():
    fm = parse_frontmatter(
        "---\nname:  my  skill \ndescription: a\n  multi line\nblank:\n---\nbody\n"
    )
    assert fm["name"] == "my skill"  # whitespace collapsed
    assert fm["description"] == "a multi line"
    assert fm["blank"] == ""  # None becomes ""


def test_parse_frontmatter_non_dict_and_invalid_return_empty():
    assert parse_frontmatter("no frontmatter here") == {}
    assert parse_frontmatter("---\n- just\n- a\n- list\n---\n") == {}
    assert parse_frontmatter("---\n: : bad yaml :\n---\n") == {}


def test_parse_frontmatter_coerces_keys_and_scalar_values_to_str():
    fm = parse_frontmatter("---\nversion: 2.0\ncount: 3\n---\n")
    assert fm["version"] == "2.0"
    assert fm["count"] == "3"


# ---------------------------------------------------------------------------
# parse_markdown_catalog_file
# ---------------------------------------------------------------------------

def test_parse_catalog_file_valid(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: alpha\ndescription: does things\nversion: 1.2\n---\nbody\n")
    entry, prob = parse_markdown_catalog_file(f, "alpha", filename="SKILL.md")
    assert prob is None
    assert entry == {
        "name": "alpha",
        "description": "does things",
        "version": "1.2",
        "path": str(f),
    }


def test_parse_catalog_file_missing_name_message(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text("---\ndescription: no name here\n---\n")
    entry, prob = parse_markdown_catalog_file(f, "folderA", filename="SKILL.md")
    assert entry is None
    assert prob == {
        "folder": "folderA",
        "reason": "SKILL.md missing required frontmatter field: name",
    }


def test_parse_catalog_file_missing_description_message(tmp_path: Path):
    f = tmp_path / "KNOWLEDGE.md"
    f.write_text("---\nname: only-name\n---\n")
    entry, prob = parse_markdown_catalog_file(f, "folderB", filename="KNOWLEDGE.md")
    assert entry is None
    assert prob == {
        "folder": "folderB",
        "reason": "KNOWLEDGE.md missing required frontmatter field: description",
    }


def test_parse_catalog_file_unreadable_message(tmp_path: Path):
    # A directory cannot be read_text()'d → OSError → "cannot read ..." message.
    d = tmp_path / "KNOWLEDGE.md"
    d.mkdir()
    entry, prob = parse_markdown_catalog_file(d, "folderC", filename="KNOWLEDGE.md")
    assert entry is None
    assert prob is not None
    assert prob["folder"] == "folderC"
    assert prob["reason"].startswith("cannot read KNOWLEDGE.md: ")


# ---------------------------------------------------------------------------
# scan_markdown_catalog
# ---------------------------------------------------------------------------

def _skill(folder: Path, name: str, desc: str = "d"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")


def test_scan_recurses_and_sorts(tmp_path: Path):
    _skill(tmp_path / "b-skill", "bravo")
    _skill(tmp_path / "a-skill", "alpha")
    # A directory tree with only subdirectories is recursed into.
    _skill(tmp_path / "group" / "nested", "nested-one")
    # Dot-prefixed directories are skipped entirely.
    _skill(tmp_path / ".hidden", "should-not-appear")

    valid, problems = scan_markdown_catalog(tmp_path, filename="SKILL.md", kind="skill")
    names = [v["name"] for v in valid]
    assert names == ["alpha", "bravo", "nested-one"]  # sorted iterdir order
    assert problems == []
    # Nested label uses the recursion prefix.
    nested = next(v for v in valid if v["name"] == "nested-one")
    assert nested["path"] == str(tmp_path / "group" / "nested" / "SKILL.md")


def test_scan_loose_files_corruption_message(tmp_path: Path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "stray.txt").write_text("loose")

    valid, problems = scan_markdown_catalog(tmp_path, filename="SKILL.md", kind="skill")
    assert valid == []
    assert problems == [{
        "folder": "broken",
        "reason": "not a skill (no SKILL.md) and has loose files — corrupted",
    }]


def test_scan_knowledge_kind_wording(tmp_path: Path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "stray.txt").write_text("loose")

    _, problems = scan_markdown_catalog(
        tmp_path, filename="KNOWLEDGE.md", kind="knowledge entry",
    )
    assert problems == [{
        "folder": "broken",
        "reason": "not a knowledge entry (no KNOWLEDGE.md) and has loose files — corrupted",
    }]


def test_scan_missing_directory_is_empty(tmp_path: Path):
    valid, problems = scan_markdown_catalog(
        tmp_path / "nope", filename="SKILL.md", kind="skill",
    )
    assert valid == []
    assert problems == []


# ---------------------------------------------------------------------------
# build_catalog_yaml — golden snapshot
# ---------------------------------------------------------------------------

def test_build_catalog_yaml_empty():
    assert build_catalog_yaml([], "PRE") == ""


def test_build_catalog_yaml_golden():
    entries = [
        {"name": "alpha", "path": "/x/alpha/KNOWLEDGE.md",
         "description": "line one\n\nline three", "version": ""},
        {"name": "beta", "path": "/x/beta/KNOWLEDGE.md",
         "description": "single", "version": "1.0"},
    ]
    out = build_catalog_yaml(entries, "PRE")
    expected = (
        "PRE\n"
        "\n"
        "- name: alpha\n"
        "  location: /x/alpha/KNOWLEDGE.md\n"
        "  description: |\n"
        "    line one\n"
        "    \n"          # blank description line → exactly four spaces
        "    line three\n"
        "- name: beta\n"
        "  location: /x/beta/KNOWLEDGE.md\n"
        "  description: |\n"
        "    single"      # no trailing newline
    )
    assert out == expected
