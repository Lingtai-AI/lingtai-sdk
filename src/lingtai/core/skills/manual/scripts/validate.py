#!/usr/bin/env python3
"""
Skill structure validation script — validates SKILL.md format, directory structure, scripts.
English error messages (usable by non-Chinese agents).
"""

import os
import sys
import re
import argparse
from pathlib import Path

import yaml


PLACEHOLDER_RE = re.compile(r'\[[A-Z][A-Z_]*\]')


def _parse_frontmatter(content: str) -> dict | None:
    """Extract YAML frontmatter between --- delimiters as a dict.

    Returns None when frontmatter is missing or unparseable.  Handles
    block scalars (``>`` / ``|``) correctly by delegating to PyYAML
    instead of line-based regex.
    """
    # Strip optional leading whitespace/newlines before the opening ---
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return None
    # Find closing ---
    rest = stripped[3:]  # skip opening ---
    end = rest.find("\n---")
    if end == -1:
        return None
    raw_yaml = rest[:end]
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def validate_frontmatter(skill_path: Path) -> tuple[bool, list[str]]:
    """Check frontmatter completeness and quality."""
    errors = []
    warnings = []
    skill_md = skill_path / "SKILL.md"

    if not skill_md.exists():
        return False, ["SKILL.md does not exist"]

    content = skill_md.read_text(encoding="utf-8")

    # Required: YAML opening delimiter
    if not content.strip().startswith("---"):
        errors.append("Frontmatter must start with '---'")

    fm = _parse_frontmatter(content)

    # Required: name field. PyYAML returns native scalar types, so coerce
    # non-string values without treating falsy scalars like 0 as missing.
    raw_name = fm.get("name") if fm else None
    name_val = "" if raw_name is None else str(raw_name).strip()
    if not name_val:
        errors.append("Missing 'name' field in frontmatter")
    else:
        # Check for unfilled placeholder
        if PLACEHOLDER_RE.match(name_val):
            errors.append(f"Unfilled placeholder: name is still '{name_val}'")

    # Required: description field — use parsed YAML so block scalars (>, |)
    # are fully expanded into a single string.
    desc_text = ""
    if fm and isinstance(fm.get("description"), str):
        desc_text = fm["description"].strip()
    elif fm and isinstance(fm.get("description"), list):
        # YAML flow-sequence bracket syntax (e.g. template placeholder
        # ``[ONE_LINE_DESCRIPTION]``).  Reconstruct the raw bracket form
        # so PLACEHOLDER_RE can match it.
        desc_text = "[" + ", ".join(str(v) for v in fm["description"]) + "]"
    elif fm and fm.get("description") is not None:
        # Fallback: non-string scalar (e.g. int) — stringify for checks
        desc_text = str(fm["description"]).strip()

    if not desc_text:
        errors.append("Missing 'description' field in frontmatter")
    else:
        # Check for unfilled placeholder
        if PLACEHOLDER_RE.search(desc_text):
            errors.append(f"Unfilled placeholder in description: {desc_text}")

    # Quality: description length (warn if > 500 chars — catalog is cluttered)
    if desc_text:
        if len(desc_text) > 500:
            warnings.append(f"WARNING: description is {len(desc_text)} chars (> 500). Catalog display will be cluttered.")

    # Quality: description is too short (should explain what it does)
    if desc_text:
        word_count = len(desc_text.split())
        if word_count < 3:
            warnings.append(f"WARNING: description is very short ({word_count} words). Should explain what this skill does.")

    return len(errors) == 0, errors + warnings


def validate_skill_md_length(skill_path: Path) -> tuple[bool, list[str]]:
    """Warn if SKILL.md is too long — content should be externalized."""
    warnings = []
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return True, []

    lines = skill_md.read_text(encoding="utf-8").splitlines()
    # Filter out empty lines and frontmatter
    code_lines = [line for line in lines if line.strip() and not line.strip().startswith('#')]

    if len(code_lines) > 500:
        warnings.append(f"WARNING: SKILL.md has {len(code_lines)} non-comment lines (> 500). Consider moving content to references/.")
    elif len(lines) > 300:
        warnings.append(f"NOTE: SKILL.md is {len(lines)} lines. Aim for < 300 lines; move detail to references/.")

    return True, warnings  # warnings don't fail validation


def validate_directory_structure(skill_path: Path) -> tuple[bool, list[str]]:
    """Check directory structure."""
    errors = []

    if not (skill_path / "SKILL.md").exists():
        errors.append("Missing SKILL.md")

    # Check referenced files exist in SKILL.md body
    # Capture full relative paths (e.g., "references/examples.md" not just "examples.md")
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8") if (skill_path / "SKILL.md").exists() else ""
    ref_matches = re.findall(r'(scripts/[\w./_-]+|assets/[\w./_-]+|references/[\w./_-]+)', content)
    for ref in ref_matches:
        clean = ref.rstrip('/')
        if clean and not (skill_path / clean).exists():
            errors.append(f"Broken reference: {clean} (mentioned in SKILL.md but does not exist)")

    return len(errors) == 0, errors


def validate_scripts(skill_path: Path) -> tuple[bool, list[str]]:
    """Check scripts are executable."""
    warnings = []
    scripts_dir = skill_path / "scripts"

    if not scripts_dir.exists():
        return True, []

    for script in scripts_dir.glob("*.py"):
        if not os.access(script, os.X_OK):
            warnings.append(f"WARNING: {script.name} is not executable (run: chmod +x {script.name})")

    return True, warnings  # non-executable is a warning, not an error


def validate_skill(skill_path: str) -> bool:
    """Validate a single skill directory."""
    skill_path = Path(skill_path)

    if not skill_path.exists():
        print(f"FAIL: directory does not exist: {skill_path}")
        return False

    print(f"\n{'='*50}")
    print(f"  Validating: {skill_path.name}")
    print(f"{'='*50}")

    all_passed = True

    # 1. Frontmatter
    passed, msgs = validate_frontmatter(skill_path)
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] Frontmatter")
    for m in msgs:
        prefix = "  " if "WARNING" in m or "NOTE" in m else "    - "
        print(f"  {prefix}{m}")
    if not passed:
        all_passed = False

    # 2. SKILL.md length
    _, msgs = validate_skill_md_length(skill_path)
    if msgs:
        for m in msgs:
            print(f"  NOTE: {m}")

    # 3. Directory structure
    passed, errors = validate_directory_structure(skill_path)
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] Directory structure")
    for e in errors:
        print(f"    - {e}")
    if not passed:
        all_passed = False

    # 4. Scripts
    passed, msgs = validate_scripts(skill_path)
    if msgs:
        for m in msgs:
            print(f"  NOTE: {m}")

    # Summary
    print(f"{'='*50}")
    print(f"  {'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Validate LingTai skill structure")
    parser.add_argument("skill", nargs="?", help="Skill directory path")
    args = parser.parse_args()

    if args.skill:
        success = validate_skill(args.skill)
        sys.exit(0 if success else 1)

    # Validate current directory
    current = Path.cwd()
    if (current / "SKILL.md").exists():
        success = validate_skill(current)
        sys.exit(0 if success else 1)
    else:
        print("Usage: python validate.py <skill-directory>")
        sys.exit(1)


if __name__ == "__main__":
    main()
