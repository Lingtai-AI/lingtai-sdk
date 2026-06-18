"""lingtai_sdk must stay import-light: a bare import pulls the dependency-light
kernel only, never the ``lingtai`` wrapper nor any heavy provider SDK. Wrapper-
backed names resolve lazily and must resolve to the SAME object the wrapper
exports.

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

# Heavy provider SDKs that must NOT be loaded by a bare ``import lingtai_sdk``.
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


def test_import_sdk_does_not_load_wrapper_or_providers():
    code = (
        "import sys, lingtai_sdk\n"
        f"providers = {_PROVIDERS_LITERAL}\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "bad += [m for m in sys.modules "
        "if any(m == p or m.startswith(p + '.') for p in providers)]\n"
        "assert not bad, bad\n"
        "assert hasattr(lingtai_sdk, 'BaseAgent')\n"
        "assert lingtai_sdk.__version__\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_touching_kernel_names_stays_clean():
    code = (
        "import sys, lingtai_sdk\n"
        "_ = (lingtai_sdk.BaseAgent, lingtai_sdk.AgentState, lingtai_sdk.AgentConfig,\n"
        "     lingtai_sdk.Message, lingtai_sdk.UnknownToolError, lingtai_sdk.LLMService,\n"
        "     lingtai_sdk.LingTaiSDKError)\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_lazy_agent_resolves_to_wrapper_object():
    code = (
        "import lingtai_sdk, lingtai\n"
        "assert lingtai_sdk.Agent is lingtai.Agent, 'lazy Agent forked from wrapper'\n"
        "assert lingtai_sdk.VisionService is lingtai.VisionService\n"
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_unknown_attribute_raises_attribute_error():
    code = (
        "import lingtai_sdk\n"
        "try:\n"
        "    lingtai_sdk.NoSuchName\n"
        "except AttributeError:\n"
        "    print('OK')\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
