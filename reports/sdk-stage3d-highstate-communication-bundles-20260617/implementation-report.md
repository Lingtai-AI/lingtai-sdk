# SDK Migration Stage 3D — High-State Communication/Execution Tool Bundles

**Implementation report**

## Branch / head / base

| | |
|---|---|
| Branch | `sdk/stage3d-highstate-communication-bundles-20260617` |
| Head | report commit containing this file, stacked after implementation commit `e2ee9366274a6b3fe4325ea4cace1fa8d2f1f08d` |
| Base | `origin/sdk/stage3c-lifecycle-system-bundle-20260617` @ `5327ecf5d50c36321a6a10eaf76c8a8135774bcd` (Stage 3C, PR #355) |
| Worktree | `/Users/huangzesen/work/GitHub/lingtai-kernel/.worktrees/sdk-stage3d-highstate-communication-bundles-20260617` |
| Remote side effects | none (no push / PR / merge / tag / publish) |

Stacked on Stage 3C as instructed (not main). Two local commits on top of the 3C
base: implementation `e2ee9366274a6b3fe4325ea4cace1fa8d2f1f08d` plus the
tracked implementation-report commit containing this file; main checkout untouched.

## Goal

Continue the P3 high-state migration begun in Stage 3C (the privileged
native-only `system` lifecycle tool) to the remaining high-state surfaces with
**external or process side effects**, mirroring the established declare-and-inject
seam (SDK declaration + host seam; wrapper-side bridge injecting the existing real
handler), **without** changing live turn-loop dispatch, installing any guard, or
mounting SDK bundle hosts onto running agents.

## Tool surfaces discovered (evidence first)

The high-state communication/execution surfaces currently exposed through
intrinsic / capability registration were discovered by reading the code, not
assuming paths:

| Surface | Kind | Live handler | Live registration | SDK manifest existed? |
|---|---|---|---|---|
| `email` | **kernel intrinsic** | `lingtai_kernel.intrinsics.email.handle(agent, args)` (delegates to `EmailManager`, booted by `email.boot`) | `BaseAgent._wire_intrinsics` → `agent._intrinsics["email"]` (`ALL_INTRINSICS`, `intrinsics/__init__.py`) | No |
| `daemon` | **wrapper capability** | `DaemonManager.handle(args)` built in `lingtai.core.daemon.setup()` | `agent.add_tool("daemon", …)` → `agent._tool_handlers["daemon"]`; mgr stashed at `agent._capability_managers["daemon"]` | No |
| `mcp` | **wrapper capability** | inline closure `handle_mcp(args)` defined *inside* `lingtai.core.mcp.setup()` (single `show` action → `_reconcile(agent)`) | `agent.add_tool("mcp", …)` | No |

Full intrinsic surface (`_wire_intrinsics` / `ALL_INTRINSICS`): `email`, `system`
(3C), `psyche`, `soul` (both declared in `core_bundles`). Wrapper capabilities with
a `setup()` pattern: `knowledge`, `skills`, `bash`, `avatar`, `daemon`, `mcp`,
`read`, `write`, `edit`, `glob`, `grep`.

## Included / excluded

### Included

- **`email`** — kernel intrinsic, the clean high-state mirror of Stage 3C's
  `system`. Stable handler seam (`intrinsics.email.handle`), boots an
  `EmailManager` at agent construction. Many actions are read-only (`check` /
  `search` / `contacts`); the outbound (`send` / `reply` / `reply_all`) and
  irreversible (`delete`) actions are the high-state risk and are never exercised
  by tests.
- **`daemon`** — wrapper capability. Spawns/kills child processes and runs long
  child-agent executions — the highest-danger in-process surface. Bridged through
  the same in-process carrier the file tools (3A/3B) use.

### Excluded — `mcp` (with rationale)

`mcp` was deliberately excluded. Its live handler is a **local closure
(`handle_mcp`) defined inside `setup()`** with no module-level factory and no
stashed reference — there is no stable real-handler seam to reuse without either
forking the closure or refactoring a third live `setup()`. Functionally it is a
**read-only `show` presentation tool**: it returns the `mcp-manual` body plus a
registry/health snapshot via `_reconcile(agent)`; actual MCP registry changes go
through `write`/`edit` on `mcp_registry.jsonl` followed by `system(action=
"refresh")`, *not* through the `mcp` tool surface. So it is neither a real
high-state execution/communication surface nor one with a stable seam, and forcing
an extraction would change live behavior for no high-state gain. This follows the
task's "implement the cleanest high-state subset and clearly report exclusions"
guidance. Excluded surface is documented in `architecture-foundation.md` §26, both
ANATOMY files, and here.

`psyche` / `soul` are out of scope (identity/context and inner-voice surfaces, not
communication/execution; already declared as core bundles, hosted via the generic
`native_core_hosts` seam).

## Design — mirrors Stage 3A/3B/3C exactly, split by live carrier

The defining property of Stage 3D is that the two surfaces ride **different live
carriers**, so each is hosted by the host class that matches its carrier — exactly
as the live kernel/wrapper already carries it:

- **`email` (native intrinsic)** → mirrors `system_bundle.py` (3C). SDK declares a
  fresh privileged-native manifest (`email_comm_manifest()`); the wrapper bridge
  injects the real kernel intrinsic `email.handle` bound to the agent into a
  `NativeBundleHost`. `email` is `privileged=True` but `native_only=False` /
  `AUGMENTABLE` (a backend could re-implement a mail surface), distinguishing it
  from the `NATIVE_ONLY` core bundles.
- **`daemon` (in-process capability)** → mirrors `file_bundle.py` / 3B. SDK
  declares a non-privileged, `in_process`, `REPLACEABLE` manifest
  (`daemon_exec_manifest()`); the wrapper bridge injects the real
  `daemon.make_handler(agent)` into a non-native `BundleHost`.

### Preserving live paths / single source of truth

- The live `_wire_intrinsics` (`email`) and `daemon.setup()` registration paths
  are **left intact**. No turn-loop dispatch change; nothing mounted on running
  agents; no guard installed; additive seam only.
- `email`: the bridge reuses the *same* `intrinsics.email.handle` the live path
  dispatches — no second implementation (the 3C pattern).
- `daemon`: to give the bridge the same single source of truth, a
  behavior-preserving `make_manager(agent, …)` / `make_handler(agent, …)` factory
  was extracted in `lingtai.core.daemon`, and `setup()` refactored to build the
  `DaemonManager` through `make_manager` and register `mgr.handle`
  byte-identically (mirrors 3B's `make_handler` extraction for `write`/`edit`).
  This is the only edit to existing kernel/wrapper code; all 109 daemon tests pass
  unchanged, confirming parity.

## Security / guard / audit posture

High-state posture was treated conservatively, with one manifest covering
mixed-risk actions via a per-action metadata table (the 3C approach):

- **Bundle-level danger is the strongest action's grade.** Both `email` and
  `daemon` are bundle-level `destructive` — `email` because `delete` is
  irreversible and `send`/`reply` reach external recipients; `daemon` because
  `emanate`/`ask`/`reclaim` spawn/kill child processes. The bundle posture can
  never under-state any one action.
- **Per-action risk tables ship as declarations, never as a second gate:**
  - `EMAIL_ACTION_RISK`: `check`/`read`/`search`/`contacts` → `SAFE`; mailbox/
    contact mutations (`dismiss`/`archive`/`*_contact`) and outbound sends
    (`send`/`reply`/`reply_all`) → `CAUTION`; `delete` → `DESTRUCTIVE`.
  - `DAEMON_ACTION_RISK`: `list`/`check` → `SAFE`; `emanate`/`ask`/`reclaim` →
    `DESTRUCTIVE`.
  - Helpers (`email_action_risk` / `daemon_action_risk`) fail safe — an unknown
    action grades `DESTRUCTIVE`, never silently safe (same direction as the guard
    bridge and the 3C `system` table).
  - Attention subsets `EMAIL_SEND_ACTIONS` (external reach) and
    `DAEMON_PROCESS_ACTIONS` (process spawn/kill) are declared for hosts that want
    them.
- **No live runtime gates invented.** The declared posture is pinned against the
  **existing** stage-17 `guard_bridge`: `tool_danger_index` returns `destructive`
  for both; in `BLOCKING` mode both are denied before dispatch; in `ADVISORY` mode
  both are allowed-with-`warn`. No guard is installed by this stage. The real
  authority/side-effect behavior stays where it lives today — the kernel intrinsic
  for `email`, the `DaemonManager` for `daemon`.
- **Action-table self-consistency is enforced.** Tests pin the per-action risk
  tables against each SDK manifest’s declared action set, so every declared action
  has an explicit risk grade and unknown actions fail safe. The action sets were
  also manually checked against the live `intrinsics.email.schema.get_schema` and
  `daemon.get_schema` surfaces during implementation/review; a future follow-up
  may add an automated cross-schema drift guard if desired.
- **Import purity / dependency direction preserved.** `communication_tools` imports
  only `.capabilities` / `.capability_host` / `.errors`; a bare import pulls in no
  `lingtai` wrapper. The wrapper bridge imports the SDK lazily inside its functions
  (wrapper → sdk edge); the kernel never imports the SDK. Both asserted by tests.
- **Secrets hygiene:** no secrets added/printed; manifest metadata is non-secret
  structural description only.

## Tests — exact results

All runs `PYTHONPATH=src python -m pytest …` in the worktree.

| Suite | Result |
|---|---|
| `tests/test_sdk_communication_tools.py` (new, SDK-side, dummy handlers) | **23 passed** |
| `tests/test_communication_bundle_bridge.py` (new, wrapper bridge parity) | **12 passed** |
| All SDK bundle + bridge suites (3A/3B/3C/3D: file_tools, file_mutation_tools, lifecycle_tools, file_bundle_bridge, system_bundle_bridge + 3D) | **147 passed** |
| `tests/test_daemon.py`, `test_daemon_check.py`, `test_daemon_preset_capabilities.py` (regression on `setup()` refactor) | **109 passed in 17.87s** |
| `tests/test_email_identity.py`, `test_layers_email.py`, `test_sdk_guard_bridge.py`, `test_sdk_capability_host.py`, `test_sdk_core_bundles.py` | **151 passed** |
| `tests/test_sdk_import_purity.py` | **4 passed** |
| `tests/test_agent_capabilities.py`, `test_mcp_capability.py`, `test_sdk_capabilities.py`, `test_sdk_capability_host.py` | **58 passed** |

What the new tests cover:

- **SDK declaration** — manifest postures per carrier (`email` native/privileged/
  AUGMENTABLE; `daemon` in-process/non-privileged/REPLACEABLE), validation,
  `load_manifest` round-trip, single-tool surfaces, stable order.
- **Per-action risk tables** — coverage equals declared SDK actions, faithful
  grading, bundle posture = strongest action, and fail-safe unknown actions. The
  live `email`/`daemon` schema action sets were manually checked during
  implementation/review; automated cross-schema drift guarding remains a possible
  follow-up.
- **Host carriers** — `email` → `NativeBundleHost` (non-native `BundleHost`
  refuses it); `daemon` → non-native `BundleHost`; declared↔provided handler
  contract refusals (missing/undeclared/non-callable).
- **Guard/wiring invariant** — `guard_bridge` denies both in BLOCKING, warns in
  ADVISORY; `tool_danger_index` reflects `destructive`.
- **Bridge parity** — invoking through the bundle host returns byte-identical
  results to the live handler, for **side-effect-free actions only**: email
  `check`/`contacts`/`search` (empty mailbox), reserved `unread` guard, unknown
  action; daemon `list`/`check` (fresh run dir), unknown action; plus an assertion
  that building the daemon host spawns no process and that live registration is
  unchanged.
- **Single source of truth** — `daemon.setup` routes through `make_manager`
  (regression guard).
- **Lazy-SDK import** — importing the wrapper bridge does not eagerly import the
  SDK.

**Test safety:** no test sends real mail, deletes a message, sleeps, spawns,
drives, or kills any process/subagent, or reconfigures MCP. Agents use a mock LLM
service and a tmp working dir; every exercised action is read-only / error / no-op.

### Lint (ruff)

- New files (`communication_tools.py`, `communication_bundle.py`, both test files):
  **All checks passed.**
- `src/lingtai/core/daemon/__init__.py`: 3 `F841` "unused variable" findings at
  lines 1000, 3666, 4099 — **pre-existing on the base** (verified by
  `git stash` + `ruff check` on `5327ecf`); the `make_manager`/`make_handler`/
  `setup` edit (≈line 4940) introduced **no new** findings.

## Diff summary

```
 docs/sdk/architecture-foundation.md       | 110 ++++++   (§26 stage 3D)
 src/lingtai/capabilities/ANATOMY.md       |   1 +        (3D bridge bullet)
 src/lingtai/core/communication_bundle.py  | 194 +++++++++ (NEW wrapper bridge)
 src/lingtai/core/daemon/ANATOMY.md        |   2 +-       (make_manager/make_handler)
 src/lingtai/core/daemon/__init__.py       |  41 ++-      (make_manager/make_handler; setup refactor)
 src/lingtai_sdk/ANATOMY.md                |   3 +-       (communication_tools entry + purity line)
 src/lingtai_sdk/communication_tools.py    | 555 +++++++++ (NEW SDK declaration + seams)
 tests/test_communication_bundle_bridge.py | 201 +++++++++ (NEW bridge parity tests)
 tests/test_sdk_communication_tools.py     | 306 +++++++++ (NEW SDK tests)
 9 files changed, 1408 insertions(+), 5 deletions(-)
```

The only change to existing kernel/wrapper runtime code is the behavior-preserving
daemon factory extraction; everything else is additive (SDK module, wrapper bridge,
tests, docs).

## Risks / non-blocking follow-ups

1. **`daemon.make_handler` builds a fresh `DaemonManager`.** Like 3B's file-tool
   `make_handler`, the daemon factory constructs a new manager (new idle
   `ThreadPoolExecutor`, a record-reaping pass over the working dir — no process
   spawn). For the declaration/test seam this is correct and safe. If a *future*
   stage mounts this host on a live agent, it should instead reuse the live
   manager (`agent._capability_managers["daemon"]` / `agent._tool_handlers
   ["daemon"]`) to avoid a second manager/thread pool — noted for the live-wiring
   stage, not needed now (this stage mounts nothing). Non-blocking.
2. **`mcp` excluded** (see rationale). A later stage that wants `mcp` bridged
   should first give it a stable handler seam (e.g. a `make_handler(agent)` like
   the other capabilities) before declaring a bundle; doing so now would have
   changed live behavior for a read-only tool. Non-blocking.
3. **Pre-existing daemon ruff F841 findings** (3) are untouched and out of scope.
4. No live guard is installed and no host is mounted — by design (additive seam).
   Live wiring of the guard bridge / bundle hosts remains a later C3 stage, as in
   3A/3B/3C.

## Ready for independent review?

**Yes.** The change is additive and scope-disciplined: it declares the two
clean-seam high-state communication/execution surfaces, bridges the real existing
handlers through them without altering any live registration or dispatch path, pins
the declared posture against the existing guard bridge and live schemas, and
excludes the one surface (`mcp`) lacking a stable seam with a documented rationale.
The single edit to existing code (daemon factory extraction) is behavior-preserving
and verified by the full daemon suite. New code is ruff-clean and fully tested with
no external sends, sleeps, process spawn/kill, or MCP reconfiguration. Committed
locally only (implementation `e2ee936` plus this report commit); no push/PR/merge performed.
