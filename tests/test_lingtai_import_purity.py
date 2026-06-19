"""The ``lingtai`` import root must stay import-light: a bare ``import lingtai``
pulls only the dependency-light kernel, never the wrapper's agent / capabilities
/ services layer nor any heavy provider SDK. Wrapper-backed names resolve lazily
via :pep:`562` and must resolve to the SAME object the wrapper submodule exports.

Each check runs in a fresh subprocess so module-import state is pristine and the
lazy ``__getattr__`` caching does not leak between assertions.

Note on the provider list: importing the kernel loads the *bare* ``google``
namespace package (an ambient site-packages artifact pulled in transitively by
``filelock``; ``google.__file__ is None``). That stub is harmless and is NOT a
provider SDK, so we target the heavy provider *submodules*
(``google.genai`` / ``google.generativeai``) rather than bare ``google``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Heavy provider SDKs that must NOT be loaded by a bare ``import lingtai``.
# Bare ``google`` is intentionally excluded (ambient namespace stub); only the
# real Google AI SDK submodules count.
_HEAVY_PROVIDERS = (
    "anthropic",
    "openai",
    "google.genai",
    "google.generativeai",
    "mcp",
    "trafilatura",
    "ddgs",
)

# Wrapper submodules that carry the agent / capabilities / services layer. A
# bare ``import lingtai`` must not eagerly load any of these.
_WRAPPER_SUBMODULES = (
    "lingtai.agent",
    "lingtai.capabilities",
    "lingtai.core",
    "lingtai.llm",
    "lingtai.services",
)


def _run(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


_PROVIDERS_LITERAL = repr(_HEAVY_PROVIDERS)
_WRAPPER_LITERAL = repr(_WRAPPER_SUBMODULES)


def test_import_lingtai_does_not_load_wrapper_or_providers():
    code = (
        "import sys, lingtai\n"
        f"providers = {_PROVIDERS_LITERAL}\n"
        f"wrapper = {_WRAPPER_LITERAL}\n"
        "bad = [m for m in sys.modules "
        "if any(m == w or m.startswith(w + '.') for w in wrapper)]\n"
        "bad += [m for m in sys.modules "
        "if any(m == p or m.startswith(p + '.') for p in providers)]\n"
        "assert not bad, bad\n"
        "assert hasattr(lingtai, 'BaseAgent')\n"
        "assert lingtai.__version__\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_touching_kernel_names_stays_clean():
    code = (
        "import sys, lingtai\n"
        "_ = (lingtai.BaseAgent, lingtai.AgentState, lingtai.AgentConfig,\n"
        "     lingtai.Message, lingtai.MSG_REQUEST, lingtai.UnknownToolError,\n"
        "     lingtai.EmailManager, lingtai.MailService, lingtai.FilesystemMailService,\n"
        "     lingtai.LoggingService, lingtai.JSONLLoggingService)\n"
        f"wrapper = {_WRAPPER_LITERAL}\n"
        "bad = [m for m in sys.modules "
        "if any(m == w or m.startswith(w + '.') for w in wrapper)]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_lazy_names_resolve_to_wrapper_objects():
    # Each lazy name must be the SAME object its wrapper submodule exports, so
    # ``from lingtai import Agent`` and ``from lingtai.agent import Agent`` agree.
    code = (
        "import lingtai\n"
        "import lingtai.agent, lingtai.capabilities, lingtai.core.bash\n"
        "import lingtai.services.vision, lingtai.services.websearch, lingtai.services.file_io\n"
        "assert lingtai.Agent is lingtai.agent.Agent\n"
        "assert lingtai.setup_capability is lingtai.capabilities.setup_capability\n"
        "assert lingtai.BashManager is lingtai.core.bash.BashManager\n"
        "assert lingtai.VisionService is lingtai.services.vision.VisionService\n"
        "assert lingtai.SearchService is lingtai.services.websearch.SearchService\n"
        "assert lingtai.FileIOService is lingtai.services.file_io.FileIOService\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_lazy_access_caches_in_module_globals():
    # After first access the name is cached in module globals, so subsequent
    # access returns the same object without re-entering ``__getattr__``.
    code = (
        "import lingtai\n"
        "first = lingtai.Agent\n"
        "assert 'Agent' in vars(lingtai), 'lazy name not cached in globals'\n"
        "assert lingtai.Agent is first\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_unknown_attribute_raises_attribute_error():
    code = (
        "import lingtai\n"
        "try:\n"
        "    lingtai.NoSuchName\n"
        "except AttributeError:\n"
        "    print('OK')\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
