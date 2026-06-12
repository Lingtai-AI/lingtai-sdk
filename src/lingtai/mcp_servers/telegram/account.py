"""TelegramAccount — single bot token, HTTP calls, polling thread.

One daemon thread per account runs the getUpdates long-poll loop.
Constructor stores config only — no threads, no API calls.
start() calls getMe and spawns the polling thread.
stop() signals the thread to stop and joins it.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# httpx is lazy-imported to keep the module importable without the optional dep.
# Actual import happens in _ensure_client() on first API call.
httpx: Any = None

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_FILE_BASE = "https://api.telegram.org/file/bot{token}/{file_path}"

# Default slash commands registered with @BotFather via setMyCommands on
# bot startup. Override per-account via the "commands" config field.
DEFAULT_COMMANDS: list[dict[str, str]] = [
    {"command": "status", "description": "Show agent status"},
    {"command": "help", "description": "List available commands"},
    {"command": "kanban", "description": "Show full agent dashboard (model, tokens, network, config)"},
    {"command": "refresh", "description": "Restart agent"},
    {"command": "sleep", "description": "Put agent to sleep"},
    {"command": "system", "description": "Browse system files (tap to view)"},
    {"command": "brief", "description": "Show current briefing"},
    {"command": "clear", "description": "Clear conversation"},
]


# ---------------------------------------------------------------------------
# Inline keyboard builders
# ---------------------------------------------------------------------------
#
# Telegram's reply_markup for inline keyboards is shaped as
#   {"inline_keyboard": [[{"text": "...", "callback_data": "..."}, ...], ...]}
# i.e. a list of rows, each row a list of buttons. callback_data is what the
# Bot API sends back in the callback_query.data field when the user taps the
# button — keep it short (Telegram caps it at 64 bytes) and dispatch on it
# from the agent side.
#
# These helpers live in account.py rather than a separate keyboards.py
# because reply_markup is the input shape consumed by send_message/edit_message
# right above; keeping them adjacent makes the relationship obvious. They are
# pure dict builders (no Telegram API calls) so they're safe to call without
# a running TelegramAccount.


def inline_keyboard_yes_no(
    yes_text: str = "Yes",
    no_text: str = "No",
    yes_data: str = "yes",
    no_data: str = "no",
) -> dict:
    """Two-button inline keyboard: [yes] [no] in a single row.

    Returned dict is suitable to pass as ``reply_markup`` to
    ``send_message`` / ``edit_message`` (or as the ``reply_markup``
    arg of the ``telegram(action="send")`` MCP tool).
    """
    return {
        "inline_keyboard": [[
            {"text": yes_text, "callback_data": yes_data},
            {"text": no_text, "callback_data": no_data},
        ]],
    }


def inline_keyboard_approve_reject(
    approve_text: str = "Approve",
    reject_text: str = "Reject",
    approve_data: str = "approve",
    reject_data: str = "reject",
) -> dict:
    """Two-button inline keyboard: [approve] [reject] in a single row."""
    return {
        "inline_keyboard": [[
            {"text": approve_text, "callback_data": approve_data},
            {"text": reject_text, "callback_data": reject_data},
        ]],
    }


def inline_keyboard_options(
    options: list[dict],
    columns: int = 1,
) -> dict:
    """N-button inline keyboard from a list of ``{"text", "data"}`` dicts.

    ``options`` items must each contain ``text`` (label shown on button) and
    ``data`` (the ``callback_data`` echoed back when tapped). Buttons are
    laid out left-to-right, top-to-bottom in rows of ``columns`` (default 1
    — one button per row, which is the conventional Telegram option-list
    style). ``columns`` must be >= 1.

    Example::

        inline_keyboard_options([
            {"text": "Tokyo",   "data": "city:tokyo"},
            {"text": "Osaka",   "data": "city:osaka"},
            {"text": "Kyoto",   "data": "city:kyoto"},
        ])
    """
    if columns < 1:
        raise ValueError("columns must be >= 1")
    if not options:
        raise ValueError("options must not be empty")

    buttons = []
    for opt in options:
        if "text" not in opt or "data" not in opt:
            raise ValueError(
                "each option must have 'text' and 'data' keys; got "
                f"{sorted(opt.keys())!r}"
            )
        buttons.append({"text": opt["text"], "callback_data": opt["data"]})

    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return {"inline_keyboard": rows}


class TelegramAccount:
    """Manages a single Telegram bot token — polling + sending."""

    def __init__(
        self,
        alias: str,
        bot_token: str,
        allowed_users: list[int] | None,
        poll_interval: float = 1.0,
        on_message: Callable[[str, dict], None] | None = None,
        state_dir: Path | None = None,
        commands: list[dict[str, str]] | None = None,
    ) -> None:
        self.alias = alias
        self._bot_token = bot_token
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._poll_interval = poll_interval
        self._on_message = on_message
        self._state_dir = state_dir
        # If commands is None, fall back to DEFAULT_COMMANDS at registration
        # time. An explicit empty list means "register no commands" and is
        # respected (Telegram clears the menu).
        self._commands: list[dict[str, str]] | None = commands

        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_update_id: int = 0
        self._bot_info: dict | None = None
        self._last_verified_at: str | None = None
        self._client: httpx.Client | None = None

        self._load_state()

    # -- API helpers ---------------------------------------------------------

    def _api_url(self, method: str) -> str:
        return _API_BASE.format(token=self._bot_token, method=method)

    def _file_url(self, file_path: str) -> str:
        return _FILE_BASE.format(token=self._bot_token, file_path=file_path)

    def _ensure_client(self) -> None:
        """Lazy-import httpx and create client on first use."""
        global httpx
        if httpx is None or isinstance(httpx, type(None)):
            import httpx as _httpx
            httpx = _httpx
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))

    def _request(self, method: str, **kwargs: Any) -> dict:
        """Make a Bot API request. Returns the 'result' field or raises."""
        self._ensure_client()
        resp = self._client.post(self._api_url(method), **kwargs)
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            logger.warning("Rate limited, sleeping %ds", retry_after)
            time.sleep(retry_after)
            resp = self._client.post(self._api_url(method), **kwargs)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description', data)}")
        return data.get("result", {})

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Call getMe, register slash commands, start polling thread."""
        if self._poll_thread is not None:
            return
        self._ensure_client()
        self._bot_info = self._request("getMe")
        self._last_verified_at = datetime.now(timezone.utc).isoformat()
        self._save_state()
        self._register_commands()
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"telegram-poll-{self.alias}",
        )
        self._poll_thread.start()
        logger.info("Telegram account '%s' started (@%s)",
                     self.alias, self._bot_info.get("username", "?"))

    def _register_commands(self) -> None:
        """Register slash commands with @BotFather via setMyCommands.

        Best-effort: any failure (network, API error, etc.) is logged at
        WARNING level but does not block startup. If the configured list is
        None, falls back to DEFAULT_COMMANDS. An explicit empty list is
        passed through as-is (Telegram interprets that as "clear the menu").
        """
        commands = self._commands if self._commands is not None else DEFAULT_COMMANDS
        try:
            self._request("setMyCommands", json={"commands": commands})
            logger.info(
                "Telegram account '%s' registered %d slash command(s)",
                self.alias, len(commands),
            )
        except Exception as e:
            logger.warning(
                "Telegram account '%s' setMyCommands failed (continuing): %s",
                self.alias, e,
            )

    def stop(self) -> None:
        """Signal polling thread to stop and join it."""
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- Polling -------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Main loop — getUpdates with long poll, dispatch to on_message."""
        while not self._stop_event.is_set():
            try:
                updates = self._request(
                    "getUpdates",
                    json={
                        "offset": self._last_update_id + 1,
                        "timeout": 30,
                    },
                )
                for update in updates:
                    self._process_update(update)
            except Exception as e:
                logger.warning("Telegram poll error (%s): %s", self.alias, e)
                # Backoff before retry
                if self._stop_event.wait(timeout=5.0):
                    return
                continue
            # Brief pause between poll cycles
            if self._stop_event.wait(timeout=self._poll_interval):
                return

    def _process_update(self, update: dict) -> None:
        """Process a single update — filter, dispatch, track offset."""
        update_id = update.get("update_id", 0)
        if update_id > self._last_update_id:
            self._last_update_id = update_id
            self._save_state()

        # Determine the user who triggered this update
        user_id = None
        if "message" in update:
            user_id = update["message"].get("from", {}).get("id")
        elif "callback_query" in update:
            user_id = update["callback_query"].get("from", {}).get("id")
            # Auto-answer callback query to dismiss spinner
            cq_id = update["callback_query"].get("id")
            if cq_id:
                try:
                    self._request("answerCallbackQuery", json={"callback_query_id": cq_id})
                except Exception:
                    pass
        elif "edited_message" in update:
            user_id = update["edited_message"].get("from", {}).get("id")

        # Filter by allowed users
        if self._allowed_users is not None and user_id not in self._allowed_users:
            return

        # Intercept slash commands before they reach the agent
        if self._handle_slash_command(update):
            return

        if self._on_message:
            self._on_message(self.alias, update)

    def _handle_slash_command(self, update: dict) -> bool:
        """Handle slash commands locally (no LLM call). Returns True if handled."""
        # Extract text from message
        text = ""
        chat_id = None
        if "message" in update:
            text = (update["message"].get("text") or "").strip()
            chat_id = update["message"]["chat"]["id"]
        elif "callback_query" in update:
            text = (update["callback_query"].get("data") or "").strip()
            cq_msg = update["callback_query"].get("message", {})
            chat_id = cq_msg.get("chat", {}).get("id")
        # Handle sys:* callback data from /system inline buttons
        if text.startswith("sys:") and chat_id:
            file_name = text[4:]  # strip "sys:" prefix
            self._cmd_system(chat_id, f"/system {file_name}")
            return True

        if not text.startswith("/") or not chat_id:
            return False

        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/kanban":
            self._cmd_kanban(chat_id)
            return True
        if cmd == "/refresh":
            self._cmd_refresh(chat_id)
            return True
        if cmd == "/sleep":
            self._cmd_sleep(chat_id)
            return True
        if cmd == "/system":
            self._cmd_system(chat_id, text)
            return True
        # Unknown slash command — let it pass through to agent
        return False

    def _cmd_kanban(self, chat_id: int) -> None:
        """Handle /kanban — show a layered agent dashboard. Pure filesystem read, no LLM."""
        agent_dir = os.environ.get("LINGTAI_AGENT_DIR", "")
        if not agent_dir:
            self.send_message(chat_id, "⚠️ LINGTAI_AGENT_DIR not set — cannot read data.")
            return

        agent_path = Path(agent_dir)
        lingtai_dir = agent_path.parent  # .lingtai/
        current_agent = agent_path.name

        # Helper: format large token counts
        def fmt(n: int | float | None) -> str:
            try:
                value = int(n or 0)
            except (TypeError, ValueError):
                value = 0
            if value >= 1_000_000:
                return f"{value/1_000_000:.1f}M"
            if value >= 1_000:
                return f"{value/1_000:.1f}K"
            return str(value)

        def fmt_duration(seconds: int | float | None) -> str:
            try:
                total = int(seconds or 0)
            except (TypeError, ValueError):
                total = 0
            if total <= 0:
                return "0m"
            days, rem = divmod(total, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _ = divmod(rem, 60)
            parts: list[str] = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes or not parts:
                parts.append(f"{minutes}m")
            return "".join(parts[:3])

        def fmt_time(value: str | None) -> str:
            if not value:
                return "?"
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone()
                return local.strftime("%Y-%m-%d %H:%M %Z")
            except (TypeError, ValueError):
                return str(value)

        def age_since(value: str | None) -> str:
            if not value:
                return "?"
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return fmt_duration((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return "?"

        def read_json(path: Path) -> dict:
            if not path.exists():
                return {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                return {}

        def count_matching(root: Path, pattern: str) -> int:
            if not root.exists():
                return 0
            try:
                return sum(1 for _ in root.rglob(pattern))
            except OSError:
                return 0

        def join_limited(items: list[str], max_chars: int = 220) -> str:
            out: list[str] = []
            used = 0
            for item in items:
                piece = item if not out else ", " + item
                if used + len(piece) > max_chars:
                    remaining = len(items) - len(out)
                    out.append(f"…(+{remaining})")
                    break
                out.append(piece if not out else piece[2:])
                used += len(piece)
            return ", ".join(out) if out else "—"

        # ---- 1. Read static and materialized agent metadata ----
        init = read_json(agent_path / "init.json")
        agent_meta = read_json(agent_path / ".agent.json")
        manifest = init.get("manifest", {})

        meta_llm = agent_meta.get("llm", {})
        init_llm = manifest.get("llm", {})
        current_model = meta_llm.get("model") or init_llm.get("model", "?")
        current_provider = meta_llm.get("provider") or init_llm.get("provider", "?")
        context_limit = meta_llm.get("context_limit") or manifest.get("context_limit", 0)
        language = agent_meta.get("language") or manifest.get("language", "?")
        soul_delay = agent_meta.get("soul_delay") or manifest.get("soul", {}).get("delay", 0)
        created_at = agent_meta.get("created_at")
        started_at = agent_meta.get("started_at")
        agent_id = agent_meta.get("agent_id") or "?"
        nickname = agent_meta.get("nickname")
        molt_count = int(agent_meta.get("molt_count") or 0)
        summaries_count = count_matching(agent_path / "system" / "summaries", "molt_*.md")
        if not molt_count:
            molt_count = summaries_count
        admin = agent_meta.get("admin") or manifest.get("admin", {})

        raw_caps = agent_meta.get("capabilities") or manifest.get("capabilities", {})
        capability_names: list[str] = []
        if isinstance(raw_caps, dict):
            capability_names = sorted(str(k) for k in raw_caps)
        elif isinstance(raw_caps, list):
            for item in raw_caps:
                if isinstance(item, (list, tuple)) and item:
                    capability_names.append(str(item[0]))
                elif isinstance(item, str):
                    capability_names.append(item)
            capability_names = sorted(set(capability_names))

        # ---- 2. Read presets ----
        preset_info = agent_meta.get("preset") or manifest.get("preset", {})
        active_preset_path = preset_info.get("active", "")
        default_preset_path = preset_info.get("default", "")
        allowed_presets = preset_info.get("allowed", [])
        preset_models: list[dict] = []
        for ppath in allowed_presets:
            p = Path(ppath).expanduser()
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        pd = json.load(f)
                    pd_llm = pd.get("manifest", {}).get("llm", {})
                    pd_desc = pd.get("description", {}).get("summary", "")
                    is_active = str(p) == str(Path(active_preset_path).expanduser())
                    preset_models.append({
                        "name": pd.get("name", p.stem),
                        "model": pd_llm.get("model", "?"),
                        "provider": pd_llm.get("provider", "?"),
                        "desc": pd_desc,
                        "active": is_active,
                    })
                except (json.JSONDecodeError, OSError):
                    pass

        # ---- 3. Read .status.json ----
        status = read_json(agent_path / ".status.json")
        runtime = status.get("runtime", {})
        tokens_status = status.get("tokens", {})
        ctx = tokens_status.get("context", {})
        agent_state = runtime.get("state", agent_meta.get("state", "?"))
        uptime = runtime.get("uptime_seconds", 0)
        stamina_left = runtime.get("stamina_left", 0)
        started_at = runtime.get("started_at") or started_at

        # ---- 4. Discover all agents and read token ledgers + lifecycle ----
        all_agents: dict[str, dict[str, Any]] = {}
        total_input = total_output = total_thinking = total_cached = 0
        total_api_calls = 0

        for child in sorted(lingtai_dir.iterdir()):
            if not child.is_dir():
                continue
            agent_json = child / ".agent.json"
            if not agent_json.exists():
                continue

            child_meta = read_json(agent_json)
            child_status = read_json(child / ".status.json")
            child_runtime = child_status.get("runtime", {})
            ledger_path = child / "logs" / "token_ledger.jsonl"
            agent_input = agent_output = agent_thinking = agent_cached = 0
            agent_calls = 0

            if ledger_path.exists():
                try:
                    with open(ledger_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                                agent_input += entry.get("input", 0)
                                agent_output += entry.get("output", 0)
                                agent_thinking += entry.get("thinking", 0)
                                agent_cached += entry.get("cached", 0)
                                agent_calls += 1
                            except json.JSONDecodeError:
                                continue
                except OSError:
                    pass

            all_agents[child.name] = {
                "input": agent_input,
                "output": agent_output,
                "thinking": agent_thinking,
                "cached": agent_cached,
                "calls": agent_calls,
                "state": child_runtime.get("state") or child_meta.get("state", "?"),
                "molt_count": int(child_meta.get("molt_count") or 0),
                "model": child_meta.get("llm", {}).get("model", "?"),
            }
            total_input += agent_input
            total_output += agent_output
            total_thinking += agent_thinking
            total_cached += agent_cached
            total_api_calls += agent_calls

        # ---- 5. Check addon status ----
        addons = init.get("addons", [])
        addon_status: dict[str, bool] = {}
        for addon in addons:
            if addon == "telegram":
                addon_status[addon] = (agent_path / ".secrets" / "telegram.json").exists()
            elif addon == "imap":
                addon_status[addon] = (agent_path / ".secrets" / "imap.json").exists()
            elif addon == "feishu":
                addon_status[addon] = (agent_path / ".secrets" / "feishu.json").exists()
            elif addon == "wechat":
                addon_status[addon] = (agent_path / ".secrets" / "wechat" / "config.json").exists()
            else:
                addon_status[addon] = addon in init.get("mcp", {})

        # ---- 6. Count durable stores ----
        delegates_path = agent_path / "delegates" / "ledger.jsonl"
        delegate_count = 0
        if delegates_path.exists():
            try:
                with open(delegates_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            delegate_count += 1
            except OSError:
                pass

        knowledge_count = count_matching(agent_path / "knowledge", "KNOWLEDGE.md")
        skill_count = count_matching(agent_path / ".library" / "custom", "SKILL.md")
        codex_dir = agent_path / "codex"
        codex_count = 0
        if codex_dir.exists():
            codex_count = len([f for f in codex_dir.iterdir() if f.suffix == ".md"])

        # ---- Build the layered message ----
        lines = [f"📊 *Kanban — {current_agent}*\n"]

        # Layer 1: Identity / lifecycle
        lines.append("🧭 *Layer 1 · Identity & Lifecycle*")
        display_name = f"`{current_agent}`"
        if nickname:
            display_name += f" ({nickname})"
        lines.append(f"  Agent: {display_name}")
        lines.append(f"  ID: `{str(agent_id)[-12:]}`")
        lines.append(f"  Born: {fmt_time(created_at)} ({age_since(created_at)} ago)")
        lines.append(f"  Session: {fmt_time(started_at)} (uptime {fmt_duration(uptime)})")
        lines.append(f"  Molts: {molt_count}  |  Summaries: {summaries_count}")
        karma = "✅" if admin.get("karma") else "❌"
        nirvana = "✅" if admin.get("nirvana") else "❌"
        lines.append(f"  Admin: karma {karma} / nirvana {nirvana}")

        # Layer 2: Model and presets
        lines.append("\n🤖 *Layer 2 · Model & Presets*")
        lines.append(f"  Active: `{current_model}` ({current_provider})")
        if active_preset_path:
            lines.append(f"  Preset: `{Path(active_preset_path).expanduser().name}`")
        if default_preset_path:
            lines.append(f"  Default: `{Path(default_preset_path).expanduser().name}`")
        if preset_models:
            for pm in preset_models[:8]:
                marker = " ←" if pm["active"] else ""
                bullet = "→" if pm["active"] else "•"
                lines.append(f"  {bullet} `{pm['model']}` ({pm['provider']}){marker}")
            if len(preset_models) > 8:
                lines.append(f"  … {len(preset_models) - 8} more preset(s)")

        # Layer 3: Live runtime
        lines.append("\n🖥 *Layer 3 · Live Runtime*")
        state_emoji = {
            "active": "🟢", "idle": "🟡", "asleep": "😴",
            "suspended": "🔴", "stuck": "⚠️",
        }.get(str(agent_state).lower(), "❓")
        lines.append(f"  State: {state_emoji} {agent_state}")
        if stamina_left > 0:
            lines.append(f"  Stamina: {fmt_duration(stamina_left)} left")
        usage_pct = ctx.get("usage_pct", 0)
        total_t = ctx.get("total_tokens", 0)
        window = ctx.get("window_size", context_limit)
        sys_t = ctx.get("system_tokens", 0)
        hist_t = ctx.get("history_tokens", 0)
        lines.append(f"  Context: {fmt(total_t)}/{fmt(window)} ({usage_pct:.1f}%)")
        lines.append(f"    ↳ fixed/system={fmt(sys_t)}  history={fmt(hist_t)}")

        # Layer 4: Mind / durable stores
        lines.append("\n🧠 *Layer 4 · Mind & Memory*")
        soul_text = f"{int(float(soul_delay) // 60)}m" if soul_delay else "off"
        lines.append(f"  Language: {language}  |  Soul delay: {soul_text}")
        store_parts = [
            f"knowledge={knowledge_count}",
            f"custom skills={skill_count}",
            f"delegates={delegate_count}",
        ]
        if codex_count:
            store_parts.append(f"codex={codex_count}")
        lines.append(f"  Stores: {' | '.join(store_parts)}")
        if capability_names:
            lines.append(f"  Capabilities ({len(capability_names)}): {join_limited([f'`{c}`' for c in capability_names])}")

        # Layer 5: Token usage
        lines.append("\n📈 *Layer 5 · Token Usage*")
        for name, data in sorted(all_agents.items()):
            if data["calls"] == 0:
                continue
            marker = " 👈" if name == current_agent else ""
            total = data["input"] + data["output"] + data["thinking"]
            lines.append(f"  *{name}*: {fmt(total)} tokens ({data['calls']} calls){marker}")
            lines.append(
                f"    ↳ in={fmt(data['input'])} out={fmt(data['output'])} "
                f"think={fmt(data['thinking'])} cache={fmt(data['cached'])}"
            )
        grand_total = total_input + total_output + total_thinking
        lines.append(f"  *Total*: {fmt(grand_total)} tokens ({total_api_calls} calls)")

        # Layer 6: Network topology
        agent_count = len(all_agents)
        agent_names = [f"`{name}`" for name in sorted(all_agents.keys())]
        lines.append("\n🌐 *Layer 6 · Network*")
        lines.append(f"  Agents ({agent_count}): {join_limited(agent_names)}")
        for name, data in sorted(all_agents.items()):
            if name == current_agent:
                continue
            total = data["input"] + data["output"] + data["thinking"]
            lines.append(
                f"  • `{name}`: {data['state']}, molts={data['molt_count']}, "
                f"model=`{data['model']}`, tokens={fmt(total)}"
            )

        # Layer 7: Addons and config
        lines.append("\n🔌 *Layer 7 · Addons & Config*")
        if addon_status:
            addon_parts = []
            for addon, ok in addon_status.items():
                emoji = "✅" if ok else "⬜"
                addon_parts.append(f"{emoji}{addon}")
            lines.append(f"  Addons: {' | '.join(addon_parts)}")
        lines.append(f"  Context limit: {fmt(context_limit)}")
        lines.append(f"  Max turns: {manifest.get('max_turns', '?')}")

        # Quick commands
        lines.append("\n⚡ /refresh /sleep /clear /brief")
        self.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_refresh(self, chat_id: int) -> None:
        """Handle /refresh — trigger agent refresh via signal file. No LLM call."""
        agent_dir = os.environ.get("LINGTAI_AGENT_DIR", "")
        if not agent_dir:
            self.send_message(chat_id, "⚠️ LINGTAI_AGENT_DIR not set — cannot send refresh signal.")
            return

        agent_path = Path(agent_dir)
        refresh_file = agent_path / ".refresh"
        taken_file = agent_path / ".refresh.taken"

        # Clean up stale .refresh.taken from a previous refresh
        if taken_file.exists():
            try:
                taken_file.unlink()
            except OSError:
                pass

        # Check if .refresh already exists (shouldn't, but be safe)
        if refresh_file.exists():
            self.send_message(chat_id, "⏳ Refresh signal already pending...")
            return

        try:
            refresh_file.write_text("", encoding="utf-8")
            self.send_message(chat_id, "🔄 Refresh signal sent — agent will restart momentarily.")
        except OSError as e:
            self.send_message(chat_id, f"⚠️ Failed to write refresh signal: {e}")

    def _cmd_sleep(self, chat_id: int) -> None:
        """Handle /sleep — put agent to sleep via signal file. No LLM call."""
        agent_dir = os.environ.get("LINGTAI_AGENT_DIR", "")
        if not agent_dir:
            self.send_message(chat_id, "⚠️ LINGTAI_AGENT_DIR not set — cannot send sleep signal.")
            return

        agent_path = Path(agent_dir)
        sleep_file = agent_path / ".sleep"

        try:
            sleep_file.write_text("", encoding="utf-8")
            self.send_message(chat_id, "😴 Sleep signal sent — agent is going to sleep. Message me to wake up.")
        except OSError as e:
            self.send_message(chat_id, f"⚠️ Failed to write sleep signal: {e}")

    def _cmd_system(self, chat_id: int, text: str) -> None:
        """Handle /system — progressive disclosure of system folder.

        /system          → list files with inline keyboard to view each
        /system <name>   → send only that specific file
        """
        agent_dir = os.environ.get("LINGTAI_AGENT_DIR", "")
        if not agent_dir:
            self.send_message(chat_id, "⚠️ LINGTAI_AGENT_DIR not set — cannot read system files.")
            return

        system_dir = Path(agent_dir) / "system"
        if not system_dir.exists():
            self.send_message(chat_id, "⚠️ system/ directory not found.")
            return

        md_files = sorted(system_dir.glob("*.md"))
        if not md_files:
            self.send_message(chat_id, "📂 No markdown files in system/")
            return

        # Parse optional filename filter
        parts = text.strip().split(maxsplit=1)
        filter_name = parts[1].strip().lower() if len(parts) > 1 else ""

        if not filter_name:
            # List mode: show directory listing with inline keyboard
            lines = ["📂 *system/* 目录\n"]
            buttons = []
            for fpath in md_files:
                size = fpath.stat().st_size
                if size < 1024:
                    size_str = f"{size}B"
                else:
                    size_str = f"{size / 1024:.1f}KB"
                lines.append(f"• `{fpath.stem}` ({size_str})")
                buttons.append([{"text": f"📄 {fpath.stem} ({size_str})", "callback_data": f"sys:{fpath.stem}"}])

            self.send_message(
                chat_id,
                "\n".join(lines) + "\n\n点击按钮查看对应文件 👇",
                parse_mode="Markdown",
                reply_markup={"inline_keyboard": buttons},
            )
            return

        # Specific file mode
        matched = [f for f in md_files if filter_name in f.stem.lower()]
        if not matched:
            file_list = ", ".join(f.stem for f in md_files)
            self.send_message(chat_id, f"❌ No match for '{filter_name}'\n\nAvailable: {file_list}")
            return

        # Send matched file(s), splitting if needed
        MAX_MSG = 4000
        for fpath in matched:
            try:
                content = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            header = f"📄 *{fpath.stem}*\n```\n"
            footer = "\n```"
            available = MAX_MSG - len(header) - len(footer)

            if len(content) <= available:
                self.send_message(chat_id, header + content + footer, parse_mode="Markdown")
            else:
                chunks = []
                while content:
                    if len(content) <= available:
                        chunks.append(content)
                        break
                    split_at = available
                    newline = content.rfind("\n", 0, available)
                    if newline > available // 2:
                        split_at = newline + 1
                    chunks.append(content[:split_at])
                    content = content[split_at:]

                for i, chunk in enumerate(chunks):
                    part_header = f"📄 *{fpath.stem}* ({i+1}/{len(chunks)})\n```\n"
                    self.send_message(chat_id, part_header + chunk + footer, parse_mode="Markdown")

    # -- Sending -------------------------------------------------------------

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        """Send a text message. Returns the sent Message object."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._request("sendMessage", json=payload)

    def send_photo(
        self, chat_id: int, photo_path: str, caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        """Send a photo via multipart upload."""
        with open(photo_path, "rb") as f:
            files = {"photo": (Path(photo_path).name, f, "image/jpeg")}
            data: dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            if reply_to_message_id:
                data["reply_to_message_id"] = str(reply_to_message_id)
            return self._request("sendPhoto", files=files, data=data)

    def send_document(
        self, chat_id: int, doc_path: str, caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        """Send a document via multipart upload."""
        with open(doc_path, "rb") as f:
            files = {"document": (Path(doc_path).name, f, "application/octet-stream")}
            data: dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            if reply_to_message_id:
                data["reply_to_message_id"] = str(reply_to_message_id)
            return self._request("sendDocument", files=files, data=data)

    def edit_message(
        self, chat_id: int, message_id: int, text: str,
        reply_markup: dict | None = None, is_caption: bool = False,
    ) -> dict:
        """Edit a sent message's text or caption."""
        if is_caption:
            payload: dict[str, Any] = {
                "chat_id": chat_id, "message_id": message_id, "caption": text,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return self._request("editMessageCaption", json=payload)
        else:
            payload = {
                "chat_id": chat_id, "message_id": message_id, "text": text,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return self._request("editMessageText", json=payload)

    def delete_message(self, chat_id: int, message_id: int) -> dict:
        """Delete a message."""
        return self._request("deleteMessage", json={
            "chat_id": chat_id, "message_id": message_id,
        })

    def send_chat_action(self, chat_id: int, action: str = "typing") -> dict:
        """Send a chat action (typing indicator).

        Telegram shows the action (e.g. "typing...", "uploading photo...") to
        the chat user. The indicator auto-expires after 5 seconds, so callers
        should re-send during long-running tasks.
        """
        return self._request(
            "sendChatAction",
            json={"chat_id": chat_id, "action": action},
        )

    def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: list[dict] | None = None,
        is_big: bool = False,
    ) -> bool:
        """Set a reaction on a message (Bot API 7.0+).

        Args:
            chat_id: Chat ID where the message is.
            message_id: Message ID to react to.
            reaction: List of reaction types. Each is a dict with "type"
                (e.g. "emoji") and "emoji" (e.g. "👀", "⏳", "✅", "❌").
                Pass None or empty list to remove reactions.
            is_big: If True, show a bigger reaction animation.

        Returns:
            True on success (Bot API returns True, not a dict).
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "is_big": is_big,
        }
        if reaction is not None:
            payload["reaction"] = reaction
        return self._request("setMessageReaction", json=payload)

    def get_file(self, file_id: str) -> tuple[str, bytes]:
        """Download a file by file_id. Returns (filename, data)."""
        file_info = self._request("getFile", json={"file_id": file_id})
        file_path = file_info["file_path"]
        filename = Path(file_path).name
        url = self._file_url(file_path)
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        resp = self._client.get(url)
        resp.raise_for_status()
        return filename, resp.content


    @property
    def allowed_users_count(self) -> int | None:
        """Return the allow-list size without exposing user IDs."""
        if self._allowed_users is None:
            return None
        return len(self._allowed_users)

    def public_identity(self) -> dict[str, Any]:
        """Non-secret Bot API identity observed from getMe/state.

        This intentionally exposes only stable public bot metadata. It never
        includes bot tokens, user IDs, chat IDs, messages, or webhook secrets.
        """
        info = self._bot_info or {}
        first_name = info.get("first_name")
        last_name = info.get("last_name")
        display_name = " ".join(
            str(part) for part in (first_name, last_name) if part
        ) or None
        identity = {
            "alias": self.alias,
            "bot_id": info.get("id"),
            "bot_username": info.get("username"),
            "bot_display_name": display_name,
            "is_bot": info.get("is_bot"),
            "last_verified_at": self._last_verified_at,
        }
        return {k: v for k, v in identity.items() if v is not None}

    # -- State persistence ---------------------------------------------------

    def _state_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / "state.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._last_update_id = data.get("last_update_id", 0)
            self._bot_info = data.get("bot_info")
            self._last_verified_at = data.get("last_verified_at")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load Telegram state: %s", e)

    def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_update_id": self._last_update_id,
            "bot_info": self._bot_info,
            "last_verified_at": self._last_verified_at,
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
