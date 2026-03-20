"""
Interactive Setup Wizard for TRW Signal Bot (CLI fallback).

RECOMMENDED: Use 'python manage.py' instead — it provides a web-based GUI
that handles both first-time setup AND ongoing management in one place.

This CLI wizard still works as a fallback if you prefer the terminal.

Run: python setup.py

SECURITY & TRANSPARENCY:
    This setup script connects to exactly ONE external service during setup:
      - https://eden.therealworld.ag  (to verify your TRW token works)

    Your tokens are stored in TWO places, both controlled by YOU:
      1. A local .env file on YOUR computer (never uploaded anywhere)
      2. Modal encrypted secrets in YOUR Modal account (you created it)

    Nothing is sent to the author or any third party. This script does not
    collect, transmit, or log your credentials anywhere beyond the two
    locations listed above.

    If you're unsure, paste this entire file into ChatGPT or Claude and ask:
    "Does this setup script send my tokens anywhere suspicious?"
"""

import os
import sys
import subprocess
import shutil
import secrets


def print_header(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def print_step(n, total, text):
    print(f"\n--- Step {n}/{total}: {text} ---\n")


def ask(prompt, secret=False):
    # Note: we intentionally don't use getpass — it breaks on some Windows terminals
    # and prevents users from seeing what they're typing. Tokens are long random
    # strings so shoulder-surfing isn't a realistic concern here.
    return input(prompt).strip()


def check_prereqs():
    print_header("Checking Prerequisites")
    ok = True

    # Python
    print(f"  Python: {sys.version.split()[0]} ✓")

    # pip packages — mapping: pip name → import name
    packages = {
        "requests": "requests",
        "python-dotenv": "dotenv",
        "eth-account": "eth_account",
        "hyperliquid-python-sdk": "hyperliquid",
    }
    for pip_name, import_name in packages.items():
        try:
            __import__(import_name)
            print(f"  {pip_name}: ✓")
        except ImportError:
            print(f"  {pip_name}: NOT INSTALLED")
            print(f"    Run: pip install {pip_name}")
            ok = False

    # Modal CLI
    if shutil.which("modal"):
        print(f"  Modal CLI: ✓")
    else:
        print(f"  Modal CLI: NOT INSTALLED")
        print(f"    Run: pip install modal")
        print(f"    Then: modal token new")
        ok = False

    return ok


def get_trw_token():
    print_step(1, 4, "TRW Session Token")
    print("  To get your TRW session token:")
    print("  1. Open The Real World in Chrome/Brave")
    print("  2. Navigate to Adam's Portfolio Signals channel")
    print("  3. Press F12 to open DevTools")
    print("  4. Click the 'Network' tab")
    print("  5. Click 'Fetch/XHR' filter")
    print("  6. Reload the page (F5)")
    print("  7. Click any request in the list")
    print("  8. In the 'Headers' tab, scroll to 'Request Headers'")
    print("  9. Find 'x-session-token' and copy the value")
    print()
    token = ask("  Paste your TRW session token: ")
    if len(token) < 20:
        print("  WARNING: That looks too short. Make sure you copied the full token.")
    return token


def get_hyperliquid_keys():
    print_step(2, 4, "Hyperliquid API Keys")
    print("  To set up Hyperliquid API access:")
    print("  1. Go to https://app.hyperliquid.xyz")
    print("  2. Connect your MetaMask wallet")
    print("  3. Click your address (top right) → 'API'")
    print("  4. Click 'Generate API Key'")
    print("  5. MetaMask will ask you to sign — confirm it")
    print("  6. Copy the API wallet PRIVATE KEY")
    print()
    print("  IMPORTANT: Your API wallet can place trades but CANNOT withdraw funds.")
    print("  Even if the key is compromised, your money is safe.")
    print()
    api_key = ask("  Paste your API wallet private key (starts with 0x): ")
    if not api_key.startswith("0x"):
        api_key = "0x" + api_key

    print()
    print("  Now enter your MAIN wallet address (the one you use with Hyperliquid).")
    print("  This is NOT the API wallet address — it's your MetaMask address.")
    print("  You can find it at the top right of the Hyperliquid app.")
    print()
    master_addr = ask("  Paste your main wallet address (starts with 0x): ")
    if not master_addr.startswith("0x"):
        master_addr = "0x" + master_addr

    return api_key, master_addr


def get_slack_webhook():
    print_step(3, 4, "Slack Notifications (Optional)")
    print("  Slack lets you get notifications on your phone when the bot")
    print("  detects a signal or makes a trade. Recommended but not required.")
    print()
    want_slack = ask("  Do you want Slack notifications? (y/n): ")
    if want_slack.lower() not in ("y", "yes"):
        print("  Skipping Slack. You can always add it later by updating your .env")
        print("  and Modal secrets with a SLACK_WEBHOOK_URL.")
        return ""

    print()
    print("  To set up Slack notifications:")
    print("  1. Go to https://api.slack.com/messaging/webhooks")
    print("  2. Click 'Create your Slack app' → 'From scratch'")
    print("  3. Name it 'Signal Bot', pick your workspace")
    print("  4. Go to 'Incoming Webhooks' → toggle ON")
    print("  5. Click 'Add New Webhook to Workspace'")
    print("  6. Pick the channel you want notifications in")
    print("  7. Copy the webhook URL")
    print()
    webhook = ask("  Slack webhook URL: ")
    return webhook


def create_env_file(trw_token, api_key, master_addr, slack_webhook):
    print_step(4, 5, "Creating .env File")
    env_content = f"""# TRW (The Real World)
TRW_SESSION_TOKEN={trw_token}
TRW_SIGNAL_CHANNEL_ID=01H83QAX979K9R7QTMH74ATR8C
TRW_PROF_ADAM_USER_ID=01GHHHWZE7Q77AKGWZDGC5PDCN

# Hyperliquid
HYPERLIQUID_API_PRIVATE_KEY={api_key}
HYPERLIQUID_MASTER_ACCOUNT_ADDRESS={master_addr}

# Slack (optional — leave empty to disable notifications)
SLACK_WEBHOOK_URL={slack_webhook}
"""
    with open(".env", "w") as f:
        f.write(env_content)
    print("  .env file created ✓")
    print("  IMPORTANT: Never share this file or commit it to git!")


def deploy_to_modal(trw_token, api_key, master_addr, slack_webhook):
    print_step(5, 5, "Deploying to Modal")

    # Detect Modal workspace name
    modal_workspace = ""
    try:
        ws_result = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
        modal_workspace = ws_result.stdout.strip()
    except Exception:
        pass

    # H1 FIX: Write secrets to a temp file instead of CLI args (avoids process list exposure)
    print("  Creating Modal secret...")
    import tempfile
    dashboard_token = secrets.token_urlsafe(24)
    secret_env = (
        f"TRW_SESSION_TOKEN={trw_token}\n"
        f"TRW_SIGNAL_CHANNEL_ID=01H83QAX979K9R7QTMH74ATR8C\n"
        f"TRW_PROF_ADAM_USER_ID=01GHHHWZE7Q77AKGWZDGC5PDCN\n"
        f"HYPERLIQUID_API_PRIVATE_KEY={api_key}\n"
        f"HYPERLIQUID_MASTER_ACCOUNT_ADDRESS={master_addr}\n"
        f"SLACK_WEBHOOK_URL={slack_webhook}\n"
        f"MODAL_WORKSPACE={modal_workspace}\n"
        f"DASHBOARD_TOKEN={dashboard_token}\n"
    )
    fd, tmp_env = tempfile.mkstemp(suffix=".env", prefix="signalbot_")
    try:
        os.write(fd, secret_env.encode())
        os.close(fd)
        result = subprocess.run(
            ["modal", "secret", "create", "signal-bot-secrets", "--from-dotenv", tmp_env],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["modal", "secret", "create", "signal-bot-secrets", "--force", "--from-dotenv", tmp_env],
                capture_output=True, text=True,
            )
    finally:
        try:
            os.remove(tmp_env)
        except OSError:
            pass

    if result.returncode == 0:
        print("  Modal secret created ✓")
        print(f"  Dashboard token: {dashboard_token}")
        print("  (Save this — you'll need it to access your dashboard)")
    else:
        print(f"  WARNING: Failed to create secret: {result.stderr}")
        print("  You may need to run: modal token new")
        return False

    # Deploy
    print("  Deploying bot...")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        ["modal", "deploy", "modal_signal_bot.py"],
        capture_output=True, env=env,
    )
    if result.returncode == 0:
        print("  Bot deployed ✓")
        # Extract dashboard URL from deploy output
        dashboard_url = ""
        output = (result.stdout or b"").decode("utf-8", errors="replace") + (result.stderr or b"").decode("utf-8", errors="replace")
        for line in output.split("\n"):
            if "signal-bot-web" in line:
                import re
                url_match = re.search(r"(https://\S+signal-bot-web\S+)", line)
                if url_match:
                    dashboard_url = url_match.group(1)
                    break

        # If we couldn't extract it, construct it from Modal workspace
        if not dashboard_url:
            try:
                ws_result = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
                workspace = ws_result.stdout.strip()
                if workspace:
                    dashboard_url = f"https://{workspace}--signal-bot-web.modal.run"
            except Exception:
                pass

        if dashboard_url:
            print(f"\n  Dashboard URL: {dashboard_url}")
            print(f"  Bookmark this on your phone!")
            # Open in browser
            try:
                import webbrowser
                webbrowser.open(dashboard_url)
                print(f"  (Opening in your browser...)")
            except Exception:
                pass

        return True
    else:
        err = (result.stderr or b"").decode("utf-8", errors="replace")
        print(f"  Deployment failed: {err}")
        return False


