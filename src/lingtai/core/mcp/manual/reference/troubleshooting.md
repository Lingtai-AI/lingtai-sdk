# MCP troubleshooting

## Updating, deregistering

- **Update**: edit the matching line in `mcp_registry.jsonl` in place. Same schema. Then `system(action="refresh")`.
- **Deregister**: remove the matching line. Note: this does NOT stop a running MCP — to deactivate, also remove the entry from `init.json`'s `mcp` section.

## Diagnosing problems

Call `mcp(action="show")` to see:
- The current registry contents
- The `problems` list (invalid registry lines, missing config, etc.)
- A runtime health snapshot (registry path, count, status per server)

Invalid registry lines are skipped silently with a warning at refresh time, so always verify with `show` after editing.

## Common boot failures

**Boot failure with cryptic `KeyError`**
The MCP subprocess hit a missing config field. The error message *is* the missing field name. For kernel-curated addons, first re-read `reference/curated-addons.md`, then use the catalog homepage for deep provider docs if needed. For third-party Python MCPs, fetch the server README via:

```bash
~/.lingtai-tui/runtime/venv/bin/python3 \
  .library/intrinsic/capabilities/mcp/scripts/find_readme.py <pkg-name>
```

Check the exact field name spelling — `email_password` not `password`, `bot_token` not `token`, etc. This is the single most common failure mode and the docs always have the correct field name.

**`MCP server failed to start` / "command not found"**
The `command` path in your `init.json` `mcp.<name>` entry doesn't have the executable. For Python addons, confirm the venv path is correct (typically `~/.lingtai-tui/runtime/venv/bin/python`). For `npx`/`uvx` servers, confirm those tools are on `PATH`.

**Tools not appearing in your tool surface**
You forgot to `system(action="refresh")` after editing config. Refresh and re-check `mcp(action="show")`.

**HTTP 401 / 403 from an http-type server**
API key missing or malformed in the `headers` field. Format is usually `"Authorization": "Bearer <key>"`. Check the MCP's README for the exact header name and value format.

**Server boots but tool calls fail with "MCP manager not initialized" or similar**
Eager-start failed silently. Check the agent's stderr / `logs/agent.log` for the underlying exception. Usually a config field missing or wrong path. Fix and refresh.

## When in doubt

1. Read the relevant docs — `reference/curated-addons.md` for kernel-curated addons, or a third-party README via:
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/python3 \
     .library/intrinsic/capabilities/mcp/scripts/find_readme.py <pkg-name>
   ```
2. `mcp(action="show")` to see registry + problems.
3. Tail `logs/agent.log` for the actual error message.
4. Re-read the setup/troubleshooting section — most MCP docs document common errors with exact symptom strings.
