"""LingTai WeChat MCP server.

Exposes a single omnibus ``wechat`` MCP tool that dispatches to
WechatManager for all 8 actions (send, check, read, reply, search,
contacts, add_contact, remove_contact). Inbound WeChat events flow into
the host agent's inbox via LICC.

Configuration:
    LINGTAI_WECHAT_CONFIG  — path to ``config.json``. ``credentials.json``
                             is read from the same directory and is written
                             by the ``lingtai-wechat-bootstrap`` flow
                             (recommended) or the headless ``cli_login``
                             fallback. See README.

Config schemas (plaintext, no env-indirection):

    config.json:
        {
          "cdn_base_url": "...",         // optional
          "poll_interval": 1.0,          // optional
          "allowed_users": ["wxid_..."]  // optional allow-list
        }

    credentials.json (written by lingtai-wechat-bootstrap or cli_login):
        {
          "bot_token": "...",
          "user_id": "wxid_...",
          "base_url": "..."
        }

Env vars injected by the LingTai kernel for LICC:
    LINGTAI_AGENT_DIR — host agent's working directory.
    LINGTAI_MCP_NAME  — this MCP's registry name (typically "wechat").
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

from . import api
from .licc import push_inbox_event
from .manager import WechatManager, SCHEMA, DESCRIPTION

log = logging.getLogger("lingtai_wechat")


_SERVER_INSTRUCTIONS = (
    "lingtai-wechat: WeChat client via iLink Bot API. "
    "Configure via the LINGTAI_WECHAT_CONFIG env var pointing at config.json "
    "(credentials.json must live in the same directory; produced by the "
    "QR-code login flow). "
    "Inbound messages flow into the host agent's inbox via LICC. "
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-wechat"
)


# ---------------------------------------------------------------------------
# LingTai MCP profile resources
# ---------------------------------------------------------------------------

_PROFILE_MIME = "application/vnd.lingtai.mcp-profile+json"
_MARKDOWN_SKILL_MIME = "text/markdown; profile=lingtai-skill"
_MARKDOWN_MIME = "text/markdown"
_JSON_MIME = "application/json"
_HTML_MIME = "text/html"

_MANIFEST_URI = "lingtai://manifest"
_SKILL_URI = "lingtai://skills/wechat"
_CONFIG_DOC_URI = "lingtai://docs/configuration"
_TROUBLESHOOTING_DOC_URI = "lingtai://docs/troubleshooting"
_STATUS_URI = "lingtai://status"
_ONBOARDING_DOC_URI = "lingtai://onboarding/wechat"
_ONBOARDING_TEMPLATE_URI = "lingtai://onboarding/html-template"

_RESOURCE_INDEX = [
    {
        "uri": _MANIFEST_URI,
        "name": "LingTai MCP profile manifest",
        "mimeType": _PROFILE_MIME,
        "description": "Machine-readable LingTai profile for this WeChat MCP server.",
    },
    {
        "uri": _SKILL_URI,
        "name": "WeChat pointer skill",
        "mimeType": _MARKDOWN_SKILL_MIME,
        "description": "Thin agent-facing routing hint for WeChat MCP usage.",
    },
    {
        "uri": _CONFIG_DOC_URI,
        "name": "WeChat configuration guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Authoritative config fields, credentials, activation, and security notes.",
    },
    {
        "uri": _TROUBLESHOOTING_DOC_URI,
        "name": "WeChat troubleshooting guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Common QR/login/session/runtime failures and diagnostic steps.",
    },
    {
        "uri": _STATUS_URI,
        "name": "WeChat safe status",
        "mimeType": _JSON_MIME,
        "description": "Redacted runtime status derived from config, credentials, and manager state.",
    },
    {
        "uri": _ONBOARDING_DOC_URI,
        "name": "WeChat browser/HTML onboarding guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Agent-facing recipe for generating and opening a local HTML+browser onboarding page; covers QR/bootstrap/headless login and secret redaction.",
    },
    {
        "uri": _ONBOARDING_TEMPLATE_URI,
        "name": "WeChat onboarding HTML template",
        "mimeType": _HTML_MIME,
        "description": "Self-contained, secret-free static HTML page template with a {{QR}} placeholder, ready to write to disk and open in a browser.",
    },
]


def _package_version() -> str:
    try:
        return version("lingtai-wechat")
    except PackageNotFoundError:
        try:
            return version("lingtai")
        except PackageNotFoundError:  # editable checkout without installation metadata
            return "0+local"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _canonical_resource_uri(uri: object) -> str:
    return str(uri).rstrip("/")


def _resolve_config_path_for_status(config_path_raw: str) -> Path:
    path = Path(config_path_raw).expanduser()
    if path.is_absolute():
        return path
    agent_dir = os.environ.get("LINGTAI_AGENT_DIR")
    if agent_dir:
        base = Path(agent_dir).parent.parent
    else:
        base = Path.cwd()
    return base / path


def _safe_status_payload(
    manager: WechatManager | None,
    *,
    startup_error: str | None = None,
    startup_error_type: str | None = None,
) -> dict[str, Any]:
    """Return runtime status without exposing bot tokens or raw credentials."""
    config_path_raw = os.environ.get("LINGTAI_WECHAT_CONFIG")
    config_path = None
    credentials_path = None
    config_readable = False
    credentials_readable = False
    allowed_users_count = None
    has_bot_token = False
    has_user_id = False
    base_url = None
    cdn_base_url = None
    poll_interval = None
    notes: list[str] = []

    if config_path_raw:
        try:
            path = _resolve_config_path_for_status(config_path_raw)
            config_path = str(path)
            credentials_path = str(path.parent / "credentials.json")
            if path.is_file():
                config_readable = True
                cfg = json.loads(path.read_text(encoding="utf-8"))
                allowed_users = cfg.get("allowed_users")
                allowed_users_count = (
                    len(allowed_users) if isinstance(allowed_users, list) else None
                )
                base_url = cfg.get("base_url")
                cdn_base_url = cfg.get("cdn_base_url")
                poll_interval = cfg.get("poll_interval")
            else:
                notes.append("WeChat config path is set but config.json is not readable.")

            creds_path = path.parent / "credentials.json"
            if creds_path.is_file():
                credentials_readable = True
                creds = json.loads(creds_path.read_text(encoding="utf-8"))
                has_bot_token = bool(creds.get("bot_token"))
                has_user_id = bool(creds.get("user_id"))
                base_url = base_url or creds.get("base_url")
            else:
                notes.append("WeChat credentials.json is not readable next to config.json.")
        except Exception as exc:  # status must never leak raw credentials or fail hard
            notes.append(f"Could not read WeChat config safely: {type(exc).__name__}: {exc}")
    else:
        notes.append("LINGTAI_WECHAT_CONFIG is not set.")

    running = False
    if manager is not None:
        try:
            running = bool(getattr(manager, "_running", False))
        except Exception:
            running = False

    if startup_error_type:
        notes.append(f"Startup error: {startup_error_type}: {startup_error}")

    return {
        "status": "ok" if manager is not None else "degraded",
        "manager_initialized": manager is not None,
        "running": running,
        "config_path_set": bool(config_path_raw),
        "config_path": config_path,
        "config_readable": config_readable,
        "credentials_path": credentials_path,
        "credentials_readable": credentials_readable,
        "has_bot_token": has_bot_token,
        "has_user_id": has_user_id,
        "allowed_users_count": allowed_users_count,
        "base_url": base_url,
        "cdn_base_url": cdn_base_url,
        "poll_interval": poll_interval,
        "startup_error_type": startup_error_type,
        "notes": notes,
    }


def _profile_manifest(
    manager: WechatManager | None,
    *,
    startup_error: str | None = None,
    startup_error_type: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "lingtai.mcp.profile.v1",
        "server": {
            "name": "lingtai-wechat",
            "registry_name": "wechat",
            "version": _package_version(),
            "summary": "WeChat client via iLink Bot API with LICC inbox callback.",
            "homepage": "https://github.com/Lingtai-AI/lingtai-wechat",
        },
        "ownership": {
            "configuration": "This MCP owns WeChat config fields, QR login/bootstrap caveats, session diagnostics, and iLink runtime status.",
            "human_ui": "LingTai TUI /mcp is the human-facing control panel and should render these resources generically.",
            "agent_interface": "Agents should use MCP tools/resources/prompts directly; LingTai skills are thin discovery pointers.",
        },
        "resources": _RESOURCE_INDEX,
        "tools": [
            {
                "name": "wechat",
                "description": "Omnibus WeChat tool for send/check/read/reply/search/contacts/accounts.",
                "actions": [
                    "send", "check", "read", "reply", "search",
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
        "status": _safe_status_payload(
            manager,
            startup_error=startup_error,
            startup_error_type=startup_error_type,
        ),
    }


def _skill_markdown() -> str:
    return """---
