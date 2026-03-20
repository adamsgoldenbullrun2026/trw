"""
Modal Signal Bot — Polls TRW for Prof Adam's portfolio signal and auto-rebalances
on Hyperliquid. Sends Slack notifications at every step.

Schedule (UK time):
  00:00-00:30  → every 2 minutes (peak signal window)
  00:30-05:00  → every 10 minutes
  05:00-00:00  → every hour

Trading mode:
  00:00-05:00  → fully autonomous (auto-execute)
  05:00-00:00  → approval required (Slack + dashboard link)

Deploy:  PYTHONUTF8=1 modal deploy modal_signal_bot.py
Dashboard: https://<your-workspace>--signal-bot-web.modal.run

Required secrets (signal-bot-secrets):
    TRW_SESSION_TOKEN, TRW_SIGNAL_CHANNEL_ID, TRW_PROF_ADAM_USER_ID
    HYPERLIQUID_API_PRIVATE_KEY, HYPERLIQUID_MASTER_ACCOUNT_ADDRESS
    SLACK_WEBHOOK_URL (optional)
"""

import os
import json
import re
import time
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import modal

app = modal.App("signal-bot")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "requests",
        "hyperliquid-python-sdk",
        "eth-account",
        "fastapi[standard]",
    )
)

# ── Slack ───────────────────────────────────────────────────────────────────

def send_slack(text: str, mention: bool = False):
    """Send notification via Slack incoming webhook. Silently skips if not configured."""
    import requests as req
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print(f"[SLACK SKIPPED] {text}")
        return
    try:
        req.post(webhook_url, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"[SLACK ERROR] {e}")


# ── TRW Signal Reader ──────────────────────────────────────────────────────

TRW_API_BASE = "https://eden.therealworld.ag"


def fetch_recent_messages(limit: int = 20) -> list[dict]:
    import requests as req
    resp = req.post(
        f"{TRW_API_BASE}/messages/query",
        headers={
            "x-session-token": os.environ["TRW_SESSION_TOKEN"],
            "Content-Type": "application/json",
            "Origin": "https://app.jointherealworld.com",
        },
        json={
            "channel": os.environ.get("TRW_SIGNAL_CHANNEL_ID", "01H83QAX979K9R7QTMH74ATR8C"),
            "limit": limit,
            "sort": "Latest",
        },
        timeout=15,
    )
    if resp.status_code == 401:
        raise RuntimeError("TRW session token expired")
    resp.raise_for_status()
    return resp.json().get("messages", [])


def find_latest_signal(messages: list[dict]) -> dict | None:
    prof_adam = os.environ.get("TRW_PROF_ADAM_USER_ID", "01GHHHWZE7Q77AKGWZDGC5PDCN")
    for msg in messages:
        if msg.get("author") == prof_adam and "Portfolio Signal Update" in msg.get("content", ""):
            return msg
    return None


