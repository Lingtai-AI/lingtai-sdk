# Third-party and legacy MCP routes

Two routes for non-curated MCPs: the **registry route** (recommended, gated by `mcp_registry.jsonl`) and the **legacy `mcp/servers.json` route** (ungated, kept for quick experiments).

## Registry route (recommended)

For any non-curated MCP — typically `npx`/`uvx`-launched servers from the broader MCP ecosystem.

1. **Fetch the MCP's setup doc.** If it's pip-installed, use the bundled script:
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/python3 \
     .library/intrinsic/capabilities/mcp/scripts/find_readme.py <pkg-name>
   ```
   Otherwise (npx/uvx servers), `web_read` the homepage URL. Either way, get the install command, env vars, and config schema before writing any config.
2. Append a single JSON record to `mcp_registry.jsonl` (one line, atomic write). For the schema, see `lingtai-kernel-anatomy reference/file-formats.md` §6.5.
3. Add an `init.json` `mcp.<name>` activation entry.
4. Run `system(action="refresh")`.

Benefits: gives you the `<homepage>` field (used by `SKILL.md` §Reading an MCP's README → fallback URL), allow-listing, and registry health diagnostics via `mcp(action="show")`.

## Legacy `mcp/servers.json` route

A second route still exists: `<working_dir>/mcp/servers.json`. The kernel loads it on startup with no registry validation — useful for quick experiments or for personal MCPs you don't want to register globally. Same JSON shape as the registry route, but mounted directly without the catalog → registry → active promotion.

```json
{
  "vision": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@z_ai/mcp-server"],
    "env": {
      "Z_AI_API_KEY": "your-key",
      "Z_AI_MODE": "ZAI"
    }
  },
  "web-search": {
    "type": "http",
    "url": "https://api.z.ai/api/mcp/web_search_prime/mcp",
    "headers": {
      "Authorization": "Bearer your-key"
    }
  }
}
```

Use the registry route for anything you want to keep. Use `mcp/servers.json` when you just want to wire up a single server quickly.

## Server config fields (both routes)

| Field      | stdio                       | http                              |
|------------|-----------------------------|-----------------------------------|
| `type`     | `"stdio"` (default)         | `"http"` (required)               |
| `command`  | executable (e.g. `npx`)     | —                                 |
| `args`     | command-line arguments      | —                                 |
| `env`      | env vars for the subprocess | —                                 |
| `url`      | —                           | streamable-http endpoint          |
| `headers`  | —                           | HTTP headers (typically auth)     |

## API keys and secrets

Plaintext credentials in `mcp_registry.jsonl` or `mcp/servers.json` are the simplest path. For sensitive keys:

- **stdio servers**: env vars in the `env` field referencing values from `.env` (the agent's `env_file`). Some servers, including curated integrations such as `imap`, require literal credentials in a separate config file pointed at by an env var — see the relevant setup docs before writing secrets.
- **http servers**: keys go in the `headers` field (typically `Authorization: Bearer ...`).
- Never commit `mcp/servers.json` or addon config files to version control if they contain secrets.
