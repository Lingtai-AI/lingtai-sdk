"""Tests for the skill-style YAML+Markdown prompt/guidance catalog.

Covers the representation refactor that moved the kernel-owned packaged prompt
sections (principle/substrate/procedures) and the runtime-guidance payload onto
the same frontmatter+Markdown model as skills:

  * the shared frontmatter primitive (kernel-owned, re-exported to the wrapper);
  * the guidance Markdown catalog loader (dict shape, ordering, id contract);
  * the body-only contract for the rendered LLM prompt / final system.md while
    the on-disk section mirrors may carry developer-facing frontmatter;
  * packaging — every catalog file is reachable as an importlib resource.
"""

import re
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from lingtai_kernel._frontmatter import split_frontmatter, strip_frontmatter
from lingtai_kernel.meta_block import (
    build_runtime_guidance,
    build_guidance_with_meta_readme,
    validate_runtime_guidance,
)
from lingtai_kernel.prompt_catalog import (
    GUIDANCE_SECTION_ORDER,
    PromptCatalogError,
    load_guidance_catalog,
)


# ---------------------------------------------------------------------------
# Frontmatter primitive
# ---------------------------------------------------------------------------


def test_split_frontmatter_basic():
    text = "---\nname: foo\ntitle: Foo Bar\n---\nbody line one\nbody line two\n"
    meta, body = split_frontmatter(text)
    assert meta == {"name": "foo", "title": "Foo Bar"}
    assert body == "body line one\nbody line two\n"


def test_split_frontmatter_no_frontmatter_is_tolerated():
    text = "no frontmatter here\njust body\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_strip_frontmatter_is_split_body():
    text = "---\nname: x\n---\nhello\n"
    assert strip_frontmatter(text) == "hello\n"
    assert strip_frontmatter("plain body") == "plain body"


def test_wrapper_reexports_kernel_frontmatter():
    """mcp_servers/_skill.py must re-export the kernel primitive (one impl)."""
    from lingtai.mcp_servers import _skill
    from lingtai_kernel import _frontmatter

    assert _skill.split_frontmatter is _frontmatter.split_frontmatter


# ---------------------------------------------------------------------------
# Section sources (principle/substrate/procedures) carry frontmatter
# ---------------------------------------------------------------------------

_SECTION_FILES = ["principle.md", "substrate.md", "procedures.md"]


@pytest.mark.parametrize("name", _SECTION_FILES)
def test_packaged_section_sources_carry_frontmatter(name):
    text = files("lingtai.prompts").joinpath(name).read_text(encoding="utf-8")
    assert text.startswith("---"), f"{name} should carry skill-style frontmatter"
    meta, body = split_frontmatter(text)
    assert meta.get("name") == name.removesuffix(".md")
    assert meta.get("kind") == "prompt-section"
    assert "audience" not in meta
    assert meta.get("summary")
    assert meta.get("why")
    assert meta.get("related_files"), f"{name} must list connected files"
    assert "src/lingtai/agent.py" in meta["related_files"]
    assert meta.get("maintenance") and "editing this file" in meta["maintenance"]
    assert body, "section body must be non-empty"
    # The body must not re-open a frontmatter fence.
    assert not body.startswith("---")


# ---------------------------------------------------------------------------
# Guidance Markdown catalog
# ---------------------------------------------------------------------------


def test_guidance_catalog_loads_into_dict_shape():
    catalog = load_guidance_catalog()
    assert isinstance(catalog["schema_version"], int)
    assert catalog["schema_version"] == 1
    assert isinstance(catalog["guidance_version"], str) and catalog["guidance_version"]
    assert catalog["priority"]
    assert catalog["render_mode"]
    assert isinstance(catalog["sections"], list) and catalog["sections"]
    for section in catalog["sections"]:
        assert set(section) == {"id", "title", "body"}
        assert section["id"] and section["title"] and section["body"]
    # Assembled catalog must pass the dict-level schema validator unchanged.
    validate_runtime_guidance(catalog)


def test_guidance_catalog_preserves_code_owned_section_order():
    """Section order is behavior and stays code-owned, not frontmatter."""
    catalog = load_guidance_catalog()
    ids = [s["id"] for s in catalog["sections"]]
    assert ids == list(GUIDANCE_SECTION_ORDER)


