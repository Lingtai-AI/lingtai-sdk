# Refactor: `intrinsics/soul.py` → `intrinsics/soul/` package

## Problem

`intrinsics/soul.py` is 1056 lines containing four distinct concerns:
1. **Public intrinsic surface** — `get_schema`, `get_description`, `handle` (the entry point `ALL_INTRINSICS` consumes)
2. **Config + voice persistence** — `init.json` read/write for cadence and voice profiles
3. **Mechanical consultation pipeline** — the soul-flow fire: snapshot loading, window fitting, diary rendering, LLM consultation with refusal rounds, synthetic pair construction
4. **Inquiry** — the synchronous mirror session

These concerns interact minimally at runtime (inquiry shares `_send_with_timeout` and `_build_soul_system_prompt` with the rest; that's it) but are bundled in a single file, making it hard to navigate, hard to test in isolation, and impossible to create a `intrinsics/soul/ANATOMY.md` that maps to code (the current `intrinsics/ANATOMY.md` notes this explicitly at line 36–37).

## Goal

Split into a `intrinsics/soul/` package with 4 sub-modules. Preserve every external import path. No behavioral changes, no renames, no bug fixes.

---

## 1. Concept boundaries

```
intrinsics/soul/
├── __init__.py       ~40 lines   Public intrinsic surface + re-exports
├── config.py        ~310 lines   Config + voice handling, persistence, prompt building
├── consultation.py  ~380 lines   Mechanical pipeline: snapshots, fitting, diary, fire, pair
└── inquiry.py        ~60 lines   Synchronous mirror session (soul_inquiry)
```

### Why 4 modules, not 7

The human's sketch proposed splitting config/voice and separating snapshots from the consultation pipeline. I disagree on both counts:

**Config and voice stay together.** Both write to `manifest.soul.*` in `init.json`, both use `_atomic_write_init`, and they share no code with the consultation pipeline. Splitting them would require either (a) duplicating `_atomic_write_init` or (b) introducing a `_util.py` for a 27-line helper. The combined `config.py` (~310 lines) is well within the comfort zone. The conceptual split (timing vs personality) is real but not load-bearing — there's no point where one module wants to evolve independently of the other.

**Snapshots stay inside `consultation.py`.** `_load_snapshot_interface`, `_list_snapshot_paths`, and `_fit_interface_to_window` have exactly one consumer: `_run_consultation_batch`. No other module or external code calls them. Extracting them into `snapshots.py` would create a module with zero independent callers — premature abstraction. If a future use case emerges (e.g., snapshot browsing as an agent action), extract then.

### Module responsibilities

**`__init__.py`** — The intrinsic interface that `ALL_INTRINSICS` and `BaseAgent._wire_intrinsics()` consume. Contains `get_schema`, `get_description`, `handle`. Re-exports every name that tests or `base_agent.py` currently import from `lingtai_kernel.intrinsics.soul.*`.

**`config.py`** — Everything that reads or writes `init.json` soul config, plus the voice-prompt resolution logic:
- `SOUL_VOICE_BUILTINS`, `SOUL_VOICE_PROMPT_MAX` — voice constants
- `_handle_config` — `action='config'` dispatcher
- `_handle_voice` — `action='voice'` dispatcher
- `_build_soul_system_prompt` — resolves the system prompt from voice profile (used by `_handle_voice` read mode and by `soul_inquiry`)
- `_persist_soul_config`, `_persist_soul_voice` — init.json writers
- `_atomic_write_init` — shared atomic JSON write helper

**`consultation.py`** — The entire mechanical soul-flow pipeline, from substrate loading to synthetic pair construction:
- `_CONSULTATION_SYSTEM_PROMPT`, `_CONSULTATION_TOOL_REFUSAL`, `_CONSULTATION_MAX_ROUNDS`, `_DIARY_CUE_TOKEN_CAP` — consultation constants
- `_load_snapshot_interface` — loads a `ChatInterface` from a snapshot file
- `_fit_interface_to_window` — tail-trims a `ChatInterface` to a token budget
- `_list_snapshot_paths` — lists snapshot files
- `_kind_for_source` — maps source label to prompt kind
- `_build_consultation_cue` — localized cue prompt (note: no longer called by `_run_consultation` after the substrate+spark patch, but still exists; leave in place per conservative convention)
- `_render_current_diary` — builds the diary cue spark
- `_write_soul_tokens` — token ledger writer (shared with inquiry)
- `_send_with_timeout` — LLM call wrapper (shared with inquiry)
- `_run_consultation` — single consultation fire
- `_run_consultation_batch` — parallel batch orchestrator
- `build_consultation_pair` — synthetic `(ToolCallBlock, ToolResultBlock)` pair builder

**`inquiry.py`** — The synchronous mirror session:
- `soul_inquiry` — clones conversation, sends question, returns answer

---

## 2. Public surface preservation

Every name imported by external consumers, and where each lives post-refactor:

### `intrinsics/__init__.py` (module registry)

```python
# Current:
from . import email, system, psyche, soul

# After — identical. `soul` is now a package, but `from . import soul` works on packages.
```

`ALL_INTRINSICS` accesses `soul.get_schema`, `soul.get_description`, `soul.handle` via the module object. All three remain at the top of `soul/__init__.py`. **No change to `intrinsics/__init__.py`.**

### `base_agent.py`

| Import site | Current import | Post-refactor re-export |
|---|---|---|
| `base_agent.py:996` | `from .intrinsics.soul import soul_inquiry` | `__init__.py` re-exports from `inquiry.py` |
| `base_agent.py:1072–1076` | `from .intrinsics.soul import _render_current_diary, _run_consultation_batch, build_consultation_pair` | `__init__.py` re-exports from `consultation.py` |

### `tests/test_soul.py`

| Import | Post-refactor |
|---|---|
| `from lingtai_kernel.intrinsics import soul` | Works as-is (package import) |

### `tests/test_soul_consultation.py`

All 30+ imports of private names (`_fit_interface_to_window`, `_run_consultation`, `_CONSULTATION_MAX_ROUNDS`, `_render_current_diary`, `_DIARY_CUE_TOKEN_CAP`, `_kind_for_source`, `_build_consultation_cue`, `_list_snapshot_paths`, `_load_snapshot_interface`, `_run_consultation_batch`, `build_consultation_pair`, `handle`, `get_schema`) — all re-exported via `soul/__init__.py`.

**No test file needs modification.** The `__init__.py` is the compatibility shim.

### Blocker check

None found. The refactor moves code; it does not rename any function, class, or constant.

---

## 3. Internal cross-references

### Dependency graph (no cycles)

```
__init__.py
  ├── imports from config.py:     _handle_config, _handle_voice
  └── imports from inquiry.py:    soul_inquiry

config.py
  └── imports from:               ..i18n (t)
                                   (no sibling imports)

inquiry.py
  ├── imports from config.py:     _build_soul_system_prompt
  └── imports from consultation.py: _send_with_timeout, _write_soul_tokens

consultation.py
  └── imports from:               ..i18n (t), ..llm.interface, ..token_counter
                                   (no sibling imports)
```

No circular imports. `consultation.py` is a leaf (no intra-package deps). `config.py` is a leaf. `inquiry.py` imports from both leaves. `__init__.py` imports from `config` and `inquiry`.

### Shared helpers

| Helper | Used by | Lives in |
|---|---|---|
| `_send_with_timeout` | `soul_inquiry`, `_run_consultation` | `consultation.py` (inquiry imports it) |
| `_write_soul_tokens` | `soul_inquiry`, `_run_consultation` | `consultation.py` (inquiry imports it) |
| `_build_soul_system_prompt` | `_handle_voice`, `soul_inquiry` | `config.py` (inquiry imports it) |
| `_atomic_write_init` | `_persist_soul_config`, `_persist_soul_voice` | `config.py` (only used here) |

Each shared helper lives in the module that is its primary domain; the other module imports it.

---

## 4. Per-call-site move table

### Module-level constants

| Name | Source lines | Destination | Public? |
|---|---|---|---|
| `SOUL_DELAY_MIN_SECONDS` | 26 | `__init__.py` | Yes (used in `get_schema`) |
| `CONSULTATION_PAST_COUNT_MIN` | 29 | `__init__.py` | Yes (used in `get_schema`) |
| `CONSULTATION_PAST_COUNT_MAX` | 30 | `__init__.py` | Yes (used in `get_schema`) |
| `SOUL_VOICE_BUILTINS` | 35 | `config.py` | Yes (re-exported via `__init__.py`) |
| `SOUL_VOICE_PROMPT_MAX` | 39 | `config.py` | Yes (re-exported via `__init__.py`) |
| `_CONSULTATION_SYSTEM_PROMPT` | 41–49 | `consultation.py` | Yes (re-exported; tests don't import it directly but `_run_consultation` uses it) |
| `_CONSULTATION_TOOL_REFUSAL` | 51–54 | `consultation.py` | Private to consultation |
| `_CONSULTATION_MAX_ROUNDS` | 56 | `consultation.py` | Yes (re-exported; tests import it) |
| `_DIARY_CUE_TOKEN_CAP` | 57 | `consultation.py` | Yes (re-exported; tests import it) |

### Functions

| Function | Source lines | Destination | Public? | Rename? |
|---|---|---|---|---|
| `get_description` | 60–62 | `__init__.py` | Yes | No |
| `get_schema` | 65–101 | `__init__.py` | Yes | No |
| `handle` | 104–148 | `__init__.py` | Yes | No |
| `_handle_config` | 151–236 | `config.py` | Re-exported | No |
| `_handle_voice` | 239–344 | `config.py` | Re-exported | No |
| `_persist_soul_config` | 347–386 | `config.py` | Re-exported | No |
| `_persist_soul_voice` | 389–431 | `config.py` | Re-exported | No |
| `_atomic_write_init` | 434–460 | `config.py` | Re-exported | No |
| `_send_with_timeout` | 463–490 | `consultation.py` | Re-exported | No |
| `_build_soul_system_prompt` | 493–524 | `config.py` | Re-exported | No |
| `_render_current_diary` | 527–586 | `consultation.py` | Re-exported | No |
| `_write_soul_tokens` | 589–609 | `consultation.py` | Re-exported | No |
| `soul_inquiry` | 612–662 | `inquiry.py` | Re-exported | No |
| `_load_snapshot_interface` | 683–712 | `consultation.py` | Re-exported | No |
| `_fit_interface_to_window` | 715–801 | `consultation.py` | Re-exported | No |
| `_kind_for_source` | 804–808 | `consultation.py` | Re-exported | No |
| `_build_consultation_cue` | 811–834 | `consultation.py` | Re-exported | No |
| `_run_consultation` | 837–935 | `consultation.py` | Re-exported | No |
| `_list_snapshot_paths` | 938–946 | `consultation.py` | Re-exported | No |
| `_run_consultation_batch` | 950–1021 | `consultation.py` | Re-exported | No |
| `build_consultation_pair` | 1024–1056 | `consultation.py` | Re-exported | No |

**No renames. Every move is a pure cut-and-paste into the target file.**

### `__init__.py` re-export surface

The `__init__.py` re-exports every name currently reachable via `from lingtai_kernel.intrinsics.soul import X`. This is the compatibility shim. Tests and `base_agent.py` keep their existing import paths.

Sketch:

```python
"""Soul intrinsic — the agent's inner voice."""

from .config import (
    SOUL_VOICE_BUILTINS,
    SOUL_VOICE_PROMPT_MAX,
    _handle_config,
    _handle_voice,
    _persist_soul_config,
    _persist_soul_voice,
    _atomic_write_init,
    _build_soul_system_prompt,
)
from .consultation import (
    _CONSULTATION_SYSTEM_PROMPT,
    _CONSULTATION_TOOL_REFUSAL,
    _CONSULTATION_MAX_ROUNDS,
    _DIARY_CUE_TOKEN_CAP,
    _send_with_timeout,
    _render_current_diary,
    _write_soul_tokens,
    _load_snapshot_interface,
    _fit_interface_to_window,
    _kind_for_source,
    _build_consultation_cue,
    _run_consultation,
    _list_snapshot_paths,
    _run_consultation_batch,
    build_consultation_pair,
)
from .inquiry import soul_inquiry

# Constants used directly in get_schema
SOUL_DELAY_MIN_SECONDS = 30.0
CONSULTATION_PAST_COUNT_MIN = 0
CONSULTATION_PAST_COUNT_MAX = 5


def get_description(lang: str = "en") -> str: ...  # moved here


def get_schema(lang: str = "en") -> dict: ...  # moved here


def handle(agent, args: dict) -> dict: ...  # moved here
```

---

## 5. Test impact

### Existing tests

All existing imports resolve through `soul/__init__.py`. **No test file needs modification.**

### Verification gate

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "import lingtai_kernel"
pytest -q tests/test_soul_consultation.py tests/test_soul.py
```

Both must pass with zero changes to test files. If a test fails, the `__init__.py` re-export surface is missing a name.

### No new tests needed

This is a structural refactor with zero behavioral change. Existing tests cover all moved code paths. New tests would test the packaging itself (import paths), which the verification gate above already exercises.

---

## 6. Risks and rollback

### State-handling code (extra care)

| Risk area | Functions | Why care |
|---|---|---|
| init.json writes | `_persist_soul_config`, `_persist_soul_voice`, `_atomic_write_init` | Regressions here corrupt agent config silently. The refactor moves these as-is — no logic changes. |
| Token ledger | `_write_soul_tokens` | Silent failure = invisible budget drift. Moved verbatim. |
| Snapshot loading | `_load_snapshot_interface`, `_fit_interface_to_window` | Broken fitting = broken consultations. Moved verbatim. |

### Rollback

One commit, atomic. `git revert` restores the single file.

---

## 7. Anatomy update

### `intrinsics/ANATOMY.md` (existing file)

After the refactor:

1. **Line 10** (`intrinsics/soul.py` description) — replace the single-file description with a package description:

   ```
   - `intrinsics/soul/` — inner voice and mechanical soul-flow. A package of four modules;
     see `intrinsics/soul/ANATOMY.md`. `__init__.py` re-exports the public intrinsic surface
     (`get_schema`, `get_description`, `handle`) plus all names consumed by `base_agent.py` and
     tests so that `from .intrinsics.soul import X` paths are unchanged.
   ```

2. **Line 24** (Composition, Subfolders) — change "none" to list `soul/`:

   ```
   - **Subfolders:** `soul/` is now a package (4 modules, see its ANATOMY.md).
     `system.py`, `psyche.py`, and `email.py` remain flat files.
   ```

3. **Line 36** (Notes, first bullet) — remove the "no per-intrinsic subdirectories" note; replace with acknowledgment that `soul/` is the first package intrinsic.

### `intrinsics/soul/ANATOMY.md` (new file)

Sketch:

```markdown
# intrinsics/soul

Inner voice and mechanical soul-flow. Three agent-callable actions
(`inquiry`, `config`, `voice`) plus one mechanical action (`flow`) that
fires on a wall-clock timer.

## Components

- `soul/__init__.py` — public intrinsic surface. `get_schema`,
  `get_description`, `handle` (the dispatcher). Re-exports all names
  consumed by `base_agent.py` and tests for backward compatibility.
- `soul/config.py` — config and voice handling. `_handle_config` and
  `_handle_voice` dispatch `action='config'` and `action='voice'`.
  `_build_soul_system_prompt` resolves voice profiles to system prompts.
  `_persist_soul_config`, `_persist_soul_voice`, `_atomic_write_init`
  write to `manifest.soul.*` in `init.json`.
- `soul/consultation.py` — mechanical soul-flow pipeline. Loads
  snapshots (`_load_snapshot_interface`), fits to window
  (`_fit_interface_to_window`), renders diary cue
  (`_render_current_diary`), runs consultation with refusal-loop
  (`_run_consultation`), orchestrates parallel batch
  (`_run_consultation_batch`), builds synthetic pair
  (`build_consultation_pair`). Also contains shared helpers
  `_send_with_timeout` and `_write_soul_tokens`.
- `soul/inquiry.py` — synchronous mirror session. `soul_inquiry` clones
  conversation (text+thinking only), sends question, returns answer.

## Connections

- `__init__.py` imports from `config` and `inquiry` for dispatch; imports
  from `consultation` for re-exports.
- `inquiry.py` imports `_build_soul_system_prompt` from `config` and
  `_send_with_timeout` + `_write_soul_tokens` from `consultation`.
- `config.py` and `consultation.py` are leaves — no intra-package imports.
- All modules use `i18n.t()` for localized strings.
- `consultation.py` reads snapshots written by `psyche._write_molt_snapshot`
  and uses `llm.interface` block types.

## State

- `config.py` mutates `init.json` (manifest.soul.*) for cadence and
  voice config.
- `consultation.py` reads `history/snapshots/snapshot_*.json` and
  `logs/events.jsonl` (diary cue), writes token-ledger entries.
- No new state files introduced by the package split.
```

---

## 8. Implementation order

1. Create `intrinsics/soul/` directory
2. Move code into the 4 new files (cut-and-paste, no edits to logic)
3. Write `__init__.py` with `get_schema`, `get_description`, `handle`, and all re-exports
4. Delete `intrinsics/soul.py`
5. Update `intrinsics/ANATOMY.md`
6. Create `intrinsics/soul/ANATOMY.md`
7. Verify: `python -c "import lingtai_kernel"` + `pytest -q tests/test_soul_consultation.py tests/test_soul.py`

---

## 9. Out of scope (follow-ups)

- **`_kind_for_source` and `_build_consultation_cue` cleanup.** Both are dead code after the substrate+spark patch (no caller in the consultation path). Leave in place per conservative convention; clean up in a follow-up sweep.
- **Voice profile simplification.** The voice system (`SOUL_VOICE_BUILTINS`, i18n strings) is now only used by inquiry mode. If the inquiry-only surface doesn't justify the complexity, simplify in a separate patch.
- **Snapshot extraction.** If a future feature needs snapshot browsing independently of consultation, extract `_load_snapshot_interface` / `_list_snapshot_paths` / `_fit_interface_to_window` into `snapshots.py` then.
- **Per-tool serializers.** Not needed for consultation (refusal-interception approach). May be useful for compaction summaries; separate design.
