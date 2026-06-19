# Refactor: `base_agent.py` → `base_agent/` package

## Problem

`base_agent.py` is 2551 lines — the single largest unmapped surface in the kernel. The kernel-root anatomy already flags it as the next refactor candidate. It contains ~55 methods and 2 free functions spanning at least 10 distinct concerns: turn loop, lifecycle/heartbeat/signals, soul orchestration, refresh/preset, mail glue, identity/manifest/status, tool wiring, system-prompt/chat-state, notifications/hooks, and guards/token bookkeeping.

All concerns live in one file, making navigation difficult, testing isolation impossible, and an `ANATOMY.md` that maps to code impractical.

## Goal

Split into a `base_agent/` package with 6 sub-modules. Preserve every external import path (only `BaseAgent` is imported externally). No behavioral changes, no renames, no bug fixes, no new abstractions.

---

## 1. Concern map

After reading `base_agent.py` end-to-end, I identified these clusters. Line ranges are approximate; the method inventory below is exact.

### Cluster A: Identity / manifest / status (~200 lines)
**What it is:** Everything the agent knows about itself — how it presents, how it serializes, how it reports runtime status.
- `_format_stamina` (56–64) — free function, renders stamina as human-friendly string
- `_build_identity_section` (67–144) — free function, renders manifest as curated prose for system prompt
- `set_name` (521–531) — immutable true-name setter
- `set_nickname` (533–536) — mutable alias setter
- `_update_identity` (538–553) — rewrites manifest + identity prompt section
- `_build_manifest` (2201–2231) — builds `.agent.json` dict
- `status` (2457–2523) — returns live runtime status for TUI/portal

**Why it's a cluster:** These methods all read/write the same identity state (`agent_name`, `nickname`, `_agent_id`, `_created_at`, `_started_at`, `_admin`, `_molt_count`) and share the `_build_manifest` → `_build_identity_section` → `_prompt_manager.write_section("identity")` pipeline. The two free functions (`_format_stamina`, `_build_identity_section`) have no callers outside this cluster.

**Why it's separate from prompt/schemas:** Identity is stable prose (changes rarely, sits in cacheable prefix). Prompt/schemas are dynamic (change every tool registration, every language switch).

### Cluster B: Heartbeat + signal detection (~220 lines)
**What it is:** The always-on health monitor thread that writes heartbeat files and detects signal files (`.sleep`, `.suspend`, `.refresh`, `.prompt`, `.clear`, `.inquiry`, `.rules`, `.interrupt`).
- `_start_heartbeat` (1238–1248)
- `_stop_heartbeat` (1250–1261)
- `_heartbeat_loop` (1263–1446) — **183 lines**, the single largest method in the file

**Why it's a cluster:** `_heartbeat_loop` runs on its own daemon thread. It has a clear boundary: it reads signal files and calls into other clusters (`_set_state`, `_cancel_event`, `_run_inquiry`, `_check_rules_file`, `send`, `_save_chat_history`, `context_forget`). Every call is a method invocation on `agent`.

**Cross-cluster calls from `_heartbeat_loop`:** `_set_state` (×6), `_cancel_event.set()` (×5), `_run_inquiry` (×1), `_check_rules_file` (×1), `send` (×1), `_save_chat_history` (×1), `_cancel_soul_timer` (×1), `_log` (×8), `_wake_nap` (×1), `_reset_uptime` (×1), `_workdir.snapshot()` (×1), `_workdir.gc()` (×1), `context_forget` (×1, via psyche). This is the highest-fan-out method — extracting it into a free function makes the dependency surface explicit.

### Cluster C: Soul orchestration (~380 lines)
**What it is:** The entire soul-flow pipeline from timer to synthetic pair.
- `_start_soul_timer` (847–866)
- `_cancel_soul_timer` (868–872)
- `_soul_whisper` (879–903) — timer callback
- `_drain_tc_inbox` (905–950) — splices queued pairs into wire chat
- `_persist_soul_entry` (952–973) — writes inquiry entries
- `_append_soul_flow_record` (975–991) — writes flow records
- `_run_inquiry` (993–1004) — calls `intrinsics.soul.soul_inquiry`
- `_flatten_v3_for_pair` (1010–1038) — bridge v3→legacy
- `_run_consultation_fire` (1040–1190) — **150 lines**, the consultation orchestrator
- `_rehydrate_appendix_tracking` (1192–1232) — restores tracking after restart

