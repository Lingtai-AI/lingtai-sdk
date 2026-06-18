# SDK Architecture Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Establish a small, reviewable foundation for the LingTai kernel → SDK split: a lightweight import-pure `lingtai_sdk` public doorway, a runtime-contract seed, a CapabilityBundle manifest seed with a low-risk proof bundle, compatibility/import-purity tests, anatomy, and architecture docs — without changing any existing `lingtai`/`lingtai_kernel` runtime or CLI behavior.

**Architecture:** A new top-level package `src/lingtai_sdk/` is the curated public front door. It eagerly imports only the zero-dependency kernel and kernel-free leaf modules; wrapper-backed names (`Agent`, services) resolve lazily via PEP 562 `__getattr__`. Public DTOs (runtime contract, capability-bundle manifest) live in the SDK; native/privileged implementations stay in the kernel/wrapper (Jason decision #2). This PR ships the *contracts and the doorway*, not a live runtime or a core-bundle migration.

**Tech Stack:** Python 3.11+ (`from __future__ import annotations`), dataclasses, stdlib only (no new third-party deps), pytest, subprocess-based import-purity tests.

## Global Constraints

- Do NOT touch the primary checkout or any other worktree. Work only in `.worktrees/sdk-architecture-foundation-20260617`.
- Do NOT push, open a PR, merge, or write external comments. Local commits only.
- Preserve existing `lingtai` / `lingtai_kernel` imports and runtime/CLI behavior — verify with smoke tests.
- Kernel must never import from `lingtai`. The SDK doorway imports the kernel eagerly and the wrapper lazily.
- No new third-party dependencies. No `[project.optional-dependencies]`.
- `import lingtai_sdk` must NOT load the `lingtai` wrapper or any provider SDK (anthropic/openai/google-genai/mcp). Enforced by a subprocess test.
- Do NOT migrate core `system`/`psyche`/`soul` bundles. The proof bundle is metadata-only and synthetic/harmless.
- Do NOT implement an Anthropic backend. Document it as a later PR.
- Every new folder gets an `ANATOMY.md` per the repo's anatomy convention (~80 lines cap, 6-section template). Update the kernel-root anatomy Composition only if a kernel folder is added.
- Run tests with `PYTHONPATH=src` (the editable install resolves to a *different* worktree; pytest's `pythonpath=["src"]` handles in-process tests, and subprocess tests must set `PYTHONPATH=src` + `cwd=repo root`).

---

## File Structure

New package `src/lingtai_sdk/` (public DTOs + doorway, eager-kernel/lazy-wrapper):
- `__init__.py` — curated public surface; eager kernel re-exports + PEP 562 lazy wrapper names + `__version__`.
- `_version.py` — best-effort version resolution (try `lingtai` metadata, fallback `0+unknown`).
- `types.py` — re-export kernel config/state/message/LLM-protocol types (kernel-only, cheap).
- `errors.py` — re-export kernel `UnknownToolError` + define SDK base error.
- `_compat.py` — machine-readable migration map (legacy path → SDK path), drives docs + round-trip test.
- `runtime.py` — runtime contract seed: `RuntimeState`, `RuntimeOptions`, `RuntimeMessage`, `EventKind`, `RuntimeEvent`, `Runtime`/`RuntimeSession` protocols. Pure DTOs/protocols, no kernel import.
- `capabilities.py` — CapabilityBundle manifest seed: role flags, surfaces, security, transport DTOs + `BundleManifest` + validation + a proof bundle factory.
- `ANATOMY.md` — 6-section anatomy of the package.

Tests (`tests/`):
- `test_sdk_import_purity.py` — subprocess: `import lingtai_sdk` loads neither wrapper nor provider SDKs; kernel-backed names stay clean; lazy `Agent` resolves to the same object as `lingtai.Agent`.
- `test_sdk_compat.py` — every active alias in the migration map resolves to the same object on both legacy and SDK paths.
- `test_sdk_runtime_contract.py` — runtime DTO construction, event constructors, options round-trip, protocol shape.
- `test_sdk_capabilities.py` — bundle manifest construction, role-flag invariants, surface DTOs, proof bundle validity, serialization round-trip.

Docs (`docs/` — gitignored by default; force-add the architecture doc with rationale, OR place under `reports/`. Decision: place the durable architecture doc at `docs/sdk/architecture-foundation.md` and force-add with rationale, since it is a long-lived design doc):
- `docs/sdk/architecture-foundation.md` — SDK/CLI split, CapabilityBundle design, staged roadmap, what's deferred.

Report (`reports/sdk-architecture-foundation-20260617/` — gitignored; force-add with rationale):
- `implementation-report.md` — final report.

---

### Task 1: SDK package skeleton — version, doorway, types, errors

**Files:**
- Create: `src/lingtai_sdk/_version.py`
- Create: `src/lingtai_sdk/types.py`
- Create: `src/lingtai_sdk/errors.py`
- Create: `src/lingtai_sdk/__init__.py`
- Test: `tests/test_sdk_import_purity.py`

**Interfaces:**
- Produces: package `lingtai_sdk` with eager names `BaseAgent`, `AgentConfig`, `AgentState`, `Message`, `MSG_REQUEST`, `MSG_USER_INPUT`, `ChatSession`, `FunctionSchema`, `LLMResponse`, `LLMService`, `ToolCall`, `UnknownToolError`, `LingTaiSDKError`, `__version__`; lazy names `Agent`, `FileIOService`, `MailService`, `LoggingService`, `SearchService`, `VisionService`.

- [ ] **Step 1: Write the failing import-purity test** at `tests/test_sdk_import_purity.py`.

```python
"""lingtai_sdk must stay import-light: a bare import pulls the zero-dep kernel
only, never the `lingtai` wrapper nor any provider SDK. Wrapper-backed names
resolve lazily and must resolve to the SAME object as the wrapper exports."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def _run(code: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(SRC)}
    import os
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
    )


def test_import_sdk_does_not_load_wrapper_or_providers():
    code = (
        "import sys, lingtai_sdk\n"
        "bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]\n"
        "providers = ['anthropic','openai','google','mcp','trafilatura','ddgs']\n"
        "bad += [m for m in sys.modules if any(m == p or m.startswith(p+'.') for p in providers)]\n"
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
        "     lingtai_sdk.Message, lingtai_sdk.UnknownToolError, lingtai_sdk.LLMService)\n"
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
        "print('OK')\n"
    )
    r = _run(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_import_purity.py -v`
Expected: FAIL — `lingtai_sdk` does not exist yet (ModuleNotFoundError in subprocess → assert on returncode fails).

- [ ] **Step 3: Create `src/lingtai_sdk/_version.py`**

```python
"""Best-effort version resolution for the SDK doorway.

The SDK ships inside the same wheel as ``lingtai`` today (Jason decision #1:
add the package now, rename the distribution later), so its version tracks the
``lingtai`` distribution metadata. Resolving via ``importlib.metadata`` keeps
``import lingtai_sdk`` dependency-free; if metadata is unavailable (e.g. running
straight from a source checkout that was never installed) we fall back to a
sentinel rather than raising at import time."""
from __future__ import annotations


def _resolve_version() -> str:
    try:
        from importlib.metadata import version

        return version("lingtai")
    except Exception:  # noqa: BLE001 - never break import over version metadata
        return "0+unknown"


__version__ = _resolve_version()
```

- [ ] **Step 4: Create `src/lingtai_sdk/errors.py`**

```python
"""SDK error surface.

A single SDK base error plus a re-export of the kernel's ``UnknownToolError``.
Kept in a leaf module with no heavy imports so ``import lingtai_sdk`` stays
cheap. Specific SDK error subclasses are added as the live runtime lands in a
later PR; this PR only needs the stable base and the kernel re-export."""
from __future__ import annotations

from lingtai_kernel.types import UnknownToolError


class LingTaiSDKError(Exception):
    """Base class for all SDK-level errors."""


__all__ = ["LingTaiSDKError", "UnknownToolError"]
```

- [ ] **Step 5: Create `src/lingtai_sdk/types.py`**

```python
"""Public type re-exports.

These names already live in the zero-dependency kernel; the SDK re-exports them
under a stable public path so consumers depend on ``lingtai_sdk.types`` rather
than reaching into kernel internals. Importing this module pulls only the
kernel (cheap, side-effect-free)."""
from __future__ import annotations

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.state import AgentState
from lingtai_kernel.message import Message, MSG_REQUEST, MSG_USER_INPUT
from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    ToolCall,
)
from lingtai_kernel.llm.service import LLMService

__all__ = [
    "AgentConfig",
    "AgentState",
    "Message",
    "MSG_REQUEST",
    "MSG_USER_INPUT",
    "ChatSession",
    "FunctionSchema",
    "LLMResponse",
    "ToolCall",
    "LLMService",
]
```

NOTE: verify the exact import paths for `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall` (`lingtai_kernel.llm.base`) and `LLMService` (`lingtai_kernel.llm.service`) against the actual kernel before finalizing — adjust if the module layout differs. If `lingtai_kernel.llm.base` imports provider SDKs at module load, drop the LLM-protocol re-exports from `types.py` to preserve import purity and document that in the anatomy.

- [ ] **Step 6: Create `src/lingtai_sdk/__init__.py`** (Candidate-E-style eager-kernel / lazy-wrapper doorway)

```python
"""lingtai_sdk — the public SDK doorway for building and embedding LingTai agents.

A single curated import path with a stable, typed public API that re-exports
from the two implementation packages underneath it:

- ``lingtai_kernel`` — the minimal standalone runtime (zero hard deps), and
- ``lingtai``        — the batteries-included wrapper (adapters, capabilities, CLI).

Layering and the lazy boundary
------------------------------
``lingtai_sdk`` imports only the **kernel** at module load. The kernel has zero
hard third-party dependencies, so ``import lingtai_sdk`` is as cheap and
side-effect-free as ``import lingtai_kernel`` — safe in tooling and in
environments where the wrapper's provider SDKs are not installed.

Wrapper-backed names (``Agent`` and the service classes) resolve lazily via
:pep:`562` ``__getattr__``. Touching ``lingtai_sdk.Agent`` imports ``lingtai``
on first access; if the wrapper (or its deps) is absent you get a clear
``ModuleNotFoundError`` naming ``lingtai`` rather than an import-time crash of
the whole SDK. This makes the one-directional dependency rule visible at the
package boundary: kernel names are eager, wrapper names are lazy.

This package ships **contracts and the doorway**, not a live runtime. The
runtime contract (:mod:`lingtai_sdk.runtime`) and capability-bundle manifest
(:mod:`lingtai_sdk.capabilities`) are seed DTOs; live runtimes and core-bundle
migrations land in later PRs. See ``docs/sdk/architecture-foundation.md``."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

# --- Kernel-backed surface (eager; zero third-party deps) ----------------
from lingtai_kernel.base_agent import BaseAgent
from .types import (
    AgentConfig,
    AgentState,
    ChatSession,
    FunctionSchema,
    LLMResponse,
    LLMService,
    Message,
    MSG_REQUEST,
    MSG_USER_INPUT,
    ToolCall,
)
from .errors import LingTaiSDKError, UnknownToolError

# --- Wrapper-backed surface (lazy; resolved on first attribute access) ---
_LAZY_WRAPPER_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": ("lingtai", "Agent"),
    "FileIOService": ("lingtai", "FileIOService"),
    "MailService": ("lingtai", "MailService"),
    "LoggingService": ("lingtai", "LoggingService"),
    "SearchService": ("lingtai", "SearchService"),
    "VisionService": ("lingtai", "VisionService"),
}

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lingtai import (  # noqa: F401
        Agent,
        FileIOService,
        LoggingService,
        MailService,
        SearchService,
        VisionService,
    )


def __getattr__(name: str):  # PEP 562 module-level lazy attributes
    target = _LAZY_WRAPPER_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__))


__all__ = [
    "__version__",
    # Runtime entrypoints
    "BaseAgent",  # kernel (eager)
    "Agent",  # wrapper (lazy)
    # Configuration / state / messaging
    "AgentConfig",
    "AgentState",
    "Message",
    "MSG_REQUEST",
    "MSG_USER_INPUT",
    # LLM protocol
    "ChatSession",
    "FunctionSchema",
    "LLMResponse",
    "LLMService",
    "ToolCall",
    # Errors
    "LingTaiSDKError",
    "UnknownToolError",
    # Services (wrapper-backed, lazy)
    "FileIOService",
    "MailService",
    "LoggingService",
    "SearchService",
    "VisionService",
]
```

NOTE: confirm the wrapper actually exports `SearchService` (per the explore, `lingtai/__init__.py` exports `SearchService` from `.services.websearch`) and the other service names. Drop any name from `_LAZY_WRAPPER_EXPORTS` and `__all__` that the wrapper does not export.

- [ ] **Step 7: Run import-purity tests**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_import_purity.py -v`
Expected: PASS (all three tests).

- [ ] **Step 8: Smoke-test existing behavior unchanged**

Run: `PYTHONPATH=src python -c "import lingtai, lingtai_kernel, lingtai_sdk; print('all import ok', lingtai_sdk.__version__)"`
Expected: prints `all import ok 0.12.3`.

- [ ] **Step 9: Commit**

```bash
git add src/lingtai_sdk/_version.py src/lingtai_sdk/types.py src/lingtai_sdk/errors.py src/lingtai_sdk/__init__.py tests/test_sdk_import_purity.py
git commit -m "feat(sdk): add lingtai_sdk public doorway (eager-kernel, lazy-wrapper)"
```

---

### Task 2: Compatibility / migration map + round-trip test

**Files:**
- Create: `src/lingtai_sdk/_compat.py`
- Test: `tests/test_sdk_compat.py`

**Interfaces:**
- Consumes: `lingtai_sdk` public names from Task 1.
- Produces: `lingtai_sdk._compat.DEPRECATIONS: tuple[Deprecation, ...]`, `Deprecation` dataclass with `.is_active_alias`, `active_aliases()`, `migration_for(legacy_path)`.

- [ ] **Step 1: Write the failing round-trip test** at `tests/test_sdk_compat.py`.

```python
"""Compatibility is by re-export, not re-implementation: every active legacy
import path must resolve to the SAME object the SDK exports (identity, not just
name equality), so there is no forked parallel hierarchy."""
from __future__ import annotations

import importlib

import pytest

from lingtai_sdk import _compat


def _resolve(dotted: str):
    module_path, _, attr = dotted.rpartition(".")
    mod = importlib.import_module(module_path)
    return getattr(mod, attr)


def test_migration_map_nonempty():
    assert _compat.active_aliases(), "expected at least one active alias"


@pytest.mark.parametrize("dep", _compat.active_aliases(), ids=lambda d: d.legacy)
def test_legacy_path_resolves_to_same_object(dep):
    assert _resolve(dep.legacy) is _resolve(dep.current), (
        f"{dep.legacy} and {dep.current} resolved to different objects; "
        "the compatibility re-export has forked."
    )


def test_migration_for_lookup():
    first = _compat.active_aliases()[0]
    assert _compat.migration_for(first.legacy) is first
    assert _compat.migration_for("does.not.exist") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_compat.py -v`
Expected: FAIL — `lingtai_sdk._compat` does not exist.

- [ ] **Step 3: Create `src/lingtai_sdk/_compat.py`**

```python
"""Migration map from legacy import paths to the SDK public surface.

The machine-readable contract behind the compatibility strategy: each entry
says "the name you used to import from *here* is now canonically reachable from
*there*, and both still work." It powers the migration table in the docs and a
round-trip test that asserts every legacy path resolves to the *same object*
the SDK exports — compatibility by re-export, never by a parallel fork.

No name is removed here. Repo policy is that the kernel public API is additive
within a major; this map records the recommended path without breaking the old
one. A name graduates from alias to removed only across a major bump, at which
point ``removed_in`` is filled."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Deprecation:
    legacy: str
    current: str
    symbol: str
    since: str
    removed_in: str | None = None
    note: str = ""

    @property
    def is_active_alias(self) -> bool:
        """True while the legacy path is still importable (not yet removed)."""
        return self.removed_in is None


_SDK_INTRODUCED = "0.12.3"

DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        legacy="lingtai_kernel.BaseAgent",
        current="lingtai_sdk.BaseAgent",
        symbol="BaseAgent",
        since=_SDK_INTRODUCED,
        note="Kernel coordinator. Still exported by lingtai_kernel and lingtai.",
    ),
    Deprecation(
        legacy="lingtai.Agent",
        current="lingtai_sdk.Agent",
        symbol="Agent",
        since=_SDK_INTRODUCED,
        note="Batteries-included agent. Lives in the wrapper; SDK re-exports lazily.",
    ),
    Deprecation(
        legacy="lingtai_kernel.config.AgentConfig",
        current="lingtai_sdk.types.AgentConfig",
        symbol="AgentConfig",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.state.AgentState",
        current="lingtai_sdk.types.AgentState",
        symbol="AgentState",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.message.Message",
        current="lingtai_sdk.types.Message",
        symbol="Message",
        since=_SDK_INTRODUCED,
    ),
    Deprecation(
        legacy="lingtai_kernel.types.UnknownToolError",
        current="lingtai_sdk.errors.UnknownToolError",
        symbol="UnknownToolError",
        since=_SDK_INTRODUCED,
    ),
)


def active_aliases() -> tuple[Deprecation, ...]:
    """Legacy paths that still import successfully (the common case today)."""
    return tuple(d for d in DEPRECATIONS if d.is_active_alias)


def migration_for(legacy_path: str) -> Deprecation | None:
    """Look up the recommended move for a legacy import path, if any."""
    for d in DEPRECATIONS:
        if d.legacy == legacy_path:
            return d
    return None


__all__ = ["Deprecation", "DEPRECATIONS", "active_aliases", "migration_for"]
```

NOTE: `lingtai.Agent` resolution in the test will import the wrapper — acceptable here (this is a compat test, not the purity test). Confirm each `legacy` path actually resolves (e.g. `lingtai_kernel.message.Message` is importable). Drop any entry that does not resolve rather than leave a failing parametrize case.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_sdk/_compat.py tests/test_sdk_compat.py
git commit -m "feat(sdk): add migration map + same-object compat round-trip test"
```

---

### Task 3: Runtime contract seed

**Files:**
- Create: `src/lingtai_sdk/runtime.py`
- Test: `tests/test_sdk_runtime_contract.py`

**Interfaces:**
- Produces: `RuntimeState` (enum), `EventKind` (enum), `RuntimeOptions` (dataclass), `RuntimeMessage` (dataclass), `RuntimeEvent` (dataclass w/ `.state()`, `.text()`, `.error()` classmethods), `Runtime`/`RuntimeSession` (ABCs/Protocols). Pure DTOs — no kernel or wrapper import, so importing `lingtai_sdk.runtime` stays clean.

- [ ] **Step 1: Write the failing test** at `tests/test_sdk_runtime_contract.py`.

```python
"""The runtime contract is a seed: pure DTOs + abstract protocols describing how
a future live runtime is driven (options in, messages in, events out). This PR
ships the shapes, not a live runtime — so the tests exercise construction,
convenience constructors, and that the abstract base cannot be instantiated."""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

import pytest

from lingtai_sdk import runtime as rt

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def test_runtime_options_construct_and_defaults():
    opts = rt.RuntimeOptions(working_dir="/tmp/agent")
    assert str(opts.working_dir) == "/tmp/agent"
    assert opts.agent_name is None
    assert opts.capabilities is None
    assert opts.extra == {}


def test_runtime_message_defaults_and_id():
    m = rt.RuntimeMessage(content="hello")
    assert m.content == "hello"
    assert m.sender == "user"
    assert m.id  # autogenerated


def test_runtime_event_constructors():
    e = rt.RuntimeEvent.state(rt.RuntimeState.ACTIVE, source="native")
    assert e.kind is rt.EventKind.STATE
    assert e.data["state"] == rt.RuntimeState.ACTIVE.value
    t = rt.RuntimeEvent.text("hi")
    assert t.kind is rt.EventKind.TEXT and t.data["text"] == "hi"
    err = rt.RuntimeEvent.error("boom", fatal=True)
    assert err.kind is rt.EventKind.ERROR and err.data["fatal"] is True


def test_runtime_abc_not_instantiable():
    with pytest.raises(TypeError):
        rt.Runtime()
    with pytest.raises(TypeError):
        rt.RuntimeSession()


def test_runtime_module_import_is_pure():
    code = (
        "import sys, lingtai_sdk.runtime\n"
        "bad = [m for m in sys.modules if m=='lingtai' or m.startswith('lingtai.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO_ROOT), env={**os.environ, "PYTHONPATH": str(SRC)})
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_runtime_contract.py -v`
Expected: FAIL — `lingtai_sdk.runtime` does not exist.

- [ ] **Step 3: Create `src/lingtai_sdk/runtime.py`** (Candidate-B-style contract, pure DTOs)

```python
"""Runtime contract seed.

Provider-agnostic shapes describing how a *future* live runtime is driven:
options in, messages in, a stream of events out. This PR ships the contract
only — there is no live runtime here. A thin ``NativeRuntime`` (wrapping the
existing ``Agent``) and any non-native backend (e.g. an Anthropic backend) land
in later PRs, once these shapes have stabilized. Keeping the contract as pure
dataclasses/ABCs with no kernel import means ``import lingtai_sdk.runtime`` is
free of provider deps and safe in tooling.

See ``docs/sdk/architecture-foundation.md`` for the staged roadmap."""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


class RuntimeState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    IDLE = "idle"
    ASLEEP = "asleep"
    STUCK = "stuck"
    STOPPED = "stopped"


class EventKind(str, enum.Enum):
    STATE = "state"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    NOTIFICATION = "notification"
    ERROR = "error"
    RAW = "raw"


@dataclass
class RuntimeOptions:
    """Declarative inputs for constructing a runtime session.

    A backend-neutral superset of what ``Agent``/``init.json`` consume today.
    A future ``NativeRuntime`` translates these into a kernel ``Agent``; other
    backends translate them into their own client config."""

    working_dir: str | Path
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    capabilities: list[str] | Mapping[str, dict] | None = None
    addons: list[str] | None = None
    system_prompt_overrides: Mapping[str, str] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)
    streaming: bool = False

    def for_adapter(self, adapter_id: str) -> Mapping[str, Any]:
        """Adapter-scoped extras, e.g. ``extra['adapters']['anthropic']``."""
        adapters = self.extra.get("adapters", {}) if self.extra else {}
        return adapters.get(adapter_id, {})


@dataclass
class RuntimeMessage:
    """An inbound message handed to a running session."""

    content: str | Mapping[str, Any]
    sender: str = "user"
    subject: str = ""
    id: str = field(default_factory=lambda: f"rtmsg_{uuid4().hex[:12]}")
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeEvent:
    """An outbound event emitted by a running session."""

    kind: EventKind
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    id: str = field(default_factory=lambda: f"rtevt_{uuid4().hex[:12]}")

    @classmethod
    def state(cls, state: RuntimeState, *, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.STATE, {"state": state.value}, source=source)

    @classmethod
    def text(cls, text: str, *, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.TEXT, {"text": text}, source=source)

    @classmethod
    def error(cls, error: str, *, fatal: bool = False, source: str = "") -> "RuntimeEvent":
        return cls(EventKind.ERROR, {"error": error, "fatal": fatal}, source=source)


class RuntimeSession(ABC):
    """A single live agent session: send messages in, iterate events out."""

    source: str = ""

    @property
    @abstractmethod
    def state(self) -> RuntimeState: ...

    @property
    @abstractmethod
    def working_dir(self) -> Path: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def send(self, message: "RuntimeMessage | str") -> None: ...

    @abstractmethod
    def events(self) -> Iterator[RuntimeEvent]: ...

    @abstractmethod
    def stop(self, timeout: float = 5.0) -> None: ...

    def __enter__(self) -> "RuntimeSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class Runtime(ABC):
    """A factory for runtime sessions. Backends subclass this."""

    id: str = ""

    @abstractmethod
    def create_session(self, options: RuntimeOptions) -> RuntimeSession: ...

    def supports(self, options: RuntimeOptions) -> bool:
        return True

    def run(self, options: RuntimeOptions) -> RuntimeSession:
        session = self.create_session(options)
        session.start()
        return session


__all__ = [
    "RuntimeState",
    "EventKind",
    "RuntimeOptions",
    "RuntimeMessage",
    "RuntimeEvent",
    "RuntimeSession",
    "Runtime",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_runtime_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_sdk/runtime.py tests/test_sdk_runtime_contract.py
git commit -m "feat(sdk): add runtime contract seed (DTOs + Runtime/RuntimeSession ABCs)"
```

---

### Task 4: CapabilityBundle manifest seed + proof bundle

**Files:**
- Create: `src/lingtai_sdk/capabilities.py`
- Test: `tests/test_sdk_capabilities.py`

**Interfaces:**
- Produces: `BackendReplaceability` (enum), `RoleFlags` (dataclass), `CapabilitySurfaces` (dataclass: tools/resources/prompts/events/hooks/lifecycle/state), `SecurityPolicy` (dataclass: permissions), `TransportSpec` (dataclass), `BundleManifest` (dataclass w/ `validate()` and `to_dict()`), `proof_bundle() -> BundleManifest` (a harmless metadata-only synthetic bundle).

- [ ] **Step 1: Write the failing test** at `tests/test_sdk_capabilities.py`.

```python
"""CapabilityBundle manifest seed: the public DTO describing a capability's
identity, role flags, surfaces, security, and transport. Native privileged
handlers stay in the kernel/wrapper (Jason decision #2) — this is the public
schema only. The proof bundle is a harmless metadata-only synthetic bundle; we
do NOT migrate core system/psyche/soul here."""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

import pytest

from lingtai_sdk import capabilities as cap

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def test_proof_bundle_is_valid():
    b = cap.proof_bundle()
    assert b.name
    assert b.version
    b.validate()  # raises on invalid


def test_role_flag_invariant_native_only_requires_privileged():
    bad = cap.BundleManifest(
        name="x", version="0.0.1",
        roles=cap.RoleFlags(privileged=False, native_only=True),
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_required_name_and_version():
    with pytest.raises(ValueError):
        cap.BundleManifest(name="", version="0.0.1").validate()
    with pytest.raises(ValueError):
        cap.BundleManifest(name="x", version="").validate()


def test_manifest_round_trips_to_dict():
    b = cap.proof_bundle()
    d = b.to_dict()
    assert d["name"] == b.name
    assert d["roles"]["privileged"] == b.roles.privileged
    assert "surfaces" in d and "security" in d and "transport" in d


def test_surfaces_default_empty():
    s = cap.CapabilitySurfaces()
    assert s.tools == () and s.resources == () and s.prompts == ()
    assert s.events == () and s.hooks == () and s.lifecycle == () and s.state == ()


def test_capabilities_module_import_is_pure():
    code = (
        "import sys, lingtai_sdk.capabilities\n"
        "bad = [m for m in sys.modules if m=='lingtai' or m.startswith('lingtai.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO_ROOT), env={**os.environ, "PYTHONPATH": str(SRC)})
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_capabilities.py -v`
Expected: FAIL — `lingtai_sdk.capabilities` does not exist.

- [ ] **Step 3: Create `src/lingtai_sdk/capabilities.py`**

```python
"""CapabilityBundle manifest seed.

The public DTO schema describing a capability bundle: its identity, role flags,
the surfaces it contributes (tools, resources, prompts, events, hooks,
lifecycle, state), its security/permission posture, and its transport. This is
the *public schema only* (Jason decision #2): native privileged handlers live
in the kernel/wrapper, never here. The schema lets the kernel, the wrapper, and
external embedders agree on what a bundle *declares* without coupling to how it
is *implemented*.

This PR ships the schema plus a single harmless ``proof_bundle()`` — a synthetic
metadata-only bundle that exercises the shape end to end. Core bundles
(``system``/``psyche``/``soul``) are intentionally NOT migrated here; that is a
later, higher-risk PR. See ``docs/sdk/architecture-foundation.md``."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any


class BackendReplaceability(str, enum.Enum):
    """How freely a non-native backend may re-implement this bundle."""

    NATIVE_ONLY = "native_only"      # only the native runtime can provide it
    REPLACEABLE = "replaceable"      # any backend may re-implement
    AUGMENTABLE = "augmentable"      # backend may extend but not replace


@dataclass(frozen=True)
class RoleFlags:
    """Privilege/role posture of a bundle."""

    required: bool = False          # boots with every agent
    privileged: bool = False        # touches kernel-protected surfaces
    native_only: bool = False       # only the native runtime can host it
    can_override: bool = False      # may override an existing intrinsic/bundle
    backend_replaceability: BackendReplaceability = BackendReplaceability.REPLACEABLE


@dataclass(frozen=True)
class CapabilitySurfaces:
    """The named surfaces a bundle contributes. Names only — the manifest is a
    declaration, not an implementation."""

    tools: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    prompts: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    lifecycle: tuple[str, ...] = ()
    state: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityPolicy:
    """Permission/security posture for the bundle's tools."""

    permissions: tuple[str, ...] = ()       # named permissions the bundle needs
    requires_confirmation: tuple[str, ...] = ()  # tool names gated on confirm
    danger: str = "safe"                    # "safe" | "caution" | "destructive"


@dataclass(frozen=True)
class TransportSpec:
    """How the bundle's surfaces are carried."""

    kind: str = "native"            # "native" | "stdio" | "http" | "in_process"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class BundleManifest:
    """The full public declaration of a capability bundle."""

    name: str
    version: str
    summary: str = ""
    roles: RoleFlags = field(default_factory=RoleFlags)
    surfaces: CapabilitySurfaces = field(default_factory=CapabilitySurfaces)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    transport: TransportSpec = field(default_factory=TransportSpec)
    manual: tuple[str, ...] = ()    # skill/manual asset paths
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Raise ``ValueError`` if the manifest violates a basic invariant."""
        if not self.name:
            raise ValueError("BundleManifest.name is required")
        if not self.version:
            raise ValueError("BundleManifest.version is required")
        if self.roles.native_only and not self.roles.privileged:
            raise ValueError(
                "native_only bundles must also be privileged "
                f"(bundle {self.name!r})"
            )
        if (
            self.roles.native_only
            and self.roles.backend_replaceability
            is not BackendReplaceability.NATIVE_ONLY
        ):
            raise ValueError(
                "native_only bundles must declare "
                "backend_replaceability=NATIVE_ONLY "
                f"(bundle {self.name!r})"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view (enums → their values) for serialization/docs."""
        d = asdict(self)
        d["roles"]["backend_replaceability"] = self.roles.backend_replaceability.value
        return d


def proof_bundle() -> BundleManifest:
    """A harmless, metadata-only synthetic bundle exercising the schema.

    Deliberately NOT one of the core bundles. It declares a single read-only
    ``echo`` tool, no privileges, and is freely backend-replaceable — the lowest
    possible risk surface to prove the manifest shape end to end."""
    return BundleManifest(
        name="sdk_proof_echo",
        version="0.0.1",
        summary="Synthetic metadata-only proof bundle for the SDK foundation.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=("echo",)),
        security=SecurityPolicy(danger="safe"),
        transport=TransportSpec(kind="in_process"),
        metadata={"proof": True},
    )


__all__ = [
    "BackendReplaceability",
    "RoleFlags",
    "CapabilitySurfaces",
    "SecurityPolicy",
    "TransportSpec",
    "BundleManifest",
    "proof_bundle",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_capabilities.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_sdk/capabilities.py tests/test_sdk_capabilities.py
git commit -m "feat(sdk): add CapabilityBundle manifest seed + harmless proof bundle"
```

---

### Task 5: Anatomy + architecture docs

**Files:**
- Create: `src/lingtai_sdk/ANATOMY.md`
- Create: `docs/sdk/architecture-foundation.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: Write `src/lingtai_sdk/ANATOMY.md`** following the repo's 6-section template (Identity, Composition, Connections, State, Notes, Maintenance), ≤80 lines. Must state: eager-kernel/lazy-wrapper boundary; that runtime.py and capabilities.py are *seeds* (no live runtime, no core-bundle migration); that the kernel must never import this package. Cite files by path (`runtime.py`, `capabilities.py`, `_compat.py`).

- [ ] **Step 2: Write `docs/sdk/architecture-foundation.md`** covering:
  - The two-package status quo and the planned three-name public surface (`lingtai_kernel`, `lingtai`, `lingtai_sdk`).
  - SDK/CLI split: SDK = curated importable surface + contracts; CLI stays in `lingtai.cli`.
  - The eager-kernel/lazy-wrapper import-purity rule and why (tooling/provider-dep-free imports).
  - CapabilityBundle design: role flags, surfaces, security, transport; public schema in SDK vs native handlers in kernel (decision #2).
  - The runtime contract seed and the deferred NativeRuntime / Anthropic backend (decisions #3, #4).
  - Migration map / compatibility-by-re-export strategy.
  - Staged roadmap with an explicit "intentionally deferred" list: live NativeRuntime, Anthropic backend, core system/psyche/soul bundle migration, distribution/package rename.
  - The migration table rendered from `_compat.DEPRECATIONS` (can be authored by hand mirroring the map; keep small).

- [ ] **Step 3: Verify docs reference real symbols** — read back each cited file/symbol once and fix any drift.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai_sdk/ANATOMY.md docs/sdk/architecture-foundation.md
git commit -m "docs(sdk): add SDK package anatomy and architecture-foundation doc"
```

Note: `docs/` is gitignored by default. Force-add with `git add -f` and state the rationale in the commit body (long-lived architecture doc with durable purpose, per repo housekeeping rule). If the team prefers, the doc may instead live under `reports/` — but architecture foundation docs are durable, so `docs/sdk/` is the right home; force-add is justified.

---

### Task 6: Regression, packaging check, full validation, final report

**Files:**
- Create: `reports/sdk-architecture-foundation-20260617/implementation-report.md`

- [ ] **Step 1: Whitespace/diff hygiene**

Run: `git diff --check origin/main...HEAD`
Expected: no output (no trailing-whitespace/conflict markers).

- [ ] **Step 2: Verify packaging discovery picks up `lingtai_sdk`**

Run: `python -c "from setuptools import find_packages; import os; print([p for p in find_packages('src') if p.startswith('lingtai_sdk')])"`
Expected: `['lingtai_sdk']`. (The `include=['lingtai*','lingtai_kernel*']` glob matches `lingtai_sdk`; confirm the `exclude` list does not.) If not discovered, add `lingtai_sdk*` to `include` in `pyproject.toml` and commit that as a separate `build:` commit.

- [ ] **Step 3: Run all new SDK tests together**

Run: `PYTHONPATH=src python -m pytest tests/test_sdk_import_purity.py tests/test_sdk_compat.py tests/test_sdk_runtime_contract.py tests/test_sdk_capabilities.py -v`
Expected: all PASS.

- [ ] **Step 4: Run a practical regression slice** (import/adapter/anatomy-adjacent existing tests that are fast and relevant)

Run: `PYTHONPATH=src python -m pytest tests/ -q -x -k "workdir or loop_guard or token or notification or filesystem_mail" `
Expected: PASS (or pre-existing failures clearly unrelated to this change — capture status). Then attempt the full suite if practical:
Run: `PYTHONPATH=src python -m pytest tests/ -q`
Capture pass/fail counts; if some pre-existing tests fail for reasons unrelated to `lingtai_sdk`, record them in the report rather than fixing scope-creep.

- [ ] **Step 5: Smoke-test runtime/CLI unchanged**

Run: `PYTHONPATH=src python -c "import lingtai; from lingtai.cli import main; print('cli import ok', lingtai.__version__)"`
Expected: `cli import ok 0.12.3`.

- [ ] **Step 6: Write the final report** at `reports/sdk-architecture-foundation-20260617/implementation-report.md` with: design summary, files added, commit list (`git log --oneline origin/main..HEAD`), tests run + results, packaging note, risks, intentionally-deferred list, and a PR title/body draft. Force-add (`git add -f`) — `reports/` is gitignored; rationale: this is the requested deliverable.

- [ ] **Step 7: Final tree-clean check + commit report**

```bash
git add -f reports/sdk-architecture-foundation-20260617/implementation-report.md
git commit -m "docs(sdk): add implementation report for architecture-foundation PR"
git status   # must be clean
```

---

## Self-Review

**Spec coverage:**
- `lingtai_sdk` public entry → Task 1. ✓
- Import-pure / no heavy runtime → Task 1 (purity test), Tasks 3/4 (per-module purity tests). ✓
- Public identity re-exports (conservative) → Task 1 (`types.py`, eager kernel names + lazy Agent/services). ✓
- Version + compatibility/deprecation map → Task 1 (`_version.py`), Task 2 (`_compat.py`). ✓
- Runtime contract DTOs/protocols (`RuntimeOptions`, `Runtime`, `RuntimeSession`, `RuntimeMessage`/events) → Task 3. ✓ NativeRuntime intentionally deferred (documented). ✓
- CapabilityBundle schema (role flags, surfaces, security, manual, transport) + low-risk proof bundle, no core migration → Task 4. ✓
- Docs (SDK/CLI split, CapabilityBundle design, staged roadmap) → Task 5. ✓
- Compatibility/import-purity tests → Tasks 1, 2. ✓
- Existing behavior unchanged → Tasks 1/6 smoke + regression. ✓
- ANATOMY for new structures → Task 5. ✓
- Final report → Task 6. ✓
- Decisions: #1 (package in-repo now) Task 1; #2 (DTOs in SDK, handlers in kernel) Tasks 3/4 + docs; #3 (skeleton + proof, no system/psyche/soul) Task 4; #4 (no Anthropic backend) Task 3 docs; #5 (worktree-only, proven) Task 6. ✓

**Placeholder scan:** All code steps contain full code. The two NOTE blocks (Task 1 step 5, Task 2 step 3) are verification instructions, not placeholders — they tell the implementer to confirm kernel import paths and drop non-resolving entries, with a concrete fallback.

**Type consistency:** `RuntimeOptions.working_dir` typed `str | Path` consistently; `RuntimeEvent` classmethods (`state`/`text`/`error`) match test usage; `BundleManifest.validate()`/`to_dict()` and `proof_bundle()` names match tests; `Deprecation.is_active_alias`/`active_aliases()`/`migration_for()` match the compat test.