name: wechat
summary: Thin routing hint for the lingtai-wechat MCP server.
---

# WeChat MCP pointer skill

This MCP is the authoritative source for WeChat/iLink Bot API setup and runtime
behavior. Do not copy platform details into a LingTai skill. Instead:

1. Read `lingtai://manifest` to discover this server's LingTai profile.
2. Read `lingtai://docs/configuration` for config fields, credentials, QR login/bootstrap, and activation.
3. Read `lingtai://docs/troubleshooting` for setup/runtime/session failures.
4. Read `lingtai://status` for safe, redacted runtime status.
5. Use the `wechat` MCP tool for agent-facing operations.

Human-facing setup should be rendered by LingTai's `/mcp` control panel from
these resources; agents use MCP tools/resources/prompts directly.
"""


def _configuration_markdown() -> str:
    return """# lingtai-wechat configuration

`lingtai-wechat` is a WeChat client MCP server backed by the iLink Bot API. It
reads `config.json` from the path in `LINGTAI_WECHAT_CONFIG` and reads sibling
`credentials.json` from the same directory.

## Environment

- `LINGTAI_WECHAT_CONFIG` — path to `config.json`. Relative paths resolve against
  the project root (`<project>/.lingtai/<agent>/` → `<project>`), matching the
  `lingtai-wechat-bootstrap` convention.