**Why it's a cluster:** These methods form a pipeline: timer → fire → persist → enqueue → drain → rehydrate. They share `tc_inbox` (the involuntary tool-call inbox) and `soul_flow.jsonl` (the on-disk log). `_run_consultation_fire` imports from `intrinsics.soul` and writes to `logs/soul_flow.jsonl`; the other methods manage the timer and the drain path.

**Why it's separate from the turn engine:** Soul orchestration runs asynchronously (timer thread, drain at safe boundaries). The turn engine processes synchronous user/system messages.

### Cluster D: Turn engine (~530 lines)
**What it is:** The core message-processing pipeline — from inbox to LLM response.
- `_run_loop` (1463–1588) — 125 lines, the main event loop with AED
- `_concat_queued_messages` (1704–1728)
- `_handle_message` (1734–1741) — router
- `_handle_request` (1743–1826) — 83 lines, sends to LLM, processes response
- `_handle_tc_wake` (1828–1973) — 145 lines, processes queued tool-call pairs
- `_process_response` (1987–2072) — 85 lines, tool-call loop
- `_dispatch_tool` (2078–2097) — 2-layer dispatch
- `_get_guard_limits` (1975–1981)
- `_enqueue_system_notification` (725–805) — 80 lines, synthesizes notification pair
- `notify` (807–814)
- `_on_mail_received` (656–663)
- `_on_normal_mail` (665–706) — mail → notification pipeline
- `send` (2367–2380) — fire-and-forget
- `mail` (2233–2238) — public mail API
- `_save_chat_history` (2398–2451) — 53 lines, writes chat + token ledger
- `get_chat_state` (2386–2388)
- `restore_chat` (2390–2392)
- `restore_token_state` (2394–2396)

**Why this is one module:** These form the message lifecycle: receive → route → LLM → process → persist. `_handle_request` calls `_drain_tc_inbox` (from Cluster C), `_process_response`, and `_save_chat_history`. `_handle_tc_wake` calls `_session.send` and `_process_response`. Notifications feed into tc_inbox which feeds into `_handle_tc_wake`. Persistence is called after every turn.

**Why notifications/mail are here, not separate:** `_on_normal_mail` creates a notification via `_enqueue_system_notification`. The notification arrives as a tc_inbox item. `_handle_tc_wake` processes it. The entire chain is: mail → notification → tc_inbox → wake → LLM. Splitting this chain across files would obscure the flow.

### Cluster E: Prompt + tool schemas + tool management (~180 lines)
**What it is:** Building the system prompt, tool schemas, and managing the tool registry.
- `_refresh_tool_inventory_section` (2103–2117) — rebuilds tools prompt section
- `_build_system_prompt` (2119–2122)
- `_build_system_prompt_batches` (2124–2134)
- `_build_tool_schemas` (2136–2183) — builds complete schema list with reasoning injection
- `get_token_usage` (2185–2195)
- `_check_rules_file` (2298–2333) — consumes `.rules` signal
- `_flush_system_prompt` (2335–2342) — rebuild + persist + update live session
- `update_system_prompt` (2344–2356)
- `add_tool` (2244–2272)
- `remove_tool` (2274–2282)
- `override_intrinsic` (2284–2296)
- `has_capability` (2240–2242)

**Why it's a cluster:** These methods all read from the same sources (`_prompt_manager`, `_intrinsics`, `_tool_schemas`, `_tool_handlers`) and write to the system prompt or tool registry. `_build_tool_schemas` is called by `_session` via a function reference set during construction. `_check_rules_file` reads `.rules` and writes to the prompt manager. `add_tool`/`remove_tool` modify `_tool_handlers`/`_tool_schemas` and refresh the live session.

**Why tool management is here, not separate:** `add_tool`/`remove_tool` directly call `_build_tool_schemas` and `_refresh_tool_inventory_section`. They're tightly coupled to the prompt/schema machinery.

### Cluster F: Refresh / preset (~120 lines)
**What it is:** The refresh handshake protocol and preset auto-fallback.
- `_perform_refresh` (1590–1663) — 73 lines, spawns deferred relaunch watcher
- `_activate_preset` (1665–1673) — NotImplementedError stub, overridden by Agent subclass
- `_can_fallback_preset` (1675–1691)
- `_activate_default_preset` (1693–1698) — NotImplementedError stub
- `_build_launch_cmd` (1700–1702) — returns None, overridden by Agent subclass

**Why it's a cluster:** `_perform_refresh` is the most complex lifecycle operation — it touches `.refresh` signal file, waits for heartbeat ack, spawns a subprocess watcher, and relaunches. The preset methods form a fallback chain: `_can_fallback_preset` → `_activate_default_preset` → `_perform_refresh`.

### What stays on `__init__.py`

