"""Fail-closed safe-status redaction for lingtai-whatsapp."""
from __future__ import annotations

from typing import Any

_SECRET_FIELDS = {"access_token", "app_secret", "verify_token", "token", "system_user_token"}
_SAFE_FIELDS = {
    "alias", "phone_number_id", "waba_id", "business_account_id",
    "display_phone_number", "api_version", "webhook", "templates",
}
# Only these template fields are non-secret identity metadata. Everything else
# (components, examples, parameter values, URLs, default substitutions) may carry
# customer data and must never be copied wholesale into a redacted account.
_SAFE_TEMPLATE_FIELDS = {"name", "language", "category", "status"}


def _redact_templates(templates: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(templates, list):
        return out
    for template in templates:
        if not isinstance(template, dict):
            out.append({})
            continue
        out.append({k: v for k, v in template.items() if k in _SAFE_TEMPLATE_FIELDS})
    return out


def redact_account(account: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (account or {}).items():
        if key in _SECRET_FIELDS:
            out[f"{key}_present"] = bool(value)
        elif key in _SAFE_FIELDS:
            if key == "webhook" and isinstance(value, dict):
                out[key] = {k: v for k, v in value.items() if k in {"public_url", "host", "port", "path"}}
            elif key == "templates":
                # Do not expose raw template objects/parameter values. Emit a count
                # plus name/language-only metadata so nothing customer-bearing leaks.
                out["template_count"] = len(value) if isinstance(value, list) else 0
                out["templates"] = _redact_templates(value)
            else:
                out[key] = value
    for secret in ("access_token", "app_secret", "verify_token"):
        out.setdefault(f"{secret}_present", bool((account or {}).get(secret)))
    return out
