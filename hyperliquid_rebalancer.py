"""
Hyperliquid Portfolio Rebalancer — Takes target allocations from the TRW signal
and rebalances perp positions to match.

All positions use 1x leverage (spot-equivalent exposure via perps).

Usage:
    python execution/hyperliquid_rebalancer.py --status                  # Show current positions
    python execution/hyperliquid_rebalancer.py --preview <signal.json>   # Preview rebalance (no trades)
    python execution/hyperliquid_rebalancer.py --execute <signal.json>   # Execute rebalance
    python execution/hyperliquid_rebalancer.py --preview-live            # Preview using live TRW signal
    python execution/hyperliquid_rebalancer.py --execute-live            # Execute using live TRW signal

Signal JSON format (from trw_signal_reader.py --json):
    {"allocations": [{"percent": 80.0, "type": "Spot", "asset": "ETH"}, ...], "no_change": false}
"""

import os
import sys
import json
import argparse
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Config ──────────────────────────────────────────────────────────────────

API_PRIVATE_KEY = os.getenv("HYPERLIQUID_API_PRIVATE_KEY")
MASTER_ADDRESS = os.getenv("HYPERLIQUID_MASTER_ACCOUNT_ADDRESS")

# Safety limits
MIN_TRADE_USD = 1.0          # Don't bother with trades smaller than $1
MAX_SLIPPAGE = 0.03           # 3% max slippage on market orders
MAX_SINGLE_ORDER_USD = 50000  # Safety cap per single order

# Asset mapping: signal name → Hyperliquid perp ticker
ASSET_MAP = {
    "ETH": "ETH",
    "BTC": "BTC",
    "HYPE": "HYPE",
    "SOL": "SOL",
    "DOGE": "DOGE",
    "XRP": "XRP",
    "PAXG/XAUT": "PAXG",   # Use PAXG perp for gold exposure
    "PAXG": "PAXG",
    "XAUT": "PAXG",         # Map XAUT to PAXG as well
    "GOLD": "PAXG",
}


# ── Client setup ────────────────────────────────────────────────────────────

def get_clients() -> tuple[Info, Exchange]:
    """Create Hyperliquid Info + Exchange clients."""
    if not API_PRIVATE_KEY or not MASTER_ADDRESS:
        print("ERROR: HYPERLIQUID_API_PRIVATE_KEY and HYPERLIQUID_MASTER_ACCOUNT_ADDRESS must be set in .env",
              file=sys.stderr)
        sys.exit(1)

    wallet: LocalAccount = eth_account.Account.from_key(API_PRIVATE_KEY)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(
        wallet,
        constants.MAINNET_API_URL,
        account_address=MASTER_ADDRESS,
    )
    return info, exchange


# ── Account state ───────────────────────────────────────────────────────────

def get_account_state(info: Info) -> dict:
    """Get current perp account state: total value, positions, available margin."""
    state = info.user_state(MASTER_ADDRESS)
    margin = state["marginSummary"]
    account_value = float(margin["accountValue"])

    positions = {}
    for pos in state.get("assetPositions", []):
        p = pos["position"]
        coin = p["coin"]
        size = float(p.get("szi", 0))
        entry_px = float(p["entryPx"]) if p.get("entryPx") else 0
        unrealized_pnl = float(p["unrealizedPnl"])
        position_value = abs(size) * entry_px

        positions[coin] = {
            "size": size,
            "entry_px": entry_px,
            "unrealized_pnl": unrealized_pnl,
            "value_usd": position_value,
        }

    return {
        "account_value": account_value,
        "positions": positions,
        "withdrawable": float(margin.get("totalRawUsd", margin.get("accountValue", 0))),
    }


