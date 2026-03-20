"""
Signal Bot Manager — Local web UI for first-time setup AND ongoing management.

Run: python manage.py
Opens a browser window with a GUI. If this is your first time, it walks you
through getting all your tokens. If you've already set up, it shows a
management dashboard with status indicators and settings.

SECURITY & TRANSPARENCY:
    This runs a LOCAL web server on your machine (127.0.0.1:8457).
    It is NOT accessible from the internet — only from your own computer.
    It reads/writes your local .env file and calls Modal CLI commands.
    No data is sent anywhere except to the services you configured
    (TRW, Hyperliquid, Slack, Modal).

    Your tokens are stored in TWO places, both controlled by YOU:
      1. A local .env file on YOUR computer (never uploaded anywhere)
      2. Modal encrypted secrets in YOUR Modal account (you created it)

    Nothing is sent to the author or any third party. This script does not
    collect, transmit, or log your credentials anywhere beyond the two
    locations listed above.

    If you're unsure, paste this entire file into ChatGPT or Claude and ask:
    "Does this script send my tokens anywhere suspicious?"
"""

import os
import sys
import json
import subprocess
import webbrowser
import threading
import secrets as _secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from pathlib import Path

PORT = 8457
# CSRF token regenerated each time manage.py starts
_CSRF_TOKEN = _secrets.token_urlsafe(32)
ENV_PATH = Path(__file__).resolve().parent / ".env"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def load_env():
    values = {}
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    values[key.strip()] = val.strip()
    return values


def save_env(values):
    # Preserve any existing env vars not in the form (e.g. DASHBOARD_TOKEN, MODAL_WORKSPACE)
    existing = load_env() if ENV_PATH.exists() else {}
    existing.update(values)

    # Known keys in display order, then any extras
    known_sections = [
        ("# TRW (The Real World)", [
            "TRW_SESSION_TOKEN",
            "TRW_SIGNAL_CHANNEL_ID",
            "TRW_PROF_ADAM_USER_ID",
        ]),
        ("# Hyperliquid", [
            "HYPERLIQUID_API_PRIVATE_KEY",
            "HYPERLIQUID_MASTER_ACCOUNT_ADDRESS",
        ]),
        ("# Slack (optional)", [
            "SLACK_WEBHOOK_URL",
        ]),
    ]

    # Ensure defaults for static keys
    existing.setdefault("TRW_SIGNAL_CHANNEL_ID", "01H83QAX979K9R7QTMH74ATR8C")
    existing.setdefault("TRW_PROF_ADAM_USER_ID", "01GHHHWZE7Q77AKGWZDGC5PDCN")

    lines = []
    written_keys = set()
    for header, keys in known_sections:
        lines.append(header)
        for key in keys:
            lines.append(f"{key}={existing.get(key, '')}")
            written_keys.add(key)
        lines.append("")

    # Append any extra keys not in the template (DASHBOARD_TOKEN, MODAL_WORKSPACE, etc.)
    extras = {k: v for k, v in existing.items() if k not in written_keys}
    if extras:
        lines.append("# Additional settings")
        for key, val in extras.items():
            lines.append(f"{key}={val}")
        lines.append("")

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines))


def has_real_config():
    """Return True if .env exists AND has at least TRW + HL key set."""
    env = load_env()
    return bool(env.get("TRW_SESSION_TOKEN")) and bool(env.get("HYPERLIQUID_API_PRIVATE_KEY"))


def mask(val, show=6):
    if not val:
        return "(not set)"
    if len(val) <= show * 2:
        return val[:3] + "..." + val[-3:]
    return val[:show] + "..." + val[-show:]


# ---------------------------------------------------------------------------
# Connection checks
# ---------------------------------------------------------------------------