- `LINGTAI_AGENT_DIR` — injected by LingTai; used for state, media, contacts, and LICC.
- `LINGTAI_MCP_NAME` — injected by LingTai; usually `wechat`.

## Files

`config.json`:

```json
{
  "cdn_base_url": "https://...",
  "poll_interval": 1.0,
  "allowed_users": ["wxid_xxxxx"]
}
```

`credentials.json` (written by `lingtai-wechat-bootstrap` or headless `cli_login`):

```json
{
  "bot_token": "...",
  "user_id": "wxid_xxxxx",
  "base_url": "https://..."
}
```

Security notes:

- `credentials.json` contains `bot_token`; keep it secret and never paste it into
  chat, logs, issues, or PRs.
- `allowed_users` is an optional allow-list of WeChat user IDs (`wxid_...`).
- `poll_interval` defaults to 1.0 second.

## Bootstrap / login

Recommended interactive setup:

```bash
lingtai-wechat-bootstrap .secrets/wechat
```

Headless fallback:

```bash
python -c "from lingtai.mcp_servers.wechat.login import cli_login; cli_login('.secrets/wechat')"
```

## Tool entrypoint

Use the `wechat` tool with actions: `send`, `check`, `read`, `reply`, `search`,
`contacts`, `add_contact`, and `remove_contact`.
"""


def _troubleshooting_markdown() -> str:
    return """# lingtai-wechat troubleshooting

## `LINGTAI_WECHAT_CONFIG env var not set`

Set `LINGTAI_WECHAT_CONFIG` to the `config.json` path.

## `WeChat config not found`

Check the path in `LINGTAI_WECHAT_CONFIG`. Relative paths resolve against the
project root, not the agent directory. Refresh the agent after config changes.

## `WeChat credentials not found`

Run the bootstrap QR-code login flow:

```bash
lingtai-wechat-bootstrap <config-directory>
```

or use the headless fallback shown in `lingtai://docs/configuration`.

## `credentials.json missing 'bot_token'` / missing `user_id`

Re-run the QR login/bootstrap flow. Do not hand-edit or paste secrets into chat.

## Session expired

The iLink session can expire. Re-run bootstrap/login to refresh credentials, then
refresh the agent so the MCP restarts with the new credentials.

## Another poller already holds the account lock

Only one `lingtai-wechat` poller may consume one iLink account at a time. Stop the
other process or use a separate account/token. Tool errors expose the startup
exception; `lingtai://status` reports the redacted startup state.