def get_current_prices(info: Info, assets: list[str]) -> dict[str, float]:
    """Get current mid prices for a list of assets."""
    all_mids = info.all_mids()
    prices = {}
    for asset in assets:
        hl_ticker = ASSET_MAP.get(asset, asset)
        if hl_ticker in all_mids:
            prices[asset] = float(all_mids[hl_ticker])
        else:
            print(f"WARNING: No price found for {asset} (ticker: {hl_ticker})", file=sys.stderr)
    return prices


# ── Rebalancing logic ──────────────────────────────────────────────────────

def compute_rebalance(
    allocations: list[dict],
    account_value: float,
    current_positions: dict,
    prices: dict[str, float],
    all_mids: dict[str, float] | None = None,
) -> list[dict]:
    """
    Compute the trades needed to rebalance from current positions to target allocations.

    Returns a list of trades:
        [{"asset": "ETH", "hl_ticker": "ETH", "side": "buy"/"sell",
          "size": 0.5, "value_usd": 1500, "price": 3000}]
    """
    trades = []

    # Build target positions (what we WANT)
    target_positions = {}
    for alloc in allocations:
        asset = alloc["asset"]
        target_pct = alloc["percent"] / 100.0
        target_usd = account_value * target_pct
        hl_ticker = ASSET_MAP.get(asset, asset)

        if asset not in prices:
            print(f"WARNING: Skipping {asset} — no price available", file=sys.stderr)
            continue

        target_size = target_usd / prices[asset]
        target_positions[hl_ticker] = {
            "asset": asset,
            "target_usd": target_usd,
            "target_size": target_size,
            "price": prices[asset],
        }

    # Determine all tickers we need to consider (current + target)
    all_tickers = set(target_positions.keys())
    for coin in current_positions:
        if current_positions[coin]["size"] != 0:
            all_tickers.add(coin)

    # Compute deltas
    for ticker in all_tickers:
        current_size = current_positions.get(ticker, {}).get("size", 0)
        target = target_positions.get(ticker, None)

        if target:
            target_size = target["target_size"]
            price = target["price"]
            asset = target["asset"]
        else:
            # Asset is in current positions but NOT in target → close it
            # Use current market price, NOT entry price
            price = None
            if all_mids and ticker in all_mids:
                price = float(all_mids[ticker])
            elif ticker in prices:
                price = prices[ticker]
            if not price:
                price = current_positions[ticker].get("entry_px", 0)
                print(f"WARNING: No market price for {ticker} — using entry price ${price:.2f} to close", file=sys.stderr)
                if not price:
                    print(f"WARNING: No entry price either for {ticker} — skipping close", file=sys.stderr)
                    continue
            target_size = 0
            asset = ticker

        delta_size = target_size - current_size
        delta_usd = abs(delta_size) * price

        if delta_usd < MIN_TRADE_USD:
            continue  # Skip tiny trades

        if delta_usd > MAX_SINGLE_ORDER_USD:
            print(f"WARNING: Trade for {asset} (${delta_usd:.2f}) exceeds safety cap of ${MAX_SINGLE_ORDER_USD}. Capping.",
                  file=sys.stderr)
            delta_size = (MAX_SINGLE_ORDER_USD / price) * (1 if delta_size > 0 else -1)
            delta_usd = MAX_SINGLE_ORDER_USD

        trades.append({
            "asset": asset,
            "hl_ticker": ticker,
            "side": "buy" if delta_size > 0 else "sell",
            "size": abs(delta_size),
            "value_usd": delta_usd,
            "price": price,
            "current_size": current_size,
            "target_size": target_size,
        })

    # Sort: sells first (free up margin), then buys
    trades.sort(key=lambda t: (0 if t["side"] == "sell" else 1, -t["value_usd"]))

    return trades


# ── Execution ───────────────────────────────────────────────────────────────

def get_sz_decimals(info: Info, ticker: str) -> int:
    """Get the size decimal precision for a perp asset."""
    meta = info.meta()
    for asset in meta["universe"]:
        if asset["name"] == ticker:
            return asset["szDecimals"]
    return 2  # default


