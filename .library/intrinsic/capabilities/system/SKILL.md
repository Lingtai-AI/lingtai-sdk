---
name: system-manual
description: >
  Operational guide for the `system` tool — lifecycle control, preset
  management, notification queries, and inter-agent karma operations.

  Reach for this manual when:
    - You called `system(action='refresh')` but something didn't reload —
      your toolbox, skills, or prompt sections look stale.
    - You tried to swap presets and got a "molt first" refusal, or the
      preset you wanted showed `unreachable` in connectivity.
    - You put an agent to sleep with `lull` and it didn't wake up on mail,
      or you're unsure whether to use `suspend` vs `lull`.
    - Notifications arrived but you can't find them, or the `notification`
      action returned nothing while you know mail is unread.
    - You need to use `clear` on a stuck agent but aren't sure what gets
      preserved vs reset.

  Does NOT cover: the system tool's argument schema (in the tool
  description itself) or the kernel's internal lifecycle state machine
  (see lingtai-kernel-anatomy for that).

  Companion: `daemon-manual` for emanation-specific inspection patterns;
  `lingtai-kernel-anatomy` for the runtime loop architecture.
version: 0.1.0
---

# system manual

The `system` tool's schema description lists each action in one line. This manual is the deeper reference: what each action actually does, when to reach for it, and the gotchas that don't fit in a schema string.

## Nested reference catalog

`system-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: system-preset-workflow
  location: reference/preset-workflow/SKILL.md
  description: |
    Saved preset authorization and switching workflow: create/edit a saved
    preset, add it to an agent's `manifest.preset.allowed[]`, refresh or swap,
    verify with `system(action='presets')`, revert safely, and avoid changing
    another agent's active preset when a daemon/avatar with the preset is enough.
```

## refresh — rebuild from init.json

`system(action='refresh')` is the **only way to reload your runtime**. There is no lighter alternative.

**What it reloads:**
- MCP servers (`mcp/servers.json`)
- All capabilities (which re-scan and re-inject the skill catalog from `.lingtai/.library/`)
- Addons (imap, telegram, feishu, wechat)
- LLM provider/model
- Language
- Soul flow
- All prompt sections (covenant, principle, rules, procedures, brief, pad — including their `*_file` references)
- Admin

**What it preserves:**
- Your identity (lingtai)
- Your pad
- Your conversation history

**When to use:**
- New MCP tools installed
- Skills added/removed in `.lingtai/.library/` (yours or another agent's)
- `init.json` edited
- Prompt-section files edited (`system/covenant.md`, `system/rules.md`, etc.)
- Addon or capability config changed

Without a refresh, none of these take effect — your toolbox, skill catalog, and system prompt are snapshots from your last boot or refresh.

**Preset swapping:**
Pass `preset='<name>'` to swap to a different {LLM, capabilities} bundle before refreshing. Use `action='presets'` first to see what's available. The swap is light, takes one call, and is reversible — you remain yourself; only your current implements change.

Pass `revert_preset=true` to swap back to your default preset (reads `manifest.preset.default`).

**Context limit guard:** If your current context already exceeds the target preset's `context_limit`, the swap is refused with a "molt first" instruction. Molt to clear history, then retry.

## presets — list available presets

`system(action='presets')` returns each preset's:
- **name** and **description** (string or structured object describing tradeoffs)
- **tags** (namespaced, e.g. `tier:4`)
- **main LLM** provider + model (credentials stripped — you see provider and model only)
- **full capabilities map**
- **connectivity** field: `ok` (responsive, includes latency_ms), `no_credentials` (api_key_env is unset), or `unreachable` (network probe failed)

Use this to decide what to swap into via `refresh(preset='<name>')`.

### Saved preset authorization workflow

For the full workflow, read `reference/preset-workflow/SKILL.md`. Keep the top
level rule in mind: a saved preset file is not usable by an agent until that
agent authorizes the preset path in `manifest.preset.allowed[]`, then refreshes.

**Tier ladder** (from the `tier:*` tag):
| Tier | Use for |
|------|---------|
| 5 ★★★★★ | Irreplaceable frontier reasoning |
| 4 ★★★★ | Premium primary cognition |
| 3 ★★★ | Strong and value-priced |
| 2 ★★ | Fast and cheap, default for daemons |
| 1 ★ | Free, rate-limited, opportunistic |

## notification — query all channels

`system(action='notification')` returns a JSON dict keyed by tool name (`email`, `soul`, `mcp.<server>`, etc.). Each value is whatever the producer wrote.

**Synthesized calls:** The kernel may synthesize this call on your behalf in two situations:
1. When notifications arrive while you are idle or asleep — the kernel wakes you with this call already made
2. When you are mid-tool-chain — the next tool result may carry a `notifications:` JSON block prepended to its content

Both voluntary and synthesized calls return the same shape.

**Replace-only semantics:** Each producer has one current state, not a history of events. There is no dismiss action — producers update their own state when their situation changes (e.g. the email intrinsic re-renders the unread digest when you read mail).

## Karma operations

These require `admin.karma=True`. All target another agent by `address` (working directory path).

| Action | Effect | When to use |
|--------|--------|-------------|
| `lull` | Put another agent to sleep | Routine "go rest until needed" — they stay reachable by mail |
| `suspend` | Freeze another agent's process entirely | Rogue agent consuming budget — process death, not rest |
| `cpr` | Resuscitate a suspended agent | Bring back a suspended agent so it can receive mail |
| `interrupt` | Cancel another agent's current LLM turn | Agent is stuck in a long turn and you need it to stop |
| `clear` | Force a full molt on another agent | Agent is stuck in a broken LLM loop — resets working memory while preserving identity, pad, codex, and inbox |

**Key distinction: ASLEEP vs SUSPENDED.**
- ASLEEP: mind paused, body still listening. Mail wakes them instantly. Use `lull` for this.
- SUSPENDED: process dead. Only `cpr` (nirvana-gated) or `lingtai cpr` from the human can bring them back. Use `suspend` for this.

**`clear` details:** Archives the target's chat history and injects a recovery summary pointing at pad/codex/inbox. The `reason` parameter becomes the `source` tag in the recovery summary (defaults to your own agent name). Use this when re-sending the same message wouldn't help — the agent needs a fresh start with its durable stores intact.

## nirvana — permanent destruction

Requires `admin.karma=True` AND `admin.nirvana=True`. Permanently destroys an agent and deletes its working directory. There is no undo.

## dismiss — deprecated

Does nothing. Returns ok with a deprecation note for backward compatibility. Notifications are now state-mirrored from `.notification/<tool>.json` files — producers manage their own state. Will be removed in a future release.