## No inbound messages arrive

Check that the MCP process is active, credentials are readable, the iLink session
is valid, and `allowed_users` includes the sender's `wxid_...` if configured.
Read `lingtai://status` for redacted config/runtime state.

## Agent-facing vs human-facing interface

`/mcp` is the human-facing TUI control panel. Agents should use this MCP's
resources and `wechat` tool directly.
"""


def _onboarding_markdown() -> str:
    return """# lingtai-wechat browser/HTML onboarding

This resource is the agent-facing recipe for walking a human through a
*local* HTML + browser onboarding page. It complements
`lingtai://docs/configuration` and `lingtai://docs/troubleshooting`, which
remain authoritative for config fields and failure modes.

This MCP owns onboarding; the LingTai `/mcp` TUI is the human control panel
and renders these resources generically. Agents drive onboarding through the
resources below — do not embed WeChat platform details in a LingTai skill.

## When to use this

A human needs to log a WeChat account in as the bot backend for the first
time (or after the iLink session expired). All login paths are QR-based.

## Login paths (pick one)

1. **Browser bootstrap (recommended).** Runs the live login server, which
   generates and auto-opens a QR page and writes `credentials.json` on
   confirmation:

   ```bash
   lingtai-wechat-bootstrap .secrets/wechat
   ```

2. **Headless fallback (no display / SSH).** Prints an ASCII QR in the
   terminal and saves credentials on confirmation:

   ```bash
   python -c "from lingtai.mcp_servers.wechat.login import cli_login; cli_login('.secrets/wechat')"
   ```

3. **Agent-generated static HTML page.** When you cannot run the live
   bootstrap server but still want a clean browser page for the human to
   scan (e.g. you already have a QR payload/image from another channel),
   read `lingtai://onboarding/html-template`, substitute the QR, write it to
   a local file, and open it. The template is self-contained — no scripts,
   no external assets, no secrets — so it is safe to drop on disk.

## Generating the local HTML page (path 3)

1. Read the `lingtai://onboarding/html-template` resource.
2. Replace the `{{QR}}` placeholder with the QR for the *login* QR payload —
   an inline `<svg>`, an `<img>` with a `data:` URI, or a scannable
   representation. HTML-escape any human-readable payload text you add so a
   compromised QR response cannot inject markup into the local `file://`
   page.
3. Write the result to a local file (e.g. `./wechat-onboarding.html`).
4. Open it in the default browser (`open`/`xdg-open`/`cmd.exe /c start`).
5. Tell the human to scan it with WeChat on *their own* phone, then run the
   bootstrap/headless flow that actually saves `credentials.json`. The
   static page only displays the QR; it does not poll or save credentials.

## Admin-login safety (do not skip)

The login QR authorizes whichever WeChat account scans it as the bot's
backend identity. It is **not** a contact/group/customer-service QR. If a
friend or end user scans it, their account replaces the bot's credentials.
The template carries a bold "admin login — do not share" banner; keep it.

## Secret handling

- Credentials are written to `credentials.json` next to `config.json`. It
  contains `bot_token`. **Never** paste, echo, log, or commit `bot_token`,
  `credentials.json`, or any QR-confirmation payload into chat, issues, PRs,
  or the generated HTML page. The onboarding template is intentionally
  secret-free and must stay that way.
- Use `lingtai://status` to confirm setup; it reports `has_bot_token`
  (boolean) and never returns the token itself.

## After login

