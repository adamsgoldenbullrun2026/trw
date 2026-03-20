"""
TRW Signal Reader — Polls Prof Adam's Portfolio Signal channel and extracts
the latest RSPS signal (portfolio allocation percentages).

Usage:
    python execution/trw_signal_reader.py                  # Fetch & display latest signal
    python execution/trw_signal_reader.py --raw             # Print raw message content
    python execution/trw_signal_reader.py --json            # Output parsed signal as JSON
    python execution/trw_signal_reader.py --watch           # Poll every 60s, print on change
"""

import os
import sys
import re
import json
import time
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parent / ".env")

TRW_API_BASE = "https://eden.therealworld.ag"
SESSION_TOKEN = os.getenv("TRW_SESSION_TOKEN")
SIGNAL_CHANNEL_ID = os.getenv("TRW_SIGNAL_CHANNEL_ID", "01H83QAX979K9R7QTMH74ATR8C")
PROF_ADAM_USER_ID = os.getenv("TRW_PROF_ADAM_USER_ID", "01GHHHWZE7Q77AKGWZDGC5PDCN")


# ── API ─────────────────────────────────────────────────────────────────────

def fetch_recent_messages(limit: int = 20) -> list[dict]:
    """Fetch the most recent messages from the signal channel."""
    if not SESSION_TOKEN:
        print("ERROR: TRW_SESSION_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(
        f"{TRW_API_BASE}/messages/query",
        headers={
            "x-session-token": SESSION_TOKEN,
            "Content-Type": "application/json",
            "Origin": "https://app.jointherealworld.com",
        },
        json={
            "channel": SIGNAL_CHANNEL_ID,
            "limit": limit,
            "sort": "Latest",
        },
        timeout=15,
    )

    if resp.status_code == 401:
        print("ERROR: TRW session token expired or invalid. Re-grab from DevTools.", file=sys.stderr)
        sys.exit(1)

    resp.raise_for_status()
    return resp.json().get("messages", [])


def find_latest_signal(messages: list[dict]) -> dict | None:
    """Find the most recent Portfolio Signal Update from Prof Adam."""
    for msg in messages:
        if (
            msg.get("author") == PROF_ADAM_USER_ID
            and "Portfolio Signal Update" in msg.get("content", "")
        ):
            return msg
    return None


# ── Parser ──────────────────────────────────────────────────────────────────

def parse_signal(content: str) -> dict:
    """
    Parse an RSPS signal message into structured data.

    Returns:
        {
            "allocations": [
                {"percent": 80.0, "type": "Spot", "asset": "ETH"},
                {"percent": 14.3, "type": "Spot", "asset": "HYPE"},
                {"percent": 5.7, "type": "Gold", "asset": "PAXG/XAUT"},
            ],
            "no_change": True/False,
            "btc_leverage": "Impermissible" or "Permissible",
            "raw": "..."
        }
    """
    result = {
        "allocations": [],
        "no_change": False,
        "btc_leverage": None,
        "raw": content,
    }

    # ── Detect "No change" ──
    exec_summary_match = re.search(
        r"Executive Summary:(.+?)(?:Associated Data|$)", content, re.DOTALL
    )
    if exec_summary_match:
        summary_text = exec_summary_match.group(1).strip().lower()
        if "no change" in summary_text:
            result["no_change"] = True

    # ── Extract allocations ──
    # Matches patterns like: **80% Spot $ETH**, 80% Spot $ETH, 71.4% ETH, 100% Cash
    # Also handles: **5.7% Gold 🟡 - $PAXG/$XAUT**
    alloc_pattern = re.compile(
        r"\*?\*?(\d+(?:\.\d+)?)\s*%\s*(Spot|Gold|Leverage)?\s*\$?([\w/\$]+)\*?\*?",
        re.IGNORECASE,
    )

    # Find the signal section — handles multiple format eras:
    #   Current:  "RSPS Signal:"
    #   Older:    "Risk-On Crypto Signal:"
    #   Oldest:   "**Signal:**" (standalone, on its own line)
    signal_section = re.search(
        r"(?:RSPS Signal|Risk-On Crypto Signal|\*\*Signal:\*\*)\s*:?\s*\*?\*?"
        r"(.+?)(?:Executive Summary|Associated Data|Dominant Denominator|───|$)",
        content,
        re.DOTALL,
    )
    if signal_section:
        section_text = signal_section.group(0)
        for match in alloc_pattern.finditer(section_text):
            pct_str, alloc_type, asset = match.groups()
            # Clean up asset name - remove leading $ and emoji artifacts
            asset = asset.strip("$*").upper()

            # Handle "Gold 🟡 - $PAXG/$XAUT" pattern — asset captured as "GOLD"
            if asset == "GOLD" or (alloc_type and alloc_type.lower() == "gold"):
                gold_match = re.search(
                    r"PAXG(?:\s*/\s*\$?XAUT)?",
                    section_text,
                    re.IGNORECASE,
                )
                if gold_match:
                    asset = gold_match.group(0).upper().replace(" ", "").replace("$", "")
                else:
                    asset = "PAXG/XAUT"
                alloc_type = "Gold"
            elif asset == "CASH" or (alloc_type and alloc_type.lower() == "cash"):
                # Cash allocation — check Dominant Denominator for what to hold
                dom_match = re.search(
                    r"Dominant Denominator.*?(?:GOLD|PAXG|USD)",
                    content,
                    re.DOTALL | re.IGNORECASE,
                )
                if dom_match and "gold" in dom_match.group(0).lower():
                    alloc_type = "Gold"
                    asset = "PAXG/XAUT"
                else:
                    # Default cash to USDC (stays in Hyperliquid wallet)
                    alloc_type = "Cash"
                    asset = "USDC"
            elif not alloc_type:
                alloc_type = "Spot"
            else:
                alloc_type = alloc_type.capitalize()

            result["allocations"].append({
                "percent": float(pct_str),
                "type": alloc_type,
                "asset": asset,
            })

    # ── BTC Leverage condition ──
    leverage_match = re.search(
        r"BTC Leverage.*?=.*?(Impermissible|Permissible)", content, re.IGNORECASE
    )
    if leverage_match:
        result["btc_leverage"] = leverage_match.group(1).capitalize()

    return result


