---
name: mcp-manual
description: >
  Operational guide for the `mcp` capability — register, activate, update,
  deregister, and troubleshoot MCP (Model Context Protocol) servers in your
  agent. Single source of truth for both generic MCP setup AND the six
  kernel-curated LingTai addon MCPs (`imap`, `telegram`, `feishu`, `wechat`, `whatsapp`, `cloud_mail`).

  Reach for this manual when:
    - The human asks to install, set up, configure, or remove an MCP server.
      Decision tree: is it kernel-curated (imap/telegram/feishu/wechat/whatsapp/cloud_mail) → the
      `addons:` + `init.json mcp.<name>` workflow using modules shipped inside
      the `lingtai` wheel is here. Is it third-party → the registry route OR the legacy
      `mcp/servers.json` route, both documented here.
    - The human asks to set up the `imap` / `telegram` / `feishu` / `wechat`
      addon, or any LingTai email/chat integration. **Step 1 is always**:
      read the curated-addon setup section and, when exact config fields are
      needed, inspect the shipped module docs/resources or the catalog homepage.
      Field names like `email_password`, `bot_token`, `app_id`/`app_secret`,
      and gewechat hosts are documented by the addon docs — do NOT guess.
    - You want to know what MCPs you currently have. `mcp(action="show")`
      returns the registry plus health; this manual explains the output.
    - An MCP isn't behaving — registry validation, `problems` list,
      refresh-after-edit verification, common boot errors are here.
    - You're exploring an unfamiliar third-party MCP. The doc-discovery flow
      (local `scripts/find_readme.py` first, homepage URL fallback) is here.

  Covers (progressively, via reference/): the three states (catalog →
  registry → active), the curated-vs-third-party install paths, the legacy
  `mcp/servers.json` direct mount route (still functional, ungated), HTTP
  and stdio server configurations, where the registry file
  (`mcp_registry.jsonl`) lives and how to mutate it (`write` / `edit` /
  `bash` — the `mcp` capability is read-only), the `<homepage>` field as
  fallback documentation, and the relationship between `init.json`'s
  `addons:` list, `mcp:` activation entries, and the registry. Replaces the
  deprecated `lingtai-mcp` skill.

  Does NOT cover the protocol spec itself: schema validation rules, env
  injection mechanics (the `LINGTAI_AGENT_DIR` / `LINGTAI_MCP_NAME`
  variables), the LICC v1 inbox callback contract, and the validator's
  internal logic all live in `lingtai-kernel-anatomy reference/mcp-protocol.md`.
  Read this for *what to do*; read anatomy for *how it works*.
version: 3.2.0
last_changed_at: "2026-06-24T16:26:32-07:00"
---

# MCP Capability — How To Use It

The `mcp` capability is your interface to Model Context Protocol (MCP) servers — both generic third-party servers and the six kernel-curated LingTai addons (`imap`, `telegram`, `feishu`, `wechat`, `whatsapp`, `cloud_mail`). Like the `library` capability, it is **pure presentation**: registered MCPs are listed in your system prompt under `<registered_mcp>`, and the registry itself is a JSONL file you edit directly with `write` / `edit` / `bash`.

This is the router. Detail lives in `reference/`. Load only what you need.

## Three states of an MCP

For any MCP server, relative to this agent:

1. **In the kernel catalog** — LingTai blesses it. Reference template ships with the kernel. The six curated addons live here: `imap`, `telegram`, `feishu`, `wechat`, `whatsapp`, `cloud_mail`.
2. **Officially registered** — appears as a line in `mcp_registry.jsonl` (sibling to `init.json`). The system prompt's `<registered_mcp>` lists it.
3. **Active** — the MCP server subprocess is running, its tools are mounted in your tool surface.

Promotion path: catalog → registry → active. You move things along by editing files and calling `system(action="refresh")`.

## Pick a sub-skill

| Task | Read |
|---|---|
| Set up an `imap` / `telegram` / `feishu` / `wechat` / `whatsapp` / `cloud_mail` addon | `reference/curated-addons.md` |
| Add a third-party MCP (`npx`/`uvx`/HTTP) | `reference/third-party-and-legacy.md` |
| Wire up a server quickly via `mcp/servers.json` (legacy/ungated) | `reference/third-party-and-legacy.md` |
| MCP not behaving / cryptic boot errors / `KeyError: 'foo'` | `reference/troubleshooting.md` |
| Update or deregister an MCP | `reference/troubleshooting.md` |
| Spec-level questions (schema, env injection, LICC) | `lingtai-kernel-anatomy reference/mcp-protocol.md` |

**Before curated addon setup**, start with `reference/curated-addons.md`; those first-party servers now ship inside the `lingtai` wheel under `lingtai.mcp_servers.*`; historical `lingtai_*` packages remain as thin compatibility wrappers.

**Before third-party setup or troubleshooting**, fetch the relevant server README with:

```bash
~/.lingtai-tui/runtime/venv/bin/python3 \
  .library/intrinsic/capabilities/mcp/scripts/find_readme.py <pkg-name>
```

For third-party Python MCPs, `<pkg-name>` is the installed distribution name. Full details: see §Reading an MCP's README below.

## Reading an MCP's README

Every MCP server's README is the canonical install + config + troubleshooting doc — config field names, env vars, error meanings, the lot. **Always read the relevant docs before guessing at config.** For kernel-curated addons, begin with `reference/curated-addons.md` and use the catalog homepage when provider-specific detail exceeds the bundled note. For third-party servers, read the README.

### 1. Local README (preferred for third-party Python MCPs)

If the MCP is installed as its own Python package, run the script with the **runtime venv's Python** — the same interpreter where the server package is actually installed:

```bash
~/.lingtai-tui/runtime/venv/bin/python3 \
  .library/intrinsic/capabilities/mcp/scripts/find_readme.py <pkg-name>
```

(`python3` from your `$PATH` may resolve to a system or conda interpreter that doesn't see the venv's installed packages — always use the venv's Python explicitly.)

The script tries the editable repo on disk first, then falls back to the README embedded in the wheel's `METADATA` file (PEP 566). Works for editable installs and normal PyPI wheels alike. Pass `--module <modname>` if you only know the importable module name instead of the distribution name. For embedded curated modules, `--module lingtai.mcp_servers.imap` resolves to the owning `lingtai` distribution, so use `reference/curated-addons.md` for the concise setup contract and the homepage for deep provider docs.

Curated addon modules shipped by the `lingtai` wheel:

| Registry name | Historical distribution | Module name        |
|---------------|-------------------------|--------------------|
| `imap`        | formerly `lingtai-imap`     | `lingtai.mcp_servers.imap`     |
| `telegram`    | formerly `lingtai-telegram` | `lingtai.mcp_servers.telegram` |
| `feishu`      | formerly `lingtai-feishu`   | `lingtai.mcp_servers.feishu`   |
| `wechat`      | formerly `lingtai-wechat`   | `lingtai.mcp_servers.wechat`   |
| `whatsapp`    | formerly `lingtai-whatsapp` | `lingtai.mcp_servers.whatsapp` |
| `cloud_mail`  | (no standalone distribution) | `lingtai.mcp_servers.cloud_mail` |

### 2. Homepage URL (fallback)

If the script prints `ERROR: no README found locally` (or the MCP isn't a Python package — e.g. an `npx`-launched server), fetch the registry's `<homepage>` field with `web_read`. Each registered MCP exposes this when known.

### 3. Runtime self-description (last resort)

If neither path yields docs, fall back to the MCP's own runtime self-description: once activated, its tool descriptions appear in your tool surface, and many servers also publish a server-level `instructions` string at connection time.

## Tool surface

One action: `mcp(action="show")`. Returns this manual body, the current registry contents, and a runtime health snapshot (registry path, count, problems).

Each `registered` entry may also carry a non-secret **`identity`** block, so you can tell *which* configured account/bot/channel an MCP surface represents without reading private config:

```json
{
  "name": "telegram",
  "summary": "...",
  "identity": {
    "mcp": "telegram",
    "account_count": 1,
    "last_verified_at": "2026-06-24T09:59:00+00:00",
    "accounts": [
      {"alias": "main", "bot_username": "my_agent_bot", "bot_id": 123456789,
       "bot_display_name": "My Agent", "is_bot": true}
    ]
  }
}
```

Identity comes from the addon-written, non-secret document at `system/mcp_identities/<name>.json` (schema `lingtai.mcp.identity.v1`), surfaced both here and as an `<identity>` block under the server in your `<registered_mcp>` prompt section. It is a **strict allowlist projection** — only non-secret identity fields (alias, provider username/id/display name, non-secret routing counts) are ever shown; tokens, passwords, app secrets, refresh/access tokens, headers, and any unrecognized field are dropped. The block appears only for servers that have published an identity file (currently the curated messaging addons: `telegram`, `feishu`, `wechat`, `whatsapp`); it is absent otherwise and reflects each account's last-cached state (no live network call). For richer per-account detail, the addon's own `accounts` action remains authoritative.

All registry mutations happen via `write` / `edit` / `bash`. The `mcp` capability never writes to the registry.

## See also

- **Canonical spec**: `lingtai-kernel-anatomy reference/mcp-protocol.md` — full three-layer model, env injection, validator schema, **LICC v1** inbox callback contract, reference implementations.
- **File formats**: `lingtai-kernel-anatomy reference/file-formats.md` §2.7 (init.json `addons` + `mcp` fields), §6 (`mcp/servers.json` legacy direct mounts), §6.5 (`mcp_registry.jsonl`), §6.6 (`.mcp_inbox/<name>/<id>.json` LICC events).

## Cleanup / Footprint

MCP itself owns registry/configuration state (`mcp_registry.jsonl`, optional
`mcp/servers.json`, and `.mcp_inbox/<name>/...` LICC event files). Curated addon
packages such as Telegram/Feishu/WeChat/IMAP also maintain their own data
stores; their README/manual is responsible for declaring addon-specific cleanup
such as downloaded voice/audio attachments. Do not delete credentials or active
registry entries as a cleanup shortcut.

Footprint check (read-only, records the audit):

```bash
python3 - <<'PY'
import json, time
from pathlib import Path
agent = Path.cwd()
roots = [p for p in [agent / "mcp_registry.jsonl", agent / "mcp", agent / ".mcp_inbox"] if p.exists()]
def size(p): return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
rows = [(p, size(p)) for p in roots]
total = sum(s for _, s in rows)
print(f"mcp roots: {len(rows)}; bytes: {total}")
for p, s in rows: print(f"{s:>12}  {p}")
log = agent / "logs" / "cleanup.jsonl"; log.parent.mkdir(parents=True, exist_ok=True)
log.open("a", encoding="utf-8").write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": "mcp", "dry_run": True, "candidates": len(rows), "bytes": total, "human_approved": False, "summary": "mcp footprint audit"}) + "\n")
PY
```

Recommended cadence: after adding/removing MCP servers, when `.mcp_inbox` grows,
and before sharing a project. Cleanup requires explicit user consent after the
dry-run report, and the audit/apply step must be recorded in `logs/cleanup.jsonl`. Prefer deregistering/updating registry files followed by
`system(action="refresh")` over deleting registry state by hand.