These must remain as methods on BaseAgent because they're either (a) the constructor, (b) properties, (c) subclass-overridable hooks, or (d) cross-cutting state machine methods called from every cluster:

- **Constructor** `__init__` (178–438, 260 lines) — wires everything together. Cannot be extracted.
- **Properties** (454–515) — `is_idle`, `state`, `agent_id`, `working_dir`, `_chat` (+setter), `_streaming`, `_token_decomp_dirty` (+setter), `_interaction_id` (+setter), `_intermediate_text_streamed` (+setter). Must stay on the class.
- **`_wire_intrinsics`** (444–448) — called from `__init__`, binds intrinsic handlers.
- **State machine** — `_set_state` (816–845), `_wake_nap` (874–877), `_log` (1448–1457). Called from every cluster. Too cross-cutting and too short to extract.
- **Hooks** — `_pre_request` (2529–2534), `_post_request` (2536–2540), `_on_tool_result_hook` (2542–2550). Overridable by subclasses, must stay on the class.
- **Public API stubs** — `wake` (717), `log` (721), `working_dir` property (712), `_cpr_agent` (2358–2365). Short, delegate to other methods.

**Estimated `__init__.py` size:** ~550 lines (constructor + properties + hooks + state machine + stubs + imports/re-exports).

---

## 2. Proposed package layout

```
base_agent/
├── __init__.py           ~550 lines   BaseAgent class: constructor, properties, hooks, state machine, stubs
├── identity.py           ~200 lines   _format_stamina, _build_identity_section, _build_manifest, set_name, set_nickname, _update_identity, status
├── heartbeat.py          ~220 lines   _start_heartbeat, _stop_heartbeat, _heartbeat_loop (signal detection)
├── soul_flow.py          ~380 lines   _start_soul_timer, _cancel_soul_timer, _soul_whisper, _drain_tc_inbox, _persist_soul_entry, _append_soul_flow_record, _run_inquiry, _flatten_v3_for_pair, _run_consultation_fire, _rehydrate_appendix_tracking
├── turn.py               ~530 lines   _run_loop, _handle_message, _handle_request, _handle_tc_wake, _process_response, _dispatch_tool, _get_guard_limits, _concat_queued_messages, _enqueue_system_notification, notify, _on_mail_received, _on_normal_mail, send, mail, _save_chat_history, get_chat_state, restore_chat, restore_token_state
├── prompt.py             ~180 lines   _refresh_tool_inventory_section, _build_system_prompt, _build_system_prompt_batches, _build_tool_schemas, get_token_usage, _check_rules_file, _flush_system_prompt, update_system_prompt, add_tool, remove_tool, override_intrinsic, has_capability
├── refresh.py            ~120 lines   _perform_refresh, _activate_preset, _can_fallback_preset, _activate_default_preset, _build_launch_cmd
```

### Pattern for extraction

Each sub-module follows the soul refactor pattern — free functions that take `agent` as first arg:

```python
# base_agent/heartbeat.py
"""Heartbeat and signal-file detection."""

def start_heartbeat(agent) -> None:
    """Start the heartbeat daemon thread."""
    if agent._heartbeat_thread is not None:
        return
    agent._heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(agent,),
        daemon=True,
        name=f"heartbeat-{agent.agent_name or agent._working_dir.name}",
    )
    agent._heartbeat_thread.start()
    agent._log("heartbeat_start")

def heartbeat_loop(agent) -> None:
    """Beat every 1 second. AED if agent is STUCK."""
    # ... body of _heartbeat_loop, with self → agent ...
```

And on BaseAgent, a thin stub:

```python
def _start_heartbeat(self) -> None:
    from .heartbeat import start_heartbeat
    start_heartbeat(self)
```

### Naming convention

Following the soul refactor: descriptive names without underscore prefix (`config.py`, `consultation.py`, `inquiry.py`). Exception: `soul_flow.py` uses underscore-separated name to avoid confusion with `intrinsics/soul/`.

### Why 6 modules, not 10

The human's sketch identified 10 clusters. I merged 4 pairs:

1. **Notifications + mail → `turn.py`.** `_on_mail_received` → `_on_normal_mail` → `_enqueue_system_notification` is a chain. Notifications arrive via tc_inbox which is drained by `_handle_tc_wake`. Splitting this chain across files obscures the flow.

2. **Guards + turn loop → `turn.py`.** `_get_guard_limits` is 7 lines, called only from `_handle_request`. No standalone value.

3. **Tool wiring + prompt → `prompt.py`.** `add_tool`/`remove_tool` directly call `_build_tool_schemas` and `_refresh_tool_inventory_section`. They're tightly coupled to the prompt/schema machinery.

