"""Compatibility namespace for LingTai curated MCP addons.

Curated MCP implementations are shipped in the `lingtai` distribution under
`lingtai.mcp_servers.*`.  Historical top-level packages (`lingtai_imap`,
`lingtai_telegram`, etc.) and this namespace preserve older imports such as
`lingtai.addons.telegram` and TUI importability checks.
"""

__all__ = ["imap", "telegram", "feishu", "wechat", "whatsapp"]