def parse_signal(content: str) -> dict:
    """Parse signal — synced with trw_signal_reader.py's parse_signal."""
    result = {"allocations": [], "no_change": False, "btc_leverage": None}

    exec_match = re.search(r"Executive Summary:(.+?)(?:Associated Data|$)", content, re.DOTALL)
    if exec_match and "no change" in exec_match.group(1).lower():
        result["no_change"] = True

    alloc_pattern = re.compile(
        r"\*?\*?(\d+(?:\.\d+)?)\s*%\s*(Spot|Gold|Leverage)?\s*\$?([\w/\$]+)\*?\*?",
        re.IGNORECASE,
    )
    # Handles multiple format eras: "RSPS Signal:", "Risk-On Crypto Signal:", "**Signal:**"
    signal_section = re.search(
        r"(?:RSPS Signal|Risk-On Crypto Signal|\*\*Signal:\*\*)\s*:?\s*\*?\*?"
        r"(.+?)(?:Executive Summary|Associated Data|Dominant Denominator|───|$)",
        content, re.DOTALL,
    )
    if signal_section:
        section_text = signal_section.group(0)
        for match in alloc_pattern.finditer(section_text):
            pct_str, alloc_type, asset = match.groups()
            asset = asset.strip("$*").upper()
            if asset == "GOLD" or (alloc_type and alloc_type.lower() == "gold"):
                gold_match = re.search(r"PAXG(?:\s*/\s*\$?XAUT)?", section_text, re.IGNORECASE)
                asset = gold_match.group(0).upper().replace(" ", "").replace("$", "") if gold_match else "PAXG/XAUT"
                alloc_type = "Gold"
            elif asset == "CASH" or (alloc_type and alloc_type.lower() == "cash"):
                # Cash allocation — check Dominant Denominator for what to hold
                dom_match = re.search(
                    r"Dominant Denominator.*?(?:GOLD|PAXG|USD)",
                    content, re.DOTALL | re.IGNORECASE,
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
            result["allocations"].append({"percent": float(pct_str), "type": alloc_type, "asset": asset})

    lev_match = re.search(r"BTC Leverage.*?=.*?(Impermissible|Permissible)", content, re.IGNORECASE)
    if lev_match:
        result["btc_leverage"] = lev_match.group(1).capitalize()
    return result


# ── Hyperliquid ─────────────────────────────────────────────────────────────

ASSET_MAP = {
    "ETH": "ETH", "BTC": "BTC", "HYPE": "HYPE", "SOL": "SOL",
    "SUI": "SUI", "DOGE": "DOGE", "XRP": "XRP", "AVAX": "AVAX",
    "LINK": "LINK", "ADA": "ADA", "DOT": "DOT",
    "PAXG/XAUT": "PAXG", "PAXG": "PAXG", "XAUT": "PAXG", "GOLD": "PAXG",
}
MIN_TRADE_USD = 1.0
MAX_SLIPPAGE = 0.03


def get_hl_clients():
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    wallet = eth_account.Account.from_key(os.environ["HYPERLIQUID_API_PRIVATE_KEY"])
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=os.environ["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"])
    return info, exchange


def get_account_state(info):
    address = os.environ["HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"]
    state = info.user_state(address)
    margin = state["marginSummary"]
    account_value = float(margin["accountValue"])
    positions = {}
    for pos in state.get("assetPositions", []):
        p = pos["position"]
        coin = p["coin"]
        size = float(p.get("szi", 0))
        entry_px = float(p["entryPx"]) if p.get("entryPx") else 0
        leverage = p.get("leverage", {})
        positions[coin] = {
            "size": size, "entry_px": entry_px,
            "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
            "value_usd": abs(size) * entry_px,
            "leverage": leverage.get("value", "?"),
            "position_value": float(p.get("positionValue", 0)),
        }
    return {"account_value": account_value, "positions": positions}


def get_current_prices(info, assets):
    all_mids = info.all_mids()
    prices = {}
    for asset in assets:
        hl_ticker = ASSET_MAP.get(asset, asset)
        if hl_ticker in all_mids:
            prices[asset] = float(all_mids[hl_ticker])
    return prices


def compute_rebalance(allocations, account_value, current_positions, prices):
    trades = []
    target_positions = {}
    skipped_assets = []
    for alloc in allocations:
        asset = alloc["asset"]
        hl_ticker = ASSET_MAP.get(asset, asset)
        if asset not in prices:
            skipped_assets.append(f"{alloc['percent']}% {asset}")
            continue
        target_usd = account_value * (alloc["percent"] / 100.0)
        target_positions[hl_ticker] = {
            "asset": asset, "target_usd": target_usd,
            "target_size": target_usd / prices[asset], "price": prices[asset],
        }
    all_tickers = set(target_positions.keys())
    for coin in current_positions:
        if current_positions[coin]["size"] != 0:
            all_tickers.add(coin)
    # H2 FIX: Fetch all current market prices for positions we need to close
    all_mids = {}
    try:
        import requests as _req
        _resp = _req.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=10)
        all_mids = {k: float(v) for k, v in _resp.json().items()}
    except Exception:
        pass
    for ticker in all_tickers:
        current_size = current_positions.get(ticker, {}).get("size", 0)
        target = target_positions.get(ticker)
        if target:
            target_size, price, asset = target["target_size"], target["price"], target["asset"]
        else:
            # Use live market price, fall back to entry_px, skip if both are 0
            price = all_mids.get(ticker, 0) or current_positions[ticker].get("entry_px", 0)
            if price == 0:
                print(f"WARNING: Cannot close {ticker} — no price available. Skipping.")
                continue
            target_size, asset = 0, ticker
        delta_size = target_size - current_size
        delta_usd = abs(delta_size) * price
        if delta_usd < MIN_TRADE_USD:
            continue
        trades.append({
            "asset": asset, "hl_ticker": ticker,
            "side": "buy" if delta_size > 0 else "sell",
            "size": abs(delta_size), "value_usd": delta_usd, "price": price,
        })
    if skipped_assets:
        send_slack(
            f"WARNING: Could not find price for: {', '.join(skipped_assets)}. "
            f"These allocations will stay in cash. You may need to add them to ASSET_MAP.",
            mention=True,
        )
    trades.sort(key=lambda t: (0 if t["side"] == "sell" else 1, -t["value_usd"]))
    return trades