4. **Session persistence + turn → `turn.py`.** `_save_chat_history` is called after every turn and after every tc_wake splice. It belongs with the turn engine because persistence is the turn's final action.

---

## 3. Public surface preservation

### External imports (grep confirmed)

Only `BaseAgent` is imported externally:

| Consumer | Import |
|---|---|
| `lingtai_kernel/__init__.py:4` | `from .base_agent import BaseAgent` |
| `lingtai/__init__.py:9` | `from lingtai_kernel.base_agent import BaseAgent` |
| `lingtai/agent.py:16` | `from lingtai_kernel.base_agent import BaseAgent` |
| 13 wrapper modules | `from lingtai_kernel.base_agent import BaseAgent` (lazy) |
| 18 test files | `from lingtai_kernel.base_agent import BaseAgent` |

**No other name is imported from `base_agent` externally.** The `__init__.py` re-exports `BaseAgent` and `_build_identity_section` (used only internally but available for testing).

### `base_agent/__init__.py` re-export surface

```python
"""BaseAgent — generic agent kernel with intrinsic tools and capability dispatch."""

from .identity import (
    _format_stamina,
    _build_identity_section,
)

# BaseAgent class defined here (constructor, properties, hooks, state machine, stubs)
class BaseAgent:
    ...

# The only public symbol:
__all__ = ["BaseAgent"]
```

---

## 4. Test impact analysis

### mock.patch targets

**Zero.** Grep confirms no `mock.patch('lingtai_kernel.base_agent.X')` calls exist in the test suite. The only match was a comment in `test_compaction.py:286` mentioning "base_agent" in prose.

All tests import `BaseAgent` directly and test through the class API. Since `BaseAgent` stays in `__init__.py` and all its methods remain on the class (as thin stubs), every test keeps working without modification.

### Static imports

All 18 test files do `from lingtai_kernel.base_agent import BaseAgent`. After the refactor, `base_agent` is a package and `BaseAgent` is defined in `__init__.py`. The import path `lingtai_kernel.base_agent.BaseAgent` resolves identically. **No test file needs modification.**

---

## 5. Per-method move table

### Free functions (currently module-level)

| Function | Lines | Destination | Public? |
|---|---|---|---|
| `_format_stamina` | 56–64 | `identity.py` | Re-exported via `__init__.py` |
| `_build_identity_section` | 67–144 | `identity.py` | Re-exported via `__init__.py` |

### BaseAgent methods

