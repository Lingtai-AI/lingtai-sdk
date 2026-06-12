#!/usr/bin/env python3
"""Find and print the locally-installed README for an MCP server's Python package.

Usage:
    python3 find_readme.py <pkg-name>          # e.g. some-mcp-package
    python3 find_readme.py --module <modname>  # e.g. some_mcp_server (resolves to dist)

Resolution order:
    1. Editable install     -> repo's README.md / .rst / .txt on disk
    2. PyPI / wheel install -> README embedded in dist-info METADATA (PEP 566)
    3. Neither found        -> exit 2 with a hint to fall back to homepage URL

Exit codes:
    0  README printed to stdout (source label printed to stderr)
    1  argument error
    2  README not found locally; fall back to homepage <web_read>

Why this exists:
    MCP server READMEs are the canonical install / config / troubleshooting
    docs (config field names, env vars, error meanings). Reading the local
    copy is faster, version-accurate, and works offline -- preferred over
    fetching the homepage URL with web_read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import importlib.metadata as md


def _find_editable_readme(dist: md.Distribution) -> tuple[str, str] | None:
    """If `dist` is an editable install, return (content, source-label) for its
    on-disk README. Otherwise return None.

    Editable installs (`pip install -e <path>`) write a PEP 610 `direct_url.json`
    into dist-info with `dir_info.editable: true` and a `file://` URL pointing
    at the source repo.
    """
    try:
        durl_text = dist.read_text("direct_url.json")
    except (FileNotFoundError, OSError):
        return None
    if not durl_text:
        return None
    try:
        durl = json.loads(durl_text)
    except ValueError:
        return None
    if not durl.get("dir_info", {}).get("editable"):
        return None
    url = durl.get("url", "")
    if not url.startswith("file://"):
        return None
    repo = Path(url[len("file://"):])
    for cand in ("README.md", "README.rst", "README.txt", "README"):
        p = repo / cand
        if p.exists():
            try:
                return p.read_text(encoding="utf-8"), f"editable:{p}"
            except OSError:
                continue
    return None


def _find_metadata_readme(dist: md.Distribution, pkg_name: str) -> tuple[str, str] | None:
    """Return the README embedded in the wheel's METADATA file (PEP 566), or None.

    Modern build backends (setuptools >= 61, hatchling, pdm, poetry) embed the
    package's README into METADATA's Description field when `pyproject.toml`
    declares `readme = "README.md"`. This survives PyPI publish -> pip install
    so non-editable users get the same docs.
    """
    meta = dist.metadata
    body: str | None = None
    if hasattr(meta, "get_payload"):
        try:
            body = meta.get_payload()
        except Exception:
            body = None
    if not body:
        try:
            raw = dist.read_text("METADATA")
        except (FileNotFoundError, OSError):
            raw = None
        if raw and "\n\n" in raw:
            body = raw.split("\n\n", 1)[1]
    if body and body.strip():
        return body, f"dist-info:{pkg_name} METADATA"
    return None


def find_readme(pkg_name: str) -> tuple[str | None, str]:
    """Return (content, source-label-or-error) for `pkg_name`.

    Tries editable repo first, then dist-info METADATA. content is None when
    neither path yields a README; in that case the second element describes
    the failure reason for stderr.
    """
    try:
        dist = md.distribution(pkg_name)
    except md.PackageNotFoundError:
        return None, f"package not installed: {pkg_name}"

    found = _find_editable_readme(dist)
    if found is not None:
        return found

    found = _find_metadata_readme(dist, pkg_name)
    if found is not None:
        return found

    return None, f"no README found locally for {pkg_name}"


def _resolve_module_to_dist(module_name: str) -> str | None:
    """Map an importable module name (e.g. `some_mcp_server`) to its owning
    distribution name. Returns None if no install can be located.

    Tries `packages_distributions()` first (works for normal wheels via
    `top_level.txt`), then falls back to the standard underscore-to-hyphen
    convention -- editable installs via `.pth` files don't always populate
    `packages_distributions`, but their distribution name is usually the
    obvious transform of the module name.
    """
    try:
        dists = md.packages_distributions().get(module_name, [])
    except Exception:
        dists = []
    if dists:
        return dists[0]

    # Editable-install fallback: try `module_name` and `module_name.replace("_", "-")`.
    for candidate in (module_name, module_name.replace("_", "-")):
        try:
            md.distribution(candidate)
            return candidate
        except md.PackageNotFoundError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("name", help="distribution name (default) or module name with --module")
    ap.add_argument(
        "--module",
        action="store_true",
        help="treat <name> as an importable module name; resolve to the owning distribution",
    )
    args = ap.parse_args()

    pkg = args.name
    if args.module:
        resolved = _resolve_module_to_dist(args.name)
        if resolved is None:
            print(f"ERROR: cannot resolve module '{args.name}' to an installed distribution", file=sys.stderr)
            return 1
        pkg = resolved

    content, source = find_readme(pkg)
    if content is None:
        print(f"ERROR: {source}", file=sys.stderr)
        print("HINT: fall back to web_read on the registry's <homepage> URL", file=sys.stderr)
        return 2

    print(f"# Source: {source}", file=sys.stderr)
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