Restart / refresh the `lingtai-wechat` MCP so it picks up the new
`credentials.json`. See `lingtai://docs/troubleshooting` if QR/session/poller
errors appear.
"""


def _onboarding_html_template() -> str:
    return _ONBOARDING_HTML


def _resource_payloads(
    manager: WechatManager | None,
    *,
    startup_error: str | None = None,
    startup_error_type: str | None = None,
) -> dict[str, tuple[str, str]]:
    return {
        _MANIFEST_URI: (
            _PROFILE_MIME,
            _json_dumps(_profile_manifest(
                manager,
                startup_error=startup_error,
                startup_error_type=startup_error_type,
            )),
        ),
        _SKILL_URI: (_MARKDOWN_SKILL_MIME, _skill_markdown()),
        _CONFIG_DOC_URI: (_MARKDOWN_MIME, _configuration_markdown()),
        _TROUBLESHOOTING_DOC_URI: (_MARKDOWN_MIME, _troubleshooting_markdown()),
        _STATUS_URI: (
            _JSON_MIME,
            _json_dumps(_safe_status_payload(
                manager,
                startup_error=startup_error,
                startup_error_type=startup_error_type,
            )),
        ),
        _ONBOARDING_DOC_URI: (_MARKDOWN_MIME, _onboarding_markdown()),
        _ONBOARDING_TEMPLATE_URI: (_HTML_MIME, _onboarding_html_template()),
    }


# Static, self-contained onboarding HTML template. No JavaScript, no external
# assets, and no secrets — an agent reads this, substitutes the QR into the
# ``{{QR}}`` placeholder (inline <svg> or an <img> data: URI), writes it to a
# local file, and opens it in a browser. Unlike the live ``_BOOTSTRAP_HTML``
# page in ``login.py`` it does not poll or save credentials; it only displays
# the QR. The bold admin-login banner mirrors the bootstrap page (GH #87): the
# login QR authorizes the scanner's WeChat account as the bot backend and must
# not be shared like a contact/group/customer-service QR.
_ONBOARDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LingTai - WeChat admin login QR (do not share)</title>
<style>
  :root {
    color-scheme: light dark;
    --fg: #1a1a1a;
    --bg: #fafafa;
    --accent: #b07a3a;
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
  .card { max-width: 520px; padding: 2.2em 2em; text-align: center; }
  h1 { font-size: 1.4em; margin: 0 0 .3em; }
  p { line-height: 1.5; margin: .5em 0; }
  .qr { background: white; padding: 1em; border-radius: 12px;
        display: inline-block; margin: 1em 0; }
  .qr svg, .qr img { width: 260px; height: 260px; }
  .hint { opacity: .75; font-size: .9em; }
  .warn {
    background: var(--warn-bg);
    color: var(--warn-fg);
    border: 1px solid var(--warn-border);
    border-radius: 8px;
    padding: .9em 1em;
    margin: 0 0 1.2em;
    font-size: .95em;
    text-align: left;
  }
  .warn strong { display: block; margin-bottom: .25em; font-size: 1em; }
  .footnote {
    border-top: 1px solid var(--warn-border);
    margin-top: 1.4em;
    padding-top: .9em;
    font-size: .82em;
    opacity: .85;
    text-align: left;
  }
</style>
</head>
<body>
  <div class="card">
    <div class="warn" role="alert">
      <strong>&#9888; Admin login QR - do not share</strong>
      Scanning this QR <em>authorizes a WeChat account as the bot's backend
      identity</em>. If a friend or end user scans it, their account will be
      bound instead, replacing your credentials. This is not a contact /
      group / customer-service QR.
    </div>
    <h1>LingTai - WeChat admin login</h1>
    <p>Open WeChat on <em>your own</em> phone, tap the scan button, and scan
       the QR below to log this account in as the bot backend.</p>
    <div class="qr">{{QR}}</div>
    <p class="hint">
      This page only displays the QR. Once you confirm the login on your
      phone, the bootstrap/headless flow that launched this onboarding will
      save your credentials and you can close this tab.
    </p>
    <div class="footnote">
      <strong>Want a friend to chat with the bot?</strong>
      Don't share this QR. After login, share the logged-in WeChat account's
      normal contact / group / customer-service QR from inside WeChat - that
      is the public entrypoint for users. This page is admin-only.
    </div>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config_and_credentials() -> tuple[dict, dict, Path]:
    """Read config.json + sibling credentials.json. Returns (config, creds, config_dir)."""
    config_path_raw = os.environ.get("LINGTAI_WECHAT_CONFIG")
    if not config_path_raw:
        raise ValueError(
            "LINGTAI_WECHAT_CONFIG env var not set — point it at your "
            "WeChat config.json file"
        )
    config_path = Path(config_path_raw).expanduser()
    if not config_path.is_absolute():
        # Resolve relative paths against the *project root*, not the agent
        # working directory.  Agent dirs live at <project>/.lingtai/<agent>/,
        # so the project root is two parents up from LINGTAI_AGENT_DIR.  This
        # matches the convention used by lingtai-wechat-bootstrap, which writes
        # config.json + credentials.json relative to the project root (e.g.
        # .secrets/wechat/).  See GH #2.
        agent_dir = os.environ.get("LINGTAI_AGENT_DIR")
        if agent_dir:
            base = Path(agent_dir).parent.parent  # .lingtai/<agent>/ -> project root
        else:
            base = Path.cwd()
        config_path = base / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"WeChat config not found: {config_path}")

    file_cfg = json.loads(config_path.read_text(encoding="utf-8"))

    creds_path = config_path.parent / "credentials.json"
    if not creds_path.is_file():
        raise FileNotFoundError(
            f"WeChat credentials not found: {creds_path}. "
            f"Run the bootstrap flow first to authenticate via QR code:\n"
            f"  lingtai-wechat-bootstrap {config_path.parent}\n"
            f"or, for a headless host:\n"
            f'  python -c "from lingtai.mcp_servers.wechat.login import cli_login; '
            f"cli_login('{config_path.parent}')\""
        )
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    return file_cfg, creds, config_path.parent


# ---------------------------------------------------------------------------
# Manager construction
# ---------------------------------------------------------------------------

def build_manager() -> tuple[WechatManager, Path]:
    """Construct manager from env + config.json + credentials.json."""
    file_cfg, creds, config_dir = load_config_and_credentials()

    bot_token = creds.get("bot_token")
    user_id = creds.get("user_id")
    if not bot_token:
        raise ValueError(
            "credentials.json missing 'bot_token'. Re-run the QR login flow."
        )
    if not user_id:
        raise ValueError(
            "credentials.json missing 'user_id'. Re-run the QR login flow."
        )

    base_url = creds.get("base_url") or file_cfg.get("base_url", api.DEFAULT_BASE_URL)
    cdn_base_url = file_cfg.get("cdn_base_url", api.CDN_BASE_URL)
    poll_interval = float(file_cfg.get("poll_interval", 1.0))
    allowed_users = file_cfg.get("allowed_users") or None

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

    mgr = WechatManager(
        base_url=base_url,
        cdn_base_url=cdn_base_url,
        token=bot_token,
        user_id=user_id,
        poll_interval=poll_interval,
        allowed_users=allowed_users,
        working_dir=working_dir,
        on_inbound=_on_inbound,
        config_source=os.environ.get("LINGTAI_WECHAT_CONFIG"),
        credentials_source=str(config_dir / "credentials.json"),
    )
    return mgr, working_dir


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def build_server(
    manager: WechatManager | None,
    *,
    startup_error: str | None = None,
    startup_error_type: str | None = None,
) -> Server:
    server: Server = Server("lingtai-wechat", instructions=_SERVER_INSTRUCTIONS)

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
            _mime, text = _resource_payloads(
                manager,
                startup_error=startup_error,
                startup_error_type=startup_error_type,
            )[resource_uri]
        except KeyError as exc:
            raise ValueError(f"unknown resource: {resource_uri}") from exc
        return text

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="wechat",
                description=DESCRIPTION,
                inputSchema=SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "wechat":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            # Surface the actual startup exception type + message so the
            # agent/operator sees concrete remediation (e.g. PollerLockBusy
            # with ps/kill hints) instead of just "check stderr". stderr is
            # not always visible at the moment a tool call returns.
            result = {
                "status": "error",
                "error": (
                    "WeChat manager not initialized — server boot failed. "
                    "Run lingtai-wechat-bootstrap first if you haven't set "
                    "up credentials, or check the startup_error fields "
                    "below for the underlying exception."
                ),
                "startup_error_type": startup_error_type,
                "startup_error": startup_error,
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
    """Run the MCP server over stdio. Eagerly starts the iLink long-poll
    so inbound messages flow before the host expects them."""
    manager: WechatManager | None = None
    started = False
    startup_error: str | None = None
    startup_error_type: str | None = None
    try:
        manager, _wd = build_manager()
        manager.start()
        started = True
        log.info("WeChat listener running")
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None
        startup_error = str(e)
        startup_error_type = type(e).__name__

    server = build_server(
        manager,
        startup_error=startup_error,
        startup_error_type=startup_error_type,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None and started:
            try:
                manager.stop()
            except Exception:
                pass
