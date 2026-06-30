---
related_files:
  - pyproject.toml
  - src/lingtai/ANATOMY.md
  - src/lingtai/core/mcp/ANATOMY.md
  - src/lingtai/mcp_catalog.json
  - src/lingtai/mcp_servers/__init__.py
  - src/lingtai/mcp_servers/_identity.py
  - src/lingtai/mcp_servers/_skill.py
  - src/lingtai/mcp_servers/cloud_mail/manager.py
  - src/lingtai/mcp_servers/feishu/manager.py
  - src/lingtai/mcp_servers/imap/manager.py
  - src/lingtai/mcp_servers/telegram/manager.py
  - src/lingtai/mcp_servers/wechat/manager.py
  - src/lingtai/mcp_servers/whatsapp/manager.py
  - tests/test_cloud_mail_addon.py
  - tests/test_mcp_skill_manuals.py
  - tests/test_telegram_rich_formatting.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# lingtai.mcp_servers

Curated MCP server package implementations shipped inside the `lingtai` Python distribution. They are launched by catalog/script entry points such as `python -m lingtai.mcp_servers.<name>` and expose real addon tools (IMAP, Telegram, Feishu, WeChat, WhatsApp, Cloud Mail) plus bundled progressive-disclosure manuals.

## Components

| File / folder | Role |
|---|---|
| `_skill.py` | Shared bundled-skill helper: re-exports the kernel-owned `split_frontmatter` from `lingtai_kernel._frontmatter` (one impl shared with the prompt-section catalog; kernel never imports the wrapper), `load_skill()` loads package `SKILL.md`, `manual_action_description()` injects frontmatter into the schema, and `manual_payload()` returns the manual body + absolute path without sidecar lists (`_skill.py:36-82`). |
| `_identity.py` | Shared public-identity envelope/path/write helper for curated messaging MCPs: builds the `lingtai.mcp.identity.v1` document, computes `system/mcp_identities/<name>.json`, and performs the newline-terminated atomic JSON write. Provider-specific account fields and redaction stay in each provider. |
| `telegram/`, `imap/`, `feishu/`, `wechat/`, `whatsapp/`, `cloud_mail/` | Curated MCPs using `_skill.py` for their `action="manual"` payloads (`telegram/manager.py`, `imap/manager.py`, `feishu/manager.py`, `wechat/manager.py`, `whatsapp/manager.py`, `cloud_mail/manager.py`). Messaging MCPs with runtime account identity also delegate their identity envelope/path/write policy to `_identity.py`. |
| Per-package `SKILL.md` | The human/agent-facing bundled manual. If a manual has sidecars, the sidecar inventory and relative paths live in this markdown, not in the tool payload. |
| `pyproject.toml` package-data entries | Ships every curated MCP `SKILL.md`; `reference/**/*` and `assets/**/*` are also packaged for future sidecar files (`pyproject.toml:81-86`). |

## Connections

- Catalog/script launchers (`pyproject.toml:43-49`) start these servers as subprocess MCPs; agents activate them through the generic MCP capability (`src/lingtai/core/mcp/ANATOMY.md`).
- Manager schemas include `manual` in each action enum and use `_skill.manual_action_description()` to advertise the bundled skill without loading the full body into the resident schema.
- Tests pin the manual contract, package-data sidecar support, and Telegram parity in `tests/test_mcp_skill_manuals.py` and `tests/test_telegram_rich_formatting.py`.

## Composition

Parent: `src/lingtai/` wrapper package (`src/lingtai/ANATOMY.md`). Sibling wrapper areas include `agent.py`, `core/`, `services/`, and `intrinsic_skills/`. Curated MCPs are independent subprocess packages, not intrinsic capabilities.

## State

The package itself is mostly code + packaged manuals. Runtime state is per-agent and server-specific: e.g. message caches, contacts, inbox replay guards, or credential-derived identities live under the agent workdir or `.secrets/`, not in `src/lingtai/mcp_servers/`. The shared manual and identity helpers have no persistent state of their own.

## Notes

- **Manual sidecar minimal contract:** `action="manual"` returns the main `SKILL.md` body, parsed metadata, and the main `SKILL.md` absolute `path` only. Concrete `assets/` and `reference/` lists MUST NOT be returned as structured tool fields; `SKILL.md` is the single source of truth for what sidecars exist and how to follow their relative paths.
- **Packaging discipline:** when adding manual sidecars, put their relative paths in `SKILL.md` and keep the package-data globs for `reference/**/*` / `assets/**/*` so wheels contain them (`pyproject.toml:81-86`).
