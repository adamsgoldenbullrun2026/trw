# TRW Auto-Trade Signal Bot

Automatically execute Prof Adam's RSPS portfolio signals on Hyperliquid. Runs 24/7 in the cloud for free.

> **New here?** Read the [full setup guide](guide.html) or download the [PDF version](TRW_Signal_Bot_Guide.pdf) for step-by-step instructions with screenshots.

## What It Does

- Reads Prof Adam's Portfolio Signal at bar close (~00:00 UTC) via TRW's API
- Parses the allocation percentages (e.g., 80% ETH, 14.3% HYPE, 5.7% PAXG)
- Rebalances your Hyperliquid portfolio to match using 1x leverage perps
- Runs 24/7 in the cloud ([Modal](https://modal.com) free tier) — no computer needed
- Slack notifications when signals are detected and trades are placed (optional)
- Web dashboard to monitor positions and approve trades from your phone

## Quick Start

Full details in the [setup guide](guide.html). The short version:

1. Get your tokens ready ([TRW session token](guide.html#step1), [Hyperliquid API keys](guide.html#step2), [Modal account](guide.html#step3))

2. Install Python 3.10+ and dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Launch the manager GUI:
   ```
   python manage.py
   ```

4. A web page opens — paste in your tokens, click **Save & Deploy**. Done.

## Managing Your Bot

Run `python manage.py` any time to:
- Check connection status (TRW, Hyperliquid, Slack)
- Update tokens if they expire
- Add Slack notifications later
- Redeploy after changes

See [Maintenance & Updating Tokens](guide.html#maintenance) in the guide for details.

## Security

This code connects to exactly **3 services** — all yours:

| Service | URL | Why |
|---------|-----|-----|
| TRW | `eden.therealworld.ag` | Read signals (same server your browser uses) |
| Hyperliquid | `api.hyperliquid.xyz` | Read positions & place trades |
| Slack | Your own webhook URL | Send you notifications |

No analytics. No telemetry. No data sent to the author. Your API wallet **cannot withdraw funds** — it can only place/cancel orders. Everything runs on YOUR Modal account.

Don't trust us? Paste any file into ChatGPT or Claude and ask if it sends your data anywhere suspicious. Read more in [Trust & Verification](guide.html#trust).

## Cost

$0/month. Modal free tier + Hyperliquid trading fees (~0.04% per trade).

## Files

| File | What it does |
|------|-------------|
| `manage.py` | **Start here** — GUI for setup and token management |
| `setup.py` | CLI alternative (`--reconfigure` to update tokens) |
| `trw_signal_reader.py` | Reads and parses signals from TRW |
| `hyperliquid_rebalancer.py` | Manages Hyperliquid positions |
| `modal_signal_bot.py` | Cloud bot (Modal cron + web dashboard) |
| [guide.html](guide.html) | Full setup guide |
| [TRW_Signal_Bot_Guide.pdf](TRW_Signal_Bot_Guide.pdf) | PDF version |

## Disclaimer

⚠️ **Work in progress.** This bot was built recently and may contain bugs. The TRW session token may expire and need refreshing. Start with a small amount to test. Use at your own risk — this is not financial advice. Always understand the signals before automating them.
