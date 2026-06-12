# Curated addons — imap / telegram / feishu / wechat / whatsapp

LingTai's first-party email and chat integrations. They now ship inside the `lingtai` distribution under `lingtai.mcp_servers.{imap,telegram,feishu,wechat,whatsapp}` so a single kernel release carries the curated MCP surface atomically. Historical `lingtai_*` import packages remain as thin compatibility wrappers. Historical standalone package names remain useful as provenance/homepage names, but the normal runtime path no longer depends on separate addon wheels.

## The four-step setup

1. **Read the curated setup docs before editing config.** The table below gives the registry/module/env/config-file names. If exact provider-specific fields are needed, inspect the shipped module resources or the catalog `homepage` for that addon. Field names like `email_password` (imap), `bot_token` (telegram), `app_id`/`app_secret` (feishu), and gewechat host (wechat) are addon-specific; do not guess them from memory.

2. **Add the addon to `init.json`.** Append the registry name to the top-level `addons:` list, then add an `mcp.<name>` activation entry with the subprocess spec from this table or the addon docs:

   ```json
   {
     "addons": ["imap"],
     "mcp": {
       "imap": {
         "type": "stdio",
         "command": "/Users/<you>/.lingtai-tui/runtime/venv/bin/python",
         "args": ["-m", "lingtai.mcp_servers.imap"],
         "env": {
           "LINGTAI_IMAP_CONFIG": ".secrets/imap.json"
         }
       }
     }
   }
   ```

3. **Create the config file** at the path referenced by the env var (e.g. `.secrets/imap.json`). Use the schema from the addon docs — copy it verbatim, don't paraphrase.

4. **Run `system(action="refresh")`.** The `mcp` capability decompresses the catalog record into `mcp_registry.jsonl`, the loader spawns the subprocess, and the omnibus tool (`imap`, `telegram`, etc.) appears in your tool surface.

## Module names

| Registry name | Historical distribution | Module name        |
|---------------|-------------------------|--------------------|
| `imap`        | formerly `lingtai-imap`     | `lingtai.mcp_servers.imap`     |
| `telegram`    | formerly `lingtai-telegram` | `lingtai.mcp_servers.telegram` |
| `feishu`      | formerly `lingtai-feishu`   | `lingtai.mcp_servers.feishu`   |
| `wechat`      | formerly `lingtai-wechat`   | `lingtai.mcp_servers.wechat`   |
| `whatsapp`    | formerly `lingtai-whatsapp` | `lingtai.mcp_servers.whatsapp` |

Use the module name in `mcp.<name>.args`, e.g. `["-m", "lingtai.mcp_servers.feishu"]`. Historical distribution names are retained only for provenance and compatibility notes.

## After it's running

Inbound events (new emails, chat messages) flow into your `.mcp_inbox/<name>/` via the LICC v1 inbox callback contract — the kernel auto-injects them into your next turn as `[system]` messages. You don't poll; the kernel does. Outbound calls go through the omnibus tool: `imap(action="send", ...)`, `telegram(action="send_message", ...)`, etc. — see each addon's README for the action list.

## WeChat setup checklist

WeChat has unique pitfalls that catch agents off-guard. Walk this checklist on every new WeChat setup to avoid wasting the human's time:

1. **Ensure LingTai's runtime venv is current** — the `lingtai-wechat-bootstrap` script is installed by the `lingtai` wheel and lives inside the venv, not necessarily on the system PATH.

2. **Run bootstrap with the full venv path** from the project root:
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/lingtai-wechat-bootstrap .secrets/wechat
   ```

3. **No manual credential copy needed** — the MCP resolves `LINGTAI_WECHAT_CONFIG` relative to the project root (the parent of `.lingtai/`), so `.secrets/wechat/config.json` works from both bootstrap and the MCP. Credentials are written next to `config.json`.

4. **WSL users**: bootstrap auto-detects WSL and uses `cmd.exe /c start` or `wslview` to open the browser. If neither works, it prints the HTML file path for manual opening.

5. **Refresh the MCP** after bootstrap writes credentials:
   ```
   system(action="refresh")
   ```

6. **Test the connection**:
   ```
   wechat(action="check")
   ```

7. **Session expiry** — WeChat sessions expire (~30 days). When expired, a LICC event with `metadata.event_type: "session_expired"` arrives. Re-run the bootstrap to re-authenticate.