| Method | Lines | Destination | Stays as stub? |
|---|---|---|---|
| `__init__` | 178–438 | `__init__.py` | No — stays as full method |
| `_wire_intrinsics` | 444–448 | `__init__.py` | No — stays as full method |
| Properties (10) | 454–515 | `__init__.py` | No — stay as properties |
| `set_name` | 521–531 | `identity.py` | Yes |
| `set_nickname` | 533–536 | `identity.py` | Yes |
| `_update_identity` | 538–553 | `identity.py` | Yes |
| `start` | 559–619 | `__init__.py` | No — stays as full method (lifecycle entry point) |
| `_reset_uptime` | 621–623 | `__init__.py` | No — 3 lines |
| `stop` | 625–654 | `__init__.py` | No — stays as full method (lifecycle exit point) |
| `_on_mail_received` | 656–663 | `turn.py` | Yes |
| `_on_normal_mail` | 665–706 | `turn.py` | Yes |
| `wake` | 717–719 | `__init__.py` | No — 3-line stub |
| `log` | 721–723 | `__init__.py` | No — 3-line stub |
| `_enqueue_system_notification` | 725–805 | `turn.py` | Yes |
| `notify` | 807–814 | `turn.py` | Yes |
| `_set_state` | 816–845 | `__init__.py` | No — state machine, called from everywhere |
| `_start_soul_timer` | 847–866 | `soul_flow.py` | Yes |
| `_cancel_soul_timer` | 868–872 | `soul_flow.py` | Yes |
| `_wake_nap` | 874–877 | `__init__.py` | No — 4 lines, called from everywhere |
| `_soul_whisper` | 879–903 | `soul_flow.py` | Yes |
| `_drain_tc_inbox` | 905–950 | `soul_flow.py` | Yes |
| `_persist_soul_entry` | 952–973 | `soul_flow.py` | Yes |
| `_append_soul_flow_record` | 975–991 | `soul_flow.py` | Yes |
| `_run_inquiry` | 993–1004 | `soul_flow.py` | Yes |
| `_flatten_v3_for_pair` | 1010–1038 | `soul_flow.py` | Yes |
| `_run_consultation_fire` | 1040–1190 | `soul_flow.py` | Yes |
| `_rehydrate_appendix_tracking` | 1192–1232 | `soul_flow.py` | Yes |
| `_start_heartbeat` | 1238–1248 | `heartbeat.py` | Yes |
| `_stop_heartbeat` | 1250–1261 | `heartbeat.py` | Yes |
| `_heartbeat_loop` | 1263–1446 | `heartbeat.py` | Yes |
| `_log` | 1448–1457 | `__init__.py` | No — 10 lines, called from everywhere |
| `_run_loop` | 1463–1588 | `turn.py` | Yes |
| `_perform_refresh` | 1590–1663 | `refresh.py` | Yes |
| `_activate_preset` | 1665–1673 | `refresh.py` | Yes |
| `_can_fallback_preset` | 1675–1691 | `refresh.py` | Yes |
| `_activate_default_preset` | 1693–1698 | `refresh.py` | Yes |
| `_build_launch_cmd` | 1700–1702 | `refresh.py` | Yes |
| `_concat_queued_messages` | 1704–1728 | `turn.py` | Yes |
| `_handle_message` | 1734–1741 | `turn.py` | Yes |
| `_handle_request` | 1743–1826 | `turn.py` | Yes |
| `_handle_tc_wake` | 1828–1973 | `turn.py` | Yes |
| `_get_guard_limits` | 1975–1981 | `turn.py` | Yes |
| `_process_response` | 1987–2072 | `turn.py` | Yes |
| `_dispatch_tool` | 2078–2097 | `turn.py` | Yes |
| `_refresh_tool_inventory_section` | 2103–2117 | `prompt.py` | Yes |
| `_build_system_prompt` | 2119–2122 | `prompt.py` | Yes |
| `_build_system_prompt_batches` | 2124–2134 | `prompt.py` | Yes |
| `_build_tool_schemas` | 2136–2183 | `prompt.py` | Yes |
| `get_token_usage` | 2185–2195 | `prompt.py` | Yes |
| `_build_manifest` | 2201–2231 | `identity.py` | Yes |
| `mail` | 2233–2238 | `turn.py` | Yes |
| `has_capability` | 2240–2242 | `prompt.py` | Yes |
| `add_tool` | 2244–2272 | `prompt.py` | Yes |
| `remove_tool` | 2274–2282 | `prompt.py` | Yes |
| `override_intrinsic` | 2284–2296 | `prompt.py` | Yes |
| `_check_rules_file` | 2298–2333 | `prompt.py` | Yes |
| `_flush_system_prompt` | 2335–2342 | `prompt.py` | Yes |
| `update_system_prompt` | 2344–2356 | `prompt.py` | Yes |
| `_cpr_agent` | 2358–2365 | `__init__.py` | No — 8 lines, hook stub |
| `send` | 2367–2380 | `turn.py` | Yes |
| `get_chat_state` | 2386–2388 | `turn.py` | Yes |
| `restore_chat` | 2390–2392 | `turn.py` | Yes |
| `restore_token_state` | 2394–2396 | `turn.py` | Yes |
| `_save_chat_history` | 2398–2451 | `turn.py` | Yes |
| `status` | 2457–2523 | `identity.py` | Yes |
| `_pre_request` | 2529–2534 | `__init__.py` | No — hook, subclass-overridable |
| `_post_request` | 2536–2540 | `__init__.py` | No — hook, subclass-overridable |
| `_on_tool_result_hook` | 2542–2550 | `__init__.py` | No — hook, subclass-overridable |

**No renames. Every move is a cut-and-paste.**

---

## 6. Internal cross-references

### Dependency graph

