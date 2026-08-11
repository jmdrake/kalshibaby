# KalshiBaby Paper Trading

A paper trading version of the KalshiBaby v3 trading engine that simulates trades against live Kalshi market data.

## Overview

This paper trading system:
- **Pulls live contract data** from the Kalshi API (prices, bid/ask spreads, available quantities)
- **Simulates buys and sells** against a local SQLite database
- **Tracks your virtual portfolio**, P/L, and fill history
- **Handles partial fills intelligently** - if you want to sell 10 contracts but only 7 are available at your target price, it sells the 7 and asks what you want to do with the remaining 3

## Quick Start

```bash
./startpaper
```

Then open http://127.0.0.1:8766 in your browser.

## Key Features

### Partial Fill Handling

The system implements the exact workflow you requested:

1. You place an order to sell 10 contracts at $0.65
2. The system checks live Kalshi liquidity and finds only 7 contracts available at that price
3. It immediately sells the 7 contracts
4. An alert appears: *"Sold 7/10 @ 0.65. 3 contracts remaining. Next available price ~0.63"*
5. You can then choose to:
   - Sell the remaining 3 at the next available price
   - Cancel the remaining order
   - Wait for better liquidity

### Live Market Data

All prices and availability come from the real Kalshi API:
- Bid/ask prices
- Available quantities at each price level
- Market depth indicators
- Implied probabilities

### Local Database

Your paper portfolio is stored in `paper_trading.db` (SQLite):
- **Positions table**: Tracks your current holdings
- **Fills table**: Complete trade history with timestamps, prices, P/L
- **Account table**: Cash balance, starting balance, realized P/L

Default starting balance: $10,000

### API Endpoints

#### Get Status
```bash
curl http://localhost:8766/api/status
```

Returns full portfolio status including:
- All events being tracked
- Current positions with live prices
- Risk metrics
- Recent actions/alerts
- Account balance and P/L
- Recent fill history

#### Place Order
```bash
curl -X POST http://localhost:8766/api/order \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "KXGOLD-24DEC-T2700",
    "order_type": "buy",
    "side": "yes",
    "qty": 10,
    "price": 0.65
  }'
```

#### Sell Position
```bash
curl -X POST http://localhost:8766/api/order \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "KXGOLD-24DEC-T2700",
    "order_type": "sell",
    "qty": 10
  }'
```

#### Confirm Partial Fill
```bash
curl -X POST http://localhost:8766/api/confirm_partial_fill \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "uuid-from-partial-fill-response",
    "sell_remaining": true,
    "next_price": 0.63
  }'
```

#### Get Account Stats
```bash
curl http://localhost:8766/api/account
```

Returns:
```json
{
  "starting_balance": 10000.0,
  "cash_balance": 8500.0,
  "portfolio_value": 1200.0,
  "total_value": 9700.0,
  "realized_pl": -150.0,
  "total_return_pct": -3.0
}
```

#### Get Fill History
```bash
curl http://localhost:8766/api/fills?limit=50
```

## Architecture

```
kalshibaby_paper.py
├── KalshiClient       # Read-only API client for market data
├── PaperDatabase      # SQLite persistence layer
├── EventEngine        # Per-event state machine
│   ├── simulate_buy()
│   ├── simulate_sell()
│   └── confirm_partial_fill()
└── Engine             # Multi-event coordinator
    └── REST API endpoints
```

## Database Schema

### positions
```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    side TEXT NOT NULL,          -- 'yes' or 'no'
    strike REAL NOT NULL,
    count INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    UNIQUE(ticker, side)
);
```

### fills
```sql
CREATE TABLE fills (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,        -- 'buy' or 'sell'
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    total_cost REAL NOT NULL,
    ts REAL NOT NULL,
    order_id TEXT NOT NULL,
    note TEXT
);
```

### account
```sql
CREATE TABLE account (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);
-- Keys: 'starting_balance', 'cash_balance', 'realized_pl'
```

## Differences from Live Backend

| Feature | Live Backend | Paper Trading |
|---------|-------------|---------------|
| Orders | Real Kalshi orders | Simulated fills |
| API Auth | Required | Optional (read-only) |
| Portfolio Sync | From Kalshi API | From local DB |
| Partial Fills | Handled by Kalshi | Simulated with confirmation |
| Starting Balance | Your real balance | $10,000 virtual |
| Mode Setting | paper/live toggle | Always paper |

## Configuration

Uses the same `config.yaml` as the live backend for consistency, but:
- API credentials are optional (only needed for reading market data)
- Mode is always forced to "paper"
- All other parameters (poll_seconds, armed, etc.) work the same

## Example Workflow

1. **Start the server**: `./startpaper`
2. **Browse to** http://127.0.0.1:8766
3. **View available events** - all open Kalshi events are auto-tracked
4. **Place a buy order** - e.g., buy 10 YES contracts on KXGOLD
5. **Watch the fill** - if only 7 available, you'll get a partial fill alert
6. **Decide on remainder** - sell remaining 3 at next price or cancel
7. **Monitor position** - see live P/L as prices update
8. **Sell when ready** - same partial fill logic applies

## Files Created

- `kalshibaby_paper.py` - Main paper trading backend
- `startpaper` - Startup script
- `paper_trading.db` - SQLite database (created on first run)
- `PAPER_TRADING_README.md` - This documentation

## Running Without UI

The existing UI from kalshibaby_backend works fine with paper trading since they share the same API structure. Just point it to port 8766 instead of 8765.

Alternatively, use the API directly via curl or build a custom interface.

## Safety Notes

- ✅ No real money at risk
- ✅ API credentials optional (read-only if provided)
- ✅ Can reset account anytime by deleting `paper_trading.db`
- ✅ All trades logged for review