def test_connection(trw_token):
    print("\n  Testing TRW connection...", end=" ")
    import requests
    try:
        resp = requests.post(
            "https://eden.therealworld.ag/messages/query",
            headers={
                "x-session-token": trw_token,
                "Content-Type": "application/json",
                "Origin": "https://app.jointherealworld.com",
            },
            json={"channel": "01H83QAX979K9R7QTMH74ATR8C", "limit": 1, "sort": "Latest"},
            timeout=15,
        )
        if resp.status_code == 200:
            print("✓")
            return True
        elif resp.status_code == 401:
            print("FAILED — token expired or invalid")
            return False
        else:
            print(f"FAILED — status {resp.status_code}")
            return False
    except Exception as e:
        print(f"FAILED — {e}")
        return False


def load_current_env():
    """Load current .env values if they exist."""
    values = {
        "TRW_SESSION_TOKEN": "",
        "HYPERLIQUID_API_PRIVATE_KEY": "",
        "HYPERLIQUID_MASTER_ACCOUNT_ADDRESS": "",
        "SLACK_WEBHOOK_URL": "",
    }
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if key in values:
                        values[key] = val.strip()
    return values


def save_and_deploy(trw_token, api_key, master_addr, slack_webhook):
    """Save .env, update Modal secrets, and redeploy."""
    create_env_file(trw_token, api_key, master_addr, slack_webhook)
    print()
    deploy = ask("  Update Modal secrets and redeploy? (y/n): ")
    if deploy.lower() in ("y", "yes"):
        success = deploy_to_modal(trw_token, api_key, master_addr, slack_webhook)
        if success:
            print_deploy_success()
        else:
            print("\n  Deployment failed. Your .env file has been updated.")
            print("  You can deploy manually: modal deploy modal_signal_bot.py")
    else:
        print("\n  .env file updated. Deploy manually when ready:")
        print("  modal deploy modal_signal_bot.py")


