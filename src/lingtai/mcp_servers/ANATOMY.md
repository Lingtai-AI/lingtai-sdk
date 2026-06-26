# lingtai.mcp_servers

Curated MCP server package implementations shipped inside the `lingtai` Python distribution. They are launched by catalog/script entry points such as `python -m lingtai.mcp_servers.<name>` and expose real addon tools (IMAP, Telegram, Feishu, WeChat, WhatsApp, Cloud Mail) plus bundled progressive-disclosure manuals.

## Components

| File / folder | Role |
|---|---|
| `_skill.py` | Shared bundled-skill helper: `load_skill()` loads package `SKILL.md`, `manual_action_description()` injects frontmatter into the schema, and `manual_payload()` returns the manual body + absolute path without sidecar lists (`_skill.py:72-118`). |
| `telegram/` | Telegram MCP. It predates `_skill.py` and keeps an inline SKILL.md loader/manual payload with the same minimal contract (`telegram/manager.py:40-118`, `telegram/manager.py:1616-1633`). |
| `imap/`, `feishu/`, `wechat/`, `whatsapp/`, `cloud_mail/` | Curated MCPs using `_skill.py` for their `action="manual"` payloads (`imap/manager.py:294-308`, `feishu/manager.py:454-466`, `wechat/manager.py:504-516`, `whatsapp/manager.py:174-186`, `cloud_mail/manager.py:213-225`). |
| Per-package `SKILL.md` | The human/agent-facing bundled manual. If a manual has sidecars, the sidecar inventory and relative paths live in this markdown, not in the tool payload. |
| `pyproject.toml` package-data entries | Ships every curated MCP `SKILL.md`; `reference/**/*` and `assets/**/*` are also packaged for future sidecar files (`pyproject.toml:81-86`). |

## Connections

- Catalog/script launchers (`pyproject.toml:43-49`) start these servers as subprocess MCPs; agents activate them through the generic MCP capability (`src/lingtai/core/mcp/ANATOMY.md`).
- Manager schemas include `manual` in each action enum and use either `_skill.manual_action_description()` or Telegram's inline equivalent to advertise the bundled skill without loading the full body into the resident schema (`_skill.py:80-93`, `telegram/manager.py:111-124`).
- Tests pin the manual contract and package-data sidecar support in `tests/test_mcp_skill_manuals.py` and Telegram's legacy path in `tests/test_telegram_rich_formatting.py`.

## Composition

Parent: `src/lingtai/` wrapper package (`src/lingtai/ANATOMY.md`). Sibling wrapper areas include `agent.py`, `core/`, `services/`, and `intrinsic_skills/`. Curated MCPs are independent subprocess packages, not intrinsic capabilities.

## State

The package itself is mostly code + packaged manuals. Runtime state is per-agent and server-specific: e.g. message caches, contacts, inbox replay guards, or credential-derived identities live under the agent workdir or `.secrets/`, not in `src/lingtai/mcp_servers/`. The shared manual helper has no persistent state.

## Notes

- **Manual sidecar minimal contract:** `action="manual"` returns the main `SKILL.md` body, parsed metadata, and the main `SKILL.md` absolute `path` only. Concrete `assets/` and `reference/` lists MUST NOT be returned as structured tool fields; `SKILL.md` is the single source of truth for what sidecars exist and how to follow their relative paths.
- **Packaging discipline:** when adding manual sidecars, put their relative paths in `SKILL.md` and keep the package-data globs for `reference/**/*` / `assets/**/*` so wheels contain them (`pyproject.toml:81-86`).
- **Telegram exception:** Telegram still has an inline copy of the helper logic for historical reasons; keep its comments/tests aligned with `_skill.py` until it is deliberately migrated.
