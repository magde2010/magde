# MEXC Adaptive DCA Grid Bot (standalone)

This project is intentionally isolated from `magde`: it has its own `gridbot` Python package, SQLite file, lock file, configuration, and Telegram handlers. It does not import or alter portfolio-bot modules.

## Safety and current scope

- `DRY_RUN=true` and `LIVE_TRADING_ENABLED=false` by default.
- Live mode requires both `DRY_RUN=false` and explicit `LIVE_TRADING_ENABLED=true`, plus MEXC API credentials.
- Use a dedicated Spot API key with **withdrawals disabled at MEXC**. The application cannot override exchange-side key permissions.
- Dry run can read public market data, but all order creation/cancellation is simulated in-process and never calls CCXT order-mutation endpoints.
- The monitor performs startup recovery before permitting strategy orders, persists order intent before network I/O, and freezes rather than blindly retrying uncertain order creation.
- Emergency stop persists in SQLite. `/emergency_stop cancel` requests cancellation only for orders recorded in this bot's own database.
- This version is an operational development build, **not certified production-ready**. Live MEXC behavior, exchange-specific client-order-ID support, minimum-notional/precision behavior, Telegram network operation, and crash recovery against real MEXC order history still require a test on a dedicated account in dry-run/public-data mode and then a tiny controlled live test. Do not enable live trading based only on unit tests.
- Optional stop-loss execution and a full interactive Telegram configuration wizard are not implemented yet. Configuration currently comes from `.env`; Telegram can start/pause the strategy, inspect status/orders, and emergency-stop it.

## Install and run

Python 3.11+ recommended:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in .env
python -m unittest discover -s tests -v
python -m gridbot
```

The bot starts paused. Send `/run` in the authorized Telegram account to activate the strategy. `/stop` pauses new strategy orders but leaves existing orders in place. `/emergency_stop` blocks new orders; `/emergency_stop cancel` additionally requests cancellation of this bot's recorded open orders. Resolve any `FROZEN`/unknown order state manually at MEXC before restarting strategy activity.

## Important implementation notes

- Buy and sell orders are limit orders. The initial order is a limit buy at the observed last price; DCA orders are limit buys at configured drop levels; TP orders are limit sells derived from average entry.
- Accounting uses `Decimal`; float conversion occurs only at the CCXT API boundary.
- Order state stores client/exchange IDs, side/role/level, price/amount, cumulative filled quantity/cost, status, timestamps, and last error.
- DCA has exactly four configured levels and separate allocation limits. TP sell percentages must total 100%, including valid `100,0,0,0` configuration.
- Recenter decisions use the last fully closed 3-minute candle and a cooldown.
- No project files are committed or pushed by this workflow.
