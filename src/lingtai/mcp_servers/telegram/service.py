"""TelegramService — multi-account orchestrator.

Creates one TelegramAccount per config entry.
Routes outbound sends to the correct account by alias.
Delegates lifecycle (start/stop) to all accounts.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from .. import _identity
from .account import TelegramAccount

logger = logging.getLogger(__name__)


class TelegramService:
    """Multi-account Telegram bot service."""

    def __init__(
        self,
        working_dir: Path,
        accounts_config: list[dict],
        on_message: Callable[[str, dict], None],
        config_source: str | None = None,
    ) -> None:
        self._working_dir = Path(working_dir)
        self._on_message = on_message
        self._config_source = config_source
        self._account_order: list[str] = []
        self._accounts: dict[str, TelegramAccount] = {}

        for cfg in accounts_config:
            alias = cfg["alias"]
            state_dir = self._working_dir / "telegram" / alias
            acct = TelegramAccount(
                alias=alias,
                bot_token=cfg["bot_token"],
                allowed_users=cfg.get("allowed_users"),
                poll_interval=cfg.get("poll_interval", 1.0),
                on_message=on_message,
                state_dir=state_dir,
                commands=cfg.get("commands"),
            )
            self._accounts[alias] = acct
            self._account_order.append(alias)

    def get_account(self, alias: str) -> TelegramAccount:
        """Get account by alias. Raises KeyError if not found."""
        return self._accounts[alias]

    @property
    def default_account(self) -> TelegramAccount:
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
        return _identity.identity_payload("telegram", self.account_details())

    def identity_path(self) -> Path:
        return _identity.identity_path(self._working_dir, "telegram")

    def write_identity_file(self) -> Path:
        """Atomically write public, non-secret MCP identity metadata."""
        return _identity.write_identity_file(
            self.identity_path(), self.identity_payload()
        )

    def _contact_count(self, alias: str) -> int | None:
        contacts_path = self._working_dir / "telegram" / alias / "contacts.json"
        if not contacts_path.is_file():
            return 0
        try:
            data = json.loads(contacts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return len(data) if isinstance(data, dict) else None

    def start(self) -> None:
        """Start all accounts' polling threads and publish public identity."""
        for acct in self._accounts.values():
            acct.start()
        try:
            path = self.write_identity_file()
            logger.info("Wrote Telegram MCP identity metadata to %s", path)
        except Exception as e:
            logger.warning(
                "Failed to write Telegram MCP identity metadata (continuing): %s", e
            )

    def stop(self) -> None:
        """Stop all accounts."""
        for acct in self._accounts.values():
            acct.stop()
