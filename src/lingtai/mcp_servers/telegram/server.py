"""LingTai Telegram MCP server.

Exposes a single omnibus ``telegram`` MCP tool that dispatches to
TelegramManager for all 11 actions (send, check, read, reply, search,
delete, edit, contacts, add_contact, remove_contact, accounts). Inbound
Telegram updates flow into the host agent's inbox via LICC.

Configuration:
    LINGTAI_TELEGRAM_CONFIG  — path to a JSON config file (required).

Config schema (plaintext, no env-indirection):

    {
      "accounts": [
        {
          "alias": "myagent",
          "bot_token": "1234567890:ABC...",
          "allowed_users": [12345678],     // optional allow-list of user IDs
          "poll_interval": 1.0,             // optional, seconds
          "commands": [                      // optional, registered via setMyCommands
            {"command": "status", "description": "Show agent status"}
          ]
        }
      ]
    }

Env vars injected by the LingTai kernel for LICC:
    LINGTAI_AGENT_DIR — host agent's working directory.
    LINGTAI_MCP_NAME  — this MCP's registry name (typically "telegram").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .licc import push_inbox_event
from .manager import TelegramManager, SCHEMA, DESCRIPTION
from .service import TelegramService

log = logging.getLogger("lingtai.mcp_servers.telegram")


_SERVER_INSTRUCTIONS = (
    "lingtai-telegram: Telegram bot client. "
    "Configure via the LINGTAI_TELEGRAM_CONFIG env var pointing at a JSON file. "
    "Inbound messages flow into the host agent's inbox via LICC. "
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-telegram"
)

_PROFILE_MIME = "application/vnd.lingtai.mcp-profile+json"
_MARKDOWN_SKILL_MIME = "text/markdown; profile=lingtai-skill"
_MARKDOWN_MIME = "text/markdown"
_JSON_MIME = "application/json"
_HTML_MIME = "text/html"

_MANIFEST_URI = "lingtai://manifest"
_SKILL_URI = "lingtai://skills/telegram"
_CONFIG_DOC_URI = "lingtai://docs/configuration"
_TROUBLESHOOTING_DOC_URI = "lingtai://docs/troubleshooting"
_STATUS_URI = "lingtai://status"
_ONBOARDING_DOC_URI = "lingtai://onboarding/telegram"
_ONBOARDING_TEMPLATE_URI = "lingtai://onboarding/html-template"

_RESOURCE_INDEX = [
    {
        "uri": _MANIFEST_URI,
        "name": "LingTai MCP profile manifest",
        "mimeType": _PROFILE_MIME,
        "description": "Machine-readable LingTai profile for this Telegram MCP server.",
    },
    {
        "uri": _SKILL_URI,
        "name": "Telegram pointer skill",
        "mimeType": _MARKDOWN_SKILL_MIME,
        "description": "Thin agent-facing routing hint for Telegram MCP usage.",
    },
    {
        "uri": _CONFIG_DOC_URI,
        "name": "Telegram configuration guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Authoritative config fields, secrets, activation, and security notes.",
    },
    {
        "uri": _TROUBLESHOOTING_DOC_URI,
        "name": "Telegram troubleshooting guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Common setup/runtime failures and diagnostic steps.",
    },
    {
        "uri": _STATUS_URI,
        "name": "Telegram safe status",
        "mimeType": _JSON_MIME,
        "description": "Redacted runtime status derived from config and manager state.",
    },
    {
        "uri": _ONBOARDING_DOC_URI,
        "name": "Telegram browser/HTML onboarding guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Agent-facing recipe for obtaining/entering a Telegram bot token from BotFather, wiring allowed_users and the /start handshake, generating a local HTML setup-checklist page; covers verification via lingtai://status and secret redaction.",
    },
    {
        "uri": _ONBOARDING_TEMPLATE_URI,
        "name": "Telegram onboarding HTML template",
        "mimeType": _HTML_MIME,
        "description": "Self-contained, secret-free static HTML setup-checklist page with a {{SETUP}} placeholder, ready to write to disk and open in a browser.",
    },
]



def _package_version() -> str:
    try:
        return version("lingtai-telegram")
    except PackageNotFoundError:
        try:
            return version("lingtai")
        except PackageNotFoundError:  # editable checkout without installation metadata
            return "0+local"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _canonical_resource_uri(uri: object) -> str:
    return str(uri).rstrip("/")


def _redact_token(token: object) -> str | None:
    if not token:
        return None
    token_s = str(token)
    if ":" in token_s:
        prefix = token_s.split(":", 1)[0]
        return f"{prefix}:***"
    if len(token_s) <= 8:
        return "***"
    return f"{token_s[:4]}…{token_s[-4:]}"


def _safe_status_payload(manager: TelegramManager | None) -> dict[str, Any]:
    """Return runtime status without exposing bot tokens or raw config."""
    config_path_raw = os.environ.get("LINGTAI_TELEGRAM_CONFIG")
    config_path = None
    config_readable = False
    accounts: list[dict[str, Any]] = []
    notes: list[str] = []

    if config_path_raw:
        try:
            path = Path(config_path_raw).expanduser()
            if not path.is_absolute():
                base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
                path = base / path
            config_path = str(path)
            if path.is_file():
                config_readable = True
                cfg = json.loads(path.read_text(encoding="utf-8"))
                for account in cfg.get("accounts") or []:
                    allowed_users = account.get("allowed_users")
                    commands = account.get("commands") or []
                    accounts.append({
                        "alias": account.get("alias"),
                        "bot_token": _redact_token(account.get("bot_token")),
                        "has_bot_token": bool(account.get("bot_token")),
                        "allowed_users_count": (
                            len(allowed_users) if isinstance(allowed_users, list) else None
                        ),
                        "poll_interval": account.get("poll_interval"),
                        "commands_count": len(commands) if isinstance(commands, list) else 0,
                    })
            else:
                notes.append("Telegram config path is set but the file is not readable.")
        except Exception as exc:  # status must never leak raw config or fail hard
            notes.append(f"Could not read Telegram config safely: {type(exc).__name__}: {exc}")
    else:
        notes.append("LINGTAI_TELEGRAM_CONFIG is not set.")

    service_started = False
    if manager is not None:
        try:
            service_started = bool(getattr(manager._service, "_running", False))
        except Exception:
            service_started = False

    status = "ok" if manager is not None else "degraded"
    return {
        "status": status,
        "manager_initialized": manager is not None,
        "service_started": service_started,
        "config_path_set": bool(config_path_raw),
        "config_path": config_path,
        "config_readable": config_readable,
        "accounts_count": len(accounts),
        "accounts": accounts,
        "notes": notes,
    }


def _profile_manifest(manager: TelegramManager | None) -> dict[str, Any]:
    return {
        "schema": "lingtai.mcp.profile.v1",
        "server": {
            "name": "lingtai-telegram",
            "registry_name": "telegram",
            "version": _package_version(),
            "summary": "Telegram Bot API client with LICC inbox callback.",
            "homepage": "https://github.com/Lingtai-AI/lingtai-telegram",
        },
        "ownership": {
            "configuration": "This MCP owns Telegram config fields, Bot API caveats, and diagnostics.",
            "human_ui": "LingTai TUI /mcp is the human-facing control panel and should render these resources generically.",
            "agent_interface": "Agents should use MCP tools/resources/prompts directly; LingTai skills are thin discovery pointers.",
        },
        "resources": _RESOURCE_INDEX,
        "tools": [
            {
                "name": "telegram",
                "description": "Omnibus Telegram tool for send/check/read/reply/search/delete/edit/contacts/accounts.",
                "actions": [
                    "send", "check", "read", "reply", "search", "delete", "edit",
                    "contacts", "add_contact", "remove_contact", "accounts",
                ],
            }
        ],
        "agent_entrypoints": {
            "skill": _SKILL_URI,
            "configuration": _CONFIG_DOC_URI,
            "troubleshooting": _TROUBLESHOOTING_DOC_URI,
            "status": _STATUS_URI,
            "onboarding": _ONBOARDING_DOC_URI,
            "onboarding_html_template": _ONBOARDING_TEMPLATE_URI,
        },
        "status": _safe_status_payload(manager),
    }


def _skill_markdown() -> str:
    return """---