def round_size(size: float, sz_decimals: int) -> float:
    """Round size to the allowed decimal precision."""
    d = Decimal(str(size)).quantize(Decimal(10) ** -sz_decimals, rounding=ROUND_DOWN)
    return float(d)


def execute_trades(info: Info, exchange: Exchange, trades: list[dict]) -> list[dict]:
    """Execute a list of trades. Returns results."""
    results = []

    # Set 1x cross leverage for ALL assets we're about to trade
    leveraged_ok = set()
    leverage_failed = set()
    for trade in trades:
        ticker = trade["hl_ticker"]
        if ticker not in leveraged_ok and ticker not in leverage_failed:
            print(f"  Setting {ticker} to 1x cross leverage...")
            try:
                exchange.update_leverage(1, ticker, is_cross=True)
                leveraged_ok.add(ticker)
            except Exception as e:
                print(f"  ABORT {ticker}: Failed to set 1x leverage: {e}", file=sys.stderr)
                leverage_failed.add(ticker)
            time.sleep(0.3)

    for trade in trades:
        if trade["hl_ticker"] in leverage_failed:
            print(f"  SKIPPING {trade['hl_ticker']} — leverage not confirmed at 1x", file=sys.stderr)
            results.append({**trade, "status": "skipped", "reason": "leverage set failed"})
            continue
        ticker = trade["hl_ticker"]
        is_buy = trade["side"] == "buy"
        sz_decimals = get_sz_decimals(info, ticker)
        size = round_size(trade["size"], sz_decimals)

        if size == 0:
            results.append({**trade, "status": "skipped", "reason": "size rounded to 0"})
            continue

        print(f"  {'BUY' if is_buy else 'SELL'} {size} {ticker} (~${trade['value_usd']:.2f})...", end=" ")

        try:
            result = exchange.market_open(
                ticker,
                is_buy=is_buy,
                sz=size,
                slippage=MAX_SLIPPAGE,
            )

            if result["status"] == "ok":
                statuses = result["response"]["data"]["statuses"]
                for status in statuses:
                    if "filled" in status:
                        filled = status["filled"]
                        print(f"FILLED {filled['totalSz']} @ ${filled['avgPx']}")
                        results.append({
                            **trade,
                            "status": "filled",
                            "filled_size": float(filled["totalSz"]),
                            "avg_price": float(filled["avgPx"]),
                        })
                    elif "error" in status:
                        print(f"ERROR: {status['error']}")
                        results.append({**trade, "status": "error", "error": status["error"]})
                    elif "resting" in status:
                        print(f"RESTING (partial fill)")
                        results.append({**trade, "status": "resting"})
            else:
                error_msg = result.get("response", {}).get("data", str(result))
                print(f"FAILED: {error_msg}")
                results.append({**trade, "status": "failed", "error": str(error_msg)})

        except Exception as e:
            print(f"EXCEPTION: {e}")
            results.append({**trade, "status": "exception", "error": str(e)})

        # Small delay between orders to avoid rate limits
        time.sleep(0.5)

    return results


# ── Display ─────────────────────────────────────────────────────────────────

def print_status(info: Info):
    """Print current account status and positions."""
    state = get_account_state(info)
    print(f"Account Value: ${state['account_value']:.2f}")
    print(f"Withdrawable:  ${state['withdrawable']:.2f}")

    if not state["positions"]:
        print("\nNo open positions.")
        return

    print(f"\nOpen Positions:")
    print(f"  {'Asset':<8} {'Size':>10} {'Entry':>10} {'Value':>10} {'PnL':>10}")
    print(f"  {'─' * 50}")
    for coin, pos in state["positions"].items():
        print(f"  {coin:<8} {pos['size']:>10.4f} {pos['entry_px']:>10.2f} "
              f"${pos['value_usd']:>9.2f} ${pos['unrealized_pnl']:>9.2f}")