def execute_trades(info, exchange, trades):
    results = []
    # C3 FIX: If leverage setting fails, ABORT that trade entirely
    leverage_ok = set()
    for ticker in {t["hl_ticker"] for t in trades}:
        try:
            exchange.update_leverage(1, ticker, is_cross=True)
            leverage_ok.add(ticker)
        except Exception as e:
            send_slack(f"CRITICAL: Failed to set 1x leverage for {ticker}: {e} — SKIPPING ALL {ticker} TRADES", mention=True)
        time.sleep(0.3)
    # Filter out trades where leverage couldn't be confirmed
    trades = [t for t in trades if t["hl_ticker"] in leverage_ok]
    if not trades:
        send_slack("ALL trades skipped — could not confirm 1x leverage on any asset", mention=True)
        return results
    meta = info.meta()
    sz_dec_map = {a["name"]: a["szDecimals"] for a in meta["universe"]}
    for trade in trades:
        ticker = trade["hl_ticker"]
        sz_decimals = sz_dec_map.get(ticker, 2)
        size = float(Decimal(str(trade["size"])).quantize(Decimal(10) ** -sz_decimals, rounding=ROUND_DOWN))
        if size == 0:
            results.append({**trade, "status": "skipped", "reason": "size rounded to 0"})
            continue
        try:
            result = exchange.market_open(ticker, is_buy=(trade["side"] == "buy"), sz=size, slippage=MAX_SLIPPAGE)
            if result["status"] == "ok":
                for status in result["response"]["data"]["statuses"]:
                    if "filled" in status:
                        filled = status["filled"]
                        results.append({**trade, "status": "filled", "filled_size": float(filled["totalSz"]), "avg_price": float(filled["avgPx"])})
                    elif "error" in status:
                        results.append({**trade, "status": "error", "error": status["error"]})
            else:
                results.append({**trade, "status": "failed", "error": str(result)})
        except Exception as e:
            results.append({**trade, "status": "exception", "error": str(e)})
        time.sleep(0.5)
    return results


# ── State ───────────────────────────────────────────────────────────────────

signal_state = modal.Dict.from_name("signal-bot-state", create_if_missing=True)


def is_autonomous_hours() -> bool:
    """00:00-05:00 UK = autonomous. Rest = approval required."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/London"))
    return 0 <= now.hour < 5


def should_poll_now() -> bool:
    """Check if we should poll based on the schedule."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/London"))
    h, m = now.hour, now.minute

    # 00:00-00:30 → every 2 minutes
    if h == 0 and m < 30:
        return m % 2 == 0
    # 00:30-05:00 → every 10 minutes
    if (h == 0 and m >= 30) or (1 <= h < 5):
        return m % 10 == 0
    # 05:00-23:59 → every hour
    return m == 0


