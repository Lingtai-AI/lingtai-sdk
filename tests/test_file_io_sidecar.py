"""Tests for the optional Rust-backed ``FileIOBackend`` (``RustFileIOBackend``).

The pure-Python tests use a fake "sidecar" — a small Python script we
write to ``tmp_path`` and invoke through the real subprocess machinery —
so the adapter is exercised end-to-end without needing a Rust toolchain.

The very last test does an honest Rust roundtrip but is skipped unless
``cargo`` is on ``PATH``. CI installs without Rust will still see the
adapter/backend covered.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lingtai.services.file_io import (
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_VISITED,
    DEFAULT_WALLTIME_S,
    GrepMatch,
    LocalFileIOService,
    TraversalStats,
)
from lingtai.services.file_io_sidecar import (
    RustFileIOBackend,
    SidecarAdapter,
    SidecarError,
    SidecarRequest,
)


@pytest.fixture(autouse=True)
def _clear_sidecar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt into a sidecar binary explicitly; never inherit from CI env."""
    monkeypatch.delenv("LINGTAI_FILE_IO_SIDECAR", raising=False)
    monkeypatch.delenv("LINGTAI_SEARCH_SIDECAR", raising=False)
    monkeypatch.delenv("LINGTAI_FILE_IO_BACKEND", raising=False)


# ---------------------------------------------------------------------------
# Fake sidecar helpers
# ---------------------------------------------------------------------------