def test_guidance_index_frontmatter_lists_connected_files():
    root = files("lingtai.prompts.guidance")
    meta, body = split_frontmatter(root.joinpath("INDEX.md").read_text(encoding="utf-8"))
    assert meta.get("kind") == "meta-guidance-catalog"
    assert "audience" not in meta
    assert meta.get("related_files")
    assert "src/lingtai_kernel/prompt_catalog.py" in meta["related_files"]
    assert "src/lingtai/prompts/guidance/*.md" in meta["related_files"]
    assert meta.get("maintenance") and "editing this file" in meta["maintenance"]
    assert not body.strip()


def test_guidance_section_frontmatter_is_documentary_not_behavior_config():
    """Guidance section frontmatter explains purpose but does not own ordering."""
    root = files("lingtai.prompts.guidance")
    forbidden = {"order", "batch", "raw", "protected"}
    for sid in GUIDANCE_SECTION_ORDER:
        meta, body = split_frontmatter(root.joinpath(f"{sid}.md").read_text(encoding="utf-8"))
        assert meta.get("id") == sid
        assert meta.get("kind") == "meta-guidance-section"
        assert "audience" not in meta
        assert meta.get("summary"), f"{sid} must summarize why this fragment exists"
        assert meta.get("why"), f"{sid} must explain purpose/why"
        assert meta.get("related_files"), f"{sid} must list connected files"
        assert "src/lingtai_kernel/prompt_catalog.py" in meta["related_files"]
        assert meta.get("maintenance") and "editing this file" in meta["maintenance"]
        assert not (forbidden & set(meta)), f"behavior config leaked into frontmatter for {sid}"
        assert body and not body.startswith("---")


def test_build_runtime_guidance_sources_from_catalog():
    import lingtai_kernel.meta_block as mb

    mb._GUIDANCE_CACHE = None
    guidance = build_runtime_guidance()
    assert guidance != {}
    assert [s["id"] for s in guidance["sections"]] == list(GUIDANCE_SECTION_ORDER)


def test_guidance_ids_referenced_by_runtime_code_exist():
    """T2 — `meta_guidance.<id>` refs in the kernel must resolve to catalog ids.

    `token_efficiency` and `notification_handling` are emitted into runtime
    `_meta` as `{"ref": "meta_guidance.<id>"}` pointers; renaming or dropping a
    referenced id would create a dangling pointer.
    """
    catalog_ids = {s["id"] for s in load_guidance_catalog()["sections"]}

    referenced: set[str] = set()
    kernel_root = Path("src/lingtai_kernel")
    for py in kernel_root.rglob("*.py"):
        referenced.update(re.findall(r"meta_guidance\.([a-z_]+)", py.read_text(encoding="utf-8")))

    # Drop refs that name the section itself, not a guidance id.
    referenced.discard("ref")
    assert {"token_efficiency", "notification_handling"} <= referenced
    missing = referenced - catalog_ids
    assert not missing, f"dangling meta_guidance.<id> refs: {sorted(missing)}"


def test_meta_guidance_assembly_matches_legacy_json_shape():
    """The assembled guidance-with-readme dict is the same shape the renderer
    consumes (id/title/body sections + meta_readme appended)."""
    guidance = build_guidance_with_meta_readme()
    ids = [s["id"] for s in guidance["sections"]]
    assert ids[-1] == "meta_readme"  # generated readme appended last
    assert "token_efficiency" in ids
    assert "notification_handling" in ids


# ---------------------------------------------------------------------------
# Catalog validation failures
# ---------------------------------------------------------------------------


def test_load_guidance_catalog_rejects_missing_package():
    with pytest.raises(PromptCatalogError):
        load_guidance_catalog(package="lingtai.prompts")  # has no INDEX.md


# ---------------------------------------------------------------------------
# Packaging — every catalog file is reachable as an importlib resource
# ---------------------------------------------------------------------------


def test_guidance_catalog_files_are_resources():
    root = files("lingtai.prompts.guidance")
    names = {p.name for p in root.iterdir() if p.name.endswith(".md")}
    assert "INDEX.md" in names
    for sid in GUIDANCE_SECTION_ORDER:
        assert f"{sid}.md" in names
        # Each resource is independently readable (catches glob regressions).
        text = root.joinpath(f"{sid}.md").read_text(encoding="utf-8")
        meta, _ = split_frontmatter(text)
        assert meta.get("id") == sid


def test_prompt_resource_packaging_metadata_stays_connected():
    """Wheel package-data and sdist manifest carry the prompt Markdown corpus."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["lingtai"]
    assert "prompts/*.md" in package_data
    assert "prompts/guidance/*.md" in package_data
    assert "prompts/*.json" not in package_data

    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include src/lingtai/prompts *.md" in manifest