def print_preview(trades: list[dict], account_value: float):
    """Print a preview of planned trades."""
    if not trades:
        print("No trades needed — portfolio already matches signal.")
        return

    print(f"\nPlanned trades (account value: ${account_value:.2f}):")
    print(f"  {'Action':<6} {'Asset':<8} {'Size':>10} {'Value':>10} {'Price':>10}")
    print(f"  {'─' * 50}")
    total_value = 0
    for t in trades:
        action = t["side"].upper()
        print(f"  {action:<6} {t['hl_ticker']:<8} {t['size']:>10.4f} "
              f"${t['value_usd']:>9.2f} ${t['price']:>9.2f}")
        total_value += t["value_usd"]
    print(f"  {'─' * 50}")
    print(f"  Total trade volume: ${total_value:.2f}")


# ── Signal loading ──────────────────────────────────────────────────────────

def load_signal_from_file(path: str) -> dict:
    """Load parsed signal from a JSON file."""
    with open(path) as f:
        return json.load(f)


def load_signal_live() -> dict:
    """Fetch and parse the live signal from TRW."""
    # Import the signal reader
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trw_signal_reader import fetch_recent_messages, find_latest_signal, parse_signal

    messages = fetch_recent_messages(limit=20)
    signal_msg = find_latest_signal(messages)
    if not signal_msg:
        print("ERROR: No signal found in TRW channel.", file=sys.stderr)
        sys.exit(1)
    return parse_signal(signal_msg["content"])


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Portfolio Rebalancer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="Show current positions")
    group.add_argument("--preview", type=str, nargs="?", const="__live__",
                       help="Preview rebalance (file path or omit for live signal)")
    group.add_argument("--execute", type=str, nargs="?", const="__live__",
                       help="Execute rebalance (file path or omit for live signal)")
    group.add_argument("--preview-live", action="store_true", help="Preview using live TRW signal")
    group.add_argument("--execute-live", action="store_true", help="Execute using live TRW signal")
    args = parser.parse_args()

    info, exchange = get_clients()

    if args.status:
        print_status(info)
        return

    # Load signal
    if args.preview_live or args.execute_live:
        signal = load_signal_live()
    elif args.preview == "__live__" or args.execute == "__live__":
        signal = load_signal_live()
    else:
        signal_path = args.preview or args.execute
        signal = load_signal_from_file(signal_path)

    # Check for no-change signal
    if signal.get("no_change"):
        print("Signal says NO CHANGE. No trades needed.")
        return

    # Get account state and prices
    state = get_account_state(info)
    account_value = state["account_value"]

    if account_value < 1.0:
        print(f"ERROR: Account value too low (${account_value:.2f}). Deposit USDC first.",
              file=sys.stderr)
        sys.exit(1)

    # Get prices for all signal assets + all_mids for closing positions
    signal_assets = [a["asset"] for a in signal["allocations"]]
    prices = get_current_prices(info, signal_assets)
    all_mids = {k: float(v) for k, v in info.all_mids().items()}

    # Compute trades
    trades = compute_rebalance(
        signal["allocations"],
        account_value,
        state["positions"],
        prices,
        all_mids=all_mids,
    )

    # Preview or execute
    is_execute = args.execute is not None or args.execute_live
    if is_execute:
        print_preview(trades, account_value)
        if not trades:
            return
        print(f"\nExecuting {len(trades)} trades...")
        results = execute_trades(info, exchange, trades)

        # Summary
        filled = [r for r in results if r["status"] == "filled"]
        failed = [r for r in results if r["status"] in ("error", "failed", "exception")]
        print(f"\nDone: {len(filled)} filled, {len(failed)} failed out of {len(results)} trades.")

        if failed:
            print("Failed trades:")
            for f in failed:
                print(f"  {f['side'].upper()} {f['hl_ticker']}: {f.get('error', 'unknown')}")
    else:
        print_preview(trades, account_value)


if __name__ == "__main__":
    main()