```
__init__.py
  ├── defines BaseAgent class
  ├── constructor calls start_heartbeat, start_soul_timer (at start())
  ├── constructor calls _build_manifest, _build_identity_section (from identity.py)
  └── constructor calls _wire_intrinsics (stays inline)

identity.py
  └── no sibling imports (reads agent attributes directly)

heartbeat.py
  ├── calls agent._set_state, agent._cancel_event, agent._log (stubs on BaseAgent)
  ├── calls agent._run_inquiry (stub → soul_flow.py)
  ├── calls agent._check_rules_file (stub → prompt.py)
  ├── calls agent.send (stub → turn.py)
  ├── calls agent._save_chat_history (stub → turn.py)
  └── calls psyche.context_forget (via import)

soul_flow.py
  ├── imports from intrinsics.soul (consultation pipeline)
  ├── calls agent._set_state, agent._log, agent._wake_nap (stubs)
  ├── calls agent._save_chat_history (stub → turn.py)
  └── calls agent._session, agent._chat, agent._tc_inbox (attributes)

turn.py
  ├── calls agent._drain_tc_inbox (stub → soul_flow.py)
  ├── calls agent._set_state, agent._cancel_event, agent._log (stubs)
  ├── calls agent._session.send, agent._process_response (internal)
  ├── calls agent._pre_request, agent._post_request (hooks on BaseAgent)
  └── calls agent._build_manifest (stub → identity.py)

prompt.py
  ├── reads agent._prompt_manager, agent._intrinsics, agent._tool_schemas
  ├── calls agent._flush_system_prompt (internal to prompt.py)
  └── calls agent._session.chat.update_tools, agent._token_decomp_dirty

refresh.py
  ├── calls agent._build_launch_cmd (internal to refresh.py)
  ├── calls agent._save_chat_history (stub → turn.py)
  ├── calls agent._perform_refresh (internal to refresh.py)
  └── calls agent._log, agent._set_state (stubs)
```

**No circular imports.** Sub-modules don't import from each other — they communicate via the `agent` object. The only cross-module import is `heartbeat.py` importing `psyche.context_forget` (same pattern as the existing code).

### Shared state accessed by multiple clusters

| Attribute | Accessed by | Owner |
|---|---|---|
| `_config` | All clusters | `__init__` |
| `_session` | turn, soul_flow, prompt | `__init__` |
| `_chat` (property) | turn, soul_flow, prompt | `__init__` |
| `_prompt_manager` | identity, prompt | `__init__` |
| `_tc_inbox` | soul_flow, turn | `__init__` |
| `_intrinsics` | turn, prompt | `__init__` |
| `_tool_handlers` / `_tool_schemas` | turn, prompt | `__init__` |
| `_cancel_event` | heartbeat, turn | `__init__` |
| `_shutdown` | heartbeat, turn, soul_flow | `__init__` |
| `_asleep` | heartbeat, turn | `__init__` |
| `_state` | All clusters | `__init__` |

This is the fundamental coupling. `BaseAgent` is a god-object — every cluster reads its state. The refactor doesn't break this coupling; it makes it explicit by requiring `agent.` prefixes.

---

## 7. Risk assessment

### High-risk areas

| Risk | Why | Mitigation |
|---|---|---|
| `_heartbeat_loop` extraction | 183 lines, 15+ cross-cluster calls, runs on own thread | Verify signal-file detection by running test_system.py, test_silence_kill.py |
| `_run_consultation_fire` extraction | 150 lines, imports from intrinsics.soul, writes to soul_flow.jsonl, enqueues on tc_inbox | Verify by running test_soul_consultation.py |
| `_handle_tc_wake` extraction | 145 lines, complex splice logic with orphan healing | Verify by running test_base_agent.py |
| Constructor `__init__` modification | 260 lines, must import and call extracted helpers | Verify by running full suite |

### Cross-cluster mutation patterns

The worst cross-cluster mutation is `_save_chat_history` (turn.py) — it's called from:
- `_handle_request` (turn.py) — after every LLM response
- `_handle_tc_wake` (turn.py) — after every splice
- `_drain_tc_inbox` (soul_flow.py) — after splicing queued pairs
- `_heartbeat_loop` (heartbeat.py) — on AED timeout
- `_perform_refresh` (refresh.py) — before refresh
- `_run_loop` (turn.py) — after AED exhaustion

Since it stays as a method stub on BaseAgent, all callers use `agent._save_chat_history()`. No issue.

### Subclass hooks

Three hooks are overridden by `lingtai.agent.Agent` (the wrapper subclass):
- `_pre_request` — stays on BaseAgent
- `_post_request` — stays on BaseAgent
- `_activate_preset` — moves to `refresh.py` as a free function, but the stub on BaseAgent calls it. The Agent subclass overrides the stub. Wait — this is a problem.

Actually, `_activate_preset` is overridden by the Agent subclass. If it moves to `refresh.py` as a free function, the Agent subclass can't override it because it's no longer a method on BaseAgent. **`_activate_preset` must stay on BaseAgent as a method.** Same for `_activate_default_preset` and `_build_launch_cmd`.

This means `refresh.py` contains:
- `_perform_refresh` (the main body) — becomes a free function
- `_can_fallback_preset` — becomes a free function (not overridden)
- `_activate_preset`, `_activate_default_preset`, `_build_launch_cmd` — **stay on BaseAgent** as methods (overridden by subclass)