# ── Display ─────────────────────────────────────────────────────────────────

def format_signal(parsed: dict, timestamp_ms: int | None = None) -> str:
    """Format a parsed signal for display."""
    lines = []
    if timestamp_ms:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        lines.append(f"Signal time: {dt.strftime('%Y-%m-%d %H:%M UTC')}")

    if parsed["no_change"]:
        lines.append("Status: NO CHANGE")
    else:
        lines.append("Status: NEW ALLOCATIONS")

    lines.append("")
    total = 0
    for alloc in parsed["allocations"]:
        lines.append(f"  {alloc['percent']:>5.1f}%  {alloc['type']:<6} {alloc['asset']}")
        total += alloc["percent"]

    if parsed["allocations"]:
        lines.append(f"  {'─' * 25}")
        lines.append(f"  {total:>5.1f}%  Total")

    if parsed["btc_leverage"]:
        lines.append(f"\nBTC Leverage: {parsed['btc_leverage']}")

    return "\n".join(lines)


# ── Watch mode ──────────────────────────────────────────────────────────────

def watch_loop(interval: int = 60):
    """Poll for new signals and print when changed."""
    last_signal_id = None
    print(f"Watching for signals every {interval}s... (Ctrl+C to stop)")

    while True:
        try:
            messages = fetch_recent_messages(limit=20)
            signal_msg = find_latest_signal(messages)

            if signal_msg and signal_msg["_id"] != last_signal_id:
                last_signal_id = signal_msg["_id"]
                parsed = parse_signal(signal_msg["content"])
                print(f"\n{'=' * 40}")
                print(format_signal(parsed, signal_msg.get("timestamp")))
                print(f"{'=' * 40}")

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            time.sleep(interval)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TRW RSPS Signal Reader")
    parser.add_argument("--raw", action="store_true", help="Print raw message content")
    parser.add_argument("--json", action="store_true", help="Output parsed signal as JSON")
    parser.add_argument("--watch", action="store_true", help="Poll every 60s, print on change")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds")
    args = parser.parse_args()

    if args.watch:
        watch_loop(args.interval)
        return

    messages = fetch_recent_messages(limit=20)
    signal_msg = find_latest_signal(messages)

    if not signal_msg:
        print("No signal found in recent messages.", file=sys.stderr)
        sys.exit(1)

    if args.raw:
        sys.stdout.buffer.write(signal_msg["content"].encode("utf-8"))
        print()
        return

    parsed = parse_signal(signal_msg["content"])

    if args.json:
        # Remove raw content from JSON output (too verbose)
        output = {k: v for k, v in parsed.items() if k != "raw"}
        output["message_id"] = signal_msg["_id"]
        output["timestamp"] = signal_msg.get("timestamp")
        print(json.dumps(output, indent=2))
        return

    print(format_signal(parsed, signal_msg.get("timestamp")))


if __name__ == "__main__":
    main()