name: telegram
summary: Thin routing hint for the lingtai-telegram MCP server.
---

# Telegram MCP pointer skill

This MCP is the authoritative source for Telegram Bot API setup and runtime
behavior. Do not copy platform details into a LingTai skill. Instead:

1. Read `lingtai://manifest` to discover this server's LingTai profile.
2. Read `lingtai://docs/configuration` for config fields, secrets, and activation.
3. Read `lingtai://docs/troubleshooting` for setup/runtime failures.
4. Read `lingtai://status` for safe, redacted runtime status.
5. Use the `telegram` MCP tool for agent-facing operations.

Human-facing setup should be rendered by LingTai's `/mcp` control panel from
these resources; agents use MCP tools/resources/prompts directly.
"""


def _configuration_markdown() -> str:
    return """# lingtai-telegram configuration

`lingtai-telegram` is a Telegram Bot API MCP server. It is configured via a
JSON file whose path is supplied in `LINGTAI_TELEGRAM_CONFIG`.

## Environment

- `LINGTAI_TELEGRAM_CONFIG` — path to the JSON config file. Relative paths are
  resolved against `LINGTAI_AGENT_DIR` when present.
- `LINGTAI_AGENT_DIR` — injected by LingTai; used for state, contacts, and LICC.
- `LINGTAI_MCP_NAME` — injected by LingTai; usually `telegram`.

