import ast
from pathlib import Path


def test_source_read_text_calls_pin_utf8_encoding() -> None:
    """Production code must not depend on the host locale when reading text."""
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read_text"
            ):
                continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            # importlib.metadata.Distribution.read_text is not pathlib.Path.read_text:
            # it has no encoding parameter and decodes dist metadata per Python's
            # importlib.metadata contract. The Windows-locale crash class is caused
            # by filesystem/importlib.resources text reads that inherit the process
            # locale, not by Distribution metadata reads.
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "dist":
                continue
            rel = path.relative_to(root)
            offenders.append(f"{rel}:{node.lineno}: {lines[node.lineno - 1].strip()}")

    assert offenders == []


def test_source_parses_as_python311() -> None:
    """Keep syntax compatible with the package's declared Python 3.11 floor."""
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "src").rglob("*.py")):
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 11),
        )