def do_rebalance(parsed: dict, msg_id: str) -> dict:
    """Execute a rebalance. Returns result dict."""
    # H4 FIX: Validate allocations sum to ~100% before trading
    alloc_sum = sum(a["percent"] for a in parsed["allocations"])
    if alloc_sum < 95 or alloc_sum > 105:
        send_slack(
            f"ALLOCATION SUM ERROR: {alloc_sum:.1f}% (expected ~100%)\n"
            f"Signal may have been parsed incorrectly. NOT TRADING.\n"
            f"Allocations: {', '.join(f'{a[\"percent\"]}% {a[\"asset\"]}' for a in parsed['allocations'])}",
            mention=True,
        )
        return {"status": "error", "error": f"allocation_sum_{alloc_sum:.1f}_pct"}

    info, exchange = get_hl_clients()
    state = get_account_state(info)
    account_value = state["account_value"]

    if account_value < 1.0:
        send_slack("Account value too low to trade. Skipping.", mention=True)
        return {"status": "error", "error": "account_value_too_low"}

    prices = get_current_prices(info, [a["asset"] for a in parsed["allocations"]])
    trades = compute_rebalance(parsed["allocations"], account_value, state["positions"], prices)

    if not trades:
        send_slack("Signal changed but positions already match. No trades needed.")
        signal_state["last_signal_id"] = msg_id
        return {"status": "already_aligned", "signal_id": msg_id}

    results = execute_trades(info, exchange, trades)
    filled = [r for r in results if r["status"] == "filled"]
    failed = [r for r in results if r["status"] in ("error", "failed", "exception")]

    trade_lines = []
    for r in results:
        if r["status"] == "filled":
            trade_lines.append(f"  {r['side'].upper()} {r['filled_size']} {r['hl_ticker']} @ ${r['avg_price']:.2f}")
        else:
            trade_lines.append(f"  FAILED {r['side'].upper()} {r['hl_ticker']}: {r.get('error', 'unknown')}")

    status_emoji = "OK" if not failed else "PARTIAL"
    send_slack(
        f"REBALANCE {status_emoji} — {len(filled)} filled, {len(failed)} failed\n"
        f"Account: ${account_value:.2f}\n" + "\n".join(trade_lines),
        mention=bool(failed),
    )
    signal_state["last_signal_id"] = msg_id
    return {"status": "rebalanced", "signal_id": msg_id, "filled": len(filled), "failed": len(failed)}


# ── Main cron ───────────────────────────────────────────────────────────────

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("signal-bot-secrets")],
    schedule=modal.Cron("* * * * *"),
    timeout=120,
)
def check_signal():
    if not should_poll_now():
        return {"status": "skipped", "reason": "not scheduled this minute"}

    try:
        messages = fetch_recent_messages(limit=20)
    except RuntimeError as e:
        send_slack(f"SIGNAL BOT AUTH ERROR: {e}", mention=True)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": str(e)}

    signal_msg = find_latest_signal(messages)
    if not signal_msg:
        return {"status": "no_signal"}

    msg_id = signal_msg["_id"]
    try:
        last_acted_id = signal_state["last_signal_id"]
    except KeyError:
        last_acted_id = None

    if msg_id == last_acted_id:
        return {"status": "already_acted", "signal_id": msg_id}

    # New signal!
    parsed = parse_signal(signal_msg["content"])
    timestamp = signal_msg.get("timestamp", 0)
    dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    alloc_lines = "\n".join(f"  {a['percent']}% {a['type']} {a['asset']}" for a in parsed["allocations"])

    if parsed["no_change"]:
        send_slack(f"New signal ({dt.strftime('%Y-%m-%d %H:%M UTC')}) — NO CHANGE\n{alloc_lines}")
        signal_state["last_signal_id"] = msg_id
        return {"status": "no_change", "signal_id": msg_id}

    # Signal has changes!
    if is_autonomous_hours():
        # Auto-execute
        send_slack(f"NEW SIGNAL — AUTO-REBALANCING (autonomous mode)\n{dt.strftime('%Y-%m-%d %H:%M UTC')}\n{alloc_lines}", mention=True)
        try:
            return do_rebalance(parsed, msg_id)
        except Exception as e:
            send_slack(f"REBALANCE ERROR: {e}", mention=True)
            return {"status": "error", "error": str(e)}
    else:
        # Store pending signal and request approval
        approval_token = secrets.token_urlsafe(16)
        signal_state["pending_signal"] = json.dumps(parsed)
        signal_state["pending_msg_id"] = msg_id
        signal_state["approval_token"] = approval_token

        # Dynamically build dashboard URL from Modal workspace
        workspace = os.environ.get("MODAL_WORKSPACE", "")
        if workspace:
            dashboard_url = f"https://{workspace}--signal-bot-web.modal.run"
        else:
            dashboard_url = "(dashboard URL not configured — set MODAL_WORKSPACE in secrets)"
        send_slack(
            f"NEW SIGNAL DETECTED — APPROVAL REQUIRED\n"
            f"{dt.strftime('%Y-%m-%d %H:%M UTC')}\n{alloc_lines}\n\n"
            f"Approve: {dashboard_url}?action=approve&token={approval_token}\n"
            f"Dashboard: {dashboard_url}",
            mention=True,
        )
        return {"status": "pending_approval", "signal_id": msg_id}