## Config schema

```json
{
  "accounts": [
    {
      "alias": "myagent",
      "bot_token": "1234567890:ABC...",
      "allowed_users": [12345678],
      "poll_interval": 1.0,
      "commands": [
        {"command": "status", "description": "Show agent status"}
      ]
    }
  ]
}
```

Required fields:

- `accounts` — non-empty list.
- `accounts[].bot_token` — BotFather token. Keep it secret; do not print it in
  logs, chat, issues, or PRs.

Common optional fields:

- `alias` — account alias used by compound message IDs and the `account` tool
  argument. Defaults are handled by the manager if omitted.
- `allowed_users` — list of Telegram user IDs allowed to contact the bot.
- `poll_interval` — update polling interval in seconds.
- `commands` — commands registered with Telegram `setMyCommands`.

## Tool entrypoint

Use the `telegram` tool with actions: `send`, `check`, `read`, `reply`,
`search`, `delete`, `edit`, `contacts`, `add_contact`, `remove_contact`, and
`accounts`. Compound message IDs have the form `account_alias:chat_id:message_id`.

For long-running work, `send` may be used with `chat_action` (for example
`typing`) and no `text`/`media` to show a Telegram status indicator.

## Contacts vs inbound permissions

`contacts` and `add_contact` manage local aliases so an agent can remember and
message a `chat_id` more conveniently. They do **not** grant inbound permission.

Inbound messages are filtered by `accounts[].allowed_users`. If the bot can send
messages to a person but their replies never appear in `check`, `read`, search,
or the agent notification stream, compare the person's Telegram user ID with the
account's `allowed_users` list. To let a newly added contact talk to the bot, add
that user ID to `allowed_users` in the config JSON and refresh/restart the MCP so
the allow-list is reloaded.
"""


def _troubleshooting_markdown() -> str:
    return """# lingtai-telegram troubleshooting

## `LINGTAI_TELEGRAM_CONFIG env var not set`

Set `LINGTAI_TELEGRAM_CONFIG` to the config JSON path. Relative paths resolve
against `LINGTAI_AGENT_DIR`.

## `Telegram config not found`

Check the path in `LINGTAI_TELEGRAM_CONFIG`, file permissions, and whether the
agent was refreshed after config changes.

## `config must contain 'accounts' (list)`

The JSON must contain a non-empty `accounts` list.

## Invalid or stale bot token

Verify the BotFather token out-of-band. Never paste the full token into chat,
logs, issues, or PRs. Rotate the token if it was exposed.

## Bot cannot DM the human

Telegram bots cannot initiate a private chat until the human opens the bot and
presses Start or sends a first message. Ask the human to open the bot link and
send `/start`, then retry.

## No inbound messages arrive

Check that the MCP process is active, `poll_interval` is reasonable, the bot can
reach Telegram, and `allowed_users` includes the sender's Telegram user ID. Read
`lingtai://status` for redacted config/runtime state.

Do not confuse saved contacts with inbound authorization. `add_contact` only
creates a local alias for a `chat_id`; it does not edit the config file and does
not add the user to `allowed_users`. A common failure mode is: the bot can send
to a saved contact, but that user's messages never arrive. In that case, add the
user ID to `accounts[].allowed_users`, refresh/restart the MCP, then ask the
human to reply again in the same bot chat.