def check_trw(token):
    if not token:
        return False, "No token set"
    try:
        import requests
        resp = requests.post(
            "https://eden.therealworld.ag/messages/query",
            headers={"x-session-token": token, "Content-Type": "application/json", "Origin": "https://app.jointherealworld.com"},
            json={"channel": "01H83QAX979K9R7QTMH74ATR8C", "limit": 1, "sort": "Latest"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "Connected"
        elif resp.status_code == 401:
            return False, "Token expired or invalid"
        else:
            return False, f"HTTP {resp.status_code}"
    except ImportError:
        return False, "requests not installed"
    except Exception as e:
        return False, str(e)[:80]


def check_hyperliquid(api_key, master_addr):
    if not api_key or not master_addr:
        return False, "Keys not set"
    try:
        import eth_account
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        eth_account.Account.from_key(api_key)
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        state = info.user_state(master_addr)
        val = float(state["marginSummary"]["accountValue"])
        if val == 0:
            return True, "Connected but $0 &mdash; check you're using your master wallet address, not the API wallet address"
        return True, f"Connected &mdash; ${val:,.2f}"
    except ImportError:
        return False, "SDK not installed"
    except Exception as e:
        return False, str(e)[:80]


def get_modal_workspace():
    try:
        r = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def deploy_bot(values):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # Detect Modal workspace
    modal_workspace = ""
    try:
        ws_result = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
        modal_workspace = ws_result.stdout.strip()
    except Exception:
        pass
    # H1 FIX: Write secrets to temp file instead of CLI args
    import tempfile, secrets as _secrets
    dashboard_token = _secrets.token_urlsafe(24)
    secret_env = (
        f"TRW_SESSION_TOKEN={values.get('TRW_SESSION_TOKEN', '')}\n"
        f"TRW_SIGNAL_CHANNEL_ID=01H83QAX979K9R7QTMH74ATR8C\n"
        f"TRW_PROF_ADAM_USER_ID=01GHHHWZE7Q77AKGWZDGC5PDCN\n"
        f"HYPERLIQUID_API_PRIVATE_KEY={values.get('HYPERLIQUID_API_PRIVATE_KEY', '')}\n"
        f"HYPERLIQUID_MASTER_ACCOUNT_ADDRESS={values.get('HYPERLIQUID_MASTER_ACCOUNT_ADDRESS', '')}\n"
        f"SLACK_WEBHOOK_URL={values.get('SLACK_WEBHOOK_URL', '')}\n"
        f"MODAL_WORKSPACE={modal_workspace}\n"
        f"DASHBOARD_TOKEN={dashboard_token}\n"
    )
    fd, tmp_env = tempfile.mkstemp(suffix=".env", prefix="signalbot_")
    try:
        os.write(fd, secret_env.encode())
        os.close(fd)
        subprocess.run(
            ["modal", "secret", "create", "signal-bot-secrets", "--force", "--from-dotenv", tmp_env],
            capture_output=True, env=env, timeout=30,
        )
    finally:
        try:
            os.remove(tmp_env)
        except OSError:
            pass
    # Save dashboard token to local .env so manage.py can show the authed URL
    existing = load_env()
    existing["DASHBOARD_TOKEN"] = dashboard_token
    existing["MODAL_WORKSPACE"] = modal_workspace
    save_env(existing)

    r2 = subprocess.run(["modal", "deploy", "modal_signal_bot.py"], capture_output=True, env=env, timeout=120)
    if r2.returncode == 0:
        return True, "Deployed successfully"
    else:
        err = (r2.stderr or b"").decode("utf-8", errors="replace")[:200]
        return False, f"Deploy failed: {err}"


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

SHARED_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #e6edf3; padding: 20px; max-width: 700px; margin: 0 auto; }
h1 { font-size: 1.5em; margin-bottom: 4px; }
h2 { font-size: 1.1em; color: #8b949e; margin: 24px 0 8px; border-bottom: 1px solid #21262d; padding-bottom: 4px; }
.subtitle { color: #8b949e; font-size: 0.9em; margin-bottom: 16px; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; margin: 8px 0; }
.field { margin: 12px 0; }
.field label { display: block; font-weight: 600; margin-bottom: 4px; font-size: 0.9em; }
.field .hint { color: #8b949e; font-size: 0.82em; margin-bottom: 6px; line-height: 1.4; }
.field .current { color: #8b949e; font-size: 0.85em; margin-bottom: 4px; font-family: monospace; }
.field input { width: 100%; padding: 8px 12px; border-radius: 6px; border: 1px solid #30363d; background: #0d1117; color: #e6edf3; font-family: monospace; font-size: 0.85em; }
.field input:focus { outline: none; border-color: #58a6ff; }
.field input::placeholder { color: #484f58; }
.status { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }
.status.ok { background: #1a7f37; color: #fff; }
.status.err { background: #da3633; color: #fff; }
.status.off { background: #30363d; color: #8b949e; }
.status.pending { background: #6e40c9; color: #fff; }
.btn { display: inline-block; padding: 10px 24px; border-radius: 6px; border: none; font-weight: 600; font-size: 0.95em; cursor: pointer; margin-right: 8px; margin-top: 8px; }
.btn-primary { background: #1f6feb; color: #fff; }
.btn-success { background: #1a7f37; color: #fff; }
.btn-secondary { background: #21262d; color: #e6edf3; border: 1px solid #30363d; }
.btn:hover { opacity: 0.85; }
.info { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px 16px; margin: 12px 0; font-size: 0.9em; }
.info a { color: #58a6ff; }
.warn { color: #d29922; font-size: 0.85em; margin-top: 4px; }
.msg-ok { color: #3fb950; font-weight: 600; margin: 12px 0; }
.msg-err { color: #f85149; font-weight: 600; margin: 12px 0; }
.step-num { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; background: #1f6feb; color: #fff; font-weight: 700; font-size: 0.85em; margin-right: 8px; flex-shrink: 0; }
.step-title { display: flex; align-items: center; margin-bottom: 12px; }
.instructions { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px 16px; margin: 8px 0 12px; font-size: 0.85em; line-height: 1.6; color: #8b949e; }
.instructions ol { padding-left: 18px; }
.instructions li { margin: 2px 0; }
.instructions code { background: #21262d; padding: 1px 5px; border-radius: 3px; color: #e6edf3; font-size: 0.95em; }
"""


# ---------------------------------------------------------------------------
# Setup wizard page (first-time)
# ---------------------------------------------------------------------------

SETUP_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Bot Setup</title>
<style>{css}</style>
</head><body>

<h1>Signal Bot Setup</h1>
<p class="subtitle">First-time setup &mdash; follow each step below to get your bot running.</p>

{message}

<form method="POST">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<input type="hidden" name="mode" value="setup">

<!-- STEP 1: TRW -->
<h2><span class="step-num">1</span> TRW Session Token</h2>
<div class="card">
    <div class="instructions">
        <ol>
            <li>Open <strong>The Real World</strong> in Chrome/Brave</li>
            <li>Navigate to Prof Adam's <strong>Portfolio Signals</strong> channel</li>
            <li>Press <code>F12</code> to open DevTools</li>
            <li>Click the <strong>Network</strong> tab, then <strong>Fetch/XHR</strong></li>
            <li>Reload the page (<code>F5</code>)</li>
            <li>Click any request &rarr; <strong>Headers</strong> tab &rarr; scroll to <strong>Request Headers</strong></li>
            <li>Copy the value of <code>x-session-token</code></li>
        </ol>
    </div>
    <div class="field">
        <input type="text" name="trw_token" value="{trw_token}" placeholder="Paste your TRW session token here">
    </div>
    <span class="status {trw_status}">{trw_indicator}</span>
</div>

<!-- STEP 2: Hyperliquid -->
<h2><span class="step-num">2</span> Hyperliquid API Keys</h2>
<div class="card">
    <div class="instructions">
        <ol>
            <li>Go to <a href="https://app.hyperliquid.xyz" target="_blank" style="color:#58a6ff">app.hyperliquid.xyz</a></li>
            <li>Connect your wallet</li>
            <li>Click your address (top right) &rarr; <strong>API</strong></li>
            <li>Click <strong>Generate API Key</strong> &rarr; sign with your wallet</li>
            <li>Copy the <strong>API wallet private key</strong></li>
        </ol>
        <p style="margin-top:8px; color:#d29922">Your API wallet can place trades but <strong>cannot withdraw funds</strong>. Your money is safe even if the key is compromised.</p>
    </div>
    <div class="field">
        <label>API Private Key</label>
        <input type="text" name="hl_key" value="{hl_key}" placeholder="0x...">
    </div>
    <div class="field">
        <label>Master Wallet Address</label>
        <div class="hint">Your main MetaMask address (NOT the API wallet address). Shown at the top right of the Hyperliquid app.</div>
        <input type="text" name="hl_addr" value="{hl_addr}" placeholder="0x...">
    </div>
    <span class="status {hl_status}">{hl_indicator}</span>
</div>

<!-- STEP 3: Slack -->
<h2><span class="step-num">3</span> Slack Notifications <span style="color:#8b949e;font-weight:400;font-size:0.85em">(optional)</span></h2>
<div class="card">
    <div class="instructions">
        <ol>
            <li>Go to <a href="https://api.slack.com/messaging/webhooks" target="_blank" style="color:#58a6ff">api.slack.com/messaging/webhooks</a></li>
            <li>Create a Slack app &rarr; name it <strong>Signal Bot</strong></li>
            <li>Go to <strong>Incoming Webhooks</strong> &rarr; toggle ON</li>
            <li>Click <strong>Add New Webhook to Workspace</strong></li>
            <li>Pick a channel and copy the webhook URL</li>
        </ol>
    </div>
    <div class="field">
        <input type="text" name="slack_url" value="{slack_url}" placeholder="https://hooks.slack.com/services/...  (leave empty to skip)">
    </div>
</div>

<!-- Deploy -->
<div style="margin-top: 24px">
    <button type="submit" name="action" value="deploy" class="btn btn-success" style="font-size:1.05em;padding:12px 36px">Save &amp; Deploy</button>
    <button type="submit" name="action" value="save" class="btn btn-secondary">Save Only (deploy later)</button>
</div>

<p class="warn" style="margin-top:12px">
    Do not share your screen while this page is open &mdash; your tokens are visible in the form fields.<br>
    Tokens are saved locally in <code>.env</code> and sent only to Modal's encrypted secrets when you deploy.
</p>

</form>
</body></html>"""


# ---------------------------------------------------------------------------
# Management dashboard page (returning user)
# ---------------------------------------------------------------------------

MANAGE_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Bot Manager</title>
<style>{css}</style>
</head><body>

<h1>Signal Bot Manager</h1>
<p class="subtitle">Manage your tokens, check connection status, and deploy.</p>

{message}

<div class="info">
    <strong>Modal workspace:</strong> {workspace}<br>
    <strong>Dashboard:</strong> {dashboard_link}
</div>

<form method="POST">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<input type="hidden" name="mode" value="manage">

<h2>Status</h2>
<div class="card">
    <div style="display:flex;gap:12px;flex-wrap:wrap">
        <span class="status {trw_status}">TRW: {trw_msg}</span>
        <span class="status {hl_status}">HL: {hl_msg}</span>
        <span class="status {slack_status}">Slack: {slack_msg}</span>
    </div>
</div>

<h2>TRW Session Token</h2>
<div class="card">
    <div class="field">
        <div class="current">Current: {trw_masked}</div>
        <input type="text" name="trw_token" value="{trw_token}" placeholder="Paste new token here (or leave to keep current)">
    </div>
    <details style="margin-top:8px">
        <summary style="color:#58a6ff;cursor:pointer;font-size:0.85em">How to get a new token</summary>
        <div class="instructions" style="margin-top:6px">
            <ol>
                <li>Open <strong>The Real World</strong> in Chrome/Brave</li>
                <li>Navigate to Prof Adam's <strong>Portfolio Signals</strong> channel</li>
                <li>Press <code>F12</code> &rarr; <strong>Network</strong> tab &rarr; <strong>Fetch/XHR</strong></li>
                <li>Reload (<code>F5</code>), click any request, find <code>x-session-token</code></li>
            </ol>
        </div>
    </details>
</div>

<h2>Hyperliquid</h2>
<div class="card">
    <div class="field">
        <label>API Private Key</label>
        <div class="current">Current: {hl_key_masked}</div>
        <input type="text" name="hl_key" value="{hl_key}" placeholder="0x...">
    </div>
    <div class="field">
        <label>Master Account Address</label>
        <div class="current">Current: {hl_addr_masked}</div>
        <input type="text" name="hl_addr" value="{hl_addr}" placeholder="0x...">
    </div>
    <details style="margin-top:8px">
        <summary style="color:#58a6ff;cursor:pointer;font-size:0.85em">How to get API keys</summary>
        <div class="instructions" style="margin-top:6px">
            <ol>
                <li>Go to <a href="https://app.hyperliquid.xyz" target="_blank" style="color:#58a6ff">app.hyperliquid.xyz</a> &rarr; connect wallet</li>
                <li>Click address (top right) &rarr; <strong>API</strong> &rarr; <strong>Generate API Key</strong></li>
                <li>Sign with wallet, copy the private key</li>
            </ol>
        </div>
    </details>
</div>

<h2>Slack Webhook <span style="color:#8b949e;font-weight:400;font-size:0.85em">(optional)</span></h2>
<div class="card">
    <div class="field">
        <div class="current">Current: {slack_masked}</div>
        <input type="text" name="slack_url" value="{slack_url}" placeholder="https://hooks.slack.com/services/...">
    </div>
</div>

<div style="margin-top: 20px">
    <button type="submit" name="action" value="save" class="btn btn-secondary">Save Settings</button>
    <button type="submit" name="action" value="deploy" class="btn btn-success">Save &amp; Deploy</button>
</div>

<p class="warn" style="margin-top:12px">
    Do not share your screen while this page is open &mdash; your tokens are visible.<br>
    Tokens are saved locally in .env and sent only to Modal's encrypted secrets when you deploy.
</p>

</form>
</body></html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress console noise

    def _html_escape(self, s):
        """Minimal HTML attribute escaping."""
        return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    def do_GET(self):
        env = load_env()

        # Decide which view: setup wizard or management dashboard
        if has_real_config():
            self._serve_manage(env, message="")
        else:
            self._serve_setup(env, message="")

    def do_POST(self):
        # CSRF protection: reject requests without valid token
        origin = self.headers.get("Origin", "")
        if origin and "127.0.0.1" not in origin and "localhost" not in origin:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden: invalid origin")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = parse_qs(body)

        csrf = params.get("csrf_token", [""])[0]
        if csrf != _CSRF_TOKEN:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden: invalid CSRF token")
            return

        action = params.get("action", ["save"])[0]
        values = {
            "TRW_SESSION_TOKEN": params.get("trw_token", [""])[0].strip(),
            "HYPERLIQUID_API_PRIVATE_KEY": params.get("hl_key", [""])[0].strip(),
            "HYPERLIQUID_MASTER_ACCOUNT_ADDRESS": params.get("hl_addr", [""])[0].strip(),
            "SLACK_WEBHOOK_URL": params.get("slack_url", [""])[0].strip(),
        }

        # Normalise HL key prefix
        if values["HYPERLIQUID_API_PRIVATE_KEY"] and not values["HYPERLIQUID_API_PRIVATE_KEY"].startswith("0x"):
            values["HYPERLIQUID_API_PRIVATE_KEY"] = "0x" + values["HYPERLIQUID_API_PRIVATE_KEY"]
        if values["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"] and not values["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"].startswith("0x"):
            values["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"] = "0x" + values["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"]

        save_env(values)
        message = '<p class="msg-ok">Settings saved to .env</p>'

        if action == "deploy":
            ok, msg = deploy_bot(values)
            if ok:
                message = f'<p class="msg-ok">{self._html_escape(msg)}</p>'
            else:
                message = f'<p class="msg-err">{self._html_escape(msg)}</p>'

        # Re-render the appropriate page (now with real config, will show manage view)
        env = load_env()
        if has_real_config():
            self._serve_manage(env, message=message)
        else:
            self._serve_setup(env, message=message)

    def _serve_setup(self, env, message=""):
        """Render first-time setup wizard."""
        trw_token = env.get("TRW_SESSION_TOKEN", "")
        hl_key = env.get("HYPERLIQUID_API_PRIVATE_KEY", "")
        hl_addr = env.get("HYPERLIQUID_MASTER_ACCOUNT_ADDRESS", "")
        slack_url = env.get("SLACK_WEBHOOK_URL", "")

        # Only check connections if values were submitted (POST redirect)
        trw_status, trw_indicator = "pending", "Fill in above"
        hl_status, hl_indicator = "pending", "Fill in above"
        if trw_token:
            trw_ok, trw_msg = check_trw(trw_token)
            trw_status = "ok" if trw_ok else "err"
            trw_indicator = trw_msg
        if hl_key and hl_addr:
            hl_ok, hl_msg = check_hyperliquid(hl_key, hl_addr)
            hl_status = "ok" if hl_ok else "err"
            hl_indicator = hl_msg

        html = SETUP_HTML.format(
            css=SHARED_CSS,
            message=message,
            csrf_token=_CSRF_TOKEN,
            trw_token=self._html_escape(trw_token),
            trw_status=trw_status,
            trw_indicator=trw_indicator,
            hl_key=self._html_escape(hl_key),
            hl_addr=self._html_escape(hl_addr),
            hl_status=hl_status,
            hl_indicator=hl_indicator,
            slack_url=self._html_escape(slack_url),
        )
        self._send_html(html)

    def _serve_manage(self, env, message=""):
        """Render management dashboard for returning users."""
        workspace = get_modal_workspace()
        dash_token = env.get("DASHBOARD_TOKEN", "")
        if workspace:
            dashboard_url = f"https://{workspace}--signal-bot-web.modal.run"
            if dash_token:
                dashboard_url += f"?token={dash_token}"
        else:
            dashboard_url = ""

        trw_token = env.get("TRW_SESSION_TOKEN", "")
        hl_key = env.get("HYPERLIQUID_API_PRIVATE_KEY", "")
        hl_addr = env.get("HYPERLIQUID_MASTER_ACCOUNT_ADDRESS", "")
        slack_url = env.get("SLACK_WEBHOOK_URL", "")

        trw_ok, trw_msg = check_trw(trw_token)
        hl_ok, hl_msg = check_hyperliquid(hl_key, hl_addr)
        slack_set = bool(slack_url)

        html = MANAGE_HTML.format(
            css=SHARED_CSS,
            message=message,
            csrf_token=_CSRF_TOKEN,
            workspace=workspace or "(not connected)",
            dashboard_url=dashboard_url,
            dashboard_link=f'<a href="{dashboard_url}" target="_blank">{dashboard_url}</a>' if dashboard_url else "(deploy first)",
            trw_token=self._html_escape(trw_token),
            trw_masked=mask(trw_token),
            trw_status="ok" if trw_ok else "err",
            trw_msg=trw_msg,
            hl_key=self._html_escape(hl_key),
            hl_key_masked=mask(hl_key),
            hl_addr=self._html_escape(hl_addr),
            hl_addr_masked=mask(hl_addr, 8),
            hl_status="ok" if hl_ok else "err",
            hl_msg=hl_msg,
            slack_url=self._html_escape(slack_url),
            slack_masked=mask(slack_url),
            slack_status="ok" if slack_set else "off",
            slack_msg="Configured" if slack_set else "Not set (optional)",
        )
        self._send_html(html)

    def _send_html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if has_real_config():
        mode = "management dashboard"
    else:
        mode = "first-time setup wizard"

    print(f"  Starting Signal Bot Manager on http://127.0.0.1:{PORT}")
    print(f"  Mode: {mode}")
    print(f"  Opening in your browser...")
    print(f"  Press Ctrl+C to stop.\n")

    server = HTTPServer(("127.0.0.1", PORT), Handler)

    # Open browser after a short delay
    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Manager stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