The stub pattern:
```python
def _perform_refresh(self) -> None:
    from .refresh import perform_refresh
    perform_refresh(self)

def _activate_preset(self, name: str) -> None:
    raise NotImplementedError(...)  # stays on class, overridden by Agent

def _can_fallback_preset(self) -> bool:
    from .refresh import can_fallback_preset
    return can_fallback_preset(self)

def _activate_default_preset(self) -> None:
    raise NotImplementedError(...)  # stays on class, overridden by Agent

def _build_launch_cmd(self) -> list[str] | None:
    return None  # stays on class, overridden by Agent
```

Similarly, `_on_tool_result_hook` and `_post_request` are hooks overridden by subclasses — they stay on BaseAgent.

**Revised `refresh.py`:** Only `_perform_refresh` and `_can_fallback_preset` move. The three hooks stay. ~90 lines extracted, ~30 lines stay.

### Where soul-style patch-point grouping may not apply

The soul refactor's pattern works best when the extracted functions have a clear entry point and limited cross-cutting state. For base_agent:

- **`_heartbeat_loop`** works well because it's a single entry point (the thread target) with well-defined state reads/writes.
- **`_run_consultation_fire`** works well because it's a single fire-and-forget call.
- **`_run_loop`** is trickier — it's the outermost event loop that calls into everything. But it's also the largest method and the core engine, so extracting it improves navigability even if the stub is thin.
- **`_handle_request` + `_handle_tc_wake`** are deeply intertwined with state (they modify `_executor`, `_last_usage`, `_cancel_event`, etc.). Extracting them works because they receive `agent` and access everything via `agent._attr`.

---

## 8. ANATOMY.md plan

### `base_agent/ANATOMY.md` (new file)

```markdown
# base_agent

Generic agent kernel. Single class `BaseAgent` with methods distributed
across 6 helper modules. `__init__.py` retains the constructor,
properties, state machine, and subclass-overridable hooks.

## Components

- `base_agent/__init__.py` — BaseAgent class. Constructor (`base_agent/__init__.py:NNN-NNN`),
  properties (`base_agent/__init__.py:NNN-NNN`), state machine (`_set_state`,
  `_wake_nap`, `_log`), hooks (`_pre_request`, `_post_request`,
  `_on_tool_result_hook`), `_wire_intrinsics`, public API stubs.
  Re-exports `_format_stamina` and `_build_identity_section` from identity.py.
- `base_agent/identity.py` — identity, manifest, status. `_format_stamina`,
  `_build_identity_section` (free functions). `set_name`, `set_nickname`,
  `_update_identity`, `_build_manifest`, `status` (agent methods → free functions).
- `base_agent/heartbeat.py` — heartbeat thread and signal-file detection.
  `_start_heartbeat`, `_stop_heartbeat`, `_heartbeat_loop`. Signal files:
  `.sleep`, `.suspend`, `.refresh`, `.prompt`, `.clear`, `.inquiry`, `.rules`,
  `.interrupt`.
- `base_agent/soul_flow.py` — soul orchestration. Timer management
  (`_start_soul_timer`, `_cancel_soul_timer`), fire callback (`_soul_whisper`),
  tc_inbox drain (`_drain_tc_inbox`), persistence (`_persist_soul_entry`,
  `_append_soul_flow_record`), inquiry (`_run_inquiry`), v3 bridge
  (`_flatten_v3_for_pair`), consultation fire (`_run_consultation_fire`),
  appendix rehydration (`_rehydrate_appendix_tracking`).
- `base_agent/turn.py` — turn engine. Main loop (`_run_loop`), message routing
  (`_handle_message`, `_handle_request`, `_handle_tc_wake`), response processing
  (`_process_response`, `_dispatch_tool`, `_get_guard_limits`), message concatenation
  (`_concat_queued_messages`), notifications (`_enqueue_system_notification`, `notify`),
  mail glue (`_on_mail_received`, `_on_normal_mail`), public send/mail API (`send`, `mail`),
  session persistence (`_save_chat_history`, `get_chat_state`, `restore_chat`,
  `restore_token_state`).
- `base_agent/prompt.py` — prompt building and tool management. System prompt
  (`_build_system_prompt`, `_build_system_prompt_batches`, `_flush_system_prompt`,
  `update_system_prompt`), tool inventory (`_refresh_tool_inventory_section`),
  tool schemas (`_build_tool_schemas`), token usage (`get_token_usage`),
  rules (`_check_rules_file`), tool registry (`add_tool`, `remove_tool`,
  `override_intrinsic`, `has_capability`).
- `base_agent/refresh.py` — refresh handshake and preset fallback.
  `_perform_refresh` (deferred relaunch protocol), `_can_fallback_preset`.
  Note: `_activate_preset`, `_activate_default_preset`, `_build_launch_cmd`
  stay on BaseAgent (subclass-overridable hooks).

## Connections

(Fill in with file:line citations after implementation.)

## State

(Fill in with file:line citations after implementation.)

## Composition

- **Parent:** `src/lingtai_kernel/` (see `ANATOMY.md`).
- **Siblings:** `intrinsics/`, `llm/`, `services/`, `i18n/`, `session.py`,
  `tc_inbox.py`, `tool_executor.py`, `loop_guard.py`, `prompt.py`,
  `meta_block.py`, `config.py`, `state.py`, `workdir.py`, `message.py`.
```