## Agent-facing vs human-facing interface

`/mcp` is the human-facing TUI control panel. Agents should use this MCP's
resources and `telegram` tool directly.
"""


def _onboarding_markdown() -> str:
    return """# lingtai-telegram browser/HTML onboarding

This resource is the agent-facing recipe for walking a human through a *local*
HTML + browser onboarding page for the `lingtai-telegram` MCP. It complements
`lingtai://docs/configuration` and `lingtai://docs/troubleshooting`, which
remain authoritative for config fields and failure modes.

This MCP owns onboarding; the LingTai `/mcp` TUI is the human control panel and
renders these resources generically. Agents drive onboarding through the
resources below — do not embed Telegram platform details in a LingTai skill.

## What "onboarding" means for Telegram (no QR/scan login)

A Telegram bot authenticates with a **bot token** — a string of the form
`<digits>:<rest>` — issued by **BotFather**. There is **no QR/scan login flow**
for a Telegram bot. Onboarding is therefore about helping a human:

1. Create (or open) a bot with **BotFather** (`/newbot`) and copy its
   **bot token**.
2. Put the token into the `lingtai-telegram` config JSON under
   `accounts[].bot_token` (never into the onboarding page).
3. Decide who may talk to the bot and list their Telegram **user/chat IDs** in
   `accounts[].allowed_users`.
4. Have each allowed human open the bot and press **Start** / send `/start` so
   Telegram lets the bot DM them (the bot cannot initiate a private chat first).
5. Choose the inbound transport — this MCP uses **long polling** (no public
   webhook required); inbound updates flow into the host agent's inbox via LICC.
6. Verify the result with `lingtai://status`.

## When to use this

A human needs to connect a Telegram bot as the backend for the first time, or
after rotating the bot token via BotFather (`/revoke`).

## Setup paths (pick one)

1. **Direct config edit (recommended).** Read `lingtai://docs/configuration`
   for the exact schema, then write the `bot_token` and `allowed_users` into the
   config JSON pointed at by `LINGTAI_TELEGRAM_CONFIG`. Refresh the MCP
   afterward. Remember that saving a user with `add_contact` is not enough for
   inbound messages; each person who should be able to talk to the bot must be
   present in `allowed_users`.

2. **Agent-generated local HTML checklist page.** When a human wants a clean,
   readable, at-a-glance setup checklist in the browser (e.g. to follow along
   while clicking through BotFather), read `lingtai://onboarding/html-template`,
   substitute the `{{SETUP}}` placeholder with **non-secret** setup context
   (config file path, account alias, the allow-list of user IDs, the `/start`
   handshake reminder, and a link-free step list), write it to a local file, and
   open it. The template is self-contained — no scripts, no external assets, no
   secrets — so it is safe to drop on disk.

## Generating the local HTML page (path 2)

1. Read the `lingtai://onboarding/html-template` resource.
2. Replace the `{{SETUP}}` placeholder with **non-secret** setup context only:
   the config file path, the account `alias`, the `allowed_users` IDs, the
   `/start` handshake reminder, the poll-based transport, and a short ordered
   checklist. HTML-escape any dynamic text you insert so nothing can inject
   markup into the local `file://` page.
3. **Never** put the `bot_token` (or any credential value) into the page. The
   page is a *checklist*, not a credential store. Redact: the only safe thing to
   show is the *field name* `bot_token`, never its value.
4. Write the result to a local file (e.g. `./telegram-onboarding.html`).
5. Open it in the default browser (`open` / `xdg-open` / `cmd.exe /c start`).
6. Walk the human through entering the token into the **config JSON** (not the
   page), then refresh the MCP.

## Verifying setup

Use `lingtai://status` to confirm the result. It reports, per account,
`has_bot_token` (boolean), a **redacted** `bot_token` (only a non-secret
prefix), `allowed_users_count`, `poll_interval`, and `commands_count`, and
**never** returns the full token. A healthy setup shows `config_readable: true`
and the expected `accounts_count`.

## Secret handling

- The `bot_token` is the only secret credential. **Never** paste, echo, log,
  commit, or render the bot token into chat, issues, PRs, or the generated HTML
  page. Always **redact** it. The onboarding template is intentionally
  secret-free and must stay that way.
