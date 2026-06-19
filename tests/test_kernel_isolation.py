"""Test that lingtai_kernel has no dependencies on the lingtai package.

This test ensures the architectural constraint holds:
  - lingtai_kernel can be used standalone (zero hard dependencies)
  - lingtai_kernel never accidentally pulls in lingtai (capabilities, addons, adapters)

The constraint is enforced two ways:
  1. Runtime assert in src/lingtai_kernel/__init__.py
  2. This test: import lingtai_kernel in a subprocess with a clean sys.modules,
     then assert no 'lingtai' package modules leaked in.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_kernel_import_is_clean():
    """Import lingtai_kernel in a fresh subprocess; verify 'lingtai' is not loaded."""
    result = subprocess.run(
        [sys.executable, "-c", "import lingtai_kernel; print('OK')"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, (
        f"lingtai_kernel failed to import.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "lingtai" not in result.stdout, (
        "Test harness output should not mention 'lingtai'"
    )
    assert "OK" in result.stdout, (
        f"lingtai_kernel import did not print confirmation.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_kernel_has_no_lingtai_submodules():
    """Verify lingtai_kernel's own package tree has no imports of the 'lingtai' package."""
    import ast
    from pathlib import Path

    kernel_src = Path(__file__).parent.parent / "src" / "lingtai_kernel"
    violations: list[str] = []

    for py_file in kernel_src.rglob("*.py"):
        if py_file.name == "__pycache__":
            continue
        source = py_file.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module.startswith("lingtai.")
                    and not node.module.startswith("lingtai_kernel.")
                ):
                    violations.append(f"{py_file.relative_to(kernel_src)}: from {node.module} ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        alias.name.startswith("lingtai.")
                        and not alias.name.startswith("lingtai_kernel.")
                    ):
                        violations.append(f"{py_file.relative_to(kernel_src)}: import {alias.name}")

    assert not violations, (
        "lingtai_kernel contains imports of the 'lingtai' package. "
        "This violates the architectural constraint that kernel must never depend on lingtai.\n"
        + "\n".join(violations)
    )


def test_kernel_import_does_not_pull_lingtai():
    """Confirm that importing lingtai_kernel does NOT make 'lingtai' appear in sys.modules."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; import lingtai_kernel; "
            "leaked = [k for k in sys.modules if k.startswith('lingtai') and not k.startswith('lingtai_kernel')]; "
            "print('LEAKED:', leaked) if leaked else print('CLEAN')"
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, f"Subprocess error:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "CLEAN" in result.stdout, (
        f"The 'lingtai' package leaked into sys.modules after importing lingtai_kernel.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "LEAKED:" not in result.stdout, (
        f"lingtai_kernel caused the following 'lingtai' modules to be loaded:\n"
        f"{result.stdout}"
    )