# ── Web Dashboard (single endpoint with path routing) ───────────────────────

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("signal-bot-secrets")],
    timeout=120,
)
@modal.fastapi_endpoint(method="GET")
def web(action: str = "", token: str = ""):
    """
    Single web endpoint with action routing.
    Dashboard: ?action=          (or no params) — requires DASHBOARD_TOKEN
    Approve:   ?action=approve&token=xxx
    Dismiss:   ?action=dismiss&token=xxx
    Force:     ?action=force&token=xxx
    Health:    ?action=health
    """
    from fastapi.responses import HTMLResponse

    # C1 FIX: All actions except health require authentication
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if action in ("force", "dismiss", ""):
        if not dashboard_token or token != dashboard_token:
            return HTMLResponse(_page("Unauthorized", "Invalid or missing dashboard token. Add ?token=YOUR_DASHBOARD_TOKEN to the URL."), status_code=403)

    # ── Approve ──
    if action == "approve":
        try:
            stored_token = signal_state.get("approval_token", "")
        except Exception:
            stored_token = ""
        if not token or token != stored_token:
            return HTMLResponse(_page("Invalid or expired approval token.", ""), status_code=403)
        try:
            pending = json.loads(signal_state.get("pending_signal", "null"))
            msg_id = signal_state.get("pending_msg_id", "")
        except Exception:
            pending, msg_id = None, ""
        if not pending:
            return HTMLResponse(_page("No pending signal to approve.", ""))
        try:
            del signal_state["pending_signal"]
            del signal_state["pending_msg_id"]
            del signal_state["approval_token"]
        except KeyError:
            pass
        try:
            result = do_rebalance(pending, msg_id)
            return HTMLResponse(_page(
                f"Rebalance executed: {result.get('status')}",
                f"Filled: {result.get('filled', 0)}, Failed: {result.get('failed', 0)}"
            ))
        except Exception as e:
            send_slack(f"APPROVAL REBALANCE ERROR: {e}", mention=True)
            return HTMLResponse(_page(f"Error: {e}", ""), status_code=500)

    # ── Dismiss ──
    if action == "dismiss":
        try:
            msg_id = signal_state.get("pending_msg_id", "")
            if msg_id:
                signal_state["last_signal_id"] = msg_id
            del signal_state["pending_signal"]
            del signal_state["pending_msg_id"]
            del signal_state["approval_token"]
        except KeyError:
            pass
        send_slack("Signal dismissed manually via dashboard.")
        return HTMLResponse(_page("Signal dismissed.", ""))

    # ── Force rebalance ──
    if action == "force":
        try:
            messages = fetch_recent_messages(limit=20)
            signal_msg = find_latest_signal(messages)
            if not signal_msg:
                return HTMLResponse(_page("No signal found.", ""))
            parsed = parse_signal(signal_msg["content"])
            parsed["no_change"] = False
            send_slack("FORCE REBALANCE triggered via dashboard", mention=True)
            result = do_rebalance(parsed, signal_msg["_id"])
            return HTMLResponse(_page(
                f"Force rebalance: {result.get('status')}",
                f"Filled: {result.get('filled', 0)}, Failed: {result.get('failed', 0)}"
            ))
        except Exception as e:
            send_slack(f"FORCE REBALANCE ERROR: {e}", mention=True)
            return HTMLResponse(_page(f"Error: {e}", ""), status_code=500)

    # ── Health ──
    if action == "health":
        issues = []
        try:
            msgs = fetch_recent_messages(limit=1)
            if not msgs:
                issues.append("TRW: no messages returned")
        except Exception as e:
            issues.append(f"TRW: {e}")
        try:
            info, _ = get_hl_clients()
            st = get_account_state(info)
        except Exception as e:
            issues.append(f"Hyperliquid: {e}")
        status = "HEALTHY" if not issues else "UNHEALTHY: " + "; ".join(issues)
        return HTMLResponse(_page("Health Check", status))

    # ── Dashboard (default) ──
    return HTMLResponse(_render_dashboard())


