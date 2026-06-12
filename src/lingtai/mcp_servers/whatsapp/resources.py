"""LingTai profile resources for WhatsApp MCP."""
from __future__ import annotations

import json
from typing import Any


def manifest() -> dict[str, Any]:
    return {
        "name": "lingtai-whatsapp",
        "profile": "lingtai-mcp-v1",
        "summary": "Official Meta WhatsApp Cloud API MCP for LingTai.",
        "transport": "stdio MCP plus public HTTP webhook for inbound Meta callbacks",
        "backend": "official_meta_cloud_api_only",
        "tools": {"name": "whatsapp", "actions": ["send", "check", "read", "reply", "search", "react", "contacts", "add_contact", "remove_contact", "templates", "accounts", "status"]},
        "resources": [
            "lingtai://manifest", "lingtai://skills/whatsapp", "lingtai://docs/configuration", "lingtai://docs/troubleshooting", "lingtai://status", "lingtai://onboarding/whatsapp", "lingtai://onboarding/html-template",
        ],
        "agent_entrypoints": {"skill": "lingtai://skills/whatsapp", "onboarding": "lingtai://onboarding/whatsapp", "onboarding_html_template": "lingtai://onboarding/html-template"},
    }


SKILL = """# WhatsApp MCP skill\n\nUse this MCP when the human wants LingTai to communicate over WhatsApp Business. This v1 uses the official Meta WhatsApp Cloud API only. Do not suggest unofficial WhatsApp Web bridges as the default path.\n\nKey constraints:\n- inbound requires a public HTTPS webhook configured in Meta App Dashboard;\n- free-form replies are limited to the 24-hour customer-service window; outside the window use approved templates;\n- status/tool resources redact access tokens, app secrets, and verify tokens.\n\nTypical setup flow: read `lingtai://onboarding/whatsapp`, collect Meta app/phone prerequisites from the human, write config, expose webhook URL, verify `lingtai://status`, then ask the human to send a test WhatsApp message.\n"""

CONFIGURATION = """# WhatsApp MCP configuration\n\nSet `LINGTAI_WHATSAPP_CONFIG` to a JSON file containing `accounts`. Required per account: `alias`, `access_token`, `phone_number_id`, `waba_id` or `business_account_id`, `app_secret`, `verify_token`. Optional: `api_version`, `display_phone_number`, `webhook`, `templates`.\n\nThe webhook object should include `public_url`, `host`, `port`, and `path`. The public URL must be HTTPS and reachable by Meta.\n"""

TROUBLESHOOTING = """# WhatsApp MCP troubleshooting\n\n- GET verification fails: check verify_token equality and callback URL path.\n- POST callbacks rejected: check `X-Hub-Signature-256`, app_secret, and raw request body handling.\n- Send fails outside customer-service window: use an approved template.\n- No inbound messages: confirm webhook subscription for the WhatsApp Business Account and public HTTPS reachability.\n"""

ONBOARDING = """# WhatsApp onboarding\n\n1. Create/choose a Meta App with WhatsApp product enabled.\n2. Connect a WhatsApp Business Account and phone number; record `phone_number_id` and `waba_id`.\n3. Create a permanent system-user access token with WhatsApp messaging permissions.\n4. Choose a random `verify_token`; store it in config and in Meta's webhook verification screen.\n5. Expose the MCP webhook endpoint over HTTPS, then complete Meta's GET challenge.\n6. Subscribe to messages/status callbacks.\n7. Send a test message from an allowed WhatsApp user.\n\nNever put secrets in generated HTML. Use the HTML template only as a local checklist/status page.\n"""

HTML_TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'><title>LingTai WhatsApp MCP setup</title><style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.5}.warn{background:#fff3cd;border:1px solid #ffe08a;padding:12px;border-radius:8px}.box{background:#f6f8fa;padding:14px;border-radius:8px;white-space:pre-wrap}</style></head><body><h1>WhatsApp MCP setup</h1><p class='warn'>Official Meta WhatsApp Cloud API only. Do not paste access tokens, app secrets, or verify tokens into this page.</p><div class='box'>{{SETUP}}</div></body></html>"""


def resource_text(uri: str, status: dict[str, Any] | None = None) -> tuple[str, str]:
    if uri == "lingtai://manifest":
        return json.dumps(manifest(), ensure_ascii=False, indent=2), "application/json"
    if uri == "lingtai://skills/whatsapp":
        return SKILL, "text/markdown; profile=lingtai-skill"
    if uri == "lingtai://docs/configuration":
        return CONFIGURATION, "text/markdown"
    if uri == "lingtai://docs/troubleshooting":
        return TROUBLESHOOTING, "text/markdown"
    if uri == "lingtai://status":
        return json.dumps(status or {"status": "not_initialized"}, ensure_ascii=False, indent=2), "application/json"
    if uri == "lingtai://onboarding/whatsapp":
        return ONBOARDING, "text/markdown"
    if uri == "lingtai://onboarding/html-template":
        return HTML_TEMPLATE, "text/html"
    raise KeyError(uri)