- Telegram user/chat IDs in `allowed_users` are non-secret identifiers and may
  appear in status and the checklist page.
- If a bot token is ever exposed, rotate it in BotFather (`/revoke`).

## After setup

Refresh the `lingtai-telegram` MCP so it picks up the new token. See
`lingtai://docs/troubleshooting` if `/start` handshake, allow-list, or polling
errors appear.
"""


def _onboarding_html_template() -> str:
    return _ONBOARDING_HTML


def _resource_payloads(manager: TelegramManager | None) -> dict[str, tuple[str, str]]:
    return {
        _MANIFEST_URI: (_PROFILE_MIME, _json_dumps(_profile_manifest(manager))),
        _SKILL_URI: (_MARKDOWN_SKILL_MIME, _skill_markdown()),
        _CONFIG_DOC_URI: (_MARKDOWN_MIME, _configuration_markdown()),
        _TROUBLESHOOTING_DOC_URI: (_MARKDOWN_MIME, _troubleshooting_markdown()),
        _STATUS_URI: (_JSON_MIME, _json_dumps(_safe_status_payload(manager))),
        _ONBOARDING_DOC_URI: (_MARKDOWN_MIME, _onboarding_markdown()),
        _ONBOARDING_TEMPLATE_URI: (_HTML_MIME, _onboarding_html_template()),
    }


# Static, self-contained onboarding HTML template. No JavaScript, no external
# assets, and no secrets — an agent reads this, substitutes a non-secret setup
# checklist into the ``{{SETUP}}`` placeholder, writes it to a local file, and
# opens it in a browser. Telegram bots have no QR/scan login, so this is a setup
# *checklist* page, not a login page. The bold banner reminds the human that the
# ``bot_token`` belongs in the config JSON, never in this page.
_ONBOARDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LingTai - Telegram bot setup checklist</title>
<style>
  :root {
    color-scheme: light dark;
    --fg: #1a1a1a;
    --bg: #fafafa;
    --accent: #2d6cdf;
    --warn-fg: #8a1a1a;
    --warn-bg: #fce8e6;
    --warn-border: #d04040;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fg: #eee; --bg: #181818;
      --warn-fg: #ffb3a8;
      --warn-bg: #3a1a1a;
      --warn-border: #c45050;
    }
  }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .card { max-width: 640px; padding: 2.2em 2em; }
  h1 { font-size: 1.4em; margin: 0 0 .3em; }
  p { line-height: 1.5; margin: .5em 0; }
  code { background: rgba(127,127,127,.18); padding: .1em .35em; border-radius: 4px; }
  .setup { margin: 1.2em 0; }
  .hint { opacity: .75; font-size: .9em; }
  .warn {
    background: var(--warn-bg);
    color: var(--warn-fg);
    border: 1px solid var(--warn-border);
    border-radius: 8px;
    padding: .9em 1em;
    margin: 0 0 1.2em;
    font-size: .95em;
  }
  .warn strong { display: block; margin-bottom: .25em; font-size: 1em; }
  .footnote {
    border-top: 1px solid var(--warn-border);
    margin-top: 1.4em;
    padding-top: .9em;
    font-size: .82em;
    opacity: .85;
  }
</style>
</head>
<body>
  <div class="card">
    <div class="warn" role="alert">
      <strong>&#9888; Do not paste your bot token into this page</strong>
      This is a read-only setup checklist. Your Telegram bot token is a
      credential — it belongs only in the <code>lingtai-telegram</code> config
      JSON, never in this HTML page, chat, issues, or PRs. Never share it;
      rotate it in BotFather if it leaks.
    </div>
    <h1>LingTai - Telegram bot setup</h1>
    <p>Connect a Telegram bot as this agent's backend. Telegram uses a
       <em>bot token</em> issued by <code>BotFather</code> — there is no QR
       login. Follow the checklist below.</p>
    <div class="setup">{{SETUP}}</div>
    <p class="hint">
      After entering the <code>bot_token</code> and <code>allowed_users</code>
      into the config JSON, refresh the MCP and verify with the
      <code>lingtai://status</code> resource. Remember each allowed human must
      open the bot and send <code>/start</code> before the bot can DM them. You
      can close this tab once <code>has_bot_token</code> is true and inbound
      messages arrive.
    </p>
    <div class="footnote">
      <strong>Where does the token go?</strong>
      Into the config file referenced by <code>LINGTAI_TELEGRAM_CONFIG</code> —
      not into this page. This page only lists the steps; it never stores or
      transmits the bot token.
    </div>
  </div>
</body>
</html>
"""



# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read config from the path in LINGTAI_TELEGRAM_CONFIG.

    Path is resolved relative to LINGTAI_AGENT_DIR (or cwd as fallback)
    if not absolute. Plaintext only — no *_env indirection.
    """
    config_path_raw = os.environ.get("LINGTAI_TELEGRAM_CONFIG")
    if not config_path_raw:
        raise ValueError(
            "LINGTAI_TELEGRAM_CONFIG env var not set — point it at your "
            "Telegram config JSON file"
        )
    config_path = Path(config_path_raw).expanduser()
    if not config_path.is_absolute():
        base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
        config_path = base / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"Telegram config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def _accounts_from_config(cfg: dict) -> list[dict]:
    """Normalize config into the accounts list TelegramService expects."""
    accounts = cfg.get("accounts")
    if not accounts:
        raise ValueError("config must contain 'accounts' (list)")
    return list(accounts)


# ---------------------------------------------------------------------------
# Manager construction
# ---------------------------------------------------------------------------

def build_manager() -> tuple[TelegramManager, Path]:
    """Construct manager + service from env + config. Returns (manager, working_dir)."""
    cfg = load_config()
    accounts = _accounts_from_config(cfg)

    agent_dir_raw = os.environ.get("LINGTAI_AGENT_DIR")
    working_dir = Path(agent_dir_raw) if agent_dir_raw else Path.cwd()
    working_dir.mkdir(parents=True, exist_ok=True)

    def _on_inbound(event: dict) -> None:
        push_inbox_event(
            sender=event["from"],
            subject=event["subject"],
            body=event["body"],
            metadata=event.get("metadata"),
            wake=event.get("wake", True),
        )

    # Forward declare the manager so the service's on_message callback can
    # reach it. Same pattern as the legacy addon's lambda + mgr_ref dance.
    mgr_ref: list[TelegramManager | None] = [None]

    svc = TelegramService(
        working_dir=working_dir,
        accounts_config=accounts,
        on_message=lambda alias, update: mgr_ref[0].on_incoming(alias, update),
        config_source=os.environ.get("LINGTAI_TELEGRAM_CONFIG"),
    )

    mgr = TelegramManager(
        service=svc,
        working_dir=working_dir,
        on_inbound=_on_inbound,
    )
    mgr_ref[0] = mgr
    return mgr, working_dir


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def build_server(manager: TelegramManager | None) -> Server:
    """Construct the MCP server. ``manager`` is None when eager start
    failed; in that case every tool call returns an error explaining why."""
    server: Server = Server("lingtai-telegram", instructions=_SERVER_INSTRUCTIONS)

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=item["uri"],
                name=item["name"],
                description=item["description"],
                mimeType=item["mimeType"],
            )
            for item in _RESOURCE_INDEX
        ]

    @server.read_resource()
    async def _read_resource(uri: object) -> str:
        resource_uri = _canonical_resource_uri(uri)
        try:
            _mime, text = _resource_payloads(manager)[resource_uri]
        except KeyError as exc:
            raise ValueError(f"unknown resource: {resource_uri}") from exc
        return text

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="telegram",
                description=DESCRIPTION,
                inputSchema=SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "telegram":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            result = {
                "status": "error",
                "error": (
                    "Telegram manager not initialized — server boot failed. "
                    "Check stderr for the underlying exception (most often "
                    "missing LINGTAI_TELEGRAM_CONFIG or invalid bot token)."
                ),
            }
        else:
            try:
                result = await asyncio.to_thread(manager.handle, arguments)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
        return [types.TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False),
        )]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Run the MCP server over stdio. Eagerly starts the polling listeners
    so inbound messages flow before the host expects them."""
    manager: TelegramManager | None = None
    service_started = False
    try:
        manager, _wd = build_manager()
        # The service starts the per-account poll threads.
        manager._service.start()
        service_started = True
        log.info("Telegram listener running")
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None

    server = build_server(manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None and service_started:
            try:
                manager._service.stop()
            except Exception:
                pass