### `kernel-root ANATOMY.md` update

After the refactor, the `base_agent.py` line in Components becomes:
```
- `base_agent/` — generic agent kernel. A package of 7 modules; see `base_agent/ANATOMY.md`.
  `__init__.py` defines `BaseAgent` (constructor, properties, hooks, state machine).
```

And in Composition:
```
- **Subfolders:** `base_agent/`, `intrinsics/` (with `soul/` sub-package).
```

---

## 9. Migration plan

**One atomic commit.** Same as the soul refactor.

Arguments:
- No mock.patch targets to rewrite (zero exist)
- Only `BaseAgent` is imported externally (one symbol)
- The `__init__.py` re-export pattern is proven from the soul refactor
- The test suite validates everything
- Revert is clean (`git revert`)

Order of operations:

1. Create `base_agent/` directory.
2. Create the 6 sub-modules (`identity.py`, `heartbeat.py`, `soul_flow.py`, `turn.py`, `prompt.py`, `refresh.py`) — cut-and-paste method bodies, convert to free functions.
3. Create `__init__.py` — BaseAgent class with constructor, properties, hooks, state machine, and thin stubs calling sub-module functions.
4. Delete `base_agent.py`.
5. Update `ANATOMY.md` files: kernel-root, create `base_agent/ANATOMY.md`.
6. Verify:
   - `python -c "import lingtai_kernel"`
   - `pytest -q tests/test_base_agent.py tests/test_agent.py`
   - `pytest -q` (full suite)
7. Commit. Don't push.

Suggested message:
```
refactor(base_agent): split base_agent.py into base_agent/ package

Per discussions/base-agent-package-refactor-patch.md. Six sub-modules
(identity, heartbeat, soul_flow, turn, prompt, refresh) extracted as
free functions taking agent as first arg. BaseAgent class retains
constructor, properties, hooks, state machine, and cross-cutting stubs.
Only BaseAgent is imported externally; __init__.py re-exports it.
Zero mock.patch targets needed rewriting. New base_agent/ANATOMY.md
added; kernel-root ANATOMY.md updated.
```

---

## 10. Open design questions

1. **Should `_save_chat_history` stay on BaseAgent or move to `turn.py`?** It's called from 3 different sub-modules (turn, soul_flow, heartbeat, refresh). If it moves to `turn.py`, the other modules import it. If it stays on BaseAgent, it's a stub. I lean toward **stub** — it keeps the cross-module dependency implicit via `agent._save_chat_history()` rather than explicit via `from .turn import save_chat_history`.

2. **Should `start()` and `stop()` move to a `lifecycle.py`?** They're 60+30 lines and the entry/exit points. I kept them on `__init__.py` because they wire everything together (start calls heartbeat, soul timer, mail listener, chat restore). But if the human prefers, they could move to a `lifecycle.py` with the constructor remaining on `__init__.py`.

3. **Module naming: `soul_flow.py` vs `soul.py`?** I used `soul_flow.py` to avoid confusion with `intrinsics/soul/`. But if the human prefers `soul.py`, that's fine — the package boundary makes the distinction clear.

---

## 11. Out of scope

- **Breaking up the constructor.** The 260-line `__init__` is complex but it's the one-time wiring point. Extracting construction sub-routines into helpers would add indirection without improving navigability.
- **New abstractions.** No mediator, DI container, mixin layer, or dispatcher class. Physical separation only.
- **Behavioral changes.** No bug fixes, no method renames, no API changes.
- **Subclass (Agent) changes.** The wrapper `lingtai.agent.Agent` overrides `_activate_preset`, `_activate_default_preset`, `_build_launch_cmd`, `_pre_request`, `_post_request`, `_cpr_agent`, `has_capability`, `_build_manifest`, and `status`. After the refactor, these overrides still work because the methods stay on BaseAgent. No changes to Agent needed.