def _esc(s) -> str:
    """HTML-escape to prevent XSS."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")


def _page(title: str, body: str) -> str:
    return f'''<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3; padding: 24px; max-width: 600px; margin: 0 auto; }}
a {{ color: #58a6ff; }}</style>
</head><body><h2>{_esc(title)}</h2><p>{_esc(body)}</p><br><a href="?">Back to dashboard</a></body></html>'''


def _render_dashboard() -> str:
    # Fetch live data
    signal_msg, parsed, signal_time, trw_ok = None, None, "N/A", False
    try:
        messages = fetch_recent_messages(limit=20)
        signal_msg = find_latest_signal(messages)
        if signal_msg:
            parsed = parse_signal(signal_msg["content"])
            signal_time = datetime.fromtimestamp(signal_msg["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        trw_ok = True
    except Exception as e:
        signal_time = f"Error: {e}"

    state, prices, hl_ok = {"account_value": 0, "positions": {}}, {}, False
    try:
        info, _ = get_hl_clients()
        state = get_account_state(info)
        prices = info.all_mids()
        hl_ok = True
    except Exception:
        pass

    # Pending approval
    pending, approval_token = None, ""
    try:
        pending = json.loads(signal_state.get("pending_signal", "null"))
        approval_token = signal_state.get("approval_token", "")
    except Exception:
        pass

    last_acted_id = "none"
    try:
        last_acted_id = signal_state["last_signal_id"]
    except KeyError:
        pass

    # Allocation rows
    alloc_html = ""
    if parsed:
        for a in parsed["allocations"]:
            alloc_html += f'<div class="alloc-row"><span class="pct">{_esc(a["percent"])}%</span><span class="type">{_esc(a["type"])}</span><span class="asset">{_esc(a["asset"])}</span></div>'

    # Position rows
    pos_html = ""
    total_pnl = 0
    for coin, pos in state["positions"].items():
        current_price = float(prices.get(coin, pos["entry_px"]))
        current_value = abs(pos["size"]) * current_price
        pnl = pos["unrealized_pnl"]
        total_pnl += pnl
        pnl_class = "positive" if pnl >= 0 else "negative"
        pct = (current_value / state["account_value"] * 100) if state["account_value"] > 0 else 0
        pos_html += f'''<div class="pos-row">
            <span class="coin">{_esc(coin)}</span><span class="size">{pos["size"]:.4f}</span>
            <span class="entry">${pos["entry_px"]:,.2f}</span><span class="current">${current_price:,.2f}</span>
            <span class="value">${current_value:,.2f}</span>
            <span class="pnl {pnl_class}">${pnl:+,.2f}</span><span class="alloc">{pct:.1f}%</span>
        </div>'''

    pnl_class = "positive" if total_pnl >= 0 else "negative"

    pending_html = ""
    if pending and approval_token:
        pa = "".join(f'<li>{_esc(a["percent"])}% {_esc(a["type"])} {_esc(a["asset"])}</li>' for a in pending["allocations"])
        dt = os.environ.get("DASHBOARD_TOKEN", "")
        dismiss_url = f"?action=dismiss&token={dt}" if dt else "?action=dismiss"
        pending_html = f'''<div class="pending-banner"><h3>Pending Signal — Approval Required</h3>
            <ul>{pa}</ul>
            <a href="?action=approve&token={approval_token}" class="btn btn-approve" onclick="return confirm('Execute rebalance now?')">APPROVE &amp; EXECUTE</a>
            <a href="{dismiss_url}" class="btn btn-dismiss">Dismiss</a></div>'''

    auto = is_autonomous_hours()
    mode_text = "AUTONOMOUS" if auto else "APPROVAL REQUIRED"
    mode_class = "auto" if auto else "manual"

    return f'''<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Bot</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #e6edf3; padding: 16px; max-width: 800px; margin: 0 auto; }}
h1 {{ font-size: 1.4em; margin-bottom: 4px; }}
h2 {{ font-size: 1.1em; color: #8b949e; margin: 20px 0 8px; border-bottom: 1px solid #21262d; padding-bottom: 4px; }}
.status-bar {{ display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }}
.badge {{ padding: 4px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }}
.badge.ok {{ background: #1a7f37; color: #fff; }} .badge.err {{ background: #da3633; color: #fff; }}
.badge.auto {{ background: #1f6feb; color: #fff; }} .badge.manual {{ background: #d29922; color: #000; }}
.card {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; margin: 8px 0; }}
.account-value {{ font-size: 2em; font-weight: 700; }}
.account-pnl {{ font-size: 1.1em; margin-top: 4px; }}
.positive {{ color: #3fb950; }} .negative {{ color: #f85149; }}
.alloc-row, .pos-row {{ display: grid; padding: 6px 0; border-bottom: 1px solid #21262d; align-items: center; }}
.alloc-row {{ grid-template-columns: 60px 60px 1fr; }}
.pos-row {{ grid-template-columns: 50px 70px 80px 80px 80px 70px 50px; font-size: 0.85em; }}
.pos-header {{ font-weight: 600; color: #8b949e; font-size: 0.8em; }}
.pct {{ font-weight: 700; color: #58a6ff; }} .asset {{ font-weight: 600; }} .type {{ color: #8b949e; }}
.no-change {{ color: #8b949e; font-style: italic; }}
.pending-banner {{ background: #2d1b00; border: 1px solid #d29922; border-radius: 8px; padding: 16px; margin: 12px 0; }}
.pending-banner h3 {{ color: #d29922; margin-bottom: 8px; }}
.pending-banner ul {{ margin: 8px 0 12px 20px; }}
.btn {{ display: inline-block; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600; margin-right: 8px; font-size: 0.95em; }}
.btn-approve {{ background: #1f6feb; color: #fff; }} .btn-dismiss {{ background: #21262d; color: #8b949e; }}
.btn-action {{ background: #21262d; color: #e6edf3; border: 1px solid #30363d; margin-top: 8px; }}
.btn:hover {{ opacity: 0.85; }}
.actions {{ margin-top: 16px; }}
.meta {{ color: #8b949e; font-size: 0.8em; margin-top: 12px; }}
@media (max-width: 600px) {{
    .pos-row {{ grid-template-columns: 45px 1fr 65px 55px; }}
    .pos-row .entry, .pos-row .current, .pos-row .alloc {{ display: none; }}
    .pos-header .entry, .pos-header .current, .pos-header .alloc {{ display: none; }}
}}
</style></head><body>
<h1>Signal Bot</h1>
<div class="status-bar">
    <span class="badge {"ok" if trw_ok else "err"}">TRW {"OK" if trw_ok else "ERR"}</span>
    <span class="badge {"ok" if hl_ok else "err"}">HL {"OK" if hl_ok else "ERR"}</span>
    <span class="badge {mode_class}">{mode_text}</span>
</div>
{pending_html}
<div class="card">
    <div class="account-value">${state["account_value"]:,.2f}</div>
    <div class="account-pnl {pnl_class}">PnL: ${total_pnl:+,.2f}</div>
</div>
<h2>Positions</h2>
<div class="card">
    <div class="pos-row pos-header"><span>Coin</span><span>Size</span><span class="entry">Entry</span>
        <span class="current">Now</span><span>Value</span><span>PnL</span><span class="alloc">%</span></div>
    {pos_html if pos_html else '<div style="color:#8b949e;padding:8px 0">No positions</div>'}
</div>
<h2>Latest Signal</h2>
<div class="card">
    <div style="color:#8b949e;font-size:0.85em;margin-bottom:8px">{signal_time}</div>
    {alloc_html if alloc_html else '<div class="no-change">No signal found</div>'}
    {'<div class="no-change" style="margin-top:8px">Executive Summary: No change</div>' if parsed and parsed["no_change"] else ""}
    {'<div style="margin-top:8px;font-size:0.85em;color:#8b949e">BTC Leverage: ' + _esc(parsed["btc_leverage"]) + '</div>' if parsed and parsed.get("btc_leverage") else ""}
</div>
<div class="actions">
    <a href="?action=force&token={os.environ.get('DASHBOARD_TOKEN', '')}" class="btn btn-action" onclick="return confirm('Force rebalance to current signal?')">Force Rebalance</a>
    <a href="?action=health" class="btn btn-action">Health Check</a>
    <a href="?" class="btn btn-action">Refresh</a>
</div>
<div class="meta">Last acted: {last_acted_id[:12] if last_acted_id != "none" else "none"}...
    | Current: {signal_msg["_id"][:12] if signal_msg else "N/A"}...</div>
</body></html>'''