def print_deploy_success():
    try:
        ws_result = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
        workspace = ws_result.stdout.strip()
        dash_url = f"https://{workspace}--signal-bot-web.modal.run"
    except Exception:
        dash_url = "https://YOUR_WORKSPACE--signal-bot-web.modal.run"

    print_header("Setup Complete!")
    print("  Your signal bot is now running in the cloud.")
    print("  It will automatically check for new signals and rebalance.")
    print()
    print(f"  DASHBOARD: {dash_url}")
    print(f"  Bookmark this on your phone!")
    print()
    print("  Schedule (UTC):")
    print("    00:00-00:30 — checks every 2 minutes")
    print("    00:30-05:00 — checks every 10 minutes")
    print("    05:00-00:00 — checks every hour")
    print()
    print("  00:00-05:00 — trades automatically")
    print("  05:00-00:00 — sends you a notification to approve")
    print()
    print("  To test: python trw_signal_reader.py")
    print("  To check positions: python hyperliquid_rebalancer.py --status")


def reconfigure():
    """Update individual settings without redoing everything."""
    print_header("TRW Signal Bot — Reconfigure")

    current = load_current_env()
    if not any(current.values()):
        print("  No existing .env found. Run 'python setup.py' first for initial setup.")
        sys.exit(1)

    print("  What would you like to update?\n")
    print("    1. TRW session token (if expired or invalid)")
    print("    2. Hyperliquid API keys (private key + wallet address)")
    print("    3. Slack webhook (add or change notifications)")
    print("    4. Update everything")
    print("    5. Just redeploy (no changes)")
    print()
    choice = ask("  Enter a number (1-5): ")

    trw_token = current["TRW_SESSION_TOKEN"]
    api_key = current["HYPERLIQUID_API_PRIVATE_KEY"]
    master_addr = current["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"]
    slack_webhook = current["SLACK_WEBHOOK_URL"]

    if choice == "1":
        while True:
            trw_token = get_trw_token()
            if test_connection(trw_token):
                break
            print("  Token didn't work. Let's try again.\n")

    elif choice == "2":
        api_key, master_addr = get_hyperliquid_keys()

    elif choice == "3":
        slack_webhook = get_slack_webhook()

    elif choice == "4":
        while True:
            trw_token = get_trw_token()
            if test_connection(trw_token):
                break
            print("  Token didn't work. Let's try again.\n")
        api_key, master_addr = get_hyperliquid_keys()
        slack_webhook = get_slack_webhook()

    elif choice == "5":
        print("\n  Redeploying with current settings...")
        deploy_to_modal(trw_token, api_key, master_addr, slack_webhook)
        print_deploy_success()
        return

    else:
        print("  Invalid choice.")
        return

    save_and_deploy(trw_token, api_key, master_addr, slack_webhook)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TRW Signal Bot Setup")
    parser.add_argument("--reconfigure", "-r", action="store_true",
                        help="Update individual settings (tokens, keys, Slack)")
    parser.add_argument("--redeploy", action="store_true",
                        help="Redeploy with current .env settings")
    args = parser.parse_args()

    if args.reconfigure:
        reconfigure()
        return

    if args.redeploy:
        current = load_current_env()
        if not current["TRW_SESSION_TOKEN"]:
            print("  No .env found. Run 'python setup.py' first.")
            sys.exit(1)
        print("  Redeploying with current settings...")
        deploy_to_modal(
            current["TRW_SESSION_TOKEN"],
            current["HYPERLIQUID_API_PRIVATE_KEY"],
            current["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"],
            current["SLACK_WEBHOOK_URL"],
        )
        print_deploy_success()
        return

    # Full setup
    print_header("TRW Signal Bot — Setup Wizard")
    print("  This wizard will help you set up the automated")
    print("  signal bot that reads Prof Adam's RSPS signals")
    print("  and rebalances your Hyperliquid portfolio.")
    print()
    print("  You'll need:")
    print("    - A TRW account (Investing Masterclass campus)")
    print("    - A Hyperliquid account with USDC deposited")
    print("    - A free Modal account (modal.com)")
    print("    - (Optional) A Slack workspace for notifications")
    print()
    print("  Already set up? Use these commands instead:")
    print("    python setup.py --reconfigure   (update tokens/keys)")
    print("    python setup.py --redeploy      (redeploy with current settings)")
    print()
    input("  Press Enter to begin full setup...")

    if not check_prereqs():
        print("\n  Please install missing prerequisites and run setup again.")
        sys.exit(1)

    while True:
        trw_token = get_trw_token()
        if test_connection(trw_token):
            break
        print("  Token didn't work. Let's try again.\n")

    api_key, master_addr = get_hyperliquid_keys()
    slack_webhook = get_slack_webhook()

    save_and_deploy(trw_token, api_key, master_addr, slack_webhook)
    print()


if __name__ == "__main__":
    main()