def _write_fake_sidecar(tmp_path: Path, name: str, script: str) -> Path:
    """Write a Python-script "sidecar" and return its path.

    The script body must consume the ``payload`` variable (already
    populated from stdin) and print one JSON envelope on stdout,
    mirroring the real Rust binary's protocol.

    We assemble the file manually — no ``textwrap.dedent`` — so the
    shebang lands at column 0. macOS will reject any executable whose
    shebang line is indented (``OSError: [Errno 8] Exec format error``).
    """
    sidecar = tmp_path / name
    prelude = f"#!{sys.executable}\nimport json, sys\npayload = json.loads(sys.stdin.read())\n"
    body = textwrap.dedent(script)
    sidecar.write_text(prelude + body, encoding="utf-8")
    sidecar.chmod(sidecar.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return sidecar


def _echo_grep_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that echoes its payload back as a single grep match.

    Useful for asserting that the adapter formed the right JSON request
    and parsed the response envelope correctly.
    """
    return _write_fake_sidecar(
        tmp_path,
        "echo-grep.py",
        """
        env = {
            "ok": True,
            "backend": "fake-echo",
            "op": payload.get("op"),
            "matches": [
                {
                    "path": "echoed.txt",
                    "line_number": 1,
                    "line": json.dumps(payload, sort_keys=True),
                }
            ],
            "paths": [],
            "visited": payload.get("max_visited", 0),
            "files_skipped_size": 0,
            "files_skipped_binary": 0,
            "dirs_pruned": 0,
            "elapsed_ms": 1,
            "truncated_reason": None,
            "error": None,
        }
        sys.stdout.write(json.dumps(env))
        """,
    )


def _glob_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that emits a fixed glob result."""
    return _write_fake_sidecar(
        tmp_path,
        "glob.py",
        """
        env = {
            "ok": True,
            "backend": "fake-glob",
            "op": "glob",
            "matches": [],
            "paths": ["/abs/b.py", "/abs/a.py"],
            "visited": 5,
            "files_skipped_size": 1,
            "files_skipped_binary": 2,
            "dirs_pruned": 3,
            "elapsed_ms": 4,
            "truncated_reason": "max_results",
            "error": None,
        }
        sys.stdout.write(json.dumps(env))
        """,
    )


def _multi_grep_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that returns two grep matches: one .py and one .log.

    Lets us prove the ``glob_filter`` basename post-filter on the Rust
    path — the sidecar itself is glob-agnostic, so the backend must drop
    the non-matching basename after the call.
    """
    return _write_fake_sidecar(
        tmp_path,
        "multi-grep.py",
        """
        env = {
            "ok": True,
            "backend": "fake-multi",
            "op": payload.get("op"),
            "matches": [
                {"path": "good.py", "line_number": 1, "line": "needle py"},
                {"path": "noisy.log", "line_number": 2, "line": "needle log"},
            ],
            "paths": [],
            "visited": 2,
            "files_skipped_size": 0,
            "files_skipped_binary": 0,
            "dirs_pruned": 0,
            "elapsed_ms": 1,
            "truncated_reason": None,
            "error": None,
        }
        sys.stdout.write(json.dumps(env))
        """,
    )


def _failing_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that returns a structured error envelope and exits 2."""
    return _write_fake_sidecar(
        tmp_path,
        "fail.py",
        """
        env = {
            "ok": False,
            "backend": "fake-fail",
            "op": payload.get("op"),
            "matches": [],
            "paths": [],
            "visited": 0,
            "files_skipped_size": 0,
            "files_skipped_binary": 0,
            "dirs_pruned": 0,
            "elapsed_ms": 0,
            "truncated_reason": None,
            "error": {"code": "bad_pattern", "message": "invalid regex"},
        }
        sys.stdout.write(json.dumps(env))
        sys.exit(2)
        """,
    )


def _silent_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that exits non-zero with no stdout."""
    return _write_fake_sidecar(
        tmp_path,
        "silent.py",
        """
        sys.stderr.write("kaboom\\n")
        sys.exit(7)
        """,
    )


def _garbage_sidecar(tmp_path: Path) -> Path:
    """Fake sidecar that prints non-JSON on stdout."""
    return _write_fake_sidecar(
        tmp_path,
        "garbage.py",
        """
        sys.stdout.write("not json at all")
        """,
    )


# ---------------------------------------------------------------------------
# SidecarAdapter (the JSON/subprocess client)
# ---------------------------------------------------------------------------


class TestSidecarAdapter:
    def test_requires_explicit_configuration(self, tmp_path: Path) -> None:
        adapter = SidecarAdapter()
        assert not adapter.available()
        with pytest.raises(SidecarError, match="not configured") as exc:
            adapter.call(
                SidecarRequest(
                    op="grep",
                    root=str(tmp_path),
                    path=str(tmp_path),
                    pattern="needle",
                    max_results=1,
                    max_visited=1,
                    walltime_ms=1000,
                    max_file_bytes=1024,
                    exclude_dirs=(),
                )
            )
        assert exc.value.code == "not_configured"
        assert exc.value.op == "grep"

    def test_canonical_env_var_resolves_binary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(sidecar))
        adapter = SidecarAdapter()
        assert adapter.available()

    def test_legacy_env_var_still_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_SEARCH_SIDECAR", str(sidecar))
        adapter = SidecarAdapter()
        assert adapter.available()

    def test_canonical_env_var_wins_over_legacy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        primary = _echo_grep_sidecar(tmp_path)
        # Legacy env points at a path that does not exist so we know which
        # one was actually picked.
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(primary))
        monkeypatch.setenv("LINGTAI_SEARCH_SIDECAR", str(tmp_path / "ghost"))
        adapter = SidecarAdapter()
        assert adapter._resolve_binary() == str(primary)

    def test_explicit_binary_path_overrides_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(tmp_path / "ghost"))
        adapter = SidecarAdapter(binary_path=str(env_binary))
        assert adapter._resolve_binary() == str(env_binary)

    def test_call_returns_envelope_dict(self, tmp_path: Path) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        adapter = SidecarAdapter(binary_path=str(sidecar))
        envelope = adapter.call(
            SidecarRequest(
                op="grep",
                root=str(tmp_path),
                path=str(tmp_path),
                pattern="needle",
                max_results=5,
                max_visited=10,
                walltime_ms=2_000,
                max_file_bytes=1_024,
                exclude_dirs=("ignored",),
            )
        )
        assert envelope["ok"] is True
        echoed = json.loads(envelope["matches"][0]["line"])
        assert echoed == {
            "op": "grep",
            "root": str(tmp_path),
            "path": str(tmp_path),
            "pattern": "needle",
            "max_results": 5,
            "max_visited": 10,
            "walltime_ms": 2_000,
            "max_file_bytes": 1_024,
            "exclude_dirs": ["ignored"],
        }

    def test_structured_error_envelope_raises_sidecar_error(self, tmp_path: Path) -> None:
        sidecar = _failing_sidecar(tmp_path)
        adapter = SidecarAdapter(binary_path=str(sidecar))
        with pytest.raises(SidecarError) as exc:
            adapter.call(
                SidecarRequest(
                    op="grep",
                    root=str(tmp_path),
                    path=str(tmp_path),
                    pattern="(",
                    max_results=1,
                    max_visited=1,
                    walltime_ms=1_000,
                    max_file_bytes=1,
                    exclude_dirs=(),
                )
            )
        assert "invalid regex" in str(exc.value)
        assert exc.value.code == "bad_pattern"
        assert exc.value.exit_code == 2
        assert exc.value.op == "grep"

    def test_silent_non_zero_exit_raises_with_stderr(self, tmp_path: Path) -> None:
        sidecar = _silent_sidecar(tmp_path)
        adapter = SidecarAdapter(binary_path=str(sidecar))
        with pytest.raises(SidecarError) as exc:
            adapter.call(
                SidecarRequest(
                    op="grep",
                    root=str(tmp_path),
                    path=str(tmp_path),
                    pattern="x",
                    max_results=1,
                    max_visited=1,
                    walltime_ms=1_000,
                    max_file_bytes=1,
                    exclude_dirs=(),
                )
            )
        assert exc.value.code == "empty_output"
        assert "kaboom" in str(exc.value)
        assert exc.value.exit_code == 7

    def test_garbage_stdout_raises_bad_json(self, tmp_path: Path) -> None:
        sidecar = _garbage_sidecar(tmp_path)
        adapter = SidecarAdapter(binary_path=str(sidecar))
        with pytest.raises(SidecarError) as exc:
            adapter.call(
                SidecarRequest(
                    op="grep",
                    root=str(tmp_path),
                    path=str(tmp_path),
                    pattern="x",
                    max_results=1,
                    max_visited=1,
                    walltime_ms=1_000,
                    max_file_bytes=1,
                    exclude_dirs=(),
                )
            )
        assert exc.value.code == "bad_json"


# ---------------------------------------------------------------------------
# RustFileIOBackend — composes LocalFileIOBackend + sidecar
# ---------------------------------------------------------------------------


class TestRustFileIOBackend:
    def test_read_write_edit_use_local_backend(self, tmp_path: Path) -> None:
        backend = RustFileIOBackend(
            root=tmp_path,
            binary_path=str(_echo_grep_sidecar(tmp_path)),
        )
        backend.write("hello.txt", "Hello, world!")
        assert backend.read("hello.txt") == "Hello, world!"
        updated = backend.edit("hello.txt", "Hello", "Goodbye")
        assert updated == "Goodbye, world!"
        assert backend.read("hello.txt") == "Goodbye, world!"

    def test_grep_routes_through_sidecar_with_full_request(self, tmp_path: Path) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))

        matches = backend.grep("needle", max_results=7)
        assert len(matches) == 1
        echoed = json.loads(matches[0].line)
        # Path must be the absolute sandbox-relative join the protocol
        # demands; sidecar returns "echoed.txt", backend rejoins on root.
        expected_root = str(tmp_path.resolve())
        assert echoed["op"] == "grep"
        assert echoed["root"] == expected_root
        assert echoed["path"] == expected_root
        assert echoed["pattern"] == "needle"
        assert echoed["max_results"] == 7
        assert echoed["max_visited"] == DEFAULT_MAX_VISITED
        assert echoed["walltime_ms"] == int(round(DEFAULT_WALLTIME_S * 1000))
        assert echoed["max_file_bytes"] == DEFAULT_MAX_FILE_BYTES
        assert echoed["exclude_dirs"] == sorted(DEFAULT_EXCLUDED_DIRS)
        # Sidecar's relative match path got rejoined onto the sandbox.
        assert matches[0].path == str(Path(expected_root) / "echoed.txt")
        assert matches[0].line_number == 1

    def test_grep_relative_path_is_resolved_under_backend_root(self, tmp_path: Path) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))

        matches = backend.grep("needle", path="src", max_results=7)
        echoed = json.loads(matches[0].line)
        assert echoed["root"] == str(tmp_path.resolve())
        assert echoed["path"] == str((tmp_path / "src").resolve())

    def test_grep_passes_explicit_overrides(self, tmp_path: Path) -> None:
        sidecar = _echo_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))

        matches = backend.grep(
            "needle",
            max_results=3,
            exclude_dirs={"only-this"},
            walltime_s=0.25,
            max_visited=11,
            max_file_bytes=99,
        )
        echoed = json.loads(matches[0].line)
        assert echoed["exclude_dirs"] == ["only-this"]
        assert echoed["walltime_ms"] == 250
        assert echoed["max_visited"] == 11
        assert echoed["max_file_bytes"] == 99
        assert echoed["max_results"] == 3

    def test_grep_glob_filter_post_filters_basenames(self, tmp_path: Path) -> None:
        # The sidecar is glob-agnostic and returns both a .py and a .log
        # match; the backend must drop the .log when glob_filter="*.py".
        sidecar = _multi_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))

        matches = backend.grep("needle", glob_filter="*.py")
        names = [Path(m.path).name for m in matches]
        assert names == ["good.py"]

    def test_grep_glob_filter_star_is_noop(self, tmp_path: Path) -> None:
        sidecar = _multi_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))

        assert len(backend.grep("needle", glob_filter="*")) == 2
        assert len(backend.grep("needle", glob_filter=None)) == 2

    def test_grep_populates_last_traversal_from_envelope(self, tmp_path: Path) -> None:
        sidecar = _write_fake_sidecar(
            tmp_path,
            "rich.py",
            """
            env = {
                "ok": True,
                "backend": "rich",
                "op": "grep",
                "matches": [],
                "paths": [],
                "visited": 99,
                "files_skipped_size": 4,
                "files_skipped_binary": 5,
                "dirs_pruned": 6,
                "elapsed_ms": 77,
                "truncated_reason": "walltime",
                "error": None,
            }
            sys.stdout.write(json.dumps(env))
            """,
        )
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))
        backend.grep("needle")
        stats = backend.last_traversal
        assert isinstance(stats, TraversalStats)
        assert stats.visited == 99
        assert stats.files_skipped_size == 4
        assert stats.files_skipped_binary == 5
        assert stats.dirs_pruned == 6
        assert stats.elapsed_ms == 77
        assert stats.truncated_reason == "walltime"

    def test_glob_uses_sidecar_paths_and_sorts(self, tmp_path: Path) -> None:
        sidecar = _glob_sidecar(tmp_path)
        backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))
        results = backend.glob("**/*.py")
        # Sidecar emits two paths in unsorted order; backend sorts.
        assert results == ["/abs/a.py", "/abs/b.py"]
        stats = backend.last_traversal
        assert stats.truncated_reason == "max_results"
        assert stats.dirs_pruned == 3

    def test_grep_raises_loudly_on_sidecar_error(self, tmp_path: Path) -> None:
        backend = RustFileIOBackend(
            root=tmp_path, binary_path=str(_failing_sidecar(tmp_path))
        )
        with pytest.raises(SidecarError) as exc:
            backend.grep("(")
        # No silent Python fallback — the explicit backend must fail loudly.
        assert exc.value.code == "bad_pattern"

    def test_grep_raises_when_binary_unconfigured(self, tmp_path: Path) -> None:
        backend = RustFileIOBackend(root=tmp_path)  # no binary
        with pytest.raises(SidecarError, match="not configured"):
            backend.grep("anything")

    def test_path_outside_root_is_used_as_its_own_sandbox(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sidecar = _echo_grep_sidecar(tmp_path)
        backend = RustFileIOBackend(root=sandbox, binary_path=str(sidecar))

        matches = backend.grep("x", path=str(outside))
        echoed = json.loads(matches[0].line)
        # When path is outside the configured root, both root and path
        # collapse to the caller-provided target; we never let the sidecar
        # see a path that escapes its sandbox.
        assert echoed["root"] == str(outside.resolve())
        assert echoed["path"] == str(outside.resolve())


# ---------------------------------------------------------------------------
# LocalFileIOService composition — should still work with RustFileIOBackend.
# ---------------------------------------------------------------------------


def test_local_file_io_service_can_use_rust_backend(tmp_path: Path) -> None:
    sidecar = _glob_sidecar(tmp_path)
    backend = RustFileIOBackend(root=tmp_path, binary_path=str(sidecar))
    svc = LocalFileIOService(backend=backend)
    results = svc.glob("**/*.py")
    assert results == ["/abs/a.py", "/abs/b.py"]
    assert svc.last_traversal.truncated_reason == "max_results"


# ---------------------------------------------------------------------------
# Default LocalFileIOService stays untouched
# ---------------------------------------------------------------------------


def test_default_local_file_io_service_does_not_touch_sidecar(tmp_path: Path) -> None:
    """LocalFileIOService default must remain pure-Python — never invokes a sidecar."""
    svc = LocalFileIOService(root=tmp_path)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    # If this call somehow went through the sidecar, we'd see a SidecarError;
    # it must succeed using the in-process Python backend.
    assert svc.grep("needle")[0].line == "needle"


# ---------------------------------------------------------------------------
# Cargo-gated integration roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("cargo") is None, reason="Rust toolchain is optional")
def test_rust_sidecar_integration_grep_and_glob(tmp_path: Path) -> None:
    crate = Path(__file__).resolve().parents[1] / "crates" / "lingtai-search-sidecar"
    subprocess.run(["cargo", "build", "--quiet"], cwd=crate, check=True, timeout=300)
    binary = crate / "target" / "debug" / (
        "lingtai-search-sidecar.exe" if os.name == "nt" else "lingtai-search-sidecar"
    )

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "src").mkdir()
    (sandbox / "src" / "a.py").write_text("alpha\nneedle one\n", encoding="utf-8")
    (sandbox / "src" / "b.txt").write_text("needle two\n", encoding="utf-8")
    # Binary file with a NUL — must be skipped.
    (sandbox / "src" / "bin.dat").write_bytes(b"needle\x00hidden")
    # Excluded dir must be pruned.
    (sandbox / ".git").mkdir()
    (sandbox / ".git" / "hidden.txt").write_text("needle hidden\n", encoding="utf-8")

    backend = RustFileIOBackend(root=sandbox, binary_path=str(binary))
    matches = backend.grep("needle")
    matched = sorted((Path(m.path).name, m.line_number, m.line) for m in matches)
    assert matched == [
        ("a.py", 2, "needle one"),
        ("b.txt", 1, "needle two"),
    ]
    assert backend.last_traversal.dirs_pruned >= 1
    assert backend.last_traversal.files_skipped_binary >= 1

    paths = backend.glob("**/*.py")
    assert any(Path(p).name == "a.py" for p in paths)
    assert not any(".git" in p for p in paths)


# ---------------------------------------------------------------------------
# Sidecar binary resolver — priority order across env, packaged, dev tree
# ---------------------------------------------------------------------------


from lingtai.services import file_io_sidecar as _sidecar_mod
from lingtai.services.file_io_sidecar import (
    BACKEND_ENV_VAR,
    default_file_io_service,
    resolve_sidecar_binary,
)
from lingtai.services.file_io import LocalFileIOBackend, LocalFileIOService


class TestResolveSidecarBinary:
    """Priority: explicit > env (canonical > legacy) > packaged > dev tree."""

    def test_explicit_path_wins_over_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = _echo_grep_sidecar(tmp_path)
        # Make sure env and packaged sources would otherwise resolve.
        env_binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(env_binary))
        # Even with the env set, an explicit path takes precedence.
        assert resolve_sidecar_binary(str(explicit)) == str(explicit)

    def test_explicit_falls_through_when_path_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit path that doesn't exist *and* isn't on PATH should resolve
        # to None; we never silently dip into env / packaged when the operator
        # explicitly named a binary.
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(_echo_grep_sidecar(tmp_path)))
        ghost = tmp_path / "nope-not-here"
        assert resolve_sidecar_binary(str(ghost)) is None

    def test_explicit_non_executable_file_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LINGTAI_FILE_IO_SIDECAR", raising=False)
        regular = tmp_path / "regular-file"
        regular.write_text("not executable", encoding="utf-8")
        regular.chmod(0o644)
        assert resolve_sidecar_binary(str(regular)) is None

    def test_canonical_env_var_wins_over_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        primary = _echo_grep_sidecar(tmp_path)
        legacy = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(primary))
        monkeypatch.setenv("LINGTAI_SEARCH_SIDECAR", str(legacy))
        assert resolve_sidecar_binary() == str(primary)

    def test_legacy_env_var_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_SEARCH_SIDECAR", str(legacy))
        assert resolve_sidecar_binary() == str(legacy)

    def test_env_non_executable_file_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        regular = tmp_path / "regular-file"
        regular.write_text("not executable", encoding="utf-8")
        regular.chmod(0o644)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(regular))
        assert resolve_sidecar_binary(skip_packaged=True, skip_dev_tree=True) is None

    def test_packaged_binary_used_when_no_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate a wheel-installed binary at ``lingtai/bin/…`` by
        # monkey-patching ``_packaged_binary`` directly. Spinning up a
        # real lingtai/bin/ in the source tree would pollute the dev
        # checkout for other tests.
        fake = _echo_grep_sidecar(tmp_path)
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: str(fake))
        # Block the dev-tree scan so we know the packaged one is what
        # got picked up.
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        assert resolve_sidecar_binary() == str(fake)

    def test_dev_tree_used_when_packaged_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dev = _echo_grep_sidecar(tmp_path)
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: None)
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: str(dev))
        assert resolve_sidecar_binary() == str(dev)

    def test_packaged_wins_over_dev_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        packaged = _echo_grep_sidecar(tmp_path)
        dev = _echo_grep_sidecar(tmp_path)
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: str(packaged))
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: str(dev))
        assert resolve_sidecar_binary() == str(packaged)

    def test_env_wins_over_packaged_and_dev_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env_bin = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(env_bin))
        # Pretend later sources would have answered too — env must still win.
        monkeypatch.setattr(
            _sidecar_mod, "_packaged_binary", lambda: str(_echo_grep_sidecar(tmp_path))
        )
        monkeypatch.setattr(
            _sidecar_mod, "_dev_tree_binary", lambda: str(_echo_grep_sidecar(tmp_path))
        )
        assert resolve_sidecar_binary() == str(env_bin)

    def test_returns_none_when_no_source_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: None)
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        assert resolve_sidecar_binary() is None


class TestSidecarAdapterAutodiscover:
    def test_autodiscover_picks_up_packaged_binary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        packaged = _echo_grep_sidecar(tmp_path)
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: str(packaged))
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        adapter = SidecarAdapter.autodiscover()
        assert adapter.available()
        assert adapter._resolve_binary() == str(packaged)

    def test_strict_constructor_ignores_packaged_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``SidecarAdapter()`` (no args, no env) must stay strict — the
        # existing test suite + opt-in API depends on it returning
        # ``not_configured`` when no env var is set. A packaged binary
        # being on disk is irrelevant to this construction mode.
        packaged = _echo_grep_sidecar(tmp_path)
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: str(packaged))
        adapter = SidecarAdapter()
        assert not adapter.available()


# ---------------------------------------------------------------------------
# default_file_io_service factory + LINGTAI_FILE_IO_BACKEND env knob
# ---------------------------------------------------------------------------


class TestDefaultFileIOService:
    def test_auto_uses_rust_when_available(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(binary))
        svc = default_file_io_service(root=tmp_path)
        assert isinstance(svc, LocalFileIOService)
        # The service is backed by ``RustFileIOBackend``, not the pure-Python
        # default. The cleanest way to confirm that without poking at private
        # state is to call grep — the echo sidecar returns one match no
        # matter what's on disk.
        results = svc.grep("anything")
        assert len(results) == 1

    def test_auto_falls_back_to_python_when_no_binary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: None)
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        svc = default_file_io_service(root=tmp_path)
        # No env, no packaged, no dev tree → pure Python fallback. The
        # service should work on a real file written via its own ``write``.
        svc.write("hello.txt", "needle\n")
        results = svc.grep("needle")
        assert len(results) == 1
        assert results[0].line == "needle"

    def test_rust_env_raises_when_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(BACKEND_ENV_VAR, "rust")
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: None)
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        with pytest.raises(SidecarError) as exc:
            default_file_io_service(root=tmp_path)
        assert exc.value.code == "not_configured"

    def test_python_env_forces_pure_python_even_when_rust_available(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A working binary on disk would normally be picked up by auto.
        binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(binary))
        monkeypatch.setenv(BACKEND_ENV_VAR, "python")
        svc = default_file_io_service(root=tmp_path)
        # Real file ops route through ``LocalFileIOBackend``, not the
        # echo sidecar — the fake binary would have returned a fixed
        # match for any pattern, but the Python backend will report
        # zero matches because the file doesn't exist yet.
        results = svc.grep("anything")
        assert results == []

    def test_explicit_backend_arg_overrides_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Env says rust, explicit arg says python — arg wins.
        binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(binary))
        monkeypatch.setenv(BACKEND_ENV_VAR, "rust")
        svc = default_file_io_service(root=tmp_path, backend="python")
        # The python path doesn't shell out, so write/read/grep on a real
        # file should work cleanly.
        svc.write("a.txt", "needle\n")
        assert svc.grep("needle")[0].line == "needle"

    def test_unknown_backend_value_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(BACKEND_ENV_VAR, "magic")
        with pytest.raises(ValueError, match="Unknown LINGTAI_FILE_IO_BACKEND"):
            default_file_io_service(root=tmp_path)


# ---------------------------------------------------------------------------
# Agent default wiring — confirms the agent path honors the env knob
# ---------------------------------------------------------------------------


class TestAgentDefaultBackend:
    """``Agent.__init__`` should call ``default_file_io_service`` for the
    auto-created file_io. These tests cover the seam without booting a
    real LLM service — the goal is to confirm the right factory is wired,
    not to drive a full agent through its message loop."""

    def _make_agent(self, tmp_path: Path):
        from unittest.mock import MagicMock
        from lingtai.agent import Agent

        # Minimal mock LLM service — Agent only needs ``provider`` /
        # ``model`` / ``get_adapter`` to construct cleanly.
        svc = MagicMock()
        svc.provider = "gemini"
        svc.model = "test-model"
        svc.get_adapter.return_value = MagicMock()
        return Agent(service=svc, agent_name="t", working_dir=tmp_path / "t")

    def test_agent_uses_python_backend_when_env_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if a binary is on disk, ``python`` wins.
        binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(binary))
        monkeypatch.setenv(BACKEND_ENV_VAR, "python")
        agent = self._make_agent(tmp_path)
        # The service uses the pure-Python backend — writing then grepping
        # a real file works; an echo sidecar would have produced a phantom
        # match for any pattern.
        agent._file_io.write("a.txt", "needle\n")
        results = agent._file_io.grep("needle")
        assert len(results) == 1
        assert results[0].line == "needle"

    def test_agent_uses_rust_backend_when_env_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = _echo_grep_sidecar(tmp_path)
        monkeypatch.setenv("LINGTAI_FILE_IO_SIDECAR", str(binary))
        monkeypatch.setenv(BACKEND_ENV_VAR, "rust")
        agent = self._make_agent(tmp_path)
        # The echo sidecar always returns one match — that's how we
        # know the Rust backend is in play.
        results = agent._file_io.grep("anything")
        assert len(results) == 1

    def test_agent_falls_back_to_python_when_rust_unavailable_in_auto(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``auto`` with nothing to resolve → pure Python, no error.
        monkeypatch.setattr(_sidecar_mod, "_packaged_binary", lambda: None)
        monkeypatch.setattr(_sidecar_mod, "_dev_tree_binary", lambda: None)
        agent = self._make_agent(tmp_path)
        agent._file_io.write("a.txt", "needle\n")
        assert agent._file_io.grep("needle")[0].line == "needle"
