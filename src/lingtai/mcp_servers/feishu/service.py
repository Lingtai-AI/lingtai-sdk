"""FeishuService — multi-account orchestrator.

Creates one FeishuAccount per config entry.
Routes outbound sends to the correct account by alias.
Delegates lifecycle (start/stop) to all accounts.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .account import FeishuAccount

logger = logging.getLogger(__name__)


class FeishuService:
    """Multi-account Feishu bot service."""

    def __init__(
        self,
        working_dir: Path,
        accounts_config: list[dict],
        on_message: Callable[[str, object], None],
        config_source: str | None = None,
    ) -> None:
        self._working_dir = Path(working_dir)
        self._on_message = on_message
        self._config_source = config_source
        self._account_order: list[str] = []
        self._accounts: dict[str, FeishuAccount] = {}

        for cfg in accounts_config:
            alias = cfg["alias"]
            state_dir = self._working_dir / "feishu" / alias
            acct = FeishuAccount(
                alias=alias,
                app_id=cfg["app_id"],
                app_secret=cfg["app_secret"],
                allowed_users=cfg.get("allowed_users"),
                on_message=on_message,
                state_dir=state_dir,
            )
            self._accounts[alias] = acct
            self._account_order.append(alias)

    def get_account(self, alias: str) -> FeishuAccount:
        """Get account by alias. Raises KeyError if not found."""
        return self._accounts[alias]

    @property
    def default_account(self) -> FeishuAccount:
        """Return the first configured account."""
        return self._accounts[self._account_order[0]]

    def list_accounts(self) -> list[str]:
        """Return list of account aliases in config order."""
        return list(self._account_order)

    def account_details(self) -> list[dict[str, Any]]:
        """Return non-secret public identity details for each account."""
        details: list[dict[str, Any]] = []
        for alias in self._account_order:
            acct = self._accounts[alias]
            item = acct.public_identity()
            item["allowed_users_count"] = acct.allowed_users_count
            item["contact_count"] = self._contact_count(alias)
            if self._config_source:
                item["config_source"] = self._config_source
            details.append(item)
        return details

    def identity_payload(self) -> dict[str, Any]:
        """Build the non-secret MCP identity document for this service."""
        now = datetime.now(timezone.utc).isoformat()
        accounts = self.account_details()
        verified = [
            a.get("last_verified_at") for a in accounts if a.get("last_verified_at")
        ]
        payload: dict[str, Any] = {
            "schema": "lingtai.mcp.identity.v1",
            "mcp": "feishu",
            "generated_at": now,
            "accounts": accounts,
        }
        if verified:
            payload["last_verified_at"] = max(str(v) for v in verified)
        return payload

    def identity_path(self) -> Path:
        return self._working_dir / "system" / "mcp_identities" / "feishu.json"

    def write_identity_file(self) -> Path:
        """Atomically write public, non-secret MCP identity metadata."""
        path = self.identity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.identity_payload(), f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return path

    def _contact_count(self, alias: str) -> int | None:
        contacts_path = self._working_dir / "feishu" / alias / "contacts.json"
        if not contacts_path.is_file():
            return 0
        try:
            data = json.loads(contacts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return len(data) if isinstance(data, dict) else None

    def start(self) -> None:
        """Start all accounts' WebSocket threads and publish public identity."""
        for acct in self._accounts.values():
            acct.start()
        try:
            path = self.write_identity_file()
            logger.info("Wrote Feishu MCP identity metadata to %s", path)
        except Exception as e:
            logger.warning(
                "Failed to write Feishu MCP identity metadata (continuing): %s", e
            )

    def stop(self) -> None:
        """Stop all accounts."""
        for acct in self._accounts.values():
            acct.stop()
