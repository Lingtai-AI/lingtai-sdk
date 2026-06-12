"""QR code login flow for WeChat iLink Bot API.

Two entry points are provided:

- ``cli_login(addon_dir)`` — terminal-only flow. Prints an ASCII QR code to
  stdout and polls for confirmation. Suitable when running inside an
  agent's stdio session or over SSH.

- ``cli_browser_login(addon_dir)`` — bootstrap flow for fresh first-time
  setup. Writes a self-contained HTML page embedding the QR as an
  SVG/data-URI, opens it in the user's default browser, and runs the same
  poll loop on the Python side. The terminal QR is still printed as a
  fallback when the browser cannot be opened (headless host, etc).

Both flows end by writing ``credentials.json`` next to ``config.json``,
chmod 600. The setup skill / human is expected to refresh the MCP after
credentials are saved.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from . import api

log = logging.getLogger(__name__)

LOGIN_TIMEOUT = 300  # 5 minutes per QR code
POLL_INTERVAL = 2.0
MAX_QR_REFRESHES = 3  # auto-refresh expired QR codes up to this many times


# ── Shared helpers ──────────────────────────────────────────────────


def _ensure_config(addon_dir: str | Path) -> Path:
    """Create addon dir + default config.json if missing. Returns config_path."""
    addon_path = Path(addon_dir).expanduser()
    addon_path.mkdir(parents=True, exist_ok=True)

    config_path = addon_path / "config.json"
    if not config_path.is_file():
        config_path.write_text(json.dumps({
            "base_url": api.DEFAULT_BASE_URL,
            "cdn_base_url": api.CDN_BASE_URL,
            "poll_interval": 1.0,
            "allowed_users": [],
        }, indent=2), encoding="utf-8")
        print(f"Created default config at {config_path}")
    return config_path


def _save_credentials(addon_dir: Path, result: dict) -> Path:
    """Atomically write credentials.json, overwriting any existing file.

    Uses ``os.replace`` on a sibling temp file so a partial write can never
    leave a half-written credentials.json on disk. If an existing
    credentials.json is present, prints a notice — see GH #6: bootstrap
    used to silently keep stale credentials when called twice, so a user
    who scanned the wrong WeChat account had no signal that rerunning
    bootstrap had fixed it.
    """
    creds_path = addon_dir / "credentials.json"
    if creds_path.exists():
        print(
            f"Notice: replacing existing {creds_path} with the new login. "
            "Previous credentials are discarded.",
        )
    creds = {
        "bot_token": result["bot_token"],
        "user_id": result["user_id"],
        "base_url": result["base_url"],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=".credentials.", suffix=".json.tmp", dir=str(addon_dir),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(creds, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, creds_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return creds_path


def _display_qr(qr_data: dict) -> None:
    """Display a QR code in the terminal (or print the URL as fallback)."""
    qrcode_str = qr_data.get("qrcode", "")
    img_content = qr_data.get("qrcode_img_content", qrcode_str)
    try:
        import qrcode as qr_lib
        qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L)
        qr.add_data(img_content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"QR code URL: {img_content}")
        print("(Install 'qrcode' package for terminal QR display)")


def _render_qr_svg(payload: str) -> str | None:
    """Render the QR payload to an inline SVG string for HTML embedding."""
    try:
        import qrcode as qr_lib
        import qrcode.image.svg as qr_svg
    except ImportError:
        return None
    qr = qr_lib.QRCode(
        error_correction=qr_lib.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    factory = qr_svg.SvgPathImage
    img = qr.make_image(image_factory=factory)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


# ── Login flow (shared core) ───────────────────────────────────────


async def _login_flow(
    base_url: str,
    *,
    on_new_qr: Callable[[dict], None] | None = None,
) -> dict | None:
    """Run the QR login flow. Returns credentials dict or None on failure.

    ``on_new_qr`` (if given) is called every time a fresh QR is fetched —
    used by the browser flow to update the HTML page. It receives the raw
    ``get_qrcode`` response dict.
    """
    print("Fetching QR code...")
    try:
        qr_data = await api.get_qrcode(base_url)
    except Exception as e:
        print(f"Error fetching QR code: {e}")
        return None

    qrcode_str = qr_data.get("qrcode")
    if not qrcode_str:
        print("Error: failed to get QR code from server.")
        return None

    if on_new_qr is not None:
        try:
            on_new_qr(qr_data)
        except Exception as e:
            log.debug("on_new_qr callback failed: %s", e)

    _display_qr(qr_data)
    print("\nScan this QR code with WeChat on your phone.")
    print("Waiting for confirmation...")

    current_base_url = base_url
    qr_refresh_count = 0

    while True:
        qr_start = time.time()
        while time.time() - qr_start < LOGIN_TIMEOUT:
            try:
                status = await api.poll_qr_status(current_base_url, qrcode_str)
            except Exception as e:
                log.debug("Poll error: %s, retrying...", e)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            s = status.get("status", "")
            if s == "wait":
                pass
            elif s == "scaned":
                print("QR code scanned — confirm on your phone...")
            elif s == "confirmed":
                return {
                    "bot_token": status["bot_token"],
                    "user_id": status.get(
                        "ilink_user_id", status.get("ilink_bot_id", ""),
                    ),
                    "base_url": status.get("baseurl", current_base_url),
                }
            elif s == "expired":
                break
            elif s == "scaned_but_redirect":
                redirect_host = status.get("redirect_host", "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
                    print(f"Redirecting to {current_base_url}...")
                continue
            else:
                log.debug("Unknown QR status: %s", s)

            await asyncio.sleep(POLL_INTERVAL)

        qr_refresh_count += 1
        if qr_refresh_count > MAX_QR_REFRESHES:
            print(f"\nQR code expired {MAX_QR_REFRESHES} times. Please try again later.")
            return None

        print(f"\n⏳ QR code expired, refreshing... ({qr_refresh_count}/{MAX_QR_REFRESHES})")
        try:
            qr_data = await api.get_qrcode(base_url)
        except Exception as e:
            print(f"Failed to refresh QR code: {e}")
            return None

        qrcode_str = qr_data.get("qrcode")
        if not qrcode_str:
            print("Failed to get new QR code from server.")
            return None

        current_base_url = base_url
        if on_new_qr is not None:
            try:
                on_new_qr(qr_data)
            except Exception as e:
                log.debug("on_new_qr callback failed: %s", e)
        _display_qr(qr_data)
        print("🔄 New QR code generated — please scan again.\n")


# ── CLI entry points ───────────────────────────────────────────────


def cli_login(addon_dir: str) -> None:
    """CLI entry point for WeChat QR login (terminal QR display).

    Called by the setup skill via:
        python -c "from lingtai.mcp_servers.wechat.login import cli_login; cli_login('.secrets')"

    Creates config.json with defaults if missing, runs QR login,
    saves credentials.json on success.
    """
    config_path = _ensure_config(addon_dir)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = cfg.get("base_url", api.DEFAULT_BASE_URL)

    _print_admin_qr_warning()

    try:
        result = asyncio.run(_login_flow(base_url))
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        sys.exit(1)

    if result is None:
        print("Login failed — QR code expired or error occurred.")
        sys.exit(1)

    creds_path = _save_credentials(config_path.parent, result)
    print(f"Connected as {result['user_id']}")
    print(f"Credentials saved to {creds_path}")


def _print_admin_qr_warning() -> None:
    """Print a stderr-equivalent warning about login-QR vs contact-QR mixup.

    Goes to stdout so it appears inline with the QR in the terminal. See
    GH #87: the iLink login QR authorizes the scanner's WeChat account as
    the backend; it is NOT a public chat QR to share with users.
    """
    print()
    print("  ⚠  Admin login QR — do NOT share")
    print("     Scanning this QR logs a WeChat account in as the bot's")
    print("     backend identity. If a friend scans it, their account")
    print("     replaces yours. This is not a contact/group/customer-")
    print("     service QR — share those from inside WeChat after login.")
    print()


def _open_browser_cross_platform(url: str) -> bool:
    """Open *url* in the default browser, with WSL / wslview fallback.

    Returns True if the browser was launched (or a launch was attempted).
    See GH #3: ``webbrowser.open()`` is a no-op inside WSL, so we detect
    the WSL kernel release string and fall back to ``cmd.exe /c start``
    or ``wslview`` when available.
    """
    import platform
    import shutil
    import subprocess

    release = platform.uname().release.lower()
    if "microsoft" in release or "wsl" in release:
        # WSL detected — try Windows-side browser first, then wslview.
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", "start", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            pass
        wslview = shutil.which("wslview")
        if wslview:
            try:
                subprocess.Popen(
                    [wslview, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                pass
        # Fall through to webbrowser as last resort.
    return webbrowser.open(url)


def cli_browser_login(addon_dir: str | None = None) -> None:
    """First-time-setup browser QR bootstrap.

    Generates a self-contained HTML page with the QR (inline SVG, with a
    polling status banner) and opens it in the default browser. Polls
    iLink for confirmation, then saves credentials.json. If the browser
    cannot be opened (headless host, missing display), falls back to the
    terminal QR display and the HTML file path is still printed so the
    human can open it themselves.

    Usage:
        lingtai-wechat-bootstrap                       # interactive prompt
        lingtai-wechat-bootstrap .secrets/wechat       # explicit dir
    """
    target = addon_dir
    if target is None:
        default = ".secrets/wechat"
        try:
            entered = input(
                f"WeChat credentials directory [{default}]: ",
            ).strip()
        except EOFError:
            entered = ""
        target = entered or default

    config_path = _ensure_config(target)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = cfg.get("base_url", api.DEFAULT_BASE_URL)

    # Use a private per-invocation temp dir so concurrent bootstrap runs on
    # a shared machine cannot collide on /tmp/lingtai_wechat_login.html and
    # so a symlink-attack vector on a predictable path is not exposed.
    tmp_dir = Path(tempfile.mkdtemp(prefix="lingtai-wechat-login-"))
    html_path = tmp_dir / "login.html"
    print(f"QR will be displayed at {html_path}")
    _print_admin_qr_warning()

    def _write_html(qr_data: dict) -> None:
        # The QR payload is server-controlled text. The SVG content is fully
        # rendered by qrcode (no payload leakage into the SVG element tree),
        # but the human-readable payload paragraph must be HTML-escaped or a
        # malicious/compromised iLink response could inject markup into the
        # local file:// page.
        payload = qr_data.get("qrcode_img_content") or qr_data.get("qrcode", "")
        svg = _render_qr_svg(payload) or ""
        page = _BOOTSTRAP_HTML.replace("__QR_SVG__", svg).replace(
            "__PAYLOAD__", html.escape(payload),
        )
        html_path.write_text(page, encoding="utf-8")

    def _on_new_qr(qr_data: dict) -> None:
        _write_html(qr_data)

    # Open the (initially empty) page so the browser is already focused
    # when the first QR is rendered. "(loading…)" is a literal — no escape
    # needed — but keep the discipline of using html.escape so future edits
    # don't accidentally introduce raw interpolation.
    html_path.write_text(
        _BOOTSTRAP_HTML.replace("__QR_SVG__", "").replace(
            "__PAYLOAD__", html.escape("(loading…)"),
        ),
        encoding="utf-8",
    )
    try:
        uri = html_path.as_uri()
        opened = _open_browser_cross_platform(uri)
    except Exception as e:
        log.debug("browser open failed: %s", e)
        opened = False
    if not opened:
        print(
            "Could not open the browser automatically. "
            f"Open this file manually: {html_path}",
        )

    try:
        result = asyncio.run(_login_flow(base_url, on_new_qr=_on_new_qr))
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        sys.exit(1)

    if result is None:
        print("Login failed — QR code expired or error occurred.")
        sys.exit(1)

    creds_path = _save_credentials(config_path.parent, result)
    print(f"Connected as {result['user_id']}")
    print(f"Credentials saved to {creds_path}")
    print(
        "Restart / refresh the lingtai-wechat MCP so it picks up the new "
        "credentials.",
    )


def _bootstrap_main() -> None:
    """Console-script entry point for ``lingtai-wechat-bootstrap``.

    Uses argparse so ``-h/--help`` actually prints help instead of being
    interpreted as the addon-directory positional argument and silently
    starting the QR login flow.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="lingtai-wechat-bootstrap",
        description=(
            "First-time WeChat setup. Generates a QR login page, opens it "
            "in your default browser, and writes credentials.json on success. "
            "Falls back to a terminal QR if the browser cannot be opened."
        ),
    )
    parser.add_argument(
        "addon_dir",
        nargs="?",
        default=None,
        help=(
            "Directory to write config.json + credentials.json into "
            "(default: prompt interactively; suggested ``.secrets/wechat``)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    cli_browser_login(args.addon_dir)


# Static HTML template for the browser bootstrap page. Self-contained;
# no external assets and no JavaScript — the human just scans the QR.
#
# The page is intentionally framed as an *admin* login: this QR authorizes
# a WeChat account as the backend identity. Sharing it with a "friend who
# wants to try the bot" would log THEIR account in as the bot and either
# disrupt or steal the working credentials (see GH #87). The bold red
# banner and the secondary block at the bottom both call this out.
_BOOTSTRAP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>LingTai — WeChat admin login QR (do not share)</title>
<meta http-equiv="refresh" content="3">
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
  .card {
    max-width: 520px;
    padding: 2.2em 2em;
    text-align: center;
  }
  h1 { font-size: 1.4em; margin: 0 0 .3em; }
  p { line-height: 1.5; margin: .5em 0; }
  .qr { background: white; padding: 1em; border-radius: 12px;
        display: inline-block; margin: 1em 0; }
  .qr svg { width: 260px; height: 260px; }
  .payload { font-family: monospace; font-size: .8em;
             color: var(--accent); word-break: break-all; }
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
      <strong>⚠ Admin login QR — do not share</strong>
      Scanning this QR <em>authorizes a WeChat account as the bot's backend
      identity</em>. If a friend or end user scans it, their account will be
      bound instead, replacing your credentials. This is not a contact /
      group / customer-service QR.
    </div>
    <h1>LingTai — WeChat admin login</h1>
    <p>Open WeChat on <em>your own</em> phone, tap the scan button, and scan
       the QR below to log this account in as the bot backend.</p>
    <div class="qr">__QR_SVG__</div>
    <p class="payload">__PAYLOAD__</p>
    <p class="hint">
      This page refreshes every 3s — once you confirm the login on your
      phone, the terminal that launched the bootstrap will save your
      credentials and you can close this tab.
    </p>
    <div class="footnote">
      <strong>Want a friend to chat with the bot?</strong>
      Don't share this QR. After login, share the logged-in WeChat account's
      normal contact / group / customer-service QR from inside WeChat — that
      is the public entrypoint for users. This page is admin-only.
    </div>
  </div>
</body>
</html>
"""
